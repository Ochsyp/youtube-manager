# -------------------------------------------------------
# Persistent auto-retry tests
#
# The worker's automatic retries of failed videos: the count and the next retry time each video
# keeps on its row across restarts, the growing waits, the give-up after MAX_AUTO_RETRIES and the
# cleanup after it, and the count a video gets back once it stops failing.
# -------------------------------------------------------

import logging
from datetime import datetime, timedelta, timezone

from youtube_manager import database, worker
from youtube_manager.database import Download
from youtube_manager.jobstate import MAX_AUTO_RETRIES

T0 = datetime(2026, 3, 1, 12, 0, tzinfo=timezone.utc)
V1, V2, V3, V4 = 'aaaaaaaaaaa', 'bbbbbbbbbbb', 'ccccccccccc', 'ddddddddddd'
ERROR = 'HTTP Error 403: Forbidden'


# T0 plus `count` minutes.
def minutes(count: int) -> datetime:
    return T0 + timedelta(minutes=count)


# One auto-retry pass with the worker's clock at `moment`.
def retry_pass(monkeypatch, moment: datetime) -> None:
    monkeypatch.setattr(worker, 'utcnow', lambda: moment)
    worker.retry_failed_jobs()


# The stored row of `job_id`, read fresh.
def row(db, job_id: int) -> Download:
    with db() as session:
        return session.get(Download, job_id)


# A stored time as an aware UTC time; SQLite hands it back naive.
def aware(value):
    return None if value is None else value.replace(tzinfo=timezone.utc)


# Mark `job_id` failed with ERROR, as the worker does when a retried download fails again.
def fail(db, job_id: int) -> None:
    with db() as session:
        job = session.get(Download, job_id)
        job.status = 'failed'
        job.error = ERROR
        session.commit()


# A failed single video, with `fields` on top.
def failed_video(make_job, youtube_id: str = V1, **fields) -> Download:
    return make_job(youtube_id=youtube_id, status='failed', error=ERROR, **fields)


# Messages logged at `level`.
def messages_at(caplog, level: int) -> list[str]:
    return [record.getMessage() for record in caplog.records if record.levelno == level]


# The count and the next retry time are on the row, so a new worker process, here a new engine on
# the same file, keeps the schedule instead of retrying at once. The in-memory record is gone.
def test_backoff_persists_across_restart(monkeypatch, db, make_job, library):
    job = failed_video(make_job, auto_retry_attempts=3, next_auto_retry_at=minutes(10))

    retry_pass(monkeypatch, T0)
    assert row(db, job.id).status == 'failed'

    database.configure_database(f'sqlite:///{database.database_file().as_posix()}')
    retry_pass(monkeypatch, minutes(5))
    assert row(db, job.id).status == 'failed'

    retry_pass(monkeypatch, minutes(10))
    stored = row(db, job.id)
    assert stored.status == 'queued'
    assert stored.auto_retry_attempts == 4
    assert aware(stored.next_auto_retry_at) == minutes(10 + 40)
    assert not hasattr(worker, '_retry_backoff')


# The first pass retries a failed video at once and sets its next time 5 minutes on. After the
# video fails again it waits until that time, then is retried with the next wait doubled.
def test_next_attempt_respected(monkeypatch, db, make_job, library):
    job = failed_video(make_job)

    retry_pass(monkeypatch, T0)
    stored = row(db, job.id)
    assert (stored.status, stored.auto_retry_attempts) == ('queued', 1)
    assert aware(stored.next_auto_retry_at) == minutes(5)

    fail(db, job.id)
    retry_pass(monkeypatch, minutes(4))
    assert (row(db, job.id).status, row(db, job.id).auto_retry_attempts) == ('failed', 1)

    retry_pass(monkeypatch, minutes(5))
    stored = row(db, job.id)
    assert (stored.status, stored.auto_retry_attempts) == ('queued', 2)
    assert aware(stored.next_auto_retry_at) == minutes(15)


# A next time further ahead than the longest wait can only come from a clock that was wrong, so
# it counts as due rather than parking the video.
def test_next_time_far_ahead_counts_as_due(monkeypatch, db, make_job, library):
    job = failed_video(make_job, auto_retry_attempts=2, next_auto_retry_at=T0 + timedelta(hours=7))

    retry_pass(monkeypatch, T0)

    stored = row(db, job.id)
    assert (stored.status, stored.auto_retry_attempts) == ('queued', 3)
    assert aware(stored.next_auto_retry_at) == minutes(20)


# A pass that resets nothing, here a failed video with a cancel pending, spends nothing.
def test_pass_without_reset_spends_nothing(monkeypatch, db, make_job, library):
    job = failed_video(make_job, cancel_requested=True)

    retry_pass(monkeypatch, T0)

    stored = row(db, job.id)
    assert (stored.status, stored.auto_retry_attempts, stored.next_auto_retry_at) == (
        'failed', 0, None,
    )


