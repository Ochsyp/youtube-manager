# -------------------------------------------------------
# Worker process tests
#
# Runs the worker's download path and its lookups against tests/fakes/yt_dlp_fake.py installed on
# PATH as an executable 'yt-dlp', so the real Popen, process-group handling and argv are exercised.
# POSIX only: the fake is started through its shebang and stopped with os.killpg.
# -------------------------------------------------------

import json
import os
import signal
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


# A yt-dlp that finished its file, closed its output and never exits is ended within seconds of a
# stop. The job goes back in the queue with its retries as they were, and the next run adopts the
# finished file instead of downloading it again.
@pytest.mark.posix
def test_stop_ends_a_yt_dlp_that_closed_its_output(ytdlp_on_path, db, make_job, library, monkeypatch):
    job = make_job(youtube_id=VIDEO_ID, title='Fake Video', auto_retry_attempts=3)
    final = library.staging / f'Fake Video [{VIDEO_ID}].mp4'
    monkeypatch.setenv('YTDLP_FAKE_MODE', 'close-output-hang')
    stopped_at = []

    # Ask for the stop once the fake has written its file and gone quiet.
    def stop_after_the_file() -> None:
        deadline = time.monotonic() + 30
        while not final.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        time.sleep(0.5)
        stopped_at.append(time.monotonic())
        worker._shutdown.set()

    stopper = threading.Thread(target=stop_after_the_file, daemon=True)
    stopper.start()
    try:
        worker.download_video(job)
        ended_at = time.monotonic()
    finally:
        stopper.join(35)
        worker._shutdown.clear()

    assert ended_at - stopped_at[0] < 1 + worker.PROCESS_KILL_TIMEOUT_SECONDS + 3
    row = stored(db, job.id)
    assert row.status == 'queued'
    assert row.error is None
    assert row.auto_retry_attempts == 3

    worker.download_video(job)

    row = stored(db, job.id)
    assert row.status == 'completed'
    assert row.filename == str(final)


# Ask for a stop once the fake in a hang-tree mode has written 'started' to `marker_dir`, and
# return the thread doing it; the time of the stop is appended to `stopped_at`.
def stop_once_started(marker_dir: Path, stopped_at: list) -> threading.Thread:
    def stop() -> None:
        deadline = time.monotonic() + 30
        while not (marker_dir / 'started').exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        stopped_at.append(time.monotonic())
        worker._shutdown.set()

    thread = threading.Thread(target=stop, daemon=True)
    thread.start()
    return thread


# A stop during a lookup that hangs ends yt-dlp and the child it started, each by its SIGTERM,
# within seconds. The job goes back in the queue with its retries as they were. The image smoke
# test repeats this inside the built image.
@pytest.mark.posix
def test_stop_during_a_hanging_lookup_ends_the_whole_tree(
    ytdlp_on_path, db, make_job, library, tmp_path, monkeypatch,
):
    job = make_job(youtube_id=VIDEO_ID, auto_retry_attempts=2)
    marker_dir = tmp_path / 'markers'
    marker_dir.mkdir()
    monkeypatch.setenv('YTDLP_FAKE_MODE', 'hang-tree-lookup')
    monkeypatch.setenv('YTDLP_FAKE_MARKER_DIR', str(marker_dir))
    stopped_at = []

    stopper = stop_once_started(marker_dir, stopped_at)
    try:
        with pytest.raises(worker.ShutdownRequested):
            worker.run_job_guarded(job)
        ended_at = time.monotonic()
    finally:
        stopper.join(35)
        worker._shutdown.clear()

    assert ended_at - stopped_at[0] < 1 + worker.PROCESS_KILL_TIMEOUT_SECONDS + 3
    assert (marker_dir / 'child-stopped').exists()
    assert (marker_dir / 'parent-stopped').exists()
    row = stored(db, job.id)
    assert row.status == 'queued'
    assert row.auto_retry_attempts == 2


# A stop during a download that hangs, after the lookups answered, ends yt-dlp and its child the
# same way, and the job goes back in the queue with its retries as they were.
@pytest.mark.posix
def test_stop_during_a_hanging_download_ends_the_whole_tree(
    ytdlp_on_path, db, make_job, library, tmp_path, monkeypatch,
):
    job = make_job(youtube_id=VIDEO_ID, auto_retry_attempts=2)
    marker_dir = tmp_path / 'markers'
    marker_dir.mkdir()
    monkeypatch.setenv('YTDLP_FAKE_MODE', 'hang-tree-download')
    monkeypatch.setenv('YTDLP_FAKE_MARKER_DIR', str(marker_dir))
    stopped_at = []

    stopper = stop_once_started(marker_dir, stopped_at)
    try:
        worker.run_job_guarded(job)
        ended_at = time.monotonic()
    finally:
        stopper.join(35)
        worker._shutdown.clear()

    assert ended_at - stopped_at[0] < 1 + worker.PROCESS_KILL_TIMEOUT_SECONDS + 3
    assert (marker_dir / 'child-stopped').exists()
    assert (marker_dir / 'parent-stopped').exists()
    row = stored(db, job.id)
    assert row.status == 'queued'
    assert row.auto_retry_attempts == 2


