# -------------------------------------------------------
# Worker shutdown tests
#
# What the worker does when a stop is asked for in the middle of its work: a job goes back in the
# queue, long passes stop between items and touch the heartbeat as they go, the main loop ends,
# and the process exits with a status that says whether it stopped or failed. The tests that run
# real processes are in test_worker_process.py.
# -------------------------------------------------------

import logging
import subprocess

import pytest

from youtube_manager import organizer, worker
from youtube_manager.database import Channel, Download, SchemaTooNewError, Subscription


# Every test starts and ends with no stop requested and no failed subscription check on record.
@pytest.fixture(autouse=True)
def no_stop_requested(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(worker, '_subscription_failures', {})
    worker._shutdown.clear()
    yield
    worker._shutdown.clear()


# Count calls of worker.touch_heartbeat; returns the list each call appends to.
@pytest.fixture
def heartbeats(monkeypatch: pytest.MonkeyPatch) -> list[None]:
    calls = []
    monkeypatch.setattr(worker, 'touch_heartbeat', lambda: calls.append(None))
    return calls


# The stored row for `job_id`, read in a fresh session.
def stored(db, job_id: int) -> Download:
    with db() as session:
        return session.get(Download, job_id)


# Insert two resolved channel subscriptions and return their ids, lowest first.
def make_two_subscriptions(db) -> list[int]:
    ids = []
    with db() as session:
        for number in (1, 2):
            channel = Channel(
                youtube_id=f'UC000000000000000000000{number}', name=f'Channel {number}',
                url=f'https://www.youtube.com/channel/UC000000000000000000000{number}',
            )
            session.add(channel)
            session.flush()
            subscription = Subscription(
                subscription_type='channel', url=f'https://www.youtube.com/@channel{number}',
                channel_id=channel.id,
            )
            session.add(subscription)
            session.flush()
            ids.append(subscription.id)
        session.commit()
    return ids


# A stop inside a job's yt-dlp lookup puts the job back where a restart would, never failed: one
# interrupted while downloading is queued again with no error, and the stop goes on to the loop.
def test_stop_during_a_lookup_puts_the_job_back_in_the_queue(db, make_job, monkeypatch):
    job = make_job(youtube_id='dQw4w9WgXcQ')

    # Stand in for a lookup cut off by a stop, after the job had reached 'downloading'.
    def interrupted_process_job(job):
        worker.update_job(job.id, status='downloading')
        worker._shutdown.set()
        raise worker.ShutdownRequested()

    monkeypatch.setattr(worker, 'process_job', interrupted_process_job)

    with pytest.raises(worker.ShutdownRequested):
        worker.run_job_guarded(job)

    row = stored(db, job.id)
    assert row.status == 'queued'
    assert row.error is None


# ShutdownRequested passes every `except Exception` guard, so a lookup that raises it is not
# counted as a failed check of its subscription.
def test_stop_inside_a_subscription_scan_records_no_failure(db, monkeypatch):
    first, second = make_two_subscriptions(db)
    calls = []

    # Stand in for a scan cut off by a stop.
    def interrupted_scan(session, subscription, playlist_items):
        calls.append(subscription.id)
        worker._shutdown.set()
        raise worker.ShutdownRequested()

    monkeypatch.setattr(worker, 'scan_subscription', interrupted_scan)

    with pytest.raises(worker.ShutdownRequested):
        worker.check_subscriptions()

    assert calls == [first]
    assert worker._subscription_failures == {}
    with db() as session:
        assert session.get(Subscription, first).last_checked is None


# A stop that arrives during one subscription's check lets that check finish and commit, then
# ends the pass before the next one; the heartbeat is touched for each subscription it starts.
def test_subscription_check_stops_between_subscriptions(db, monkeypatch, heartbeats):
    first, second = make_two_subscriptions(db)
    calls = []

    # Scan successfully, finding nothing, and ask for a stop meanwhile.
    def scan(session, subscription, playlist_items):
        calls.append(subscription.id)
        worker._shutdown.set()
        return [], None, subscription.url

    monkeypatch.setattr(worker, 'scan_subscription', scan)

    with pytest.raises(worker.ShutdownRequested):
        worker.check_subscriptions()

    assert calls == [first]
    assert len(heartbeats) >= 1
    with db() as session:
        assert session.get(Subscription, first).last_checked is not None
        assert session.get(Subscription, second).last_checked is None


# Insert two playlist parents with one child each and no title, as both backfills find them, and
# return their ids.
def make_two_parents(make_job) -> list[int]:
    ids = []
    for number in (1, 2):
        parent = make_job(
            url=f'https://www.youtube.com/playlist?list=PL{number}', request_type='playlist',
            status='completed',
        )
        make_job(
            url=f'https://www.youtube.com/watch?v=aaaaaaaaaa{number}', youtube_id=f'aaaaaaaaaa{number}',
            parent_id=parent.id, status='completed',
        )
        ids.append(parent.id)
    return ids


# The title backfill stops between parents once a stop is asked for, touching the heartbeat for
# each parent it starts.
def test_title_backfill_stops_between_parents(db, make_job, monkeypatch, heartbeats):
    make_two_parents(make_job)
    lookups = []

    # Answer the lookup and ask for a stop meanwhile.
    def discover(job, fail_on_error=True, store_error=True):
        lookups.append(job.id)
        worker._shutdown.set()
        return None

    monkeypatch.setattr(worker, 'discover_metadata', discover)

    with pytest.raises(worker.ShutdownRequested):
        worker.populate_missing_parent_metadata()

    assert len(lookups) == 1
    assert len(heartbeats) >= 1


# The playlist index backfill stops between parents once a stop is asked for, touching the
# heartbeat for each parent it starts.
def test_index_backfill_stops_between_parents(db, make_job, monkeypatch, heartbeats):
    make_two_parents(make_job)
    scans = []

    # Answer the scan with nothing and ask for a stop meanwhile.
    def scan(url, playlist_items=None):
        scans.append(url)
        worker._shutdown.set()
        return [], None

    monkeypatch.setattr(worker, 'scan_video_ids', scan)

    with pytest.raises(worker.ShutdownRequested):
        worker.backfill_all_playlist_indexes()

    assert len(scans) == 1
    assert len(heartbeats) >= 1


# The organizer asks its tick before each video and stops filing when it answers False, but still
# finishes its pass: Jellyfin hears about the video already filed.
def test_organizer_stops_between_videos_and_still_notifies(db, make_job, monkeypatch):
    for number in (1, 2):
        make_job(
            url=f'https://www.youtube.com/watch?v=bbbbbbbbbb{number}', youtube_id=f'bbbbbbbbbb{number}',
            status='completed',
        )
    filed, notified, ticks = [], [], []
    monkeypatch.setattr(organizer, 'library_ready', lambda: True)
    monkeypatch.setattr(organizer, 'relocate_misfiled_shows', lambda session, dry_run: 0)
    monkeypatch.setattr(organizer, 'repair_stale_filenames', lambda session, dry_run: 0)
    monkeypatch.setattr(organizer, 'repair_missing_thumbnails', lambda session, dry_run: 0)
    monkeypatch.setattr(organizer, 'is_organizable', lambda session, job, dry_run=False: True)
    monkeypatch.setattr(organizer, 'organize_job', lambda session, job, dry_run: filed.append(job.id) or True)
    monkeypatch.setattr(organizer, 'notify_jellyfin', lambda: notified.append(None))

    # Go on before the first video only.
    def tick() -> bool:
        ticks.append(None)
        return len(ticks) == 1

    assert organizer.scan_and_process(dry_run=False, tick=tick) == 1

    assert len(filed) == 1
    assert len(ticks) == 2
    assert notified == [None]


# The worker's organizer tick touches the heartbeat and says to stop once a stop is asked for.
def test_organizer_tick_touches_heartbeat_and_follows_the_stop(heartbeats):
    assert worker.organizer_tick() is True
    worker._shutdown.set()
    assert worker.organizer_tick() is False
    assert len(heartbeats) == 2


# Replace the worker's startup housekeeping and signal setup with no-ops, run every periodic task
# on every pass, and return the list each task's name is appended to when it runs.
def quiet_worker_loop(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    ran = []
    monkeypatch.setattr(worker.signal, 'signal', lambda signum, handler: None)
    for name in (
        'recover_stuck_jobs', 'backfill_missing_youtube_ids', 'retire_invalid_scan_rows',
        'resequence_all_channel_indexes', 'take_daily_snapshot',
    ):
        monkeypatch.setattr(worker, name, lambda *args: None)
    for name in (
        'ORGANIZER_INTERVAL_SECONDS', 'RETRY_INTERVAL_SECONDS', 'SUBSCRIPTION_CHECK_INTERVAL_SECONDS',
    ):
        monkeypatch.setattr(worker, name, 0)
    for name in (
        'retry_failed_jobs', 'check_subscriptions', 'populate_missing_parent_metadata',
        'backfill_all_playlist_indexes',
    ):
        monkeypatch.setattr(worker, name, lambda name=name: ran.append(name))
    monkeypatch.setattr(
        worker, 'organizer_scan_and_process', lambda dry_run, tick=None: ran.append('organize'),
    )
    return ran


# A stop that cuts a job short ends the main loop at once: no periodic task runs after it, and the
# worker logs that it stopped.
def test_main_loop_ends_after_a_stop_inside_a_job(db, make_job, monkeypatch, caplog):
    caplog.set_level(logging.INFO)
    ran = quiet_worker_loop(monkeypatch)
    job = make_job()
    monkeypatch.setattr(worker, 'get_next_job', lambda: job)

    # Stand in for a job cut off by a stop.
    def interrupted(job):
        worker._shutdown.set()
        raise worker.ShutdownRequested()

    monkeypatch.setattr(worker, 'run_job_guarded', interrupted)

    worker.worker()

    assert ran == []
    assert any('YouTube worker stopped' in record.getMessage() for record in caplog.records)


# A stop asked for during one periodic task keeps the next one from starting.
def test_main_loop_runs_no_periodic_task_after_a_stop(db, monkeypatch):
    ran = quiet_worker_loop(monkeypatch)
    monkeypatch.setattr(worker, 'get_next_job', lambda: None)
    monkeypatch.setattr(worker._shutdown, 'wait', lambda timeout=None: False)

    # The first periodic task finishes, and a stop arrives meanwhile.
    def organize(dry_run, tick=None):
        ran.append('organize')
        worker._shutdown.set()

    monkeypatch.setattr(worker, 'organizer_scan_and_process', organize)

    worker.worker()

    assert ran == ['organize']


# The worker exits 1 when the web app has not prepared the schema in time, with an ERROR line, so
# a container set to restart on failure comes back and monitoring sees a failure.
def test_main_exits_1_when_the_schema_wait_runs_out(monkeypatch, caplog):
    monkeypatch.setattr(worker, 'wait_for_schema', lambda timeout_seconds=None: False)
    monkeypatch.setattr(worker, 'worker', lambda: pytest.fail('the worker loop must not start'))

    assert worker.main() == 1
    assert any(
        'Timed out waiting for database schema' in record.getMessage()
        for record in caplog.records if record.levelno == logging.ERROR
    )


# The worker exits 1 on a schema from a later major release, which it must not touch.
def test_main_exits_1_on_a_newer_major_schema(monkeypatch):
    # Stand in for wait_for_schema finding a schema from a later hundred.
    def too_new(timeout_seconds=None):
        raise SchemaTooNewError('newer')

    monkeypatch.setattr(worker, 'wait_for_schema', too_new)
    monkeypatch.setattr(worker, 'worker', lambda: pytest.fail('the worker loop must not start'))

    assert worker.main() == 1


# The worker exits 0 once its loop has ended on a requested stop.
def test_main_exits_0_after_a_stop(monkeypatch):
    ran = []
    monkeypatch.setattr(worker, 'wait_for_schema', lambda timeout_seconds=None: True)
    monkeypatch.setattr(worker, 'worker', lambda: ran.append(None))

    assert worker.main() == 0
    assert ran == [None]


# checkpoint() touches the heartbeat, and raises ShutdownRequested once a stop is asked for.
def test_checkpoint_touches_heartbeat_and_raises_on_a_stop(heartbeats):
    worker.checkpoint()
    worker._shutdown.set()
    with pytest.raises(worker.ShutdownRequested):
        worker.checkpoint()
    assert len(heartbeats) == 2


# ShutdownRequested is not an Exception, so the worker's `except Exception` guards pass it on.
def test_shutdown_requested_passes_exception_guards():
    assert not issubclass(worker.ShutdownRequested, Exception)
    with pytest.raises(worker.ShutdownRequested):
        try:
            raise worker.ShutdownRequested()
        except Exception:
            pytest.fail('caught by an Exception guard')


# A finished lookup still returns its output and relays its warnings once Popen replaces
# subprocess.run, and a failed one still raises CalledProcessError with yt-dlp's stderr.
def test_run_command_returns_output_and_raises_on_failure(monkeypatch):
    # A process that has already exited with `returncode`, printing `stdout` and `stderr`.
    class Finished:
        def __init__(self, returncode, stdout, stderr):
            self.returncode, self.pid = returncode, 0
            self._output = (stdout, stderr)

        def communicate(self, timeout=None):
            return self._output

        def poll(self):
            return self.returncode

    outcomes = [Finished(0, '{"id": "x"}', ''), Finished(1, '', 'ERROR: gone')]
    monkeypatch.setattr(worker.subprocess, 'Popen', lambda command, **kwargs: outcomes.pop(0))
    monkeypatch.setattr(worker, 'throttle_youtube_requests', lambda: None)

    assert worker.run_command(['yt-dlp', '--dump-single-json', 'u']).stdout == '{"id": "x"}'
    with pytest.raises(subprocess.CalledProcessError) as raised:
        worker.run_command(['yt-dlp', '--dump-single-json', 'u'])
    assert raised.value.stderr == 'ERROR: gone'


# A stop during a lookup that runs while the job is still in a status the worker picks up, a
# metadata refresh or an expansion, leaves that status as it was, so the next start does the same
# work again: a refresh stays a refresh instead of becoming a new download.
@pytest.mark.parametrize('status, request_type', [
    ('metadata', 'video'), ('metadata', 'playlist'), ('expanding', 'playlist'),
])
def test_stop_leaves_a_job_in_a_pickup_status_as_it_was(db, make_job, monkeypatch, status, request_type):
    parent = make_job(
        url='https://www.youtube.com/playlist?list=PL9', request_type=request_type, status=status,
        filename='/downloads/Fake Video [dQw4w9WgXcQ].mp4' if request_type == 'video' else None,
    )
    if request_type == 'playlist' and status == 'metadata':
        make_job(youtube_id='ccccccccccc', parent_id=parent.id, status='completed')

    # Stand in for the job's lookup cut off by a stop.
    def interrupted_process_job(job):
        worker._shutdown.set()
        raise worker.ShutdownRequested()

    monkeypatch.setattr(worker, 'process_job', interrupted_process_job)

    with pytest.raises(worker.ShutdownRequested):
        worker.run_job_guarded(parent)

    row = stored(db, parent.id)
    assert (row.status, row.error) == (status, None)


# A job cut short by a stop is logged as stopped, not as a stuck job recovered after a crash, and
# a pending cancel reads as one at a stop.
def test_stop_logs_a_stop_not_a_crash_recovery(db, make_job, monkeypatch, caplog):
    caplog.set_level(logging.INFO)
    job = make_job(youtube_id='dQw4w9WgXcQ', status='downloading', cancel_requested=True)

    # Stand in for a download cut off by a stop.
    def interrupted_process_job(job):
        worker._shutdown.set()
        raise worker.ShutdownRequested()

    monkeypatch.setattr(worker, 'process_job', interrupted_process_job)

    with pytest.raises(worker.ShutdownRequested):
        worker.run_job_guarded(job)

    assert stored(db, job.id).error == 'Cancelled during shutdown'
    assert not any('stuck job' in record.getMessage() for record in caplog.records)


# A lookup asked for after a stop raises ShutdownRequested at once and starts no yt-dlp.
def test_run_command_starts_nothing_after_a_stop(monkeypatch):
    monkeypatch.setattr(worker, 'throttle_youtube_requests', lambda: None)
    monkeypatch.setattr(
        worker.subprocess, 'Popen', lambda *args, **kwargs: pytest.fail('yt-dlp was started'),
    )
    worker._shutdown.set()

    with pytest.raises(worker.ShutdownRequested):
        worker.run_command(['yt-dlp', '--dump-single-json', 'u'])


# A stop that arrives during the lookups before a download starts no download: the job stays
# queued, as it was, and goes back to the loop as ShutdownRequested.
def test_download_not_started_after_a_stop(db, make_job, monkeypatch):
    job = make_job(youtube_id='dQw4w9WgXcQ', title='Known title')

    # The expected-path lookup returns quickly, and a stop arrives meanwhile.
    def resolve(job, output_template=None):
        worker._shutdown.set()
        return None

    monkeypatch.setattr(worker, 'resolve_expected_output_path', resolve)
    monkeypatch.setattr(
        worker.subprocess, 'Popen', lambda *args, **kwargs: pytest.fail('yt-dlp was started'),
    )

    with pytest.raises(worker.ShutdownRequested):
        worker.download_video(job)

    assert stored(db, job.id).status == 'queued'
