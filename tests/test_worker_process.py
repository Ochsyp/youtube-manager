# -------------------------------------------------------
# Worker process tests
#
# Runs the worker's download path and its lookups against tests/fakes/yt_dlp_fake.py installed on
# PATH as an executable 'yt-dlp', so the real Popen, process-group handling and argv are exercised.
# POSIX only: the fake is started through its shebang and stopped with os.killpg.
# -------------------------------------------------------

import os
import stat
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from youtube_manager import worker
from youtube_manager.database import Download
from youtube_manager.media import STAGING_TEMP

VIDEO_ID = 'dQw4w9WgXcQ'
FAKE_YTDLP = Path(__file__).parent / 'fakes' / 'yt_dlp_fake.py'


# Copy the fake into a temp bin directory as an executable 'yt-dlp' run by this interpreter, and
# put that directory first on PATH. The yt-dlp throttle and the organizer pass are switched off.
@pytest.fixture
def ytdlp_on_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    bin_dir = tmp_path / 'bin'
    bin_dir.mkdir()
    source = FAKE_YTDLP.read_text(encoding='utf-8').split('\n', 1)[1]
    executable = bin_dir / 'yt-dlp'
    executable.write_text(f'#!{sys.executable}\n{source}', encoding='utf-8')
    executable.chmod(executable.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

    monkeypatch.setenv('PATH', f'{bin_dir}{os.pathsep}{os.environ.get("PATH", "")}')
    monkeypatch.setattr(worker, 'throttle_youtube_requests', lambda: None)
    monkeypatch.setattr(worker, 'finalize_after_download', lambda job: None)


# The stored row for `job_id`, read in a fresh session.
def stored(db, job_id: int) -> Download:
    with db() as session:
        return session.get(Download, job_id)


# A format stream left in staging by an older layout, and a fresh one from a download that fails,
# are never adopted: the failed run stays failed, its stream sits in .incomplete, and the next
# run completes with the merged file yt-dlp reports.
@pytest.mark.posix
def test_leftover_format_fragment_not_adopted_end_to_end(ytdlp_on_path, db, make_job, library, monkeypatch):
    leftover = library.staging / f'Fake Video [{VIDEO_ID}].f137.mp4'
    leftover.write_bytes(b'old stream')
    job = make_job(youtube_id=VIDEO_ID)

    monkeypatch.setenv('YTDLP_FAKE_MODE', 'fragment-then-fail')
    worker.download_video(job)

    row = stored(db, job.id)
    assert row.status == 'failed'
    assert row.filename is None
    assert 'fake failure' in row.error
    assert (STAGING_TEMP / leftover.name).exists()

    monkeypatch.setenv('YTDLP_FAKE_MODE', 'complete')
    worker.download_video(job)

    final = library.staging / f'Fake Video [{VIDEO_ID}].mp4'
    row = stored(db, job.id)
    assert row.status == 'completed'
    assert row.filename == str(final)
    assert final.with_name(final.stem + '.info.json').exists()
    assert not worker.final_path_file(job.id).exists()


# The argv of a metadata lookup, as discover_metadata starts it.
LOOKUP = ['yt-dlp', '--dump-single-json', f'https://www.youtube.com/watch?v={VIDEO_ID}']


# Whether process `pid` still runs: neither gone nor a zombie waiting to be reaped.
def is_running(pid: int) -> bool:
    try:
        state = Path(f'/proc/{pid}/stat').read_text().rsplit(')', 1)[1].split()[0]
    except FileNotFoundError:
        return False
    return state != 'Z'


# Wait up to `seconds` for process `pid` to stop running; True once it has.
def stops_within(pid: int, seconds: float) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if not is_running(pid):
            return True
        time.sleep(0.1)
    return not is_running(pid)


# A stop asked for during a lookup that prints nothing ends it within about two seconds, with
# ShutdownRequested, instead of waiting out the lookup's timeout.
@pytest.mark.posix
def test_stop_ends_a_stalled_lookup(ytdlp_on_path, monkeypatch):
    monkeypatch.setenv('YTDLP_FAKE_MODE', 'stall')
    timer = threading.Timer(0.5, worker._shutdown.set)
    started = time.monotonic()
    timer.start()
    try:
        with pytest.raises(worker.ShutdownRequested):
            worker.run_command(LOOKUP, timeout=60)
    finally:
        timer.cancel()
        worker._shutdown.clear()
    assert time.monotonic() - started < 5


# A lookup that runs out of time is ended with every process it started: the child it forked,
# as yt-dlp forks deno or ffmpeg, stops too.
@pytest.mark.posix
def test_lookup_timeout_ends_its_child(ytdlp_on_path, tmp_path, monkeypatch):
    pid_file = tmp_path / 'child.pid'
    monkeypatch.setenv('YTDLP_FAKE_MODE', 'spawn-child')
    monkeypatch.setenv('YTDLP_FAKE_CHILD_PID_FILE', str(pid_file))

    with pytest.raises(subprocess.TimeoutExpired):
        worker.run_command(LOOKUP, timeout=2)

    child = int(pid_file.read_text())
    assert stops_within(child, 5)


# A yt-dlp that ignores SIGTERM is killed with SIGKILL after PROCESS_KILL_TIMEOUT_SECONDS, which
# leaves the worker time to put its job back within Docker's default 10 s stop.
@pytest.mark.posix
def test_ignored_sigterm_is_followed_by_sigkill_within_5_s(ytdlp_on_path, monkeypatch):
    monkeypatch.setenv('YTDLP_FAKE_MODE', 'ignore-sigterm')
    started = time.monotonic()

    with pytest.raises(subprocess.TimeoutExpired):
        worker.run_command(LOOKUP, timeout=1)

    assert worker.PROCESS_KILL_TIMEOUT_SECONDS == 5
    assert time.monotonic() - started < 1 + worker.PROCESS_KILL_TIMEOUT_SECONDS + 3
