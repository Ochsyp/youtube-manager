# -------------------------------------------------------
# Log level tests
#
# Pins which worker and organizer failures log at WARN and which at ERROR. ERROR pages through the
# jelly-stack alert, so it must stay limited to conditions a human has to look at. Nothing here
# runs yt-dlp or reaches the network: subprocess calls, scans and HTTP requests are replaced.
# -------------------------------------------------------

import errno
import io
import logging
import subprocess

import pytest
import requests

import applog
import organizer
import worker
from database import Channel, Download, Subscription

# A scan result as scan_video_ids returns it after the proxy refused the request.
FAILED_SCAN = ([], 'ERROR: [youtube:tab] Unable to download API page: HTTP Error 503: Service Unavailable')


# Messages logged at exactly `level`, in the order they were logged.
def messages_at(caplog: pytest.LogCaptureFixture, level: int) -> list[str]:
    return [record.getMessage() for record in caplog.records if record.levelno == level]


# Stands in for the yt-dlp Popen: stdout is fixed text, and the process has already exited with
# `returncode`, so terminate_process_group() never has to signal it.
class FinishedProcess:
    def __init__(self, output: str, returncode: int = 0):
        self.stdout = io.StringIO(output)
        self.returncode = returncode
        self.pid = -1

    def poll(self) -> int:
        return self.returncode

    def wait(self, timeout: float | None = None) -> int:
        return self.returncode


# A requests.Response carrying only `status`, standing in for Jellyfin's answer to the refresh.
def jellyfin_response(status: int) -> requests.Response:
    response = requests.Response()
    response.status_code = status
    response.url = 'https://jellyfin.invalid:8920/Library/Refresh'
    return response


