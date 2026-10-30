# -------------------------------------------------------
# Download file-identity tests
#
# Pins how the worker learns which file a download produced: the -P/-o arguments both yt-dlp calls
# share, the final path yt-dlp reports through --print-to-file, the WARN fallback when it reports
# none, the .info.json id check and the cancel cleanup. yt-dlp is the fake under tests/fakes.
# -------------------------------------------------------

import io
import json
import logging
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

import organizer
import worker
from database import Download
from media import DOWNLOAD_PATH, STAGING_TEMP

VIDEO_ID = 'dQw4w9WgXcQ'
OTHER_ID = 'aaaaaaaaaaa'
URL = f'https://www.youtube.com/watch?v={VIDEO_ID}'
FAKE_YTDLP = Path(__file__).parent / 'fakes' / 'yt_dlp_fake.py'


# Messages logged at exactly `level`, in the order they were logged.
def messages_at(caplog: pytest.LogCaptureFixture, level: int) -> list[str]:
    return [record.getMessage() for record in caplog.records if record.levelno == level]


# True when `needle` appears in `haystack` as one contiguous run.
def contains_run(haystack: list[str], needle: list[str]) -> bool:
    return any(haystack[i:i + len(needle)] == needle for i in range(len(haystack) - len(needle) + 1))


# Stands in for the yt-dlp Popen: stdout is fixed text, and the process has already exited with
# `returncode`.
class FinishedProcess:
    def __init__(self, output: str, returncode: int = 0):
        self.stdout = io.StringIO(output)
        self.returncode = returncode
        self.pid = -1

    def poll(self) -> int:
        return self.returncode

    def wait(self, timeout: float | None = None) -> int:
        return self.returncode


# Run every 'yt-dlp' command, Popen and subprocess.run alike, as the fake under this interpreter,
# so the worker's real argv reaches it on Windows too. The process is reaped instead of signalled,
# since Windows has no killpg, and nothing is organized afterwards. Mode 'complete' unless a test
# sets YTDLP_FAKE_MODE itself.
@pytest.fixture
def fake_ytdlp(monkeypatch: pytest.MonkeyPatch) -> None:
    real_popen = subprocess.Popen

    # Swap a leading 'yt-dlp' for the fake script; any other command runs unchanged.
    def popen(command, *args, **kwargs):
        if command and command[0] == 'yt-dlp':
            command = [sys.executable, str(FAKE_YTDLP), *command[1:]]
        return real_popen(command, *args, **kwargs)

    monkeypatch.setattr(subprocess, 'Popen', popen)
    monkeypatch.setattr(worker, 'throttle_youtube_requests', lambda: None)
    monkeypatch.setattr(worker, 'terminate_process_group', lambda process, grace_seconds=0.0: process.wait())
    monkeypatch.setattr(worker, 'finalize_after_download', lambda job: None)
    monkeypatch.setenv('YTDLP_FAKE_MODE', 'complete')


# Replace the yt-dlp process with FinishedProcess(output), running `on_start` first, and skip the
# pre-check lookup and the organizer, so a test controls exactly what lands in staging.
def stub_download(monkeypatch: pytest.MonkeyPatch, on_start=lambda: None, output: str = '') -> None:
    # Ignore the argv: produce the files, then hand back a process that has already exited 0.
    def popen(command, **kwargs):
        on_start()
        return FinishedProcess(output)

    monkeypatch.setattr(worker.subprocess, 'Popen', popen)
    monkeypatch.setattr(worker, 'resolve_expected_output_path', lambda job, output_template=None: None)
    monkeypatch.setattr(worker, 'terminate_process_group', lambda process, grace_seconds=0.0: None)
    monkeypatch.setattr(worker, 'finalize_after_download', lambda job: None)


# The stored row for `job_id`, read in a fresh session.
def stored(db, job_id: int) -> Download:
    with db() as session:
        return session.get(Download, job_id)


# Create each path, folders included, with a little content.
def touch_all(*paths: Path) -> None:
    for path in paths:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('x', encoding='utf-8')


# Final files go to staging and intermediates to its .incomplete folder, through -P. The template
# itself stays relative: an absolute -o would make yt-dlp ignore both -P paths.
def test_output_args_use_home_temp_and_relative_template():
    for template in (worker.OUTPUT_TEMPLATE_FLAT, worker.OUTPUT_TEMPLATE_CHANNEL):
        args = worker.ytdlp_output_args(template)
        assert args == ['-P', f'home:{DOWNLOAD_PATH}', '-P', f'temp:{STAGING_TEMP}', '-o', template]
        assert not Path(args[-1]).is_absolute()
    assert STAGING_TEMP == DOWNLOAD_PATH / '.incomplete'


