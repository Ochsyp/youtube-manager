# -------------------------------------------------------
# Web app smoke tests
#
# End-to-end checks of app.py through a TestClient with the lifespan running: the healthcheck,
# the dashboard on an empty database, and queuing a video from the add form.
# -------------------------------------------------------

from datetime import datetime, timezone

from sqlalchemy import event, text

from youtube_manager import database, worker
from youtube_manager.database import Download

VIDEO_ID = 'dQw4w9WgXcQ'

# A time well before any test runs, for rows whose updated_at a write must move.
EARLIER = datetime(2026, 1, 1, tzinfo=timezone.utc)


# /health answers ok without touching the database or the filesystem.
def test_health(client):
    response = client.get('/health')
    assert response.status_code == 200
    assert response.json() == {'status': 'ok'}


# The dashboard renders on an empty database with the add form, the queue table's empty-state
# row and no bulk-action buttons.
def test_index_renders_empty_queue(client):
    response = client.get('/')
    assert response.status_code == 200
    assert response.headers['content-type'].startswith('text/html')
    body = response.text
    assert 'action="/add"' in body
    assert 'Active Queue' in body
    assert 'No downloads' in body
    assert 'action="/cancel-all"' not in body
    assert 'action="/retry-all"' not in body


# Posting a youtu.be link as a video redirects back to the dashboard with a flash message and
# stores one queued top-level row under the canonical watch URL, its id already extracted.
def test_add_video_queues_row(client, db):
    response = client.post(
        '/add',
        data={'url': f'  https://youtu.be/{VIDEO_ID}  ', 'request_type': 'video'},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers['location'] == '/'
    assert 'flash_message' in response.cookies

    with db() as session:
        rows = session.query(Download).all()
    assert [
        (row.url, row.request_type, row.youtube_id, row.status, row.parent_id) for row in rows
    ] == [(f'https://www.youtube.com/watch?v={VIDEO_ID}', 'video', VIDEO_ID, 'queued', None)]

    # The redirect target shows the flash message and lists the new job.
    page = client.get('/')
    assert 'Video queued.' in page.text
    assert 'No downloads' not in page.text


# The dashboard's change check reads one value, max(updated_at), in one SELECT, and still sees a
# write: a poll with the value it returned reports no change until a row is written.
def test_api_queue_uses_single_max_updated_at(client, make_job):
    job = make_job(youtube_id=VIDEO_ID, status='downloading')
    statements = []

    # A cursor hook that records each statement the engine runs.
    def record(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    event.listen(database.engine, 'before_cursor_execute', record)
    try:
        latest = client.get('/api/queue').json()['latest']
    finally:
        event.remove(database.engine, 'before_cursor_execute', record)

    selects = [statement for statement in statements if 'downloads' in statement]
    assert len(selects) == 1
    assert 'max(downloads.updated_at)' in selects[0]
    assert 'completed_at' not in selects[0] and 'created_at' not in selects[0]
    assert client.get('/api/queue', params={'since': latest}).json()['changed'] is False

    worker.update_job(job.id, progress='50%')

    assert client.get('/api/queue', params={'since': latest}).json()['changed'] is True


# SQLite answers the change check from the updated_at index, without reading the table.
def test_queue_poll_max_uses_updated_at_index(db):
    with database.engine.connect() as conn:
        plan = conn.execute(text(
            'EXPLAIN QUERY PLAN SELECT max(downloads.updated_at) FROM downloads'
        )).all()

    assert any('idx_download_updated_at' in row[-1] for row in plan)


# Each way a Download row is written moves its updated_at, which the one-value check relies on:
# an ORM change (Retry), the conditional bulk update behind Fetch Metadata, and the worker's
# update_job.
def test_every_download_write_moves_updated_at(client, db, make_job):
    retried = make_job(youtube_id='aaaaaaaaaaa', status='failed', updated_at=EARLIER)
    fetched = make_job(youtube_id='bbbbbbbbbbb', status='completed', updated_at=EARLIER)
    worked = make_job(youtube_id='ccccccccccc', status='downloading', updated_at=EARLIER)

    client.post(f'/retry/{retried.id}', follow_redirects=False)
    client.post(f'/job/{fetched.id}/redownload', follow_redirects=False)
    worker.update_job(worked.id, progress='50%')

    with db() as session:
        for job in (retried, fetched, worked):
            stored = session.get(Download, job.id)
            assert stored.updated_at > EARLIER.replace(tzinfo=None), job.youtube_id