# Every test starts with no failed subscription checks on record, so no count leaks between tests.
@pytest.fixture(autouse=True)
def fresh_failure_counter(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(worker, '_subscription_failures', {})


# One enabled channel subscription that is already resolved, so a check goes straight to its scan.
# Returns the subscription's id.
@pytest.fixture
def subscription_id(db) -> int:
    with db() as session:
        channel = Channel(youtube_id='UCtest', name='Test channel', url='https://www.youtube.com/channel/UCtest')
        session.add(channel)
        session.flush()
        subscription = Subscription(
            subscription_type='channel',
            url='https://www.youtube.com/@testchannel',
            channel_id=channel.id,
        )
        session.add(subscription)
        session.commit()
        return subscription.id


# Make successive scan_video_ids calls return `results` in order, one (ids, error) pair per call.
def script_scans(monkeypatch: pytest.MonkeyPatch, *results: tuple[list[str], str | None]) -> None:
    pending = list(results)
    monkeypatch.setattr(worker, 'scan_video_ids', lambda url: pending.pop(0))


# One failed scan is a transient blip: WARN naming the subscription and yt-dlp's reason, no ERROR.
def test_scan_failure_logs_warning(caplog, monkeypatch, subscription_id):
    caplog.set_level(logging.INFO)
    script_scans(monkeypatch, FAILED_SCAN)

    worker.check_subscriptions()

    assert any(
        f'Subscription {subscription_id}: scan failed' in message and 'HTTP Error 503' in message
        for message in messages_at(caplog, logging.WARNING)
    )
    assert messages_at(caplog, logging.ERROR) == []


# The third failed check in a row logs ERROR, and so does every third after it; the rest stay WARN.
def test_third_consecutive_scan_failure_logs_error(caplog, monkeypatch, subscription_id):
    caplog.set_level(logging.INFO)
    script_scans(monkeypatch, *[FAILED_SCAN] * 6)

    errors_per_check = []
    for _ in range(6):
        caplog.clear()
        worker.check_subscriptions()
        errors_per_check.append(messages_at(caplog, logging.ERROR))

    assert [len(errors) for errors in errors_per_check] == [0, 0, 1, 0, 0, 1]
    assert f'Subscription {subscription_id}: 3 consecutive checks failed' in errors_per_check[2][0]
    assert f'Subscription {subscription_id}: 6 consecutive checks failed' in errors_per_check[5][0]


# A successful scan clears the run, so two failures on either side of it never reach ERROR.
def test_successful_scan_resets_failure_count(caplog, monkeypatch, subscription_id):
    caplog.set_level(logging.INFO)
    script_scans(monkeypatch, FAILED_SCAN, FAILED_SCAN, ([], None), FAILED_SCAN, FAILED_SCAN)

    for _ in range(5):
        worker.check_subscriptions()

    assert messages_at(caplog, logging.ERROR) == []
    scan_warnings = [m for m in messages_at(caplog, logging.WARNING) if 'scan failed' in m]
    assert len(scan_warnings) == 4


# A successful call relays each distinct stderr WARNING: line once, at WARN, in first-seen order.
# Other stderr lines stay out of the log, and stdout comes back untouched for the caller to parse.
def test_run_command_relays_ytdlp_warnings_deduplicated(caplog, monkeypatch):
    caplog.set_level(logging.INFO)
    pot_warning = 'WARNING: [youtube] [pot] No PO token provider available'
    jsc_warning = 'WARNING: [youtube] [jsc] JS challenge solver unavailable, falling back'
    stderr = f'{pot_warning}\n[youtube] dQw4w9WgXcQ: Downloading webpage\n{jsc_warning}\n{pot_warning}\n'

    def fake_run(command, **kwargs):
        return subprocess.CompletedProcess(command, 0, stdout='{"id": "dQw4w9WgXcQ"}', stderr=stderr)

    monkeypatch.setattr(worker.subprocess, 'run', fake_run)
    monkeypatch.setattr(worker, 'throttle_youtube_requests', lambda: None)

    result = worker.run_command(['yt-dlp', '--dump-single-json', 'https://www.youtube.com/watch?v=dQw4w9WgXcQ'])

    assert result.stdout == '{"id": "dQw4w9WgXcQ"}'
    assert messages_at(caplog, logging.WARNING) == [pot_warning, jsc_warning]
    assert not any('Downloading webpage' in record.getMessage() for record in caplog.records)


# Neither metadata lookup passes --no-warnings any more, so yt-dlp's warnings reach that relay.
def test_metadata_lookups_keep_ytdlp_warnings(monkeypatch):
    commands = []

    # Record the argv and answer as a successful yt-dlp call that printed an empty info dict.
    def fake_run_command(command, timeout=worker.COMMAND_TIMEOUT_SECONDS):
        commands.append(command)
        return subprocess.CompletedProcess(command, 0, stdout='{}', stderr='')

    monkeypatch.setattr(worker, 'run_command', fake_run_command)
    job = Download(id=1, url='https://www.youtube.com/watch?v=dQw4w9WgXcQ', request_type='video')
    subscription = Subscription(id=1, subscription_type='channel', url='https://www.youtube.com/@testchannel')

    assert worker.discover_metadata(job) == {}
    # An empty info dict carries no channel id, so the resolve fails without touching a session.
    assert worker.resolve_subscription_target(None, subscription) is False

    assert len(commands) == 2
    assert not any('--no-warnings' in command for command in commands)


# A failed discovery relays yt-dlp's WARNING: lines at WARN on their own, so the ERROR: line is
# what gets stored and logged even when the warnings ahead of it would fill the 500-char log line.
def test_discovery_failure_keeps_ytdlp_error_past_warnings(caplog, monkeypatch, db, make_job):
    caplog.set_level(logging.INFO)
    job = make_job()
    pot_warning = 'WARNING: [youtube] [pot:bgutil:http] ' + 'PO token provider unreachable; ' * 10
    sabr_warning = 'WARNING: [youtube] Some web client formats are missing due to SABR; ' * 5
    reason = 'ERROR: [youtube] dQw4w9WgXcQ: Video unavailable. This video has been removed'
    stderr = f'{pot_warning}\n{sabr_warning}\n{reason}\n'

    def fake_run(command, **kwargs):
        raise subprocess.CalledProcessError(1, command, output='', stderr=stderr)

    monkeypatch.setattr(worker.subprocess, 'run', fake_run)
    monkeypatch.setattr(worker, 'throttle_youtube_requests', lambda: None)

    assert worker.discover_metadata(job) is None

    with db() as session:
        stored = session.get(Download, job.id)
        assert stored.status == 'failed'
        assert stored.error == reason
    warnings = messages_at(caplog, logging.WARNING)
    assert warnings[:2] == [pot_warning.strip(), sabr_warning.strip()]
    assert f'Job {job.id}: metadata discovery failed: {reason}' in warnings
    assert messages_at(caplog, logging.ERROR) == []


# The stream remembers yt-dlp's last ERROR: line and returns it with the outcome. yt-dlp's WARNING:
# and ERROR: lines are relayed at WARN, everything else at INFO, and nothing at ERROR.
def test_stream_records_last_ytdlp_error_line(caplog, make_job):
    caplog.set_level(logging.INFO)
    job = make_job(status='downloading')
    output = (
        '[youtube] dQw4w9WgXcQ: Downloading webpage\n'
        'WARNING: [youtube] Falling back to generic n function search\n'
        'ERROR: [download] Got error: HTTP Error 403: Forbidden\n'
        'ERROR: [youtube] dQw4w9WgXcQ: Requested format is not available\n'
    )

    outcome, last_error = worker.stream_process_output(FinishedProcess(output), job.id)

    assert outcome == 'finished'
    assert last_error == 'ERROR: [youtube] dQw4w9WgXcQ: Requested format is not available'
    assert messages_at(caplog, logging.WARNING) == [
        'WARNING: [youtube] Falling back to generic n function search',
        'ERROR: [download] Got error: HTTP Error 403: Forbidden',
        'ERROR: [youtube] dQw4w9WgXcQ: Requested format is not available',
    ]
    assert '[youtube] dQw4w9WgXcQ: Downloading webpage' in messages_at(caplog, logging.INFO)
    assert messages_at(caplog, logging.ERROR) == []


# A failed download stores yt-dlp's last ERROR: line as the job's error rather than the exit code,
# and reports it at WARN: auto-retry gets the next attempt, so it does not page.
def test_failed_download_stores_last_ytdlp_error(caplog, monkeypatch, db, make_job, library):
    caplog.set_level(logging.INFO)
    job = make_job(title='Known title', youtube_id='dQw4w9WgXcQ')
    reason = 'ERROR: [youtube] dQw4w9WgXcQ: Video unavailable'

    monkeypatch.setattr(worker, 'resolve_expected_output_path', lambda job, output_template=None: None)
    monkeypatch.setattr(worker.subprocess, 'Popen', lambda command, **kwargs: FinishedProcess(f'{reason}\n', 1))
    # The real one signals with SIGKILL, which Windows lacks; this process has exited anyway.
    monkeypatch.setattr(worker, 'terminate_process_group', lambda process, grace_seconds=0.0: None)
    monkeypatch.setattr(worker, 'finalize_after_download', lambda job: None)

    worker.download_video(job)

    with db() as session:
        stored = session.get(Download, job.id)
        assert stored.status == 'failed'
        assert stored.error == reason
    assert f'Job {job.id}: download failed: {reason}' in messages_at(caplog, logging.WARNING)
    assert messages_at(caplog, logging.ERROR) == []


# 'No space left on device' in the download output logs one ERROR naming the staging directory,
# however many lines (yt-dlp's and ffmpeg's) repeat it.
def test_enospc_logs_error(caplog, make_job):
    caplog.set_level(logging.INFO)
    job = make_job(status='downloading')
    output = (
        'ERROR: unable to write data: [Errno 28] No space left on device\n'
        '[out#0/mp4] Error writing trailer: No space left on device\n'
    )

    worker.stream_process_output(FinishedProcess(output), job.id)

    errors = messages_at(caplog, logging.ERROR)
    assert len(errors) == 1
    assert str(worker.DOWNLOAD_PATH) in errors[0]


# A move into the library that fails, here on a full disk, logs ERROR naming the destination and
# leaves the source in staging for the next scan.
def test_organizer_move_failure_logs_error(caplog, monkeypatch, library):
    caplog.set_level(logging.INFO)
    source = library.staging / 'Title [dQw4w9WgXcQ].mp4'
    source.write_bytes(b'video')
    movie_dir = library.media / 'Title [dQw4w9WgXcQ]'
    plan = organizer.OrganizePlan(
        dest_video=movie_dir / 'Title [dQw4w9WgXcQ].mp4',
        dest_nfo=movie_dir / 'Title [dQw4w9WgXcQ].nfo',
        dest_thumb=movie_dir / 'Title [dQw4w9WgXcQ]-thumb.jpg',
        nfo_root='movie',
        nfo_fields={'title': 'Title'},
    )
    job = Download(id=1, url='https://www.youtube.com/watch?v=dQw4w9WgXcQ', request_type='video',
                   youtube_id='dQw4w9WgXcQ', filename=str(source))

    def full_disk(src, dst):
        raise OSError(errno.ENOSPC, 'No space left on device')

    monkeypatch.setattr(organizer, 'safe_move', full_disk)

    assert organizer.execute_plan(None, job, plan, source, {}, dry_run=False) is False

    errors = messages_at(caplog, logging.ERROR)
    assert len(errors) == 1
    assert errors[0].startswith('[organizer] ')
    assert str(plan.dest_video) in errors[0] and 'No space left on device' in errors[0]
    assert source.exists()


# An unexpected exception while organizing one job logs ERROR with its traceback, which applog
# renders indented under the message so Promtail keeps it in the same entry.
def test_organizer_catch_all_logs_traceback(caplog, monkeypatch, make_job, library):
    caplog.set_level(logging.INFO)
    video = library.staging / 'Title [dQw4w9WgXcQ].mp4'
    video.write_bytes(b'video')
    job = make_job(status='completed', youtube_id='dQw4w9WgXcQ', filename=str(video))

    def explode(db, job, dry_run, force_nfo=False):
        raise RuntimeError('planner exploded')

    monkeypatch.setattr(organizer, 'organize_job', explode)
    monkeypatch.setattr(organizer, 'JELLYFIN_URL', None)

    assert organizer.scan_and_process() == 0

    [record] = [record for record in caplog.records if record.levelno == logging.ERROR]
    assert f'[organizer] Job {job.id}: unexpected failure' in record.getMessage()
    assert record.exc_info is not None
    first_line, *traceback_lines = applog.StackFormatter().format(record).splitlines()
    assert first_line.startswith('[')
    assert any('RuntimeError: planner exploded' in line for line in traceback_lines)
    assert all(line.startswith('    ') for line in traceback_lines)


# Jellyfin refusing the token fails every refresh until someone fixes it, so 401 and 403 log ERROR.
@pytest.mark.parametrize('status', [401, 403])
def test_jellyfin_401_logs_error(caplog, monkeypatch, status):
    caplog.set_level(logging.INFO)
    monkeypatch.setattr(organizer, 'JELLYFIN_URL', 'https://jellyfin.invalid:8920')
    monkeypatch.setattr(organizer, 'JELLYFIN_TOKEN', 'not-a-real-token')
    monkeypatch.setattr(organizer.requests, 'post', lambda *args, **kwargs: jellyfin_response(status))

    organizer.notify_jellyfin()

    errors = messages_at(caplog, logging.ERROR)
    assert len(errors) == 1
    assert f'HTTP {status}' in errors[0]


# Any other refresh failure, a server error or an unreachable Jellyfin, logs WARN and never ERROR.
@pytest.mark.parametrize(
    'failure',
    [500, requests.ConnectionError('connection refused')],
    ids=['http-500', 'unreachable'],
)
def test_jellyfin_other_failures_log_warning(caplog, monkeypatch, failure):
    caplog.set_level(logging.INFO)
    monkeypatch.setattr(organizer, 'JELLYFIN_URL', 'https://jellyfin.invalid:8920')
    monkeypatch.setattr(organizer, 'JELLYFIN_TOKEN', 'not-a-real-token')

    # Raise `failure` when it is an exception, otherwise answer with that HTTP status.
    def post(*args, **kwargs):
        if isinstance(failure, Exception):
            raise failure
        return jellyfin_response(failure)

    monkeypatch.setattr(organizer.requests, 'post', post)

    organizer.notify_jellyfin()

    assert messages_at(caplog, logging.ERROR) == []
    assert any('Failed to notify Jellyfin' in m for m in messages_at(caplog, logging.WARNING))


# A stored path outside both roots is refused with an ERROR naming it, and the file is left alone.
def test_out_of_root_delete_logs_error(caplog, tmp_path, library):
    caplog.set_level(logging.INFO)
    outside = tmp_path / 'elsewhere' / 'Title [dQw4w9WgXcQ].mp4'
    outside.parent.mkdir()
    outside.write_bytes(b'video')
    job = Download(id=1, url='https://www.youtube.com/watch?v=dQw4w9WgXcQ', request_type='video',
                   filename=str(outside))

    organizer.remove_downloaded_files(job)

    assert outside.exists()
    errors = messages_at(caplog, logging.ERROR)
    assert len(errors) == 1
    assert str(outside) in errors[0]