# The pre-check asks yt-dlp with the download's own -P/-o arguments, so its answer is the path the
# download then reports, uploader subfolder included.
def test_expected_path_uses_same_output_args(fake_ytdlp, db, make_job, library, monkeypatch):
    commands = []
    real_run_command = worker.run_command

    # Record each argv, then run it as usual.
    def recording_run_command(command, timeout=worker.COMMAND_TIMEOUT_SECONDS):
        commands.append(command)
        return real_run_command(command, timeout)

    monkeypatch.setattr(worker, 'run_command', recording_run_command)
    monkeypatch.setattr(worker, 'resolve_output_template', lambda job: worker.OUTPUT_TEMPLATE_CHANNEL)
    job = make_job(youtube_id=VIDEO_ID, title='Known title')

    expected = worker.resolve_expected_output_path(job, worker.OUTPUT_TEMPLATE_CHANNEL)
    output_args = worker.ytdlp_output_args(worker.OUTPUT_TEMPLATE_CHANNEL)
    assert contains_run(commands[0], output_args)
    assert contains_run(worker.build_download_command(URL, worker.OUTPUT_TEMPLATE_CHANNEL, job.id), output_args)
    assert expected == library.staging / 'Fake Uploader' / f'Fake Video [{VIDEO_ID}].mp4'

    worker.download_video(job)

    assert stored(db, job.id).filename == str(expected)


# The download command has yt-dlp append the final path to this job's file in the temp directory,
# without --print, which would silence the progress output.
def test_build_download_command_writes_final_path_file():
    command = worker.build_download_command(URL, worker.OUTPUT_TEMPLATE_FLAT, 42)

    path_file = worker.final_path_file(42)
    assert path_file == Path(tempfile.gettempdir()) / 'ytm-job-42.path'
    assert contains_run(command, ['--print-to-file', 'after_move:filepath', str(path_file)])
    assert contains_run(command, worker.ytdlp_output_args(worker.OUTPUT_TEMPLATE_FLAT))
    assert '--print' not in command and '-O' not in command
    assert command[-1] == URL


# The report's last line wins. A last line naming a sidecar, a file in .incomplete, a file outside
# staging or a '..' escape is no report at all, and neither is a missing or empty file.
def test_read_final_path_uses_last_line(library, tmp_path):
    earlier = library.staging / f'Earlier [{VIDEO_ID}].mp4'
    final = library.staging / 'Uploader' / f'Title [{VIDEO_ID}].mkv'
    sidecar = library.staging / f'Title [{VIDEO_ID}].info.json'
    waiting = STAGING_TEMP / f'Title [{VIDEO_ID}].mp4'
    outside = tmp_path / f'Title [{VIDEO_ID}].mp4'
    touch_all(earlier, final, sidecar, waiting, outside)
    path_file = tmp_path / 'ytm-job-1.path'

    path_file.write_text(f'{earlier}\n{final}\n\n', encoding='utf-8')
    assert worker.read_final_path(path_file) == final

    # A real file one level above staging, named through staging so it passes a lexical prefix test.
    escaped = library.staging.parent / f'Escaped [{VIDEO_ID}].mp4'
    touch_all(escaped)
    escape = library.staging / '..' / escaped.name
    for last_line in (sidecar, waiting, outside, escape, library.staging / 'missing.mp4'):
        path_file.write_text(f'{final}\n{last_line}\n', encoding='utf-8')
        assert worker.read_final_path(path_file) is None, last_line
    escaped.unlink()

    path_file.write_text('', encoding='utf-8')
    assert worker.read_final_path(path_file) is None
    assert worker.read_final_path(tmp_path / 'never-written.path') is None


# A successful download records the path yt-dlp reported, with no fallback WARN, keeps yt-dlp's
# output in the job log, and leaves neither the report file nor anything in .incomplete behind.
def test_download_records_path_reported_by_ytdlp(fake_ytdlp, db, make_job, library, caplog):
    caplog.set_level(logging.INFO)
    job = make_job(youtube_id=VIDEO_ID, title='Known title')

    worker.download_video(job)

    final = library.staging / f'Fake Video [{VIDEO_ID}].mp4'
    row = stored(db, job.id)
    assert row.status == 'completed'
    assert row.filename == str(final)
    assert row.metadata_file == str(final.with_name(final.stem + '.info.json'))
    assert '[download] Destination:' in row.log
    assert not any('falls back' in m for m in messages_at(caplog, logging.WARNING))
    assert not worker.final_path_file(job.id).exists()
    assert [p for p in STAGING_TEMP.rglob('*') if p.is_file()] == []


# With no usable report, a zero exit still completes the job from the lookup in staging, and a
# WARN says the report was missing. An unusable report (here a sidecar's path) counts as missing,
# and a report left by an earlier attempt is discarded before yt-dlp starts, never read back.
@pytest.mark.parametrize('report', ['missing', 'sidecar'])
def test_missing_path_file_falls_back_with_warning(monkeypatch, db, make_job, library, caplog, report):
    caplog.set_level(logging.INFO)
    job = make_job(youtube_id=VIDEO_ID, title='Known title')
    media = library.staging / f'Known title [{VIDEO_ID}].mp4'
    info = media.with_name(media.stem + '.info.json')
    stale = library.staging / f'Earlier attempt [{OTHER_ID}].mp4'
    touch_all(stale)
    worker.final_path_file(job.id).write_text(f'{stale}\n', encoding='utf-8')

    # What yt-dlp leaves on disk, written when the stub "process" starts.
    def write_output() -> None:
        touch_all(media)
        info.write_text(json.dumps({'id': VIDEO_ID}), encoding='utf-8')
        if report == 'sidecar':
            worker.final_path_file(job.id).write_text(f'{info}\n', encoding='utf-8')

    stub_download(monkeypatch, on_start=write_output)

    worker.download_video(job)

    row = stored(db, job.id)
    assert row.status == 'completed'
    assert row.filename == str(media)
    fallbacks = [m for m in messages_at(caplog, logging.WARNING) if 'falls back' in m]
    assert len(fallbacks) == 1 and f'Job {job.id}' in fallbacks[0]
    assert not worker.final_path_file(job.id).exists()