# The pids of the children the fake's stubborn modes started, read from the file they append to.
# Any child still alive after the test is killed, so a failing test leaves nothing running.
@pytest.fixture
def stubborn_pids(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    pid_file = tmp_path / 'children.pid'
    monkeypatch.setenv('YTDLP_FAKE_CHILD_PID_FILE', str(pid_file))

    def pids() -> list[int]:
        return [int(pid) for pid in pid_file.read_text().split()] if pid_file.exists() else []

    yield pids
    for pid in pids():
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


# The pids among `pids` that still run.
def still_running(pids: list[int]) -> list[int]:
    return [pid for pid in pids if is_running(pid)]


# A lookup that runs out of time also ends a child of yt-dlp that ignores SIGTERM: SIGKILL follows
# after PROCESS_KILL_TIMEOUT_SECONDS, so the call takes no longer than when yt-dlp itself ignores
# SIGTERM.
@pytest.mark.posix
def test_lookup_timeout_ends_a_stubborn_child(ytdlp_on_path, stubborn_pids, monkeypatch):
    monkeypatch.setenv('YTDLP_FAKE_MODE', 'stubborn-child')
    started = time.monotonic()

    with pytest.raises(subprocess.TimeoutExpired):
        worker.run_command(LOOKUP, timeout=2)

    assert time.monotonic() - started < 2 + 2 * worker.PROCESS_KILL_TIMEOUT_SECONDS + 3
    assert stubborn_pids()
    assert still_running(stubborn_pids()) == []


# A cancelled download ends a child of yt-dlp that ignores SIGTERM before download_video returns,
# and the job ends cancelled.
@pytest.mark.posix
def test_cancel_ends_a_stubborn_child(ytdlp_on_path, stubborn_pids, db, make_job, library, monkeypatch):
    job = make_job(youtube_id=VIDEO_ID, title='Fake Video')
    monkeypatch.setenv('YTDLP_FAKE_MODE', 'stubborn-child-download')

    # Ask for the cancel once the download has started its child.
    def cancel_once_started() -> None:
        deadline = time.monotonic() + 30
        while not stubborn_pids() and time.monotonic() < deadline:
            time.sleep(0.05)
        with db() as session:
            session.get(Download, job.id).cancel_requested = True
            session.commit()

    canceller = threading.Thread(target=cancel_once_started, daemon=True)
    canceller.start()
    try:
        worker.download_video(job)
    finally:
        canceller.join(35)

    assert stored(db, job.id).status == 'cancelled'
    assert stubborn_pids()
    assert still_running(stubborn_pids()) == []


# A child that yt-dlp leaves running when it exits by itself, here after a successful lookup, is
# ended before run_command returns.
@pytest.mark.posix
def test_child_left_by_a_finished_lookup_is_ended(ytdlp_on_path, stubborn_pids, monkeypatch):
    monkeypatch.setenv('YTDLP_FAKE_MODE', 'stubborn-child-exit')

    result = worker.run_command(LOOKUP, timeout=60)

    assert json.loads(result.stdout)['id'] == VIDEO_ID
    assert stubborn_pids()
    assert still_running(stubborn_pids()) == []


# The same for a download that completes: the children its lookup and its download left are
# ended, and the job completes.
@pytest.mark.posix
def test_children_left_by_a_finished_download_are_ended(
    ytdlp_on_path, stubborn_pids, db, make_job, library, monkeypatch,
):
    job = make_job(youtube_id=VIDEO_ID, title='Fake Video')
    monkeypatch.setenv('YTDLP_FAKE_MODE', 'stubborn-child-exit')

    worker.download_video(job)

    assert stored(db, job.id).status == 'completed'
    assert stubborn_pids()
    assert still_running(stubborn_pids()) == []


# A process whose pid names no group of its own is never signalled, running or not: killpg(0) is
# the caller's own group, killpg(-1) reaches pid 1, and pid 1 and the caller's group are not
# yt-dlp's. Stand-ins in tests carry such pids; os.killpg is replaced, so a failure signals nothing.
@pytest.mark.posix
@pytest.mark.parametrize('running', [False, True])
@pytest.mark.parametrize('pid', [0, -1, 1, 'own group'])
def test_terminate_never_signals_a_group_that_is_not_yt_dlps(monkeypatch, pid, running):
    sent = []
    monkeypatch.setattr(worker.os, 'killpg', lambda pgid, sig: sent.append((pgid, sig)))
    monkeypatch.setattr(worker, 'PROCESS_KILL_TIMEOUT_SECONDS', 0.2)

    # A Popen stand-in with that pid, still running or already exited 0.
    class Process:
        def __init__(self):
            self.pid = os.getpgrp() if pid == 'own group' else pid
            self.returncode = None if running else 0

        def poll(self):
            return self.returncode

        def wait(self, timeout=None):
            return self.returncode

    worker.terminate_process_group(Process())

    assert sent == []
