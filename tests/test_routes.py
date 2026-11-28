# -------------------------------------------------------
# Route tests
#
# What the web routes answer over HTTP, beyond the URL helpers tests/test_urls.py covers: each
# rejection of /add and /subscriptions/add ends in a redirect with its message and stores
# nothing, the two subscription toggles flip and report, and the polling endpoints the dashboard,
# the job page and the log page use report a change only after a write.
# -------------------------------------------------------

import pytest

from youtube_manager import worker
from youtube_manager.database import Download, Subscription

VIDEO_ID = 'dQw4w9WgXcQ'
VIDEO_URL = f'https://www.youtube.com/watch?v={VIDEO_ID}'
CHANNEL_URL = 'https://www.youtube.com/@example'
PLAYLIST_URL = 'https://www.youtube.com/playlist?list=PLexample'


# POST `data` to `path` without following the redirect; returns the response.
def post(client, path: str, data: dict | None = None):
    return client.post(path, data=data or {}, follow_redirects=False)


# Count of stored rows of `model`.
def count(db, model) -> int:
    with db() as session:
        return session.query(model).count()


# Each rejection of /add redirects to the dashboard, which then shows the reason, and queues
# nothing.
@pytest.mark.parametrize(('url', 'request_type', 'message'), [
    (VIDEO_URL, 'movie', 'Invalid request type.'),
    ('https://example.com/watch?v=dQw4w9WgXcQ', 'video', 'Not a YouTube URL.'),
    (CHANNEL_URL, 'video', 'Wrong type selected. URL is a channel.'),
    (VIDEO_URL, 'channel', 'Wrong type selected. URL is a video.'),
])
def test_add_rejections_store_nothing(client, db, url, request_type, message):
    response = post(client, '/add', {'url': url, 'request_type': request_type})

    assert response.status_code == 303
    assert response.headers['location'] == '/'
    assert message in client.get('/').text
    assert count(db, Download) == 0


# Each rejection of /subscriptions/add redirects to the subscriptions page with its reason and
# stores nothing; a URL already subscribed is refused with its own message.
@pytest.mark.parametrize(('url', 'subscription_type', 'message'), [
    (CHANNEL_URL, 'movie', 'Invalid subscription type.'),
    ('https://example.com/@example', 'channel', 'Not a YouTube URL.'),
    (PLAYLIST_URL, 'channel', 'Wrong type selected. URL is a playlist, not a channel.'),
    (CHANNEL_URL, 'playlist', 'Wrong type selected. URL is a channel, not a playlist.'),
])
def test_subscription_rejections_store_nothing(client, db, url, subscription_type, message):
    response = post(client, '/subscriptions/add',
                    {'url': url, 'subscription_type': subscription_type})

    assert response.status_code == 303
    assert response.headers['location'] == '/subscriptions'
    assert message in client.get('/subscriptions').text
    assert count(db, Subscription) == 0


# A second subscription to a URL already subscribed is refused with its own message.
def test_second_subscription_to_a_url_is_refused(client, db):
    form = {'url': CHANNEL_URL, 'subscription_type': 'channel'}
    post(client, '/subscriptions/add', form)
    assert 'Subscription added.' in client.get('/subscriptions').text

    post(client, '/subscriptions/add', form)

    assert 'Subscription already exists.' in client.get('/subscriptions').text
    assert count(db, Subscription) == 1


# A stored subscription, enabled with backfill on; returns its id.
def make_subscription(db) -> int:
    with db() as session:
        sub = Subscription(url=CHANNEL_URL, subscription_type='channel', enabled=True,
                           backfill_existing=True)
        session.add(sub)
        session.commit()
        return sub.id


# The stored (enabled, backfill_existing) of subscription `sub_id`.
def flags(db, sub_id: int) -> tuple[bool, bool]:
    with db() as session:
        sub = session.get(Subscription, sub_id)
        return sub.enabled, sub.backfill_existing


