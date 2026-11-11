# -------------------------------------------------------
# Job state machine tests
#
# Pins the job-state transitions the web app and the worker share: how a parent settles once its
# children stop, cancel against auto-retry, explicit requests queuing inert rows again, retry
# routing, and refusing a redownload while a job runs or a worker write to a deleted row. Nothing
# runs yt-dlp.
# -------------------------------------------------------

import logging
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest
from sqlalchemy import event
from sqlalchemy.engine import Engine

from youtube_manager import organizer, worker
from youtube_manager.database import Download, Playlist
from youtube_manager.jobstate import SKIPPED_STATUS, compute_parent_speed

PLAYLIST_YOUTUBE_ID = 'PLstatemachinetest'
PLAYLIST_URL = f'https://www.youtube.com/playlist?list={PLAYLIST_YOUTUBE_ID}'
CHANNEL_URL = 'https://www.youtube.com/@example'
V1, V2, V3, V4, V5, V6 = (f'aaaaaaaaaa{n}' for n in range(1, 7))

# When the rows below last changed, long before any test runs.
EARLIER = datetime(2026, 1, 1, tzinfo=timezone.utc)


# Every test starts with no refused worker writes and no auto-retry backoff on record, so neither
# leaks between tests. raising=False: a module without the refusal record gains one for the test.
@pytest.fixture(autouse=True)
def fresh_worker_state(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(worker, '_refused_write_job_ids', set(), raising=False)
    monkeypatch.setattr(worker, '_retry_backoff', {})


# The playlist the numbered rows below belong to, and its id.
@pytest.fixture
def playlist_id(db) -> int:
    with db() as session:
        playlist = Playlist(youtube_id=PLAYLIST_YOUTUBE_ID, title='Test playlist')
        session.add(playlist)
        session.commit()
        return playlist.id


# The stored row for `job_id`, read in a fresh session.
def stored(db, job_id: int) -> Download:
    with db() as session:
        return session.get(Download, job_id)


# The stored status of each row in `rows`, in order.
def statuses(db, *rows: Download) -> list[str]:
    return [stored(db, row.id).status for row in rows]


# Messages logged at exactly `level`, in the order they were logged.
def messages_at(caplog: pytest.LogCaptureFixture, level: int) -> list[str]:
    return [record.getMessage() for record in caplog.records if record.levelno == level]


# Insert a video row for `youtube_id`, its URL built from the id as a scan builds it.
def video(make_job, youtube_id: str, **fields) -> Download:
    return make_job(url=f'https://www.youtube.com/watch?v={youtube_id}', youtube_id=youtube_id,
                    **fields)


# Insert a playlist download of PLAYLIST_URL in `status`, the parent its children hang off.
def playlist_parent(make_job, status: str = 'queued', **fields) -> Download:
    return make_job(url=PLAYLIST_URL, request_type='playlist', status=status, **fields)


# Create `path`, folders included, as a small media file.
def write_media(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b'video')
    return path


# Run `request` while the worker takes `job_id` up: the row moves to 'metadata', from another
# connection, just before the request's first UPDATE of the downloads table runs. Returns what
# `request` returns, and fails the test if the request runs no UPDATE of that table.
def with_worker_taking_up(empty_db: Path, job_id: int, request):
    taken = []

    # Moves the row on the request's first UPDATE only.
    def take_up(conn, cursor, statement, parameters, context, executemany):
        if not statement.startswith('UPDATE downloads') or taken:
            return
        taken.append(statement)
        connection = sqlite3.connect(str(empty_db), isolation_level=None)
        try:
            take = "UPDATE downloads SET status = 'metadata' WHERE id = ?"
            connection.execute(take, (job_id,))
        finally:
            connection.close()

    event.listen(Engine, 'before_cursor_execute', take_up)
    try:
        response = request()
    finally:
        event.remove(Engine, 'before_cursor_execute', take_up)
    assert taken
    return response


# A playlist download adopts the rows a subscription only recorded as skipped and queues them, so
# the parent moves on instead of sitting in 'expanding', and the worker takes them up. Each keeps
# the episode number its scan position gives it.
def test_explicit_request_requeues_adopted_skipped_rows(db, make_job, playlist_id):
    skipped = [
        video(make_job, youtube_id, playlist_id=playlist_id, status=SKIPPED_STATUS)
        for youtube_id in (V1, V2)
    ]
    parent = playlist_parent(make_job, 'expanding', playlist_id=playlist_id)

    worker.create_child_jobs(parent, [V1, V2])

    rows = [stored(db, row.id) for row in skipped]
    assert [(row.parent_id, row.status, row.playlist_index) for row in rows] == [
        (parent.id, 'queued', 1), (parent.id, 'queued', 2),
    ]
    assert stored(db, parent.id).status == 'queued'
    assert worker.get_next_job().id == skipped[0].id


# A channel download adopting a row deleted or cancelled earlier queues it again, the earlier
# attempt's error, cancel flag, progress, speed and completion time cleared, and the parent shows
# queued rather than taking their old verdict.
def test_adopted_deleted_and_cancelled_rows_requeued(db, make_job):
    deleted = video(make_job, V1, status='deleted', completed_at=EARLIER)
    cancelled = video(make_job, V2, status='cancelled', error='Cancelled by user',
                      cancel_requested=True, progress='40.0%', speed='1.0MiB/s')
    parent = make_job(url=CHANNEL_URL, request_type='channel', status='expanding')

    worker.create_child_jobs(parent, [V1, V2])

    for row in (stored(db, deleted.id), stored(db, cancelled.id)):
        assert (row.parent_id, row.status, row.error, row.cancel_requested) == (
            parent.id, 'queued', None, False,
        )
        assert (row.progress, row.speed, row.completed_at) == (None, None, None)
    assert stored(db, parent.id).status == 'queued'


# Pasting the URL of a video a subscription only recorded as skipped queues that row instead of
# reporting a duplicate, so the video is downloaded; no second row is made. A deleted one is
# queued again too, as the retry it is.
def test_add_url_of_skipped_video_requeues(client, db, make_job, library):
    skipped = video(make_job, V1, status=SKIPPED_STATUS)

    page = client.post('/add', data={'url': f'https://youtu.be/{V1}', 'request_type': 'video'})

    assert stored(db, skipped.id).status == 'queued'
    with db() as session:
        assert session.query(Download).count() == 1
    assert 'Video queued.' in page.text

    deleted = video(make_job, V2, status='deleted')

    page = client.post('/add', data={'url': f'https://youtu.be/{V2}', 'request_type': 'video'})

    assert stored(db, deleted.id).status == 'queued'
    assert 'Retry queued.' in page.text


# A child's terminal status settles its parent wherever the worker writes it: a failure written
# through update_job outranks a finished sibling, and refusing a child whose cancel arrived before
# it ran settles the parent too.
def test_terminal_child_update_settles_parent(db, make_job):
    parent = playlist_parent(make_job)
    video(make_job, V1, parent_id=parent.id, status='completed')
    running = video(make_job, V2, parent_id=parent.id, status='downloading')

    worker.update_job(running.id, status='failed', error='HTTP Error 403: Forbidden')

    row = stored(db, parent.id)
    assert (row.status, row.completed_at is not None) == ('failed', True)

    other = playlist_parent(make_job)
    video(make_job, V3, parent_id=other.id, status='completed')
    refused = video(make_job, V4, parent_id=other.id, status='redownload', cancel_requested=True)

    worker.process_job(refused)

    assert statuses(db, refused, other) == ['cancelled', 'completed']


# Cancelling a playlist's last queued child in the web UI settles the playlist in the same request:
# its finished sibling makes it completed. While another child is still in flight the parent is
# left to the worker, which settles it when that child stops.
def test_app_single_child_cancel_settles_parent(client, db, make_job):
    parent = playlist_parent(make_job)
    video(make_job, V1, parent_id=parent.id, status='completed')
    last = video(make_job, V2, parent_id=parent.id, status='queued')

    client.post(f'/cancel/{last.id}')

    row = stored(db, parent.id)
    assert (stored(db, last.id).status, row.status) == ('cancelled', 'completed')
    assert row.completed_at is not None

    busy = playlist_parent(make_job)
    video(make_job, V3, parent_id=busy.id, status='downloading')
    queued = video(make_job, V4, parent_id=busy.id, status='queued')

    client.post(f'/cancel/{queued.id}')

    assert statuses(db, queued, busy) == ['cancelled', 'queued']


# Deleting a failed child's files in the web UI settles its playlist in the same request: with the
# failure gone, the finished sibling makes it completed.
def test_app_single_child_delete_settles_parent(client, db, make_job, library):
    parent = playlist_parent(make_job, 'failed')
    video(make_job, V1, parent_id=parent.id, status='completed')
    failed = video(make_job, V2, parent_id=parent.id, status='failed')

    client.post(f'/job/{failed.id}/delete')

    assert statuses(db, failed, parent) == ['deleted', 'completed']


# The organizer holds a child back by its parent's effective status, which the children decide: a
# parent stored as still queued whose children have all finished is filed, and one stored as
# completed whose other child was queued again waits for that child.
def test_organizer_gate_uses_effective_parent_status(monkeypatch, db, make_job, library):
    filed = []

    # Stands in for filing a video: records the job and reports it organized.
    def organize(session, job, dry_run, force_nfo=False) -> bool:
        filed.append(job.id)
        return True

    monkeypatch.delenv('LIBRARY_SENTINEL', raising=False)
    monkeypatch.setattr(organizer, 'is_organizable', lambda session, job, dry_run=False: True)
    monkeypatch.setattr(organizer, 'organize_job', organize)
    monkeypatch.setattr(organizer, 'notify_jellyfin', lambda: None)
    stale = playlist_parent(make_job, 'queued')
    finished = video(make_job, V1, parent_id=stale.id, status='completed')
    reopened = playlist_parent(make_job, 'completed')
    video(make_job, V2, parent_id=reopened.id, status='completed')
    video(make_job, V3, parent_id=reopened.id, status='queued')

    organizer.scan_and_process(dry_run=False)

    assert filed == [finished.id]


# Auto-retry leaves alone the failed children of a playlist the user cancelled, whether the cancel
# has settled or is still pending, and retries those of a playlist that simply failed.
def test_auto_retry_skips_cancelled_parent(db, make_job, library):
    cancelled = playlist_parent(make_job, 'cancelled')
    left = video(make_job, V1, parent_id=cancelled.id, status='failed')
    pending = playlist_parent(make_job, 'queued', cancel_requested=True)
    pending_left = video(make_job, V2, parent_id=pending.id, status='failed')
    failed = playlist_parent(make_job, 'failed')
    retried = video(make_job, V3, parent_id=failed.id, status='failed')

    worker.retry_failed_jobs()

    assert statuses(db, left, cancelled, pending_left, pending) == [
        'failed', 'cancelled', 'failed', 'queued',
    ]
    assert statuses(db, retried, failed) == ['queued', 'queued']


# A cancelled playlist stays cancelled when a child finishes late, whether the cancel had settled
# already or was still pending while that child downloaded; a deleted playlist stays deleted.
def test_settle_keeps_stored_cancelled(db, make_job):
    settled = playlist_parent(make_job, 'cancelled')
    late = video(make_job, V1, parent_id=settled.id, status='completed')
    video(make_job, V2, parent_id=settled.id, status='cancelled')

    assert worker.finalize_parent_if_done(late.id) is True
    assert stored(db, settled.id).status == 'cancelled'

    pending = playlist_parent(make_job, 'downloading', cancel_requested=True)
    running = video(make_job, V3, parent_id=pending.id, status='downloading', cancel_requested=True)
    video(make_job, V4, parent_id=pending.id, status='cancelled', cancel_requested=True)

    worker.update_job(running.id, status='completed', progress='100%')

    row = stored(db, pending.id)
    assert (row.status, row.cancel_requested) == ('cancelled', False)

    deleted = playlist_parent(make_job, 'deleted')
    fetched_again = video(make_job, V5, parent_id=deleted.id, status='completed')

    assert worker.finalize_parent_if_done(fetched_again.id) is True
    assert stored(db, deleted.id).status == 'deleted'


# Retry All sends a failed or cancelled video whose recorded file is still on disk, in staging or
# in the library, to 'redownload', which reuses the file; one without a file is queued, and a
# playlist shows queued while its failed child reruns.
def test_retry_all_routes_on_disk_files_to_redownload(client, db, make_job, library):
    staged = write_media(library.staging / f'Video [{V1}].mp4')
    filed = write_media(library.media / f'Video [{V2}]' / f'Video [{V2}].mp4')
    in_staging = video(make_job, V1, status='failed', filename=str(staged))
    in_library = video(make_job, V2, status='cancelled', filename=str(filed), organized_at=EARLIER)
    missing = video(make_job, V3, status='failed')
    parent = playlist_parent(make_job, 'completed')
    child = video(make_job, V4, parent_id=parent.id, status='failed')

    client.post('/retry-all')

    assert statuses(db, in_staging, in_library, missing, child, parent) == [
        'redownload', 'redownload', 'queued', 'queued', 'queued',
    ]


# Retry All looks for each file only where its row says it is: a file moved since, which a walk of
# the library would find by its id, does not count, and no directory is walked in the request.
def test_retry_all_does_not_walk_library(monkeypatch, client, db, make_job, library):
    present = write_media(library.media / 'Show' / f'Video [{V1}].mp4')
    write_media(library.media / 'Elsewhere' / f'Video [{V2}].mp4')
    recorded = video(make_job, V1, status='failed', filename=str(present))
    moved = video(make_job, V2, status='failed',
                  filename=str(library.media / 'Show' / f'Video [{V2}].mkv'))
    walks = []
    real_rglob = Path.rglob

    # Records each tree walk before running it.
    def rglob(self, pattern, *args, **kwargs):
        walks.append((self, pattern))
        return real_rglob(self, pattern, *args, **kwargs)

    monkeypatch.setattr(Path, 'rglob', rglob)

    client.post('/retry-all')

    assert statuses(db, recorded, moved) == ['redownload', 'queued']
    assert walks == []


# The admin sweep settles each parent it reaches by the shared rule: a failed child whose file is
# still missing outranks a completed one, while a parent whose failed child turns out to be on
# disk is completed.
def test_sweep_parent_uses_shared_rule(client, db, make_job, library):
    failing = playlist_parent(make_job, 'failed')
    video(make_job, V1, parent_id=failing.id, status='completed')
    video(make_job, V2, parent_id=failing.id, status='failed')
    recovered = playlist_parent(make_job, 'failed')
    video(make_job, V3, parent_id=recovered.id, status='completed')
    on_disk = video(make_job, V4, parent_id=recovered.id, status='failed')
    write_media(library.staging / f'Video [{V4}].mp4')

    client.post('/admin/sweep_existing')

    assert statuses(db, failing, on_disk, recovered) == ['failed', 'completed', 'completed']


# Fetch Metadata is refused while a job is in progress, a video downloading or a playlist with a
# child in flight, and a flash says so; the bulk action resets the rest of a selection. Neither the
# job page nor the dashboard's bulk button offers it on a job in progress.
def test_redownload_refused_while_active(client, db, make_job, library):
    downloading = video(make_job, V1, status='downloading', progress='50.0%')

    page = client.post(f'/job/{downloading.id}/redownload')

    row = stored(db, downloading.id)
    assert (row.status, row.progress) == ('downloading', '50.0%')
    assert 'Still in progress' in page.text
    assert f'action="/job/{downloading.id}/redownload"' not in page.text

    parent = playlist_parent(make_job)
    running_child = video(make_job, V2, parent_id=parent.id, status='downloading')
    done = video(make_job, V3, status='completed',
                 filename=str(write_media(library.staging / f'Video [{V3}].mp4')))

    page = client.post('/bulk/redownload', data={'job_ids': [parent.id, done.id], 'return_to': '/'})

    assert statuses(db, parent, running_child, done) == ['queued', 'downloading', 'redownload']
    assert '1 still in progress were left alone' in page.text
    redownloadable = dict(re.findall(
        r'name="job_ids" value="(\d+)"[^>]*data-redownloadable="(\d)"', client.get('/').text,
    ))
    assert redownloadable[str(downloading.id)] == '0'
    job_page = client.get(f'/job/{parent.id}').text
    assert f'action="/job/{running_child.id}/redownload"' not in job_page


# A job the worker takes up between the request reading it and resetting it is left as the worker
# set it: the reset is one conditional update, and the flash says the job is in progress.
def test_redownload_not_reset_once_worker_takes_job_up(client, empty_db, db, make_job):
    failed = video(make_job, V1, status='failed', error='HTTP Error 403: Forbidden')

    page = with_worker_taking_up(
        empty_db, failed.id, lambda: client.post(f'/job/{failed.id}/redownload'),
    )

    row = stored(db, failed.id)
    assert (row.status, row.error) == ('metadata', 'HTTP Error 403: Forbidden')
    assert 'Redownload requested by user' not in (row.log or '')
    assert 'Still in progress' in page.text


# The worker never writes over a row the user deleted, its status and progress included, and
# says so in one WARN per job however many writes follow; a row gone from the table is a no-op.
def test_worker_status_write_refuses_deleted_row(caplog, db, make_job):
    caplog.set_level(logging.INFO)
    deleted = video(make_job, V1, status='deleted')

    worker.update_job(deleted.id, status='downloading', progress='10.0%')
    worker.update_job(deleted.id, status='completed', progress='100%', filename=f'Video [{V1}].mp4')

    row = stored(db, deleted.id)
    assert (row.status, row.progress, row.filename) == ('deleted', None, None)
    warnings = messages_at(caplog, logging.WARNING)
    assert len([message for message in warnings if f'Job {deleted.id}' in message]) == 1

    caplog.clear()
    worker.update_job(deleted.id + 1000, status='failed')

    assert messages_at(caplog, logging.WARNING) == messages_at(caplog, logging.ERROR) == []


# A write the worker makes still moves updated_at, which the pages poll for changes, while a write
# refused on a deleted row leaves that row, updated_at included, as it was.
def test_update_job_still_moves_updated_at(db, make_job):
    running = video(make_job, V1, status='downloading', updated_at=EARLIER)
    deleted = video(make_job, V2, status='deleted', updated_at=EARLIER)

    worker.update_job(running.id, progress='50.0%')
    worker.update_job(deleted.id, progress='50.0%')

    earlier = EARLIER.replace(tzinfo=None)
    moved = stored(db, running.id)
    assert (moved.progress, moved.updated_at > earlier) == ('50.0%', True)
    untouched = stored(db, deleted.id)
    assert (untouched.progress, untouched.updated_at) == (None, earlier)


# A parent left in flight with nothing but skipped children, adopted unchanged, fails with the
# way to fetch them instead of staying in 'expanding' for good, and Fetch Metadata on it then
# queues every child. A parent already settled keeps its status.
def test_all_skipped_children_settle_parent_failed(client, db, make_job):
    stuck = playlist_parent(make_job, 'expanding')
    children = [
        video(make_job, youtube_id, parent_id=stuck.id, status=SKIPPED_STATUS)
        for youtube_id in (V1, V2)
    ]

    worker.create_child_jobs(stuck, [V1, V2])

    row = stored(db, stuck.id)
    assert (row.status, row.completed_at is not None) == ('failed', True)
    assert 'skipped' in row.error and 'Redownload' in row.error

    client.post(f'/job/{stuck.id}/redownload')

    assert statuses(db, stuck, *children) == ['redownload', 'queued', 'queued']

    settled = playlist_parent(make_job, 'completed')
    skipped = video(make_job, V3, parent_id=settled.id, status=SKIPPED_STATUS)

    worker.finalize_parent_if_done(skipped.id)

    assert stored(db, settled.id).status == 'completed'


# Retrying a playlist whose cancel request was still pending runs it again: the request is
# dropped with the requeue, so the rerun settles by its children rather than as cancelled.
def test_retry_clears_pending_parent_cancel(client, db, make_job, library):
    parent = playlist_parent(make_job, 'completed', cancel_requested=True)
    video(make_job, V1, parent_id=parent.id, status='completed')
    failed = video(make_job, V2, parent_id=parent.id, status='failed')

    client.post(f'/retry/{parent.id}')

    row = stored(db, parent.id)
    assert (row.status, row.cancel_requested, stored(db, failed.id).status) == (
        'queued', False, 'queued',
    )


# Insert a completed playlist with one video that has a recorded file and one that failed, send it
# to Fetch Metadata, and return the playlist and both videos.
def refreshed_playlist(client, make_job) -> tuple[Download, Download, Download]:
    parent = playlist_parent(make_job, 'completed')
    on_disk = video(make_job, V1, parent_id=parent.id, status='completed',
                    filename=f'Video [{V1}].mp4')
    missing = video(make_job, V2, parent_id=parent.id, status='failed')
    client.post(f'/job/{parent.id}/redownload')
    return parent, on_disk, missing


# Have the worker's metadata lookup POST `path` while it runs, as a click during the lookup does.
def click_during_lookup(monkeypatch: pytest.MonkeyPatch, client, path: str) -> None:
    # Stands in for the lookup: sends the click, then reports metadata found.
    def discover(job, **kwargs):
        client.post(path)
        return {'title': 'Test playlist'}

    monkeypatch.setattr(worker, 'discover_metadata', discover)
    monkeypatch.setattr(worker, 'apply_metadata', lambda job, metadata: None)


# Cancelling a playlist while the worker refreshes its metadata for Fetch Metadata stands: the
# refresh leaves the rows as the cancel left them, the video it had sent for a refresh is cancelled
# when the worker takes it up, and the playlist settles as cancelled.
def test_cancel_during_parent_refresh_stands(monkeypatch, client, db, make_job):
    parent, on_disk, missing = refreshed_playlist(client, make_job)
    click_during_lookup(monkeypatch, client, f'/cancel/{parent.id}')

    worker.process_job(worker.get_next_job())

    assert statuses(db, parent, on_disk, missing) == ['queued', 'redownload', 'cancelled']
    assert stored(db, parent.id).cancel_requested is True

    worker.process_job(worker.get_next_job())

    row = stored(db, parent.id)
    assert (row.status, row.cancel_requested) == ('cancelled', False)
    assert statuses(db, on_disk, missing) == ['cancelled', 'cancelled']


# Cancelling a playlist during that refresh while none of its videos had started cancels them
# outright and settles the playlist at once: the refresh leaves it cancelled.
def test_cancel_of_idle_playlist_during_refresh_stands(monkeypatch, client, db, make_job):
    parent = playlist_parent(make_job, 'completed')
    first = video(make_job, V1, parent_id=parent.id, status='failed')
    second = video(make_job, V2, parent_id=parent.id, status='failed')
    client.post(f'/job/{parent.id}/redownload')
    click_during_lookup(monkeypatch, client, f'/cancel/{parent.id}')

    worker.process_job(worker.get_next_job())

    assert statuses(db, parent, first, second) == ['cancelled', 'cancelled', 'cancelled']


# Cancelling one video during that refresh stands too: the refresh leaves that video as the
# cancel left it.
def test_child_cancel_during_parent_refresh_stands(monkeypatch, client, db, make_job):
    parent, on_disk, missing = refreshed_playlist(client, make_job)
    click_during_lookup(monkeypatch, client, f'/cancel/{missing.id}')

    worker.process_job(worker.get_next_job())

    assert statuses(db, parent, on_disk, missing) == ['queued', 'redownload', 'cancelled']
    assert stored(db, missing.id).cancel_requested is True


# Auto-retry also leaves alone a video whose cancel was still pending when it failed, inside a
# playlist that simply failed, and retries its failed sibling.
def test_auto_retry_skips_child_with_pending_cancel(db, make_job, library):
    parent = playlist_parent(make_job, 'failed')
    cancelled = video(make_job, V1, parent_id=parent.id, status='failed', cancel_requested=True)
    retried = video(make_job, V2, parent_id=parent.id, status='failed')

    worker.retry_failed_jobs()

    row = stored(db, cancelled.id)
    assert (row.status, row.cancel_requested) == ('failed', True)
    assert statuses(db, retried, parent) == ['queued', 'queued']


# A restart that finds a video with its cancel pending cancels it and settles its playlist, which
# would otherwise show 'cancel requested' for good and hold its finished video back.
def test_restart_settles_parent_of_cancelled_child(db, make_job):
    parent = playlist_parent(make_job, 'queued', cancel_requested=True)
    stuck = video(make_job, V1, parent_id=parent.id, status='metadata', cancel_requested=True)
    video(make_job, V2, parent_id=parent.id, status='completed')

    worker.recover_stuck_jobs()

    row = stored(db, parent.id)
    assert (stored(db, stuck.id).status, row.status, row.cancel_requested) == (
        'cancelled', 'cancelled', False,
    )


# Cancel acts only on what is in progress: a bulk cancel that also selects a failed playlist and a
# failed video leaves both unflagged, so auto-retry still retries them, and of a playlist it does
# cancel, a video that has finished is left unflagged.
def test_cancel_leaves_stopped_jobs_unflagged(client, db, make_job, library):
    running = video(make_job, V1, status='downloading')
    failed_playlist = playlist_parent(make_job, 'failed')
    failed_child = video(make_job, V2, parent_id=failed_playlist.id, status='failed')
    failed_video = video(make_job, V3, status='failed')
    busy_playlist = playlist_parent(make_job, 'downloading')
    finished_child = video(make_job, V4, parent_id=busy_playlist.id, status='completed')
    busy_child = video(make_job, V5, parent_id=busy_playlist.id, status='downloading')
    selected = [running.id, failed_playlist.id, failed_video.id, busy_playlist.id]

    page = client.post('/bulk/cancel', data={'job_ids': selected, 'return_to': '/'})

    rows = (running, failed_playlist, failed_child, failed_video,
            busy_playlist, finished_child, busy_child)
    flags = [stored(db, row.id).cancel_requested for row in rows]
    assert flags == [True, False, False, False, True, False, True]
    assert 'Cancel requested for 2 item(s).' in page.text

    worker.retry_failed_jobs()

    assert statuses(db, failed_child, failed_video) == ['queued', 'queued']


# Cancel all leaves alone a playlist stored as queued whose videos have all finished, which shows
# as completed, rather than turning it cancelled.
def test_cancel_all_leaves_finished_playlist_alone(client, db, make_job):
    lagging = playlist_parent(make_job, 'queued')
    video(make_job, V1, parent_id=lagging.id, status='completed')
    running = video(make_job, V2, status='downloading')

    page = client.post('/cancel-all')

    row = stored(db, lagging.id)
    assert (row.status, row.cancel_requested) == ('queued', False)
    assert stored(db, running.id).cancel_requested is True
    assert 'Cancel requested for 1 active job(s).' in page.text


# A stale cancel flag on a playlist that has finished does not turn it cancelled when its videos
# are fetched again, one or two at once: the flag goes, and the children decide, as the UI shows.
def test_settle_ignores_stale_cancel_flag(db, make_job):
    parent = playlist_parent(make_job, 'completed', cancel_requested=True)
    video(make_job, V1, parent_id=parent.id, status='completed')
    again = video(make_job, V2, parent_id=parent.id, status='downloading')

    worker.update_job(again.id, status='completed')

    row = stored(db, parent.id)
    assert (row.status, row.cancel_requested) == ('completed', False)

    twice = playlist_parent(make_job, 'failed', cancel_requested=True)
    first = video(make_job, V3, parent_id=twice.id, status='downloading')
    second = video(make_job, V4, parent_id=twice.id, status='queued')

    worker.update_job(first.id, status='completed')

    row = stored(db, twice.id)
    assert (row.status, row.cancel_requested) == ('queued', False)

    worker.update_job(second.id, status='completed')

    assert stored(db, twice.id).status == 'completed'


# A cancel pending on a playlist waits for every video in flight: when one stops while another
# still downloads, the playlist stays in flight with the request kept.
def test_pending_cancel_waits_for_children_in_flight(db, make_job):
    parent = playlist_parent(make_job, 'downloading', cancel_requested=True)
    stopped = video(make_job, V1, parent_id=parent.id, status='downloading', cancel_requested=True)
    video(make_job, V2, parent_id=parent.id, status='downloading', cancel_requested=True)

    worker.update_job(stopped.id, status='cancelled')

    row = stored(db, parent.id)
    assert (row.status, row.cancel_requested, row.completed_at) == ('downloading', True, None)


# A cancelled playlist stays cancelled however many of its videos are fetched again at once: a
# settle while one still runs leaves the stored status alone instead of reopening the playlist.
def test_settle_keeps_cancelled_while_videos_rerun(db, make_job):
    parent = playlist_parent(make_job, 'cancelled')
    first = video(make_job, V1, parent_id=parent.id, status='downloading')
    second = video(make_job, V2, parent_id=parent.id, status='queued')

    worker.update_job(first.id, status='completed')

    assert stored(db, parent.id).status == 'cancelled'

    worker.update_job(second.id, status='completed')

    assert stored(db, parent.id).status == 'cancelled'


# A channel download adopts every row of a video but queues at most one, the oldest: duplicate
# rows of one video are not downloaded twice, none is queued beside a row that holds the video,
# and a failed or finished row is adopted as it is.
def test_adoption_requeues_one_inert_row_per_video(db, make_job):
    held = video(make_job, V1, status='completed', filename=f'Video [{V1}].mp4')
    beside_held = video(make_job, V1, status='deleted')
    first = video(make_job, V2, status='deleted')
    second = video(make_job, V2, status='cancelled')
    failed = video(make_job, V3, status='failed')
    parent = make_job(url=CHANNEL_URL, request_type='channel', status='expanding')

    worker.create_child_jobs(parent, [V1, V2, V3])

    assert statuses(db, held, beside_held, first, second, failed) == [
        'completed', 'deleted', 'queued', 'cancelled', 'failed',
    ]
    assert stored(db, beside_held.id).parent_id == parent.id


# A playlist that failed because every video was skipped offers no Retry, which would reset
# nothing; its error points at Fetch Metadata instead.
def test_all_skipped_parent_offers_no_retry(client, db, make_job):
    parent = playlist_parent(make_job, 'expanding')
    video(make_job, V1, parent_id=parent.id, status=SKIPPED_STATUS)
    worker.create_child_jobs(parent, [V1])

    page = client.get(f'/job/{parent.id}')

    assert stored(db, parent.id).status == 'failed'
    assert f'action="/retry/{parent.id}"' not in page.text
    assert f'action="/job/{parent.id}/redownload"' in page.text


# A child's progress write moves its playlist's stored progress. The organizer runs after a child
# stops only once its playlist has settled, read from the playlist's effective status: a cancelled
# playlist whose other video still reruns holds it back.
def test_child_updates_reach_parent_and_organizer(monkeypatch, db, make_job):
    runs = []
    monkeypatch.setattr(worker, 'organizer_scan_and_process',
                        lambda dry_run=False: runs.append(dry_run))
    parent = playlist_parent(make_job)
    first = video(make_job, V1, parent_id=parent.id, status='downloading')
    second = video(make_job, V2, parent_id=parent.id, status='downloading')
    rerun = playlist_parent(make_job, 'cancelled')
    rerun_done = video(make_job, V3, parent_id=rerun.id, status='downloading')
    video(make_job, V4, parent_id=rerun.id, status='queued')

    worker.update_job(first.id, progress='50.0%')

    assert stored(db, parent.id).progress == '25.0%'

    for job in (first, rerun_done):
        worker.update_job(job.id, status='completed')
        worker.finalize_after_download(stored(db, job.id))

    assert runs == []

    worker.update_job(second.id, status='completed')
    worker.finalize_after_download(stored(db, second.id))

    assert runs == [False]


# A single Retry, and auto-retry, still find a video's file moved within the library, and refresh
# it in place rather than downloading it again.
def test_retry_finds_moved_library_file(client, db, make_job, library):
    write_media(library.media / 'Elsewhere' / f'Video [{V1}].mp4')
    write_media(library.media / 'Elsewhere' / f'Video [{V2}].mp4')
    moved = video(make_job, V1, status='failed',
                  filename=str(library.media / 'Show' / f'Video [{V1}].mp4'))

    client.post(f'/retry/{moved.id}')

    assert stored(db, moved.id).status == 'redownload'

    auto = video(make_job, V2, status='failed',
                 filename=str(library.media / 'Show' / f'Video [{V2}].mp4'))

    worker.retry_failed_jobs()

    assert stored(db, auto.id).status == 'redownload'


# The admin sweep also settles a playlist it reaches only through a failed video, and leaves a
# video in flight, and so its playlist, to the worker even when that video's file is on disk.
def test_sweep_settles_through_children_and_skips_in_flight(client, db, make_job, library):
    lagging = playlist_parent(make_job, 'queued')
    video(make_job, V1, parent_id=lagging.id, status='completed')
    recovered = video(make_job, V2, parent_id=lagging.id, status='failed')
    busy = playlist_parent(make_job, 'failed')
    fixed = video(make_job, V3, parent_id=busy.id, status='failed')
    running = video(make_job, V4, parent_id=busy.id, status='redownload')
    for youtube_id in (V2, V3, V4):
        write_media(library.staging / f'Video [{youtube_id}].mp4')

    client.post('/admin/sweep_existing')

    assert statuses(db, recovered, lagging) == ['completed', 'completed']
    assert statuses(db, fixed, running, busy) == ['completed', 'redownload', 'failed']


# Deleting a failed video through the bulk action settles its playlist as a single delete does.
def test_bulk_delete_of_child_settles_parent(client, db, make_job, library):
    parent = playlist_parent(make_job, 'failed')
    video(make_job, V1, parent_id=parent.id, status='completed')
    failed = video(make_job, V2, parent_id=parent.id, status='failed')

    client.post('/bulk/delete', data={'job_ids': [failed.id], 'return_to': '/history'})

    assert statuses(db, failed, parent) == ['deleted', 'completed']


# Fetch Metadata on a playlist leaves alone a video the worker takes up between the request
# reading it and resetting it, and resets the rest; Refresh all likewise leaves such an entry
# alone and says so.
def test_redownload_leaves_rows_the_worker_took_up(client, empty_db, db, make_job):
    parent = playlist_parent(make_job, 'completed')
    reset = video(make_job, V1, parent_id=parent.id, status='completed',
                  filename=f'Video [{V1}].mp4')
    taken = video(make_job, V2, parent_id=parent.id, status='completed',
                  filename=f'Video [{V2}].mp4')

    with_worker_taking_up(empty_db, taken.id, lambda: client.post(f'/job/{parent.id}/redownload'))

    assert statuses(db, parent, reset, taken) == ['redownload', 'redownload', 'metadata']

    first = video(make_job, V3, status='completed', filename=f'Video [{V3}].mp4')
    second = video(make_job, V4, status='completed', filename=f'Video [{V4}].mp4')

    page = with_worker_taking_up(empty_db, first.id, lambda: client.post('/history/redownload-all'))

    assert statuses(db, first, second) == ['metadata', 'redownload']
    assert 'Metadata fetch requested for 1 history entry. 1 still in progress' in page.text


# Cancelling a playlist stored as failed while some of its videos run again on their own cancels
# it for good, whether one video or two were running: it ends cancelled, and auto-retry leaves
# its other failed video alone.
def test_cancel_of_playlist_rerunning_videos_stands(client, db, make_job, library):
    parent = playlist_parent(make_job, 'failed')
    video(make_job, V1, parent_id=parent.id, status='completed')
    rerun = video(make_job, V2, parent_id=parent.id, status='failed')
    other = video(make_job, V3, parent_id=parent.id, status='failed')
    client.post(f'/retry/{rerun.id}')

    client.post(f'/cancel/{parent.id}')
    worker.retry_failed_jobs()

    row = stored(db, parent.id)
    assert (row.status, row.cancel_requested) == ('cancelled', False)
    assert statuses(db, rerun, other) == ['cancelled', 'failed']

    busy = playlist_parent(make_job, 'failed')
    video(make_job, V4, parent_id=busy.id, status='completed')
    running = video(make_job, V5, parent_id=busy.id, status='downloading')
    waiting = video(make_job, V6, parent_id=busy.id, status='redownload')

    client.post(f'/cancel/{busy.id}')
    worker.update_job(running.id, status='cancelled', error='Cancelled by user')
    worker.process_job(stored(db, waiting.id))

    assert statuses(db, running, waiting, busy) == ['cancelled', 'cancelled', 'cancelled']


# A deleted playlist stays deleted when auto-retry reruns a video fetched again under it that
# failed once: the video is retried, and the playlist is not reopened.
def test_auto_retry_keeps_deleted_parent(db, make_job, library):
    parent = playlist_parent(make_job, 'deleted')
    video(make_job, V1, parent_id=parent.id, status='deleted')
    refetched = video(make_job, V2, parent_id=parent.id, status='failed')

    worker.retry_failed_jobs()

    assert statuses(db, refetched, parent) == ['queued', 'deleted']

    worker.update_job(refetched.id, status='completed')

    assert stored(db, parent.id).status == 'deleted'


# A playlist that stops as cancelled keeps no speed of its own, which its display would otherwise
# fall back to as though a transfer were still running.
def test_settled_parent_keeps_no_stale_speed(client, db, make_job):
    parent = playlist_parent(make_job)
    running = video(make_job, V1, parent_id=parent.id, status='downloading')
    video(make_job, V2, parent_id=parent.id, status='queued')
    worker.update_job(running.id, progress='40.0%', speed='2.00MiB/s')
    worker.update_job(running.id, status='failed')

    client.post(f'/cancel/{parent.id}')

    with db() as session:
        row = session.get(Download, parent.id)
        assert (row.status, row.speed, compute_parent_speed(row)) == ('cancelled', None, None)