# A video that stopped without failing gets its full count back; one still queued for its retry
# keeps its count. A pass with nothing left to change writes no row.
def test_budget_returned_once_download_stops_failing(monkeypatch, db, make_job, library):
    later = minutes(30)
    stopped = [
        make_job(youtube_id=youtube_id, status=status, auto_retry_attempts=3,
                 next_auto_retry_at=later)
        for youtube_id, status in ((V1, 'completed'), (V2, 'cancelled'), (V3, 'deleted'))
    ]
    waiting = make_job(youtube_id=V4, status='queued', auto_retry_attempts=4,
                       next_auto_retry_at=later)

    retry_pass(monkeypatch, T0)

    for job in stopped:
        stored = row(db, job.id)
        assert (stored.auto_retry_attempts, stored.next_auto_retry_at) == (0, None), job.status
    stored = row(db, waiting.id)
    assert (stored.auto_retry_attempts, aware(stored.next_auto_retry_at)) == (4, later)

    before = {job.id: row(db, job.id).updated_at for job in [*stopped, waiting]}
    retry_pass(monkeypatch, minutes(5))
    assert {job.id: row(db, job.id).updated_at for job in [*stopped, waiting]} == before


# A video that keeps failing is retried at 0, 5, 15, 35, 75, 155, 315 and 635 minutes, as the
# waits double from 5 minutes up to 6 hours, and given up on at 995 minutes, about 17 hours on.
def test_full_schedule_gives_up_after_about_17_hours(monkeypatch, db, make_job, library):
    job = failed_video(make_job)
    resets = []

    for minute in range(0, 1000, 5):
        retry_pass(monkeypatch, minutes(minute))
        if row(db, job.id).status == 'queued':
            resets.append(minute)
            fail(db, job.id)
        if minute == 990:
            assert row(db, job.id).error == ERROR

    assert MAX_AUTO_RETRIES == 8
    assert resets == [0, 5, 15, 35, 75, 155, 315, 635]
    stored = row(db, job.id)
    assert stored.error == f'Gave up after 8 automatic retries: {ERROR}'
    assert (stored.status, stored.next_auto_retry_at) == ('failed', None)


# At the cap, once its last wait has passed, the video stays failed with an error that says so,
# and one ERROR line names it. The give-up happens once: a later pass writes and logs nothing.
def test_gives_up_after_cap_with_message(monkeypatch, caplog, db, make_job, library):
    caplog.set_level(logging.INFO)
    job = failed_video(make_job, auto_retry_attempts=MAX_AUTO_RETRIES,
                       next_auto_retry_at=minutes(-1))

    retry_pass(monkeypatch, T0)

    stored = row(db, job.id)
    assert stored.error == f'Gave up after {MAX_AUTO_RETRIES} automatic retries: {ERROR}'
    assert (stored.status, stored.next_auto_retry_at) == ('failed', None)
    errors = messages_at(caplog, logging.ERROR)
    assert len(errors) == 1 and str(job.id) in errors[0]

    updated = stored.updated_at
    caplog.clear()
    retry_pass(monkeypatch, minutes(5))
    assert row(db, job.id).updated_at == updated
    assert messages_at(caplog, logging.ERROR) == []


# A give-up removes what the video left in staging, for a single video and for a playlist's
# video. A failure there is a WARN, and the give-up stands.
def test_give_up_removes_staging_leftovers(monkeypatch, caplog, db, make_job, library):
    single = failed_video(make_job, V1, auto_retry_attempts=MAX_AUTO_RETRIES,
                          next_auto_retry_at=minutes(-1))
    parent = make_job(url='https://www.youtube.com/playlist?list=PL1', request_type='playlist',
                      status='failed')
    child = failed_video(make_job, V2, parent_id=parent.id, auto_retry_attempts=MAX_AUTO_RETRIES,
                         next_auto_retry_at=minutes(-1))
    removed = []
    monkeypatch.setattr(worker, 'organizer_remove_staging_leftovers',
                        lambda job: removed.append(job.id))

    retry_pass(monkeypatch, T0)

    assert sorted(removed) == sorted([single.id, child.id])

    third = failed_video(make_job, V3, auto_retry_attempts=MAX_AUTO_RETRIES,
                         next_auto_retry_at=minutes(-1))

    # The organizer's cleanup as on a file it cannot remove.
    def cleanup_fails(job):
        raise OSError(13, 'Permission denied')

    monkeypatch.setattr(worker, 'organizer_remove_staging_leftovers', cleanup_fails)
    retry_pass(monkeypatch, minutes(5))

    assert row(db, third.id).error.startswith('Gave up after')
    assert any(str(third.id) in message for message in messages_at(caplog, logging.WARNING))


