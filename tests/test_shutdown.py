# -------------------------------------------------------
# Worker shutdown tests
#
# What the worker does on a stop mid-work: a job returns to the queue, long passes stop between
# items and touch the heartbeat, the main loop ends, and the exit status says stopped or failed.
# Tests that run yt-dlp as a real process are in test_worker_process.py.
# -------------------------------------------------------

import io
import logging
import os
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

import pytest
import requests

from youtube_manager import database, organizer, worker
from youtube_manager.database import Channel, Download, SchemaTooNewError, Subscription

REPO_ROOT = Path(__file__).resolve().parent.parent


# Every test starts and ends with no stop requested and no failed subscription check on record.
@pytest.fixture(autouse=True)
def no_stop_requested(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(worker, '_subscription_failures', {})
    worker._shutdown.clear()
    yield
    worker._shutdown.clear()


# Count calls of worker.touch_heartbeat; returns the list each call appends to.
@pytest.fixture
def heartbeats(monkeypatch: pytest.MonkeyPatch) -> list[None]:
    calls = []
    monkeypatch.setattr(worker, 'touch_heartbeat', lambda: calls.append(None))
    return calls


# The stored row for `job_id`, read in a fresh session.
def stored(db, job_id: int) -> Download:
    with db() as session:
        return session.get(Download, job_id)


# Insert two resolved channel subscriptions and return their ids, lowest first.
def make_two_subscriptions(db) -> list[int]:
    ids = []
    with db() as session:
        for number in (1, 2):
            channel = Channel(
                youtube_id=f'UC000000000000000000000{number}', name=f'Channel {number}',
                url=f'https://www.youtube.com/channel/UC000000000000000000000{number}',
            )
            session.add(channel)
            session.flush()
            subscription = Subscription(
                subscription_type='channel', url=f'https://www.youtube.com/@channel{number}',
                channel_id=channel.id,
            )
            session.add(subscription)
            session.flush()
            ids.append(subscription.id)
        session.commit()
    return ids


# A stop inside a job's yt-dlp lookup puts the job back where a restart would, never failed: one
# interrupted while downloading is queued again with no error, and the stop goes on to the loop.
def test_stop_during_a_lookup_puts_the_job_back_in_the_queue(db, make_job, monkeypatch):
    job = make_job(youtube_id='dQw4w9WgXcQ')

    # Stand in for a lookup cut off by a stop, after the job had reached 'downloading'.
    def interrupted_process_job(job):
        worker.update_job(job.id, status='downloading')
        worker._shutdown.set()
        raise worker.ShutdownRequested()

    monkeypatch.setattr(worker, 'process_job', interrupted_process_job)

    with pytest.raises(worker.ShutdownRequested):
        worker.run_job_guarded(job)

    row = stored(db, job.id)
    assert row.status == 'queued'
    assert row.error is None


# ShutdownRequested passes every `except Exception` guard, so a lookup that raises it is not
# counted as a failed check of its subscription.
def test_stop_inside_a_subscription_scan_records_no_failure(db, monkeypatch):
    first, second = make_two_subscriptions(db)
    calls = []

    # Stand in for a scan cut off by a stop.
    def interrupted_scan(session, subscription, playlist_items):
        calls.append(subscription.id)
        worker._shutdown.set()
        raise worker.ShutdownRequested()

    monkeypatch.setattr(worker, 'scan_subscription', interrupted_scan)

    with pytest.raises(worker.ShutdownRequested):
        worker.check_subscriptions()

    assert calls == [first]
    assert worker._subscription_failures == {}
    with db() as session:
        assert session.get(Subscription, first).last_checked is None


# A stop that arrives during one subscription's check lets that check finish and commit, then
# ends the pass before the next one; the heartbeat is touched for each subscription it starts.
def test_subscription_check_stops_between_subscriptions(db, monkeypatch, heartbeats):
    first, second = make_two_subscriptions(db)
    calls = []

    # Scan successfully, finding nothing, and ask for a stop meanwhile.
    def scan(session, subscription, playlist_items):
        calls.append(subscription.id)
        worker._shutdown.set()
        return [], None, subscription.url

    monkeypatch.setattr(worker, 'scan_subscription', scan)

    with pytest.raises(worker.ShutdownRequested):
        worker.check_subscriptions()

    assert calls == [first]
    assert len(heartbeats) >= 1
    with db() as session:
        assert session.get(Subscription, first).last_checked is not None
        assert session.get(Subscription, second).last_checked is None


# Insert two playlist parents with one child each and no title, as both backfills find them, and
# return their ids.
def make_two_parents(make_job) -> list[int]:
    ids = []
    for number in (1, 2):
        parent = make_job(
            url=f'https://www.youtube.com/playlist?list=PL{number}', request_type='playlist',
            status='completed',
        )
        make_job(
            url=f'https://www.youtube.com/watch?v=aaaaaaaaaa{number}', youtube_id=f'aaaaaaaaaa{number}',
            parent_id=parent.id, status='completed',
        )
        ids.append(parent.id)
    return ids


# The title backfill stops between parents once a stop is asked for, touching the heartbeat for
# each parent it starts.
def test_title_backfill_stops_between_parents(db, make_job, monkeypatch, heartbeats):
    make_two_parents(make_job)
    lookups = []

    # Answer the lookup and ask for a stop meanwhile.
    def discover(job, fail_on_error=True, store_error=True):
        lookups.append(job.id)
        worker._shutdown.set()
        return None

    monkeypatch.setattr(worker, 'discover_metadata', discover)

    with pytest.raises(worker.ShutdownRequested):
        worker.populate_missing_parent_metadata()

    assert len(lookups) == 1
    assert len(heartbeats) >= 1


# The playlist index backfill stops between parents once a stop is asked for, touching the
# heartbeat for each parent it starts.
def test_index_backfill_stops_between_parents(db, make_job, monkeypatch, heartbeats):
    make_two_parents(make_job)
    scans = []

    # Answer the scan with nothing and ask for a stop meanwhile.
    def scan(url, playlist_items=None):
        scans.append(url)
        worker._shutdown.set()
        return [], None

    monkeypatch.setattr(worker, 'scan_video_ids', scan)

    with pytest.raises(worker.ShutdownRequested):
        worker.backfill_all_playlist_indexes()

    assert len(scans) == 1
    assert len(heartbeats) >= 1


# The organizer asks its tick before each video and stops filing when it answers False, but still
# finishes its pass: Jellyfin hears about the video already filed.
def test_organizer_stops_between_videos_and_still_notifies(db, make_job, monkeypatch):
    for number in (1, 2):
        make_job(
            url=f'https://www.youtube.com/watch?v=bbbbbbbbbb{number}', youtube_id=f'bbbbbbbbbb{number}',
            status='completed',
        )
    filed, notified, ticks = [], [], []
    monkeypatch.setattr(organizer, 'library_ready', lambda: True)
    monkeypatch.setattr(organizer, 'relocate_misfiled_shows', lambda session, dry_run: 0)
    monkeypatch.setattr(organizer, 'repair_stale_filenames', lambda session, dry_run: 0)
    monkeypatch.setattr(organizer, 'repair_missing_thumbnails', lambda session, dry_run: 0)
    monkeypatch.setattr(organizer, 'is_organizable', lambda session, job, dry_run=False: True)
    monkeypatch.setattr(organizer, 'organize_job', lambda session, job, dry_run: filed.append(job.id) or True)
    monkeypatch.setattr(organizer, 'notify_jellyfin', lambda tick=None: notified.append(None))

    # Go on before the first video only.
    def tick() -> bool:
        ticks.append(None)
        return len(ticks) == 1

    assert organizer.scan_and_process(dry_run=False, tick=tick) == 1

    assert len(filed) == 1
    assert len(ticks) == 2
    assert notified == [None]


# The worker's organizer tick touches the heartbeat and says to stop once a stop is asked for.
def test_organizer_tick_touches_heartbeat_and_follows_the_stop(heartbeats):
    assert worker.organizer_tick() is True
    worker._shutdown.set()
    assert worker.organizer_tick() is False
    assert len(heartbeats) == 2


# Replace the worker's startup housekeeping and signal setup with no-ops, run every periodic task
# on every pass, and return the list each task's name is appended to when it runs.
def quiet_worker_loop(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    ran = []
    monkeypatch.setattr(worker.signal, 'signal', lambda signum, handler: None)
    for name in (
        'recover_stuck_jobs', 'backfill_missing_youtube_ids', 'retire_invalid_scan_rows',
        'resequence_all_channel_indexes', 'take_daily_snapshot',
    ):
        monkeypatch.setattr(worker, name, lambda *args: None)
    for name in (
        'ORGANIZER_INTERVAL_SECONDS', 'RETRY_INTERVAL_SECONDS', 'SUBSCRIPTION_CHECK_INTERVAL_SECONDS',
    ):
        monkeypatch.setattr(worker, name, 0)
    for name in (
        'retry_failed_jobs', 'check_subscriptions', 'populate_missing_parent_metadata',
        'backfill_all_playlist_indexes',
    ):
        monkeypatch.setattr(worker, name, lambda name=name: ran.append(name))
    monkeypatch.setattr(
        worker, 'organizer_scan_and_process', lambda dry_run, tick=None: ran.append('organize'),
    )
    return ran


# A stop that cuts a job short ends the main loop at once: no periodic task runs after it, and the
# worker logs that it stopped.
def test_main_loop_ends_after_a_stop_inside_a_job(db, make_job, monkeypatch, caplog):
    caplog.set_level(logging.INFO)
    ran = quiet_worker_loop(monkeypatch)
    job = make_job()
    monkeypatch.setattr(worker, 'get_next_job', lambda: job)

    # Stand in for a job cut off by a stop.
    def interrupted(job):
        worker._shutdown.set()
        raise worker.ShutdownRequested()

    monkeypatch.setattr(worker, 'run_job_guarded', interrupted)

    worker.worker()

    assert ran == []
    assert any('YouTube worker stopped' in record.getMessage() for record in caplog.records)


# A stop asked for during one periodic task keeps the next one from starting.
def test_main_loop_runs_no_periodic_task_after_a_stop(db, monkeypatch):
    ran = quiet_worker_loop(monkeypatch)
    monkeypatch.setattr(worker, 'get_next_job', lambda: None)
    monkeypatch.setattr(worker._shutdown, 'wait', lambda timeout=None: False)

    # The first periodic task finishes, and a stop arrives meanwhile.
    def organize(dry_run, tick=None):
        ran.append('organize')
        worker._shutdown.set()

    monkeypatch.setattr(worker, 'organizer_scan_and_process', organize)

    worker.worker()

    assert ran == ['organize']


# The worker exits 1 when the web app has not prepared the schema in time, with an ERROR line, so
# a container set to restart on failure comes back and monitoring sees a failure.
def test_main_exits_1_when_the_schema_wait_runs_out(monkeypatch, caplog):
    monkeypatch.setattr(worker, 'wait_for_schema', lambda timeout_seconds=None: False)
    monkeypatch.setattr(worker, 'worker', lambda: pytest.fail('the worker loop must not start'))

    assert worker.main() == 1
    assert any(
        'Timed out waiting for database schema' in record.getMessage()
        for record in caplog.records if record.levelno == logging.ERROR
    )


# The worker exits 1 on a schema from a later major release, which it must not touch.
def test_main_exits_1_on_a_newer_major_schema(monkeypatch):
    # Stand in for wait_for_schema finding a schema from a later hundred.
    def too_new(timeout_seconds=None):
        raise SchemaTooNewError('newer')

    monkeypatch.setattr(worker, 'wait_for_schema', too_new)
    monkeypatch.setattr(worker, 'worker', lambda: pytest.fail('the worker loop must not start'))

    assert worker.main() == 1


# The worker exits 0 once its loop has ended on a requested stop.
def test_main_exits_0_after_a_stop(monkeypatch):
    ran = []
    monkeypatch.setattr(worker, 'wait_for_schema', lambda timeout_seconds=None: True)
    monkeypatch.setattr(worker, 'worker', lambda: ran.append(None))

    assert worker.main() == 0
    assert ran == [None]


# checkpoint() touches the heartbeat, and raises ShutdownRequested once a stop is asked for.
def test_checkpoint_touches_heartbeat_and_raises_on_a_stop(heartbeats):
    worker.checkpoint()
    worker._shutdown.set()
    with pytest.raises(worker.ShutdownRequested):
        worker.checkpoint()
    assert len(heartbeats) == 2


# ShutdownRequested is not an Exception, so the worker's `except Exception` guards pass it on.
def test_shutdown_requested_passes_exception_guards():
    assert not issubclass(worker.ShutdownRequested, Exception)
    with pytest.raises(worker.ShutdownRequested):
        try:
            raise worker.ShutdownRequested()
        except Exception:
            pytest.fail('caught by an Exception guard')


# A finished lookup still returns its output and relays its warnings once Popen replaces
# subprocess.run, and a failed one still raises CalledProcessError with yt-dlp's stderr.
def test_run_command_returns_output_and_raises_on_failure(monkeypatch):
    # A process that has already exited with `returncode`, printing `stdout` and `stderr`.
    class Finished:
        def __init__(self, returncode, stdout, stderr):
            self.returncode, self.pid = returncode, 0
            self._output = (stdout, stderr)

        def communicate(self, timeout=None):
            return self._output

        def poll(self):
            return self.returncode

    outcomes = [Finished(0, '{"id": "x"}', ''), Finished(1, '', 'ERROR: gone')]
    monkeypatch.setattr(worker.subprocess, 'Popen', lambda command, **kwargs: outcomes.pop(0))
    monkeypatch.setattr(worker, 'throttle_youtube_requests', lambda: None)

    assert worker.run_command(['yt-dlp', '--dump-single-json', 'u']).stdout == '{"id": "x"}'
    with pytest.raises(subprocess.CalledProcessError) as raised:
        worker.run_command(['yt-dlp', '--dump-single-json', 'u'])
    assert raised.value.stderr == 'ERROR: gone'


# A stop during a lookup that runs while the job is still in a status the worker picks up, a
# metadata refresh or an expansion, leaves that status as it was, so the next start does the same
# work again: a refresh stays a refresh instead of becoming a new download.
@pytest.mark.parametrize('status, request_type', [
    ('metadata', 'video'), ('metadata', 'playlist'), ('expanding', 'playlist'),
])
def test_stop_leaves_a_job_in_a_pickup_status_as_it_was(db, make_job, monkeypatch, status, request_type):
    parent = make_job(
        url='https://www.youtube.com/playlist?list=PL9', request_type=request_type, status=status,
        filename='/downloads/Fake Video [dQw4w9WgXcQ].mp4' if request_type == 'video' else None,
    )
    if request_type == 'playlist' and status == 'metadata':
        make_job(youtube_id='ccccccccccc', parent_id=parent.id, status='completed')

    # Stand in for the job's lookup cut off by a stop.
    def interrupted_process_job(job):
        worker._shutdown.set()
        raise worker.ShutdownRequested()

    monkeypatch.setattr(worker, 'process_job', interrupted_process_job)

    with pytest.raises(worker.ShutdownRequested):
        worker.run_job_guarded(parent)

    row = stored(db, parent.id)
    assert (row.status, row.error) == (status, None)


# A job cut short by a stop is logged as stopped, not as a stuck job recovered after a crash, and
# a pending cancel reads as one at a stop.
def test_stop_logs_a_stop_not_a_crash_recovery(db, make_job, monkeypatch, caplog):
    caplog.set_level(logging.INFO)
    job = make_job(youtube_id='dQw4w9WgXcQ', status='downloading', cancel_requested=True)

    # Stand in for a download cut off by a stop.
    def interrupted_process_job(job):
        worker._shutdown.set()
        raise worker.ShutdownRequested()

    monkeypatch.setattr(worker, 'process_job', interrupted_process_job)

    with pytest.raises(worker.ShutdownRequested):
        worker.run_job_guarded(job)

    assert stored(db, job.id).error == 'Cancelled during shutdown'
    assert not any('stuck job' in record.getMessage() for record in caplog.records)


# A lookup asked for after a stop raises ShutdownRequested at once and starts no yt-dlp.
def test_run_command_starts_nothing_after_a_stop(monkeypatch):
    monkeypatch.setattr(worker, 'throttle_youtube_requests', lambda: None)
    monkeypatch.setattr(
        worker.subprocess, 'Popen', lambda *args, **kwargs: pytest.fail('yt-dlp was started'),
    )
    worker._shutdown.set()

    with pytest.raises(worker.ShutdownRequested):
        worker.run_command(['yt-dlp', '--dump-single-json', 'u'])


# A stop that arrives during the lookups before a download starts no download: the job stays
# queued, as it was, and goes back to the loop as ShutdownRequested.
def test_download_not_started_after_a_stop(db, make_job, monkeypatch):
    job = make_job(youtube_id='dQw4w9WgXcQ', title='Known title')

    # The expected-path lookup returns quickly, and a stop arrives meanwhile.
    def resolve(job, output_template=None):
        worker._shutdown.set()
        return None

    monkeypatch.setattr(worker, 'resolve_expected_output_path', resolve)
    monkeypatch.setattr(
        worker.subprocess, 'Popen', lambda *args, **kwargs: pytest.fail('yt-dlp was started'),
    )

    with pytest.raises(worker.ShutdownRequested):
        worker.download_video(job)

    assert stored(db, job.id).status == 'queued'


# Stands in for a yt-dlp that has closed its output: stdout is at EOF, and wait() times out until
# the call numbered `exits_on_wait`, when the process exits 0. None means it never exits by itself.
class LingeringProcess:
    def __init__(self, exits_on_wait: int | None = None):
        self.stdout = io.StringIO('')
        self.pid = -1
        self.returncode = None
        self.waits = 0
        self.exits_on_wait = exits_on_wait

    def poll(self) -> int | None:
        return self.returncode

    def wait(self, timeout: float | None = None) -> int:
        self.waits += 1
        if self.exits_on_wait is not None and self.waits >= self.exits_on_wait:
            self.returncode = 0
            return 0
        time.sleep(min(timeout or 0, 0.01))
        raise subprocess.TimeoutExpired('yt-dlp', timeout)


# Start `process` as the download's yt-dlp, running `on_start` first. Ending the process group sets
# exit code `ended_with`. Returns the list of jobs handed to the filing pass.
def start_download(
    monkeypatch, process: LingeringProcess, on_start=lambda: None, ended_with: int = -15,
) -> list[Download]:
    def popen(command, **kwargs):
        on_start()
        return process

    def end(target):
        if target.returncode is None:
            target.returncode = ended_with

    filed = []
    monkeypatch.setattr(worker.subprocess, 'Popen', popen)
    monkeypatch.setattr(worker, 'resolve_expected_output_path', lambda job, output_template=None: None)
    monkeypatch.setattr(worker, 'terminate_process_group', end)
    monkeypatch.setattr(worker, 'finalize_after_download', filed.append)
    return filed


# A stop during the wait for a yt-dlp that closed its output ends it and puts the job back: queued,
# no error, its retry count and next retry time unchanged, and no filing pass.
def test_stop_during_the_exit_wait_requeues_the_job(db, make_job, library, monkeypatch):
    next_retry = datetime(2030, 1, 1, 12, 0)
    job = make_job(
        youtube_id='dQw4w9WgXcQ', title='Known title', auto_retry_attempts=3,
        next_auto_retry_at=next_retry,
    )
    process = LingeringProcess()
    filed = start_download(monkeypatch, process, on_start=worker._shutdown.set)

    worker.download_video(job)

    row = stored(db, job.id)
    assert row.status == 'queued'
    assert row.error is None
    assert row.auto_retry_attempts == 3
    assert row.next_auto_retry_at == next_retry
    assert filed == []
    assert process.returncode == -15


# With no stop, a yt-dlp still there when the wait runs out is ended and its job fails.
def test_exit_wait_running_out_fails_the_job(db, make_job, library, monkeypatch):
    job = make_job(youtube_id='dQw4w9WgXcQ', title='Known title')
    monkeypatch.setattr(worker, 'PROCESS_EXIT_WAIT_SECONDS', 0.05)
    process = LingeringProcess()
    filed = start_download(monkeypatch, process)

    worker.download_video(job)

    row = stored(db, job.id)
    assert row.status == 'failed'
    assert 'exited with code -15' in row.error
    assert len(filed) == 1
    assert process.waits >= 1


# A yt-dlp that exits 0 has finished, even with a stop pending and even if it got there just as the
# stop ended the wait: its file counts.
@pytest.mark.parametrize(
    'exits_on_wait, ended_with', [(1, -15), (None, 0)], ids=['during-the-wait', 'at-the-signal'],
)
def test_clean_exit_completes_despite_a_stop(
    db, make_job, library, monkeypatch, exits_on_wait, ended_with,
):
    job = make_job(youtube_id='dQw4w9WgXcQ', title='Known title')
    media = library.staging / 'Known title [dQw4w9WgXcQ].mp4'

    def finish_and_stop() -> None:
        media.write_bytes(b'video')
        worker.final_path_file(job.id).write_text(f'{media}\n', encoding='utf-8')
        worker._shutdown.set()

    filed = start_download(
        monkeypatch, LingeringProcess(exits_on_wait=exits_on_wait), on_start=finish_and_stop,
        ended_with=ended_with,
    )

    worker.download_video(job)

    row = stored(db, job.id)
    assert row.status == 'completed'
    assert row.filename == str(media)
    assert len(filed) == 1


# Point the organizer at a Jellyfin that is never really contacted.
@pytest.fixture
def jellyfin(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(organizer, 'JELLYFIN_URL', 'https://jellyfin.invalid:8920')
    monkeypatch.setattr(organizer, 'JELLYFIN_TOKEN', 'not-a-real-token')


# A requests response with `status`, whose raise_for_status behaves like the real one.
def jellyfin_response(status: int) -> requests.Response:
    response = requests.Response()
    response.status_code = status
    return response


# Messages logged at exactly `level`.
def messages_at(caplog: pytest.LogCaptureFixture, level: int) -> list[str]:
    return [record.getMessage() for record in caplog.records if record.levelno == level]


# A stop during a refresh that gets no answer ends the wait after the short limit, with a WARN.
def test_stop_cuts_a_hung_jellyfin_refresh_short(caplog, monkeypatch, jellyfin):
    caplog.set_level(logging.INFO)
    monkeypatch.setattr(organizer, 'JELLYFIN_STOP_WAIT_SECONDS', 0.2)
    release = threading.Event()

    def hung_post(*args, **kwargs):
        release.wait(30)
        return jellyfin_response(204)

    monkeypatch.setattr(organizer.requests, 'post', hung_post)
    started = time.monotonic()
    try:
        organizer.notify_jellyfin(tick=lambda: time.monotonic() - started < 0.1)
        elapsed = time.monotonic() - started
    finally:
        release.set()
        for thread in threading.enumerate():
            if thread.name == 'jellyfin-refresh':
                thread.join(5)

    assert elapsed < 2
    assert any('Jellyfin' in message for message in messages_at(caplog, logging.WARNING))


# A Jellyfin that answers still gets its refresh when a stop is already pending.
def test_refresh_is_still_sent_after_a_stop(caplog, monkeypatch, jellyfin):
    caplog.set_level(logging.INFO)
    calls = []
    monkeypatch.setattr(
        organizer.requests, 'post',
        lambda *args, **kwargs: calls.append(kwargs['timeout']) or jellyfin_response(204),
    )

    organizer.notify_jellyfin(tick=lambda: False)

    assert calls == [10]
    assert messages_at(caplog, logging.WARNING) == []
    assert any('Requested Jellyfin' in message for message in messages_at(caplog, logging.INFO))


# A refresh reports its result from the calling thread, with or without a stop: a refused token is
# an ERROR, and any other failure a WARN.
@pytest.mark.parametrize('stop', [False, True], ids=['no-stop', 'stop'])
@pytest.mark.parametrize('failure, level', [
    (401, logging.ERROR),
    (403, logging.ERROR),
    (500, logging.WARNING),
    (requests.ConnectionError('connection refused'), logging.WARNING),
], ids=['http-401', 'http-403', 'http-500', 'unreachable'])
def test_refresh_result_is_logged_on_the_calling_thread(
    caplog, monkeypatch, jellyfin, failure, level, stop,
):
    caplog.set_level(logging.INFO)

    def post(*args, **kwargs):
        if isinstance(failure, Exception):
            raise failure
        return jellyfin_response(failure)

    monkeypatch.setattr(organizer.requests, 'post', post)

    organizer.notify_jellyfin(tick=lambda: not stop)

    assert any('Jellyfin' in message for message in messages_at(caplog, level))
    if level == logging.WARNING:
        assert messages_at(caplog, logging.ERROR) == []
    assert {record.threadName for record in caplog.records} == {threading.current_thread().name}


# An error the refresh does not expect is raised to the caller, as it is without a thread.
@pytest.mark.parametrize('stop', [False, True], ids=['no-stop', 'stop'])
def test_unexpected_refresh_error_reaches_the_caller(monkeypatch, jellyfin, stop):
    def post(*args, **kwargs):
        raise UnicodeEncodeError('latin-1', 'token', 0, 1, 'not encodable')

    monkeypatch.setattr(organizer.requests, 'post', post)

    with pytest.raises(UnicodeEncodeError):
        organizer.notify_jellyfin(tick=lambda: not stop)


# The worker's guard logs that error as one ERROR, with or without a stop.
@pytest.mark.parametrize('stop', [False, True], ids=['no-stop', 'stop'])
def test_unexpected_refresh_error_is_logged_as_error(
    caplog, db, make_job, monkeypatch, jellyfin, stop,
):
    caplog.set_level(logging.INFO)
    job = make_job(youtube_id='dQw4w9WgXcQ', status='completed')

    def post(*args, **kwargs):
        raise RuntimeError('refresh broke')

    monkeypatch.setattr(organizer.requests, 'post', post)
    monkeypatch.setattr(
        worker, 'organizer_scan_and_process',
        lambda dry_run, tick: organizer.notify_jellyfin(tick=tick),
    )
    if stop:
        worker._shutdown.set()

    worker.finalize_after_download(job)

    errors = messages_at(caplog, logging.ERROR)
    assert len(errors) == 1
    assert 'Organizer run failed' in errors[0]


# A refresh the worker has stopped waiting for ends in silence, even if it fails: the WARN has said
# so, and a thread that writes to the log while Python shuts down can abort the process.
def test_abandoned_refresh_logs_nothing(caplog, monkeypatch, jellyfin):
    caplog.set_level(logging.INFO)
    monkeypatch.setattr(organizer, 'JELLYFIN_STOP_WAIT_SECONDS', 0.05)
    release = threading.Event()
    crashed = []
    monkeypatch.setattr(threading, 'excepthook', crashed.append)

    def post(*args, **kwargs):
        release.wait(30)
        raise RuntimeError('too late')

    monkeypatch.setattr(organizer.requests, 'post', post)

    try:
        organizer.notify_jellyfin(tick=lambda: False)
        logged = len(caplog.records)
    finally:
        release.set()
        for thread in threading.enumerate():
            if thread.name == 'jellyfin-refresh':
                thread.join(5)

    assert len(messages_at(caplog, logging.WARNING)) == 1
    assert len(caplog.records) == logged
    assert crashed == []


# A pass that only repaired something asks for the refresh after its database session has closed.
def test_repair_only_refresh_runs_after_the_session_closes(db, monkeypatch):
    real_session = organizer.Session
    open_sessions = []
    seen = []

    @contextmanager
    def counted_session():
        with real_session() as session:
            open_sessions.append(session)
            try:
                yield session
            finally:
                open_sessions.remove(session)

    monkeypatch.setattr(organizer, 'Session', counted_session)
    monkeypatch.setattr(organizer, 'library_ready', lambda: True)
    monkeypatch.setattr(organizer, 'relocate_misfiled_shows', lambda session, dry_run: 0)
    monkeypatch.setattr(organizer, 'repair_stale_filenames', lambda session, dry_run: 0)
    monkeypatch.setattr(organizer, 'repair_missing_thumbnails', lambda session, dry_run: 1)
    monkeypatch.setattr(organizer, 'notify_jellyfin', lambda tick=None: seen.append(len(open_sessions)))

    assert organizer.scan_and_process(dry_run=False) == 0

    assert seen == [0]


# The refresh runs on a daemon thread and opens no database session; the stop check stays on the
# calling thread, which owns the heartbeat.
def test_refresh_thread_is_a_daemon_and_uses_no_session(monkeypatch, jellyfin):
    seen = []
    tick_threads = []
    release = threading.Event()

    def post(*args, **kwargs):
        seen.append(threading.current_thread())
        release.wait(5)
        return jellyfin_response(204)

    def tick() -> bool:
        tick_threads.append(threading.current_thread())
        release.set()
        return True

    monkeypatch.setattr(organizer.requests, 'post', post)
    for module in (organizer, database):
        monkeypatch.setattr(module, 'Session', lambda: pytest.fail('the refresh opened a session'))

    organizer.notify_jellyfin(tick=tick)

    assert len(seen) == 1
    assert seen[0] is not threading.current_thread()
    assert seen[0].daemon is True
    assert set(tick_threads) == {threading.current_thread()}


# What the exit test runs: a refresh that never answers, a stop, and then the end of the program.
REFRESH_AT_EXIT = '''
import time
from youtube_manager import organizer
organizer.JELLYFIN_URL = 'https://jellyfin.invalid:8920'
organizer.JELLYFIN_TOKEN = 'not-a-real-token'
organizer.JELLYFIN_STOP_WAIT_SECONDS = 0.1
organizer.requests.post = lambda *args, **kwargs: time.sleep(3600)
organizer.notify_jellyfin(tick=lambda: False)
print('returned', flush=True)
'''


# A refresh still running when the program ends does not keep the process alive.
def test_refresh_in_flight_does_not_keep_the_process_alive():
    result = subprocess.run(
        [sys.executable, '-c', REFRESH_AT_EXIT],
        cwd=REPO_ROOT,
        env={**os.environ, 'PYTHONPATH': str(REPO_ROOT)},
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert result.returncode == 0, result.stderr
    assert 'returned' in result.stdout
