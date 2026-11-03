# -------------------------------------------------------
# Worker process tests
#
# Runs the worker's download path against tests/fakes/yt_dlp_fake.py installed on PATH as an
# executable 'yt-dlp', so the real Popen, process-group handling and argv are exercised. POSIX
# only: the fake is started through its shebang and stopped with os.killpg.
# -------------------------------------------------------

import os
import stat
import sys
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