# In a playlist each video keeps its own count: a video at the cap is given up on alone, another
# is retried with its own count, and the playlist goes back to the queue for it.
def test_playlist_videos_keep_their_own_counts(monkeypatch, db, make_job, library):
    parent = make_job(url='https://www.youtube.com/playlist?list=PL1', request_type='playlist',
                      status='failed')
    dead = failed_video(make_job, V1, parent_id=parent.id, auto_retry_attempts=MAX_AUTO_RETRIES,
                        next_auto_retry_at=minutes(-1))
    alive = failed_video(make_job, V2, parent_id=parent.id, auto_retry_attempts=2,
                         next_auto_retry_at=minutes(-1))

    retry_pass(monkeypatch, T0)

    assert row(db, dead.id).error.startswith('Gave up after')
    assert row(db, dead.id).status == 'failed'
    stored = row(db, alive.id)
    assert (stored.status, stored.auto_retry_attempts) == ('queued', 3)
    assert row(db, parent.id).status == 'queued'
    assert row(db, parent.id).auto_retry_attempts == 0


# A playlist row and the failed videos under it, each video with `attempts` retries spent.
def failed_playlist(make_job, list_id: str, videos: dict) -> tuple:
    parent = make_job(url=f'https://www.youtube.com/playlist?list={list_id}',
                      request_type='playlist', status='failed')
    children = [
        failed_video(make_job, youtube_id, parent_id=parent.id, auto_retry_attempts=attempts,
                     next_auto_retry_at=minutes(5))
        for youtube_id, attempts in videos.items()
    ]
    return parent, children


# Whether `job_id` has its full count back: no retries spent and no next time.
def count_restarted(db, job_id: int) -> bool:
    stored = row(db, job_id)
    return (stored.auto_retry_attempts, stored.next_auto_retry_at) == (0, None)


# Retry gives a video its full count back, a video already given up on included, and for a
# playlist each video it requeues; Retry on one video of a playlist does the same for it.
def test_manual_retry_resets_attempts(client, db, make_job, library):
    gone = failed_video(make_job, V1, auto_retry_attempts=MAX_AUTO_RETRIES)
    parent, children = failed_playlist(make_job, 'PL1', {V2: 3, V3: 5})
    _, (child,) = failed_playlist(make_job, 'PL2', {V4: 2})

    for job_id in (gone.id, parent.id, child.id):
        client.post(f'/retry/{job_id}', follow_redirects=False)

    for job in (gone, *children, child):
        assert count_restarted(db, job.id), job.youtube_id
        assert row(db, job.id).status in ('queued', 'redownload')


# Retry All gives every failed video it requeues its full count back, and leaves a row it does
# not requeue as it was, updated_at included.
def test_retry_all_resets_attempts(client, db, make_job, library):
    single = failed_video(make_job, V1, auto_retry_attempts=4, next_auto_retry_at=minutes(5))
    _, (child,) = failed_playlist(make_job, 'PL1', {V2: 6})
    done = make_job(youtube_id=V3, status='completed', auto_retry_attempts=2)
    before = row(db, done.id).updated_at

    client.post('/retry-all', follow_redirects=False)

    assert count_restarted(db, single.id)
    assert count_restarted(db, child.id)
    assert (row(db, done.id).auto_retry_attempts, row(db, done.id).updated_at) == (2, before)


# The job page shows "auto-retry n/8" under a video's status while it has retries spent, on the
# page and in the section the page refreshes from, and nothing without; a playlist's page shows
# each video's count in its video list.
def test_job_page_shows_auto_retry_count(client, db, make_job, library):
    spent = failed_video(make_job, V1, auto_retry_attempts=3)
    fresh = failed_video(make_job, V2)
    parent, _ = failed_playlist(make_job, 'PL1', {V3: 5})

    assert f'auto-retry 3/{MAX_AUTO_RETRIES}' in client.get(f'/job/{spent.id}').text
    assert f'auto-retry 3/{MAX_AUTO_RETRIES}' in client.get(f'/api/job/{spent.id}/section').text
    assert 'auto-retry' not in client.get(f'/job/{fresh.id}').text
    assert f'auto-retry 5/{MAX_AUTO_RETRIES}' in client.get(f'/job/{parent.id}').text


# Fetch Metadata gives each video it resets its full count back, for a single video given up on
# and for each video of a playlist.
def test_redownload_resets_attempts(client, db, make_job, library):
    gone = failed_video(make_job, V1, auto_retry_attempts=MAX_AUTO_RETRIES)
    parent, children = failed_playlist(make_job, 'PL1', {V2: 3, V3: 7})

    client.post(f'/job/{gone.id}/redownload', follow_redirects=False)
    client.post(f'/job/{parent.id}/redownload', follow_redirects=False)

    for job in (gone, *children):
        assert count_restarted(db, job.id), job.youtube_id
