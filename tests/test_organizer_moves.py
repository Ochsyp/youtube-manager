# -------------------------------------------------------
# Organizer move tests
#
# Pins how the organizer writes into the library: safe_move's copy-then-rename across disks, the
# rules for a destination that already exists, atomic NFO and artwork writes, the library sentinel
# and the artwork URL checks. Nothing reaches the network: an autouse fixture fails any request a
# test did not arrange.
# -------------------------------------------------------

import errno
import io
import logging
import os
import time
from pathlib import Path

import pytest
import requests

import organizer
from database import Download

VIDEO_ID = 'dQw4w9WgXcQ'
THUMB_URL = f'https://i.ytimg.com/vi/{VIDEO_ID}/maxresdefault.jpg'


# Messages logged at exactly `level`, in the order they were logged.
def messages_at(caplog: pytest.LogCaptureFixture, level: int) -> list[str]:
    return [record.getMessage() for record in caplog.records if record.levelno == level]


# Stands in for requests.get and requests.post: a request no test arranged fails that test.
def refuse_network(url: str, *args, **kwargs) -> None:
    pytest.fail(f'unexpected network request to {url}')


# Every test starts with LIBRARY_SENTINEL unset, no missing-library ERROR on record, no Jellyfin
# to notify and no network, so neither the sentinel state nor a refresh leaks between tests.
@pytest.fixture(autouse=True)
def fresh_organizer_state(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv('LIBRARY_SENTINEL', raising=False)
    monkeypatch.setattr(organizer, '_library_error_logged_at', None)
    monkeypatch.setattr(organizer, 'JELLYFIN_URL', None)
    monkeypatch.setattr(organizer.requests, 'get', refuse_network)
    monkeypatch.setattr(organizer.requests, 'post', refuse_network)


# Make safe_move take its copy branch, as it does between the Pi's staging and library disks.
@pytest.fixture
def cross_device(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(organizer, 'same_filesystem', lambda src, dest: False)


# A source file in one folder and a destination path in another, both under tmp_path.
@pytest.fixture
def move_paths(tmp_path: Path) -> tuple[Path, Path]:
    src = tmp_path / 'staging' / 'video.mp4'
    dest = tmp_path / 'library' / 'video.mp4'
    src.parent.mkdir()
    dest.parent.mkdir()
    src.write_bytes(b'complete video')
    return src, dest


# The movie plan plan_movie builds for VIDEO_ID titled "Title" with no quality tag.
def movie_plan(media: Path) -> organizer.OrganizePlan:
    movie_dir = media / f'Title [{VIDEO_ID}]'
    return organizer.OrganizePlan(
        dest_video=movie_dir / f'Title [{VIDEO_ID}].mp4',
        dest_nfo=movie_dir / f'Title [{VIDEO_ID}].nfo',
        dest_thumb=movie_dir / f'Title [{VIDEO_ID}]-thumb.jpg',
        nfo_root='movie',
        nfo_fields={'title': 'Title'},
    )


# Put `source_bytes` in staging and `dest_bytes` at the plan's destination, then insert a completed
# job pointing at the staging copy. Returns the plan, the source path and the job.
def filed_twice(library, make_job, source_bytes: bytes, dest_bytes: bytes):
    plan = movie_plan(library.media)
    source = library.staging / f'Title [{VIDEO_ID}].mp4'
    source.write_bytes(source_bytes)
    plan.dest_video.parent.mkdir(parents=True)
    plan.dest_video.write_bytes(dest_bytes)
    job = make_job(status='completed', title='Title', youtube_id=VIDEO_ID, filename=str(source))
    return plan, source, job


# Run execute_plan for the job's row in a session of its own, as organize_job would.
def execute(db, job: Download, plan: organizer.OrganizePlan, source: Path) -> bool:
    with db() as session:
        row = session.get(Download, job.id)
        return organizer.execute_plan(session, row, plan, source, {}, dry_run=False)


# The job's row as stored now.
def stored(db, job: Download) -> Download:
    with db() as session:
        return session.get(Download, job.id)


# A streamed requests.Response serving `body`, an image/jpeg unless `headers` says otherwise.
def image_response(body: bytes = b'jpeg bytes', status: int = 200, headers: dict | None = None) -> requests.Response:
    response = requests.Response()
    response.status_code = status
    response.headers.update({'Content-Type': 'image/jpeg', **(headers or {})})
    response.raw = io.BytesIO(body)
    response.url = THUMB_URL
    return response


# Answer every requests.get with `response`. Returns the list each call's keyword arguments land in.
def serve(monkeypatch: pytest.MonkeyPatch, response: requests.Response) -> list[dict]:
    calls = []

    def fake_get(url, **kwargs):
        calls.append(kwargs)
        return response

    monkeypatch.setattr(organizer.requests, 'get', fake_get)
    return calls


# =======================================================
#                       SAFE_MOVE
# =======================================================

# Across disks the source is copied to a hidden temp, fsynced, renamed into place with its mtime,
# and only then unlinked: at the rename the source still exists and the temp is already complete.
def test_safe_move_cross_device_copy_then_replace(monkeypatch, move_paths, cross_device):
    src, dest = move_paths
    os.utime(src, (1_600_000_000, 1_600_000_000))
    temp = organizer.partial_path(dest)
    real_replace, real_fsync = os.replace, os.fsync
    events = []

    # Record the rename with what the source and temp hold at that moment, then carry it out.
    def watching_replace(old, new):
        events.append(('replace', Path(old), Path(new), src.exists(), Path(old).read_bytes()))
        real_replace(old, new)

    # Record the fsync, then carry it out.
    def watching_fsync(fd):
        events.append(('fsync',))
        real_fsync(fd)

    monkeypatch.setattr(organizer.os, 'replace', watching_replace)
    monkeypatch.setattr(organizer.os, 'fsync', watching_fsync)

    organizer.safe_move(src, dest)

    assert temp.name == '.video.mp4.partial'
    assert events[0] == ('fsync',)
    assert events[1] == ('replace', temp, dest, True, b'complete video')
    assert not src.exists() and not temp.exists()
    assert dest.read_bytes() == b'complete video'
    assert dest.stat().st_mtime == 1_600_000_000


# A copy that fails part-way, here on a full library disk, re-raises with the source intact, the
# destination's previous content untouched and no temp left behind.
def test_safe_move_failure_leaves_source_and_no_partial(monkeypatch, move_paths, cross_device):
    src, dest = move_paths
    dest.write_bytes(b'previous')

    # Write the first bytes of the copy, then fail the way a full disk does.
    def full_disk(source, target):
        Path(target).write_bytes(b'compl')
        raise OSError(errno.ENOSPC, 'No space left on device')

    monkeypatch.setattr(organizer.shutil, 'copyfile', full_disk)

    with pytest.raises(OSError, match='No space left'):
        organizer.safe_move(src, dest)

    assert src.read_bytes() == b'complete video'
    assert dest.read_bytes() == b'previous'
    assert not organizer.partial_path(dest).exists()


# Two bind mounts of one filesystem share st_dev but refuse a rename between them with EXDEV;
# safe_move then copies, as it does across disks.
def test_safe_move_falls_back_to_copy_on_exdev(monkeypatch, move_paths):
    src, dest = move_paths
    monkeypatch.setattr(organizer, 'same_filesystem', lambda source, target: True)
    real_replace = os.replace

    # Refuse the direct rename of the source; let the temp's rename through.
    def cross_mount_replace(old, new):
        if Path(old) == src:
            raise OSError(errno.EXDEV, 'Invalid cross-device link')
        real_replace(old, new)

    monkeypatch.setattr(organizer.os, 'replace', cross_mount_replace)

    organizer.safe_move(src, dest)

    assert dest.read_bytes() == b'complete video'
    assert not src.exists() and not organizer.partial_path(dest).exists()


# =======================================================
#                 EXISTING DESTINATIONS
# =======================================================

# A destination of a different size that no other row claims is most likely a truncated copy: the
# source replaces it (through the cross-device copy) with a WARN, and the job is filed there.
def test_unclaimed_truncated_destination_is_replaced(caplog, db, make_job, library, cross_device):
    caplog.set_level(logging.INFO)
    plan, source, job = filed_twice(library, make_job, b'complete video', b'compl')

    assert execute(db, job, plan, source) is True

    assert plan.dest_video.read_bytes() == b'complete video'
    assert not source.exists()
    assert plan.dest_nfo.exists()
    row = stored(db, job)
    assert row.filename == str(plan.dest_video) and row.organized_at is not None
    assert any(str(plan.dest_video) in m and 'replacing it' in m for m in messages_at(caplog, logging.WARNING))
    assert messages_at(caplog, logging.ERROR) == []


# A destination of the same size is the video already filed: the source is discarded, the
# destination left exactly as it was, and nothing is logged above INFO.
def test_identical_destination_discards_source(caplog, db, make_job, library):
    caplog.set_level(logging.INFO)
    plan, source, job = filed_twice(library, make_job, b'complete video', b'already filed!')

    assert execute(db, job, plan, source) is True

    assert plan.dest_video.read_bytes() == b'already filed!'
    assert not source.exists()
    assert stored(db, job).filename == str(plan.dest_video)
    assert messages_at(caplog, logging.WARNING) == []
    assert messages_at(caplog, logging.ERROR) == []


# A destination of a different size that another live row points at is never replaced: both files
# stay, the job is filed at the destination, and one WARN names both paths.
def test_claimed_destination_with_size_mismatch_keeps_both(caplog, db, make_job, library, cross_device):
    caplog.set_level(logging.INFO)
    plan, source, job = filed_twice(library, make_job, b'complete video', b'other copy')
    make_job(status='completed', youtube_id=VIDEO_ID, filename=str(plan.dest_video))

    assert execute(db, job, plan, source) is True

    assert plan.dest_video.read_bytes() == b'other copy'
    assert source.read_bytes() == b'complete video'
    row = stored(db, job)
    assert row.filename == str(plan.dest_video) and row.organized_at is not None
    [warning] = [m for m in messages_at(caplog, logging.WARNING) if 'keeping both' in m]
    assert str(plan.dest_video) in warning and str(source) in warning
    assert messages_at(caplog, logging.ERROR) == []


# '.partial' temps an interrupted run left beside the destination go when the plan is carried out.
def test_stale_partial_temp_removed(db, make_job, library):
    plan, source, job = filed_twice(library, make_job, b'complete video', b'already filed!')
    stale = [organizer.partial_path(plan.dest_video), organizer.partial_path(plan.dest_nfo)]
    for path in stale:
        path.write_bytes(b'half')

    assert execute(db, job, plan, source) is True

    assert not any(path.exists() for path in stale)
    assert plan.dest_nfo.exists()


# With no session to ask, the claim check answers "claimed" and logs ERROR, so a delete keeps the
# file; given a session explicitly, the same detached job is checked properly.
def test_file_claim_check_fails_closed_without_session(caplog, db, make_job, library):
    caplog.set_level(logging.INFO)
    video = library.media / f'Title [{VIDEO_ID}]' / f'Title [{VIDEO_ID}].mp4'
    video.parent.mkdir()
    video.write_bytes(b'video')
    job = make_job(status='completed', youtube_id=VIDEO_ID, filename=str(video))

    assert organizer.file_claimed_by_another_row(job, video) is True
    organizer.remove_downloaded_files(job)
    assert video.exists()
    assert any('no database session' in m for m in messages_at(caplog, logging.ERROR))

    with db() as session:
        assert organizer.file_claimed_by_another_row(job, video, session) is False


# The web UI's delete still removes an unclaimed file and its folder: the route's rows belong to
# its session, so the claim check runs rather than failing closed.
def test_app_delete_removes_unclaimed_file(client, db, make_job, library):
    folder = library.media / f'Title [{VIDEO_ID}]'
    video = folder / f'Title [{VIDEO_ID}].mp4'
    folder.mkdir()
    video.write_bytes(b'video')
    (folder / f'Title [{VIDEO_ID}].nfo').write_text('<movie/>', encoding='utf-8')
    job = make_job(status='completed', youtube_id=VIDEO_ID, filename=str(video))

    response = client.post(f'/job/{job.id}/delete', follow_redirects=False)

    assert response.status_code == 303
    assert not video.exists() and not folder.exists()
    assert stored(db, job).status == 'deleted'


# The same delete keeps a file another live row still points at.
def test_app_delete_keeps_file_claimed_by_another_row(client, db, make_job, library):
    video = library.media / f'Title [{VIDEO_ID}]' / f'Title [{VIDEO_ID}].mp4'
    video.parent.mkdir()
    video.write_bytes(b'video')
    job = make_job(status='completed', youtube_id=VIDEO_ID, filename=str(video))
    make_job(status='completed', youtube_id=VIDEO_ID, filename=str(video))

    response = client.post(f'/job/{job.id}/delete', follow_redirects=False)

    assert response.status_code == 303
    assert video.exists()
    assert stored(db, job).status == 'deleted'


# =======================================================
#                     ATOMIC WRITES
# =======================================================

# An NFO write interrupted part-way leaves the previous NFO as it was and no temp; a completed one
# replaces it with a parseable document.
def test_write_nfo_is_atomic(monkeypatch, tmp_path):
    nfo = tmp_path / 'Title.nfo'
    nfo.write_text('<movie><title>Old</title></movie>', encoding='utf-8')
    real_write = organizer.ET.ElementTree.write

    # Write the start of the document, then fail the way a full disk does.
    def interrupted(self, file, *args, **kwargs):
        file.write(b'<?xml version=')
        raise OSError(errno.ENOSPC, 'No space left on device')

    monkeypatch.setattr(organizer.ET.ElementTree, 'write', interrupted)
    with pytest.raises(OSError):
        organizer.write_nfo(nfo, 'movie', {'title': 'New'})

    assert nfo.read_text(encoding='utf-8') == '<movie><title>Old</title></movie>'
    assert not organizer.partial_path(nfo).exists()

    monkeypatch.setattr(organizer.ET.ElementTree, 'write', real_write)
    organizer.write_nfo(nfo, 'movie', {'title': 'New'})

    assert organizer.ET.parse(nfo).getroot().findtext('title') == 'New'
    assert not organizer.partial_path(nfo).exists()


# Copied artwork keeps the source's mtime, as shutil.copy2 did, and leaves no temp behind.
def test_copy_image_keeps_mtime_and_leaves_no_partial(tmp_path):
    src = tmp_path / 'thumb.jpg'
    src.write_bytes(b'jpeg bytes')
    os.utime(src, (1_600_000_000, 1_600_000_000))
    dest = tmp_path / 'show' / 'poster.jpg'

    assert organizer.copy_image(src, dest, 'poster') is True

    assert dest.read_bytes() == b'jpeg bytes'
    assert dest.stat().st_mtime == 1_600_000_000
    assert not organizer.partial_path(dest).exists()


# =======================================================
#                   LIBRARY SENTINEL
# =======================================================

# A missing sentinel stops both entry points before they create anything under the library: no
# mkdir, the job stays completed and unorganized in staging, and one ERROR covers both calls.
def test_missing_library_sentinel_blocks_organize_before_mkdir(caplog, monkeypatch, tmp_path, db, make_job, library):
    caplog.set_level(logging.INFO)
    source = library.staging / f'Title [{VIDEO_ID}].mp4'
    source.write_bytes(b'video')
    job = make_job(status='completed', title='Title', youtube_id=VIDEO_ID, filename=str(source))
    library.media.rmdir()
    monkeypatch.setenv('LIBRARY_SENTINEL', str(tmp_path / '.library-mounted'))

    assert organizer.scan_and_process() == 0
    assert organizer.organize_single_job(job.id) is False

    assert not library.media.exists()
    assert source.exists()
    row = stored(db, job)
    assert row.status == 'completed' and row.organized_at is None and row.filename == str(source)
    errors = messages_at(caplog, logging.ERROR)
    assert len(errors) == 1 and '.library-mounted' in errors[0]


# The refusal repeats at most once an hour; the sentinel's return logs one INFO line, and a new
# outage after it reports at once.
def test_sentinel_error_rate_limited(caplog, monkeypatch, tmp_path, library):
    caplog.set_level(logging.INFO)
    sentinel = tmp_path / '.library-mounted'
    monkeypatch.setenv('LIBRARY_SENTINEL', str(sentinel))

    for _ in range(3):
        assert organizer.scan_and_process() == 0
    assert len(messages_at(caplog, logging.ERROR)) == 1

    an_hour_ago = time.monotonic() - organizer.LIBRARY_ERROR_INTERVAL_SECONDS
    monkeypatch.setattr(organizer, '_library_error_logged_at', an_hour_ago)
    assert organizer.scan_and_process() == 0
    assert len(messages_at(caplog, logging.ERROR)) == 2

    sentinel.touch()
    assert organizer.library_ready() is True
    assert organizer.library_ready() is True
    assert len([m for m in messages_at(caplog, logging.INFO) if 'is back' in m]) == 1

    sentinel.unlink()
    assert organizer.library_ready() is False
    assert len(messages_at(caplog, logging.ERROR)) == 3


# With the sentinel present, or the variable unset, a pass files the video as before and leaves no
# '.partial' temp anywhere in the library.
@pytest.mark.parametrize('sentinel_state', ['present', 'unset'])
def test_present_sentinel_allows_organize(monkeypatch, db, make_job, library, sentinel_state):
    if sentinel_state == 'present':
        sentinel = library.media / '.library-mounted'
        sentinel.touch()
        monkeypatch.setenv('LIBRARY_SENTINEL', str(sentinel))
    source = library.staging / f'Title [{VIDEO_ID}].mp4'
    source.write_bytes(b'video')
    make_job(status='completed', title='Title', youtube_id=VIDEO_ID, filename=str(source))
    plan = movie_plan(library.media)

    assert organizer.scan_and_process() == 1

    assert plan.dest_video.read_bytes() == b'video'
    assert plan.dest_nfo.exists()
    assert not source.exists()
    assert not any(path.name.endswith('.partial') for path in library.media.rglob('*'))


# =======================================================
#                   ARTWORK DOWNLOADS
# =======================================================

# The hosts YouTube serves video thumbnails (i.ytimg.com, i9.ytimg.com) and channel avatars
# (yt3.ggpht.com, yt3.googleusercontent.com) from are fetched, streamed with redirects off.
@pytest.mark.parametrize('url', [
    THUMB_URL,
    f'https://i9.ytimg.com/vi/{VIDEO_ID}/mqdefault.jpg',
    'https://yt3.ggpht.com/ytc/avatar=s900-c-k-c0x00ffffff-no-rj',
    'https://yt3.googleusercontent.com/avatar=s0',
    f'https://I.YTIMG.COM:443/vi/{VIDEO_ID}/hqdefault.jpg',
])
def test_download_image_accepts_youtube_image_hosts(monkeypatch, tmp_path, url):
    calls = serve(monkeypatch, image_response())
    dest = tmp_path / 'show' / 'poster.jpg'

    assert organizer.download_image(url, dest, 'poster') is True

    assert dest.read_bytes() == b'jpeg bytes'
    assert calls == [{'timeout': 15, 'stream': True, 'allow_redirects': False}]
    assert not organizer.partial_path(dest).exists()


# Anything but https on a YouTube image host, with no userinfo and the default port, is refused at
# WARN before any request is made.
@pytest.mark.parametrize('url', [
    'https://evil-ytimg.com/vi/x/hqdefault.jpg',
    'https://i.ytimg.com.evil.example/vi/x/hqdefault.jpg',
    'https://example.com/poster.jpg',
    f'http://i.ytimg.com/vi/{VIDEO_ID}/hqdefault.jpg',
    f'https://user@i.ytimg.com/vi/{VIDEO_ID}/hqdefault.jpg',
    f'https://i.ytimg.com:8443/vi/{VIDEO_ID}/hqdefault.jpg',
    'https://i.ytimg.com:notaport/x.jpg',
])
def test_download_image_rejects_foreign_host(caplog, monkeypatch, tmp_path, url):
    caplog.set_level(logging.INFO)
    calls = serve(monkeypatch, image_response())
    dest = tmp_path / 'poster.jpg'

    assert organizer.download_image(url, dest, 'poster') is False

    assert calls == [] and not dest.exists()
    assert any('Refusing to download poster' in m for m in messages_at(caplog, logging.WARNING))
    assert messages_at(caplog, logging.ERROR) == []


# A redirect is a failure, not something to follow: nothing is written and the response is closed.
def test_download_image_rejects_redirect(caplog, monkeypatch, tmp_path):
    caplog.set_level(logging.INFO)
    response = image_response(b'', status=302, headers={'Location': 'https://example.com/x.jpg'})
    serve(monkeypatch, response)
    dest = tmp_path / 'poster.jpg'

    assert organizer.download_image(THUMB_URL, dest, 'poster') is False

    assert not dest.exists() and not organizer.partial_path(dest).exists()
    assert response.raw.closed
    assert any('HTTP 302' in m for m in messages_at(caplog, logging.WARNING))
    assert messages_at(caplog, logging.ERROR) == []


# A body over the cap is refused, whether Content-Length announces it or the stream only runs past
# it, and leaves neither the image nor a temp.
@pytest.mark.parametrize('declared', [True, False], ids=['content-length', 'streamed'])
def test_download_image_caps_size(caplog, monkeypatch, tmp_path, declared):
    caplog.set_level(logging.INFO)
    monkeypatch.setattr(organizer, 'MAX_IMAGE_BYTES', 1024)
    body = b'x' * 4096
    response = image_response(body, headers={'Content-Length': str(len(body))} if declared else None)
    serve(monkeypatch, response)
    dest = tmp_path / 'poster.jpg'

    assert organizer.download_image(THUMB_URL, dest, 'poster') is False

    assert not dest.exists() and not organizer.partial_path(dest).exists()
    assert response.raw.closed
    assert any('exceeds 1024 bytes' in m for m in messages_at(caplog, logging.WARNING))
    assert messages_at(caplog, logging.ERROR) == []


# A body that is not image/* (an HTML error page, say) is refused and nothing is written.
def test_download_image_requires_image_content_type(caplog, monkeypatch, tmp_path):
    caplog.set_level(logging.INFO)
    serve(monkeypatch, image_response(b'<html></html>', headers={'Content-Type': 'text/html; charset=utf-8'}))
    dest = tmp_path / 'poster.jpg'

    assert organizer.download_image(THUMB_URL, dest, 'poster') is False

    assert not dest.exists()
    assert any('is not an image' in m for m in messages_at(caplog, logging.WARNING))
    assert messages_at(caplog, logging.ERROR) == []