# An .info.json describing another video is refused with a WARN and changes nothing; one for this
# video, even with yt-dlp's 'yt:' prefix on the id, is folded in.
def test_info_json_with_foreign_id_not_applied(db, make_job, library, caplog):
    caplog.set_level(logging.INFO)
    job = make_job(youtube_id=VIDEO_ID, title='Original title')
    info_path = library.staging / f'Title [{VIDEO_ID}].info.json'

    info_path.write_text(json.dumps({'id': OTHER_ID, 'title': 'Foreign title'}), encoding='utf-8')
    assert worker.apply_metadata_from_info_file(job, info_path) is False
    row = stored(db, job.id)
    assert (row.youtube_id, row.title, row.metadata_json) == (VIDEO_ID, 'Original title', None)
    assert any(OTHER_ID in m and str(info_path) in m for m in messages_at(caplog, logging.WARNING))

    info_path.write_text(json.dumps({'id': f'yt:{VIDEO_ID}', 'title': 'Own title'}), encoding='utf-8')
    assert worker.apply_metadata_from_info_file(job, info_path) is True
    assert stored(db, job.id).title == 'Own title'


# A cancelled download removes this video's files from .incomplete and its partials, fragments
# and sidecars from staging. Another video's files, a name without the bracketed tag, a file of
# unknown kind and anything outside staging all stay.
def test_cancel_removes_staging_leftovers_for_own_id_only(monkeypatch, db, make_job, library, tmp_path):
    job = make_job(youtube_id=VIDEO_ID, title='Title')
    removed = [
        STAGING_TEMP / f'Title [{VIDEO_ID}].f137.mp4',
        STAGING_TEMP / f'Title [{VIDEO_ID}].f251.webm.part',
        STAGING_TEMP / 'Uploader' / f'Title [{VIDEO_ID}].mp4',
        library.staging / f'Title [{VIDEO_ID}].info.json',
        library.staging / f'Title [{VIDEO_ID}].description',
        library.staging / f'Title [{VIDEO_ID}].webp',
        library.staging / f'Title [{VIDEO_ID}].f137.mp4',
        library.staging / f'Title [{VIDEO_ID}].mp4.part',
    ]
    kept = [
        STAGING_TEMP / f'Other [{OTHER_ID}].f137.mp4',
        library.staging / f'Other [{OTHER_ID}].info.json',
        library.staging / f'Title {VIDEO_ID}.info.json',
        library.staging / f'Title [{VIDEO_ID}].nfo',
        tmp_path / 'elsewhere' / f'Title [{VIDEO_ID}].info.json',
    ]
    touch_all(*removed, *kept)
    stub_download(monkeypatch)
    monkeypatch.setattr(worker, 'stream_process_output', lambda process, job_id: ('cancelled', None))

    worker.download_video(job)

    assert stored(db, job.id).status == 'cancelled'
    assert [path for path in removed if path.exists()] == []
    assert [path for path in kept if not path.exists()] == []


# While another row for the same video is still active, its download may resume these files, so
# nothing is removed; once that row has stopped, the same call cleans up.
def test_leftovers_kept_while_another_row_with_same_id_is_active(db, make_job, library):
    job = make_job(youtube_id=VIDEO_ID, status='cancelled')
    other = make_job(youtube_id=VIDEO_ID, status='queued')
    leftovers = [
        STAGING_TEMP / f'Title [{VIDEO_ID}].f137.mp4',
        library.staging / f'Title [{VIDEO_ID}].info.json',
    ]
    touch_all(*leftovers)

    organizer.remove_staging_leftovers(job)
    assert all(path.exists() for path in leftovers)

    with db() as session:
        session.get(Download, other.id).status = 'failed'
        session.commit()

    organizer.remove_staging_leftovers(job)
    assert not any(path.exists() for path in leftovers)


# Sidecars beside this video's finished media in staging belong to that file and stay, while the
# same id's sidecars in a folder with no media go.
def test_leftovers_beside_finished_media_are_kept(db, make_job, library):
    job = make_job(youtube_id=VIDEO_ID, status='cancelled')
    done = library.staging / 'Uploader' / f'Title [{VIDEO_ID}].mp4'
    beside = [done, done.with_name(done.stem + '.info.json'), done.with_name(done.stem + '.jpg')]
    orphan = library.staging / f'Title [{VIDEO_ID}].info.json'
    touch_all(*beside, orphan)

    organizer.remove_staging_leftovers(job)

    assert all(path.exists() for path in beside)
    assert not orphan.exists()