# Pause and resume flip enabled and say which; backfill_existing is left as it was.
def test_toggle_enabled_flips_and_reports(client, db):
    sub_id = make_subscription(db)

    post(client, f'/subscriptions/{sub_id}/toggle-enabled')
    assert flags(db, sub_id) == (False, True)
    assert 'Subscription disabled.' in client.get('/subscriptions').text

    post(client, f'/subscriptions/{sub_id}/toggle-enabled')
    assert flags(db, sub_id) == (True, True)
    assert 'Subscription enabled.' in client.get('/subscriptions').text


# The backfill toggle flips backfill_existing and names the new preference; enabled is left alone.
def test_toggle_backfill_flips_and_reports(client, db):
    sub_id = make_subscription(db)

    post(client, f'/subscriptions/{sub_id}/toggle-backfill')
    assert flags(db, sub_id) == (True, False)
    assert 'Backfill preference set to only new.' in client.get('/subscriptions').text

    post(client, f'/subscriptions/{sub_id}/toggle-backfill')
    assert flags(db, sub_id) == (True, True)
    assert 'Backfill preference set to download all existing.' in client.get('/subscriptions').text


# Either toggle of an id with no subscription says so and changes nothing.
@pytest.mark.parametrize('action', ['toggle-enabled', 'toggle-backfill'])
def test_toggle_of_a_missing_subscription_says_so(client, action):
    response = post(client, f'/subscriptions/999/{action}')

    assert response.status_code == 303
    assert 'Subscription not found.' in client.get('/subscriptions').text


# A playlist row with one video under it; returns (parent, child).
def playlist_with_a_video(make_job) -> tuple[Download, Download]:
    parent = make_job(url=PLAYLIST_URL, request_type='playlist', status='downloading')
    child = make_job(youtube_id=VIDEO_ID, parent_id=parent.id, status='queued')
    return parent, child


# The job page's poll reports no change until a row of the job is written, a video under it
# included, and carries the status, progress and speed it shows in place.
def test_job_poll_reports_a_change_after_a_write(client, make_job):
    parent, child = playlist_with_a_video(make_job)
    first = client.get(f'/api/job/{parent.id}').json()
    assert first['changed'] is False and first['latest']

    assert client.get(f'/api/job/{parent.id}', params={'since': first['latest']}).json()['changed'] is False

    worker.update_job(child.id, status='downloading', progress='40%', speed='1.0MiB/s')

    answer = client.get(f'/api/job/{parent.id}', params={'since': first['latest']}).json()
    assert answer['changed'] is True
    assert answer['latest'] > first['latest']
    assert answer['status'] == 'downloading'
    video = client.get(f'/api/job/{child.id}').json()
    assert (video['status'], video['progress'], video['speed']) == ('downloading', '40%', '1.0MiB/s')


# The log page's poll carries the whole log and reports a change once the log grows.
def test_log_poll_reports_a_change_and_carries_the_log(client, make_job):
    job = make_job(youtube_id=VIDEO_ID, status='downloading', log='first line\n')
    first = client.get(f'/api/job/{job.id}/log').json()
    assert (first['changed'], first['log_content']) == (False, 'first line\n')
    assert client.get(f'/api/job/{job.id}/log', params={'since': first['latest']}).json()['changed'] is False

    worker.update_job(job.id, log='first line\nsecond line\n')

    answer = client.get(f'/api/job/{job.id}/log', params={'since': first['latest']}).json()
    assert answer['changed'] is True
    assert answer['log_content'] == 'first line\nsecond line\n'


# Both polls answer 404 for a job that is gone, so the page stops polling it.
@pytest.mark.parametrize('path', ['/api/job/999', '/api/job/999/log'])
def test_poll_of_a_missing_job_is_404(client, path):
    response = client.get(path)

    assert response.status_code == 404


# The queue section is a fragment for the dashboard to swap in: it lists a job in progress and
# carries no page around it.
def test_queue_section_is_a_fragment_listing_the_queue(client, make_job):
    make_job(youtube_id=VIDEO_ID, status='downloading', title='A video in progress')

    response = client.get('/api/queue/section')

    assert response.status_code == 200
    assert 'A video in progress' in response.text
    assert '<html' not in response.text.lower()
