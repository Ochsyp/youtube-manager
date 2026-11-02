# -------------------------------------------------------
# Web app smoke tests
#
# End-to-end checks of app.py through a TestClient with the lifespan running: the healthcheck,
# the dashboard on an empty database, and queuing a video from the add form.
# -------------------------------------------------------

from youtube_manager.database import Download

VIDEO_ID = 'dQw4w9WgXcQ'


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
