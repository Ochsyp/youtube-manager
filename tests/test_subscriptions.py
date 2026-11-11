# -------------------------------------------------------
# Subscription scan tests
#
# Pins how the worker checks subscriptions: only a successful scan of video ids records a baseline,
# no yt-dlp call runs inside a write transaction, a periodic channel scan reads only the newest
# uploads unless 50 or more of them are new, a change the web app makes during a check is kept,
# rows that are no video are retired at startup, background repairs never fail a parent, and
# "Download existing now" queues a back catalogue in scan order. Nothing runs yt-dlp.
# -------------------------------------------------------

import json
import logging
import sqlite3
import subprocess
import threading
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import event
from sqlalchemy.engine import Engine

from youtube_manager import worker
from youtube_manager.database import Channel, Download, Playlist, Subscription
from youtube_manager.jobstate import SKIPPED_STATUS

# Shorter than a real channel id, so their channels have no uploads playlist and a check reads
# the subscription's own URL.
CHANNEL_YOUTUBE_ID = 'UCsubscriptionstest'
OTHER_CHANNEL_YOUTUBE_ID = 'UCsubscriptionsother'
# A channel id of a real one's shape, and the uploads playlist a check reads for its channel.
UPLOADS_CHANNEL_ID = 'UCuploadsuploadsuploads1'
UPLOADS_URL = 'https://www.youtube.com/playlist?list=UUuploadsuploadsuploads1'
CHANNEL_URL = 'https://www.youtube.com/@example'
OTHER_CHANNEL_URL = 'https://www.youtube.com/@other'
PLAYLIST_YOUTUBE_ID = 'PLsubscriptionstest'
PLAYLIST_URL = f'https://www.youtube.com/playlist?list={PLAYLIST_YOUTUBE_ID}'
V1, V2, V3, V4 = (f'aaaaaaaaaa{n}' for n in range(1, 5))

# Lines a scan can print that are no video ids: here a channel's ids.
NOT_VIDEO_IDS = ('UCaaaaaaaaaaaaaaaaaaaaaa', 'UCbbbbbbbbbbbbbbbbbbbbbb')

# A clip's id as yt-dlp reports it for a clip a user asked for: 26 characters, no video id.
CLIP_ID = 'Ug' + 'c' * 24

# A scan result as scan_video_ids returns it after the proxy refused the request.
FAILED_SCAN = (
    [], 'ERROR: [youtube:tab] Unable to download API page: HTTP Error 503: Service Unavailable',
)

# When a subscription that already has its baseline was last checked.
CHECKED_AT = datetime(2026, 1, 1, tzinfo=timezone.utc)

# yt-dlp's --dump-single-json answer for CHANNEL_URL, as much of it as the resolve reads.
CHANNEL_METADATA = {
    'id': CHANNEL_YOUTUBE_ID,
    'title': 'Example channel',
    'channel_id': CHANNEL_YOUTUBE_ID,
    'channel': 'Example channel',
    'channel_url': f'https://www.youtube.com/channel/{CHANNEL_YOUTUBE_ID}',
}


# Every test starts with no failed subscription checks or uploads-playlist fallbacks on record, so
# none leaks between tests.
@pytest.fixture(autouse=True)
def fresh_failure_counter(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(worker, '_subscription_failures', {})
    monkeypatch.setattr(worker, '_uploads_playlist_fallbacks', set())


# Messages logged at exactly `level`, in the order they were logged.
def messages_at(caplog: pytest.LogCaptureFixture, level: int) -> list[str]:
    return [record.getMessage() for record in caplog.records if record.levelno == level]


# Insert the channel `youtube_id` and return its id.
def make_channel(db, youtube_id: str = CHANNEL_YOUTUBE_ID) -> int:
    with db() as session:
        channel = Channel(
            youtube_id=youtube_id, name='Example channel',
            url=f'https://www.youtube.com/channel/{youtube_id}',
        )
        session.add(channel)
        session.commit()
        return channel.id


# Insert PLAYLIST_YOUTUBE_ID as a playlist of `channel_id`, which may be None, and return its id.
def make_playlist(db, channel_id: int | None) -> int:
    with db() as session:
        playlist = Playlist(
            youtube_id=PLAYLIST_YOUTUBE_ID, title='Example playlist', channel_id=channel_id,
        )
        session.add(playlist)
        session.commit()
        return playlist.id


# Insert a subscription of type `kind`, CHANNEL_URL or PLAYLIST_URL unless `fields` names a url,
# and return its id. It is unresolved unless `fields` sets channel_id or playlist_id.
def make_subscription(db, kind: str = 'channel', **fields) -> int:
    values = {'subscription_type': kind, 'url': CHANNEL_URL if kind == 'channel' else PLAYLIST_URL}
    values.update(fields)
    with db() as session:
        subscription = Subscription(**values)
        session.add(subscription)
        session.commit()
        return subscription.id


# Set `fields` on subscription `subscription_id` and commit.
def update_subscription(db, subscription_id: int, **fields) -> None:
    with db() as session:
        subscription = session.get(Subscription, subscription_id)
        for key, value in fields.items():
            setattr(subscription, key, value)
        session.commit()


# The stored subscription `subscription_id`, read in a fresh session.
def stored_subscription(db, subscription_id: int) -> Subscription:
    with db() as session:
        return session.get(Subscription, subscription_id)


# The stored row for `job_id`, read in a fresh session.
def stored(db, job_id: int) -> Download:
    with db() as session:
        return session.get(Download, job_id)


# Every download row, lowest id first.
def all_rows(db) -> list[Download]:
    with db() as session:
        return session.query(Download).order_by(Download.id).all()


# Each row's (youtube_id, status), sorted.
def row_states(db) -> list[tuple[str, str]]:
    return sorted((row.youtube_id, row.status) for row in all_rows(db))


# Insert a video row for `youtube_id`, its URL built from the id as a scan builds it.
def video_row(make_job, youtube_id: str, **fields) -> Download:
    url = f'https://www.youtube.com/watch?v={youtube_id}'
    return make_job(url=url, youtube_id=youtube_id, **fields)


# Insert a video row grouped under the channel `channel_id`, as a channel subscription records one.
def channel_row(make_job, channel_id: int, youtube_id: str, **fields) -> Download:
    return video_row(make_job, youtube_id, channel_id=channel_id, group_by_channel=True, **fields)


# Make successive scan_video_ids calls return `results` in order, one (ids, error) pair per call.
# Returns the list each call's (url, playlist_items) is appended to.
def script_scans(monkeypatch: pytest.MonkeyPatch, *results) -> list[tuple[str, str | None]]:
    calls = []
    pending = list(results)

    # Record the call and answer with the next scripted result.
    def scan(url, playlist_items=None):
        calls.append((url, playlist_items))
        return pending.pop(0)

    monkeypatch.setattr(worker, 'scan_video_ids', scan)
    return calls


# Replace run_command with a yt-dlp stand-in that appends each argv to the returned list. A
# --dump-single-json call answers with `metadata`; any other call takes the next of `outputs`,
# from the list kept for its URL when `outputs` is a dict: stdout text, or an exception to raise.
def fake_run_command(monkeypatch: pytest.MonkeyPatch, outputs=(), metadata=None) -> list[list[str]]:
    commands = []
    if isinstance(outputs, dict):
        pending = {url: list(answers) for url, answers in outputs.items()}
    else:
        pending = list(outputs)

    # Record the argv and answer as described above.
    def run_command(command, timeout=worker.COMMAND_TIMEOUT_SECONDS):
        commands.append(command)
        if '--dump-single-json' in command:
            stdout = json.dumps(metadata or {})
            return subprocess.CompletedProcess(command, 0, stdout=stdout, stderr='')
        answer = pending[command[-1]].pop(0) if isinstance(pending, dict) else pending.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return subprocess.CompletedProcess(command, 0, stdout=answer, stderr='')

    monkeypatch.setattr(worker, 'run_command', run_command)
    return commands


# The scans among recorded yt-dlp argvs: every call that prints ids.
def scan_commands(commands: list[list[str]]) -> list[list[str]]:
    return [command for command in commands if '--print' in command]


# Write to subscription `subscription_id` from a second connection whose busy_timeout is 100 ms,
# as the web app writes, and append 'written' or SQLite's error to `outcomes`.
def write_from_second_connection(path, subscription_id: int, outcomes: list) -> None:
    # timeout is the connection's busy_timeout, in seconds.
    connection = sqlite3.connect(str(path), timeout=0.1, isolation_level=None)
    try:
        write = 'UPDATE subscriptions SET enabled = 1 WHERE id = ?'
        connection.execute(write, (subscription_id,))
        outcomes.append('written')
    except sqlite3.OperationalError as exc:
        outcomes.append(str(exc))
    finally:
        connection.close()


# The youtube_id of each job get_next_job() hands out, each completed before the next is taken.
def taken_order(db) -> list[str]:
    order = []
    for _ in range(20):
        job = worker.get_next_job()
        if job is None:
            break
        order.append(job.youtube_id)
        with db() as session:
            session.get(Download, job.id).status = 'completed'
            session.commit()
    return order


# The worker's stop flag for run_worker_passes: wait() reports the flag at once instead of
# sleeping, so a pass with no job costs no real time.
class InstantEvent(threading.Event):
    # Return the flag without blocking for `timeout`.
    def wait(self, timeout: float | None = None) -> bool:
        return self.is_set()


# Periodic tasks of the worker loop that run_worker_passes replaces with recorders.
LOOP_TASKS = (
    'organizer_scan_and_process', 'retry_failed_jobs', 'check_subscriptions',
    'populate_missing_parent_metadata', 'backfill_all_playlist_indexes',
)


# Run worker() for one pass of its loop per entry of `pass_times`, each pass's clock reading in
# seconds after start, with no job to take, no signal handler installed and every LOOP_TASKS entry
# recording (name, pass time) instead of running. Returns the records in call order.
def run_worker_passes(
    monkeypatch: pytest.MonkeyPatch, pass_times: list[float],
) -> list[tuple[str, float]]:
    start = 1000.0
    clock = [start]
    pending = list(pass_times)
    stop = InstantEvent()
    calls = []

    # Move the clock to the next pass's time, stopping the loop after the last pass.
    def next_job():
        clock[0] = start + pending.pop(0)
        if not pending:
            stop.set()
        return None

    monkeypatch.setattr(worker, '_shutdown', stop)
    monkeypatch.setattr(worker.signal, 'signal', lambda signum, handler: None)
    monkeypatch.setattr(worker.time, 'monotonic', lambda: clock[0])
    monkeypatch.setattr(worker, 'get_next_job', next_job)
    for name in LOOP_TASKS:
        monkeypatch.setattr(
            worker, name, lambda *args, name=name, **kwargs: calls.append((name, clock[0] - start)),
        )

    worker.worker()
    return calls


# A first scan that fails stamps nothing: last_checked stays empty, no row is recorded, and the
# failure is counted at WARN like any failed check.
def test_failed_first_scan_does_not_stamp_last_checked(caplog, monkeypatch, db):
    caplog.set_level(logging.INFO)
    subscription_id = make_subscription(db, channel_id=make_channel(db), backfill_existing=False)
    script_scans(monkeypatch, FAILED_SCAN)

    worker.check_subscriptions()

    assert stored_subscription(db, subscription_id).last_checked is None
    assert all_rows(db) == []
    assert any(
        f'Subscription {subscription_id}: scan failed' in message
        for message in messages_at(caplog, logging.WARNING)
    )
    assert messages_at(caplog, logging.ERROR) == []


# An "Only new" subscription whose first scan failed records its baseline at the next check that
# succeeds: the lineup is stored as skipped and nothing is queued.
def test_baseline_recorded_after_recovery_not_queued(monkeypatch, db):
    subscription_id = make_subscription(db, channel_id=make_channel(db), backfill_existing=False)
    script_scans(monkeypatch, FAILED_SCAN, ([V3, V2, V1], None))

    worker.check_subscriptions()
    worker.check_subscriptions()

    assert row_states(db) == [(V1, SKIPPED_STATUS), (V2, SKIPPED_STATUS), (V3, SKIPPED_STATUS)]
    assert worker.get_next_job() is None
    assert stored_subscription(db, subscription_id).last_checked is not None


# A resolve is committed before the scan runs: during the scan, a second connection with a 100 ms
# busy_timeout, as the web app writes, reads the resolved channel and can write.
def test_resolution_committed_before_scan(monkeypatch, empty_db, db):
    subscription_id = make_subscription(db)
    fake_run_command(monkeypatch, metadata=CHANNEL_METADATA)
    during_scan = []

    # Read the subscription's channel from another connection, then write from one.
    def scan(url, playlist_items=None):
        connection = sqlite3.connect(str(empty_db), isolation_level=None)
        try:
            read = 'SELECT channel_id FROM subscriptions WHERE id = ?'
            during_scan.append(connection.execute(read, (subscription_id,)).fetchone()[0])
        finally:
            connection.close()
        write_from_second_connection(empty_db, subscription_id, during_scan)
        return [V1], None

    monkeypatch.setattr(worker, 'scan_video_ids', scan)

    worker.check_subscriptions()

    channel_id = stored_subscription(db, subscription_id).channel_id
    assert channel_id is not None
    assert during_scan == [channel_id, 'written']


# A resolve that fails after creating a row is rolled back before the next subscription's scan:
# during that scan a second connection can write, and the failed subscription keeps no channel.
def test_failed_resolve_rolled_back_before_next_scan(monkeypatch, empty_db, db):
    failed = make_subscription(db, 'playlist')
    make_subscription(
        db, url=OTHER_CHANNEL_URL, channel_id=make_channel(db, OTHER_CHANNEL_YOUTUBE_ID),
        last_checked=CHECKED_AT,
    )
    # Playlist metadata that names a channel but no playlist id: the resolve creates the channel's
    # row, then fails.
    channel_only = {key: CHANNEL_METADATA[key] for key in ('channel_id', 'channel', 'channel_url')}
    fake_run_command(monkeypatch, metadata=channel_only)
    during_scan = []

    # Write from another connection while the resolved subscription is scanned.
    def scan(url, playlist_items=None):
        write_from_second_connection(empty_db, failed, during_scan)
        return [V1], None

    monkeypatch.setattr(worker, 'scan_video_ids', scan)

    worker.check_subscriptions()

    assert during_scan == ['written']
    assert stored_subscription(db, failed).channel_id is None


# A scan keeps the lines that are video ids, a 'yt:' prefix stripped, each once in listing order,
# and drops the rest in one WARN with their number and a sample. A subscription's check of such a
# listing records one row per video.
def test_scan_filters_non_video_ids_and_dedupes(caplog, monkeypatch, db):
    caplog.set_level(logging.INFO)
    listing = (
        f'{V1}\n{NOT_VIDEO_IDS[0]}\n{V2}\n\n{V1}\nyt:{V3}\nNA\n{NOT_VIDEO_IDS[1]}\n{V2}\n'
    )
    fake_run_command(monkeypatch, outputs=[listing, listing])

    assert worker.scan_video_ids(PLAYLIST_URL) == ([V1, V2, V3], None)

    [warning] = [m for m in messages_at(caplog, logging.WARNING) if 'not video ids' in m]
    assert 'dropped 3 line(s)' in warning and NOT_VIDEO_IDS[0] in warning

    make_subscription(db, channel_id=make_channel(db))
    worker.check_subscriptions()

    assert row_states(db) == [(V1, 'queued'), (V2, 'queued'), (V3, 'queued')]


# Output in which no line is a video id is a failed scan, so it cannot use up a baseline: nothing is
# recorded or stamped, and the failure is counted. Output with no line at all is a successful scan
# of an empty listing, which stamps the check and clears the count.
def test_all_lines_rejected_counts_as_failure(caplog, monkeypatch, db):
    caplog.set_level(logging.INFO)
    subscription_id = make_subscription(db, channel_id=make_channel(db), backfill_existing=False)
    fake_run_command(monkeypatch, outputs=['\n'.join(NOT_VIDEO_IDS) + '\n', ''])

    worker.check_subscriptions()

    assert all_rows(db) == []
    assert stored_subscription(db, subscription_id).last_checked is None
    assert any(
        f'Subscription {subscription_id}: scan failed: No video ids' in message
        for message in messages_at(caplog, logging.WARNING)
    )
    assert worker._subscription_failures == {subscription_id: 1}

    worker.check_subscriptions()

    assert all_rows(db) == []
    assert stored_subscription(db, subscription_id).last_checked is not None
    assert worker._subscription_failures == {}


# Retiring maps rows to subscriptions as their scans file them: a channel subscription owns the
# rows grouped under its channel, a playlist subscription those of its playlist, here one with no
# channel. Scan-built rows whose id is no video id in a retirable status become deleted, each owner
# is re-baselined record-only with one WARN naming it, and the count is logged, 0 included. A clip
# a user asked for and every other row stay, and so does a subscription that owned none.
def test_retire_invalid_rows_maps_channel_and_playlist_subscriptions(caplog, db, make_job):
    caplog.set_level(logging.INFO)

    worker.retire_invalid_scan_rows()

    retired_none = 'Retired 0 row(s) whose youtube_id is not a video id'
    assert retired_none in messages_at(caplog, logging.INFO)

    channel_id = make_channel(db)
    other_channel_id = make_channel(db, OTHER_CHANNEL_YOUTUBE_ID)
    playlist_id = make_playlist(db, None)
    channel_sub = make_subscription(db, channel_id=channel_id, last_checked=CHECKED_AT)
    playlist_sub = make_subscription(db, 'playlist', playlist_id=playlist_id,
                                     last_checked=CHECKED_AT)
    other_sub = make_subscription(db, url=OTHER_CHANNEL_URL, channel_id=other_channel_id,
                                  last_checked=CHECKED_AT)
    retired = [
        channel_row(make_job, channel_id, NOT_VIDEO_IDS[0], status='queued'),
        channel_row(make_job, channel_id, NOT_VIDEO_IDS[1], status='failed'),
        video_row(make_job, PLAYLIST_YOUTUBE_ID, playlist_id=playlist_id, status=SKIPPED_STATUS),
        # Of the other channel but not grouped under it, so no subscription of that channel's.
        video_row(make_job, NOT_VIDEO_IDS[1], channel_id=other_channel_id, group_by_channel=False,
                  status='failed'),
    ]
    kept = [
        channel_row(make_job, other_channel_id, NOT_VIDEO_IDS[0], status='completed'),
        channel_row(make_job, other_channel_id, V1, status='queued'),
        channel_row(make_job, channel_id, V2, status=SKIPPED_STATUS),
        make_job(url=f'https://www.youtube.com/clip/{CLIP_ID}', youtube_id=CLIP_ID,
                 status='failed'),
    ]

    worker.retire_invalid_scan_rows()

    for row in retired:
        assert (stored(db, row.id).status, stored(db, row.id).error) == (
            'deleted', 'Not a video id (channel tab listing)',
        )
    assert [stored(db, row.id).status for row in kept] == [
        'completed', 'queued', SKIPPED_STATUS, 'failed',
    ]
    for subscription_id in (channel_sub, playlist_sub):
        subscription = stored_subscription(db, subscription_id)
        assert (subscription.last_checked, subscription.backfill_existing) == (None, False)
    other = stored_subscription(db, other_sub)
    assert other.last_checked is not None and other.backfill_existing is True

    warnings = messages_at(caplog, logging.WARNING)
    for subscription_id in (channel_sub, playlist_sub):
        [warning] = [m for m in warnings if m.startswith(f'Subscription {subscription_id} ')]
        assert '"Only new"' in warning and 'Download existing now' in warning
    assert not any(m.startswith(f'Subscription {other_sub} ') for m in warnings)
    retired_count = 'Retired 4 row(s) whose youtube_id is not a video id'
    assert retired_count in messages_at(caplog, logging.INFO)


# A re-baselined subscription, here an "All existing" one, records its current listing as skipped
# at its next check, a full scan, without queuing any of it: a video it recorded as skipped before
# stays skipped.
def test_rebaseline_records_without_queueing(monkeypatch, db, make_job):
    channel_id = make_channel(db)
    subscription_id = make_subscription(db, channel_id=channel_id, last_checked=CHECKED_AT)
    channel_row(make_job, channel_id, NOT_VIDEO_IDS[0], status='queued')
    channel_row(make_job, channel_id, V1, status=SKIPPED_STATUS)
    commands = fake_run_command(monkeypatch, outputs=[f'{V3}\n{V2}\n{V1}\n'])

    worker.retire_invalid_scan_rows()
    worker.check_subscriptions()

    assert row_states(db) == [
        (NOT_VIDEO_IDS[0], 'deleted'),
        (V1, SKIPPED_STATUS), (V2, SKIPPED_STATUS), (V3, SKIPPED_STATUS),
    ]
    assert worker.get_next_job() is None
    [scan] = scan_commands(commands)
    assert '--playlist-items' not in scan and scan[-1] == CHANNEL_URL
    assert stored_subscription(db, subscription_id).last_checked is not None


# Children of a channel download are retired too and their parent re-synced: one with a finished
# video left is completed, one with nothing but retired rows deleted, and one whose other children
# are all skipped keeps its status rather than vanish as deleted. The channel's subscription is
# re-baselined record-only, since a download of its channel adopts the rows it recorded.
def test_retired_children_resettle_parent(db, make_job):
    channel_id = make_channel(db)
    subscription_id = make_subscription(db, channel_id=channel_id, last_checked=CHECKED_AT)
    mixed = make_job(url=CHANNEL_URL, request_type='channel', channel_id=channel_id,
                     status='failed')
    channel_row(make_job, channel_id, V1, parent_id=mixed.id, status='completed')
    channel_row(make_job, channel_id, NOT_VIDEO_IDS[0], parent_id=mixed.id, status='failed')
    only_junk = make_job(url=f'{CHANNEL_URL}/', request_type='channel', channel_id=channel_id,
                         status='failed')
    channel_row(make_job, channel_id, NOT_VIDEO_IDS[0], parent_id=only_junk.id, status='queued')
    channel_row(make_job, channel_id, NOT_VIDEO_IDS[1], parent_id=only_junk.id, status='failed')
    skipped_left = make_job(url=f'{CHANNEL_URL}/streams', request_type='channel',
                            channel_id=channel_id, status='failed')
    channel_row(make_job, channel_id, V2, parent_id=skipped_left.id, status=SKIPPED_STATUS)
    channel_row(make_job, channel_id, NOT_VIDEO_IDS[1], parent_id=skipped_left.id, status='failed')

    worker.retire_invalid_scan_rows()

    assert stored(db, mixed.id).status == 'completed'
    assert stored(db, only_junk.id).status == 'deleted'
    assert stored(db, skipped_left.id).status == 'failed'
    subscription = stored_subscription(db, subscription_id)
    assert (subscription.last_checked, subscription.backfill_existing) == (None, False)


# A channel subscription's first scan, its baseline, reads the whole channel. Once it has its
# baseline, a check reads only the newest 50 uploads and queues what is new among them, while a
# playlist subscription's checks read the whole playlist.
def test_periodic_channel_scan_limited_after_baseline(monkeypatch, db):
    channel_id = make_channel(db)
    make_subscription(db, channel_id=channel_id, backfill_existing=False)
    make_subscription(
        db, 'playlist', channel_id=channel_id, playlist_id=make_playlist(db, channel_id),
        last_checked=CHECKED_AT,
    )
    commands = fake_run_command(monkeypatch, outputs={
        CHANNEL_URL: [f'{V1}\n', f'{V2}\n{V1}\n'], PLAYLIST_URL: [f'{V3}\n', f'{V3}\n'],
    })

    worker.check_subscriptions()
    worker.check_subscriptions()

    scans = scan_commands(commands)
    [baseline, periodic] = [command for command in scans if command[-1] == CHANNEL_URL]
    assert '--playlist-items' not in baseline
    assert periodic[-3:] == ['--playlist-items', '1:50', CHANNEL_URL]
    playlist_scans = [command for command in scans if command[-1] == PLAYLIST_URL]
    assert len(playlist_scans) == 2
    assert not any('--playlist-items' in command for command in playlist_scans)
    assert row_states(db) == [(V1, SKIPPED_STATUS), (V2, 'queued'), (V3, 'queued')]


# When 50 or more of the uploads a periodic scan reads are new, more may have arrived than its
# window holds: the check reads the whole listing again, through the uploads playlist or the
# channel URL alike, and queues each upload it has not recorded, oldest first.
@pytest.mark.parametrize('channel_youtube_id, url', [
    (CHANNEL_YOUTUBE_ID, CHANNEL_URL), (UPLOADS_CHANNEL_ID, UPLOADS_URL),
])
def test_periodic_scan_reads_whole_channel_when_all_new(
    monkeypatch, db, make_job, channel_youtube_id, url,
):
    channel_id = make_channel(db, channel_youtube_id)
    make_subscription(db, channel_id=channel_id, last_checked=CHECKED_AT)
    known = channel_row(make_job, channel_id, V1, status='completed')
    uploads = [f'b{number:010d}' for number in range(55, 0, -1)]
    commands = fake_run_command(monkeypatch, outputs=[
        '\n'.join(uploads[:50]) + '\n', '\n'.join([*uploads, V1]) + '\n',
    ])

    worker.check_subscriptions()

    [limited, full] = scan_commands(commands)
    assert limited[-3:] == ['--playlist-items', '1:50', url]
    assert '--playlist-items' not in full and full[-1] == url
    queued = [row.youtube_id for row in all_rows(db) if row.status == 'queued']
    assert queued == uploads[::-1]
    assert stored(db, known.id).status == 'completed'


# When that whole-channel scan fails, the check still queues the newest uploads it read and says
# in a WARN that older ones may be missed, so a channel too big to list in time is still checked.
def test_failed_whole_channel_scan_keeps_newest_uploads(caplog, monkeypatch, db):
    caplog.set_level(logging.INFO)
    subscription_id = make_subscription(db, channel_id=make_channel(db), last_checked=CHECKED_AT)
    uploads = [f'b{number:010d}' for number in range(50, 0, -1)]
    timed_out = subprocess.TimeoutExpired(['yt-dlp'], worker.COMMAND_TIMEOUT_SECONDS)
    fake_run_command(monkeypatch, outputs=['\n'.join(uploads) + '\n', timed_out])

    worker.check_subscriptions()

    queued = [row.youtube_id for row in all_rows(db) if row.status == 'queued']
    assert queued == uploads[::-1]
    assert stored_subscription(db, subscription_id).last_checked > CHECKED_AT.replace(tzinfo=None)
    assert any(
        f'Subscription {subscription_id}: the whole-channel scan failed' in message
        for message in messages_at(caplog, logging.WARNING)
    )


# A failed scan during the background index backfill logs WARN and leaves a settled playlist as it
# was, with no ERROR; a later pass whose scan succeeds numbers the child.
def test_background_index_backfill_does_not_fail_parent(caplog, monkeypatch, db, make_job):
    caplog.set_level(logging.INFO)
    playlist_id = make_playlist(db, make_channel(db))
    parent = make_job(url=PLAYLIST_URL, request_type='playlist', playlist_id=playlist_id,
                      status='completed')
    child = video_row(make_job, V1, parent_id=parent.id, playlist_id=playlist_id,
                      status='completed')
    script_scans(monkeypatch, FAILED_SCAN, ([V2, V1], None))

    worker.backfill_all_playlist_indexes()

    row = stored(db, parent.id)
    assert (row.status, row.error) == ('completed', None)
    assert any(
        f'parent {parent.id}' in message and 'HTTP Error 503' in message
        for message in messages_at(caplog, logging.WARNING)
    )
    assert messages_at(caplog, logging.ERROR) == []

    worker.backfill_all_playlist_indexes()

    assert stored(db, child.id).playlist_index == 2


# A failed title lookup during the background metadata repair logs WARN and leaves a settled
# parent as it was, its error included.
def test_background_metadata_repair_does_not_fail_parent(caplog, monkeypatch, db, make_job):
    caplog.set_level(logging.INFO)
    parent = make_job(url=PLAYLIST_URL, request_type='playlist', title=None, status='completed')
    video_row(make_job, V1, parent_id=parent.id, status='completed')

    # Fail every yt-dlp call as a refused request does.
    def refuse(command, timeout=worker.COMMAND_TIMEOUT_SECONDS):
        raise subprocess.CalledProcessError(1, command, stderr='ERROR: HTTP Error 503')

    monkeypatch.setattr(worker, 'run_command', refuse)

    worker.populate_missing_parent_metadata()

    row = stored(db, parent.id)
    assert (row.status, row.error) == ('completed', None)
    assert any(
        f'Job {parent.id}: metadata discovery failed' in message
        for message in messages_at(caplog, logging.WARNING)
    )


# The parent metadata and playlist index backfills first run one subscription interval after the
# worker starts, never on its first pass.
def test_backfill_timer_not_backdated(monkeypatch, db):
    interval = worker.SUBSCRIPTION_CHECK_INTERVAL_SECONDS

    calls = run_worker_passes(monkeypatch, [0, interval - 1, interval])

    backfills = [call for call in calls if call[0] in LOOP_TASKS[3:]]
    assert backfills == [
        ('populate_missing_parent_metadata', interval), ('backfill_all_playlist_indexes', interval),
    ]


# Startup retires the rows that are no video after the youtube_id backfill and before the channel
# resequence.
def test_startup_retires_between_id_backfill_and_resequence(monkeypatch, db):
    housekeeping = []
    for name in ('recover_stuck_jobs', 'backfill_missing_youtube_ids', 'retire_invalid_scan_rows',
                 'resequence_all_channel_indexes'):
        monkeypatch.setattr(worker, name, lambda name=name: housekeeping.append(name))

    run_worker_passes(monkeypatch, [0])

    assert housekeeping == [
        'recover_stuck_jobs', 'backfill_missing_youtube_ids', 'retire_invalid_scan_rows',
        'resequence_all_channel_indexes',
    ]


# A subscription deleted while a check runs is skipped, and the check goes on with the next one.
def test_subscription_deleted_during_check_is_skipped(monkeypatch, empty_db, db):
    channel_id = make_channel(db)
    make_subscription(db, channel_id=channel_id, last_checked=CHECKED_AT)
    deleted = make_subscription(db, url=OTHER_CHANNEL_URL, last_checked=CHECKED_AT,
                                channel_id=make_channel(db, OTHER_CHANNEL_YOUTUBE_ID))
    last = make_subscription(db, 'playlist', channel_id=channel_id,
                             playlist_id=make_playlist(db, channel_id), last_checked=CHECKED_AT)
    scanned = []

    # Delete the second subscription from another connection during the first one's scan.
    def scan(url, playlist_items=None):
        scanned.append(url)
        if url == CHANNEL_URL:
            connection = sqlite3.connect(str(empty_db), isolation_level=None)
            try:
                connection.execute('DELETE FROM subscriptions WHERE id = ?', (deleted,))
            finally:
                connection.close()
        return ([V1] if url == CHANNEL_URL else [V3]), None

    monkeypatch.setattr(worker, 'scan_video_ids', scan)

    worker.check_subscriptions()

    assert scanned == [CHANNEL_URL, PLAYLIST_URL]
    assert stored_subscription(db, last).last_checked > CHECKED_AT.replace(tzinfo=None)


# A subscription deleted during its own scan is dropped with the rows its check made, and logged
# at INFO, not as a failed check.
def test_subscription_deleted_during_its_own_scan_is_dropped(caplog, monkeypatch, empty_db, db):
    caplog.set_level(logging.INFO)
    subscription_id = make_subscription(db, channel_id=make_channel(db), last_checked=CHECKED_AT)

    # Delete the subscription from another connection while it is scanned.
    def scan(url, playlist_items=None):
        connection = sqlite3.connect(str(empty_db), isolation_level=None)
        try:
            connection.execute('DELETE FROM subscriptions WHERE id = ?', (subscription_id,))
        finally:
            connection.close()
        return [V1], None

    monkeypatch.setattr(worker, 'scan_video_ids', scan)

    worker.check_subscriptions()

    assert all_rows(db) == []
    assert messages_at(caplog, logging.ERROR) == []
    dropped = f'Subscription {subscription_id} changed during its check; the next check repeats it'
    assert dropped in messages_at(caplog, logging.INFO)


# "Download existing now" on an enabled, resolved subscription sets it to "All existing" and
# clears last_checked, so its next check is a full first scan, and queues nothing itself. A
# disabled subscription is left as it was with a flash to enable it first, and an unresolved one
# keeps its message.
def test_download_existing_now_asks_for_full_rescan(client, db, make_job):
    channel_id = make_channel(db)
    enabled = make_subscription(db, channel_id=channel_id, backfill_existing=False,
                                last_checked=CHECKED_AT)
    skipped = channel_row(make_job, channel_id, V1, status=SKIPPED_STATUS)

    page = client.post(f'/subscriptions/{enabled}/backfill-now')

    subscription = stored_subscription(db, enabled)
    assert (subscription.backfill_existing, subscription.last_checked) == (True, None)
    assert stored(db, skipped.id).status == SKIPPED_STATUS
    assert (
        'queued at the next subscription check, which runs every 30 minutes between downloads'
        in page.text
    )

    other_channel_id = make_channel(db, OTHER_CHANNEL_YOUTUBE_ID)
    disabled = make_subscription(
        db, url=OTHER_CHANNEL_URL, channel_id=other_channel_id,
        backfill_existing=False, last_checked=CHECKED_AT, enabled=False,
    )
    paused = channel_row(make_job, other_channel_id, V2, status=SKIPPED_STATUS)

    page = client.post(f'/subscriptions/{disabled}/backfill-now')

    subscription = stored_subscription(db, disabled)
    assert subscription.backfill_existing is False and subscription.last_checked is not None
    assert stored(db, paused.id).status == SKIPPED_STATUS
    assert 'Enable the subscription first' in page.text

    unresolved = make_subscription(db, url='https://www.youtube.com/@unresolved',
                                   backfill_existing=False)

    page = client.post(f'/subscriptions/{unresolved}/backfill-now')

    assert 'has not finished its first scan yet' in page.text
    assert stored_subscription(db, unresolved).backfill_existing is False


# "Download existing now" clears last_checked even when it reads it empty: a first scan's stamp
# committed between the press's read and its write is undone, so the next check is still the
# backfill scan.
def test_download_existing_now_undoes_a_stamp_committed_meanwhile(client, empty_db, db):
    subscription_id = make_subscription(db, channel_id=make_channel(db), backfill_existing=False)
    stamped = []

    # Stamp the subscription from another connection just before the press's UPDATE runs, as a
    # baseline scan committing at that moment does.
    def stamp_first(conn, cursor, statement, parameters, context, executemany):
        if not statement.startswith('UPDATE subscriptions') or stamped:
            return
        stamped.append(statement)
        connection = sqlite3.connect(str(empty_db), isolation_level=None)
        try:
            stamp = (
                "UPDATE subscriptions SET last_checked = '2026-01-01 00:00:00.000000' "
                'WHERE id = ?'
            )
            connection.execute(stamp, (subscription_id,))
        finally:
            connection.close()

    event.listen(Engine, 'before_cursor_execute', stamp_first)
    try:
        client.post(f'/subscriptions/{subscription_id}/backfill-now')
    finally:
        event.remove(Engine, 'before_cursor_execute', stamp_first)

    assert stamped
    subscription = stored_subscription(db, subscription_id)
    assert (subscription.backfill_existing, subscription.last_checked) == (True, None)


# A "Download existing now" committed while a check runs is kept. Pressed during an earlier
# subscription's scan, it is read before this subscription's scan, which is then the full backfill
# scan; pressed during this subscription's own scan, the check leaves it unstamped and the next
# check runs the backfill scan. Either way the skipped video is queued.
@pytest.mark.parametrize('pressed_during, expected_scans', [
    ('earlier scan', [(OTHER_CHANNEL_URL, None), (OTHER_CHANNEL_URL, '1:50')]),
    ('own scan', [(OTHER_CHANNEL_URL, '1:50'), (OTHER_CHANNEL_URL, None)]),
])
def test_download_existing_now_during_check_is_kept(
    client, monkeypatch, db, make_job, pressed_during, expected_scans,
):
    make_subscription(db, channel_id=make_channel(db), backfill_existing=False,
                      last_checked=CHECKED_AT)
    other_channel_id = make_channel(db, OTHER_CHANNEL_YOUTUBE_ID)
    pressed = make_subscription(db, url=OTHER_CHANNEL_URL, channel_id=other_channel_id,
                                backfill_existing=False, last_checked=CHECKED_AT)
    skipped = channel_row(make_job, other_channel_id, V1, status=SKIPPED_STATUS)
    press_during = CHANNEL_URL if pressed_during == 'earlier scan' else OTHER_CHANNEL_URL
    presses = []
    calls = []

    # Answer each scan, pressing the button during the first scan of `press_during`.
    def scan(url, playlist_items=None):
        calls.append((url, playlist_items))
        if url == press_during and not presses:
            presses.append(client.post(f'/subscriptions/{pressed}/backfill-now'))
        return ([V1] if url == OTHER_CHANNEL_URL else [V2]), None

    monkeypatch.setattr(worker, 'scan_video_ids', scan)

    worker.check_subscriptions()
    worker.check_subscriptions()

    assert [call for call in calls if call[0] == OTHER_CHANNEL_URL] == expected_scans
    assert stored(db, skipped.id).status == 'queued'
    subscription = stored_subscription(db, pressed)
    assert subscription.backfill_existing is True and subscription.last_checked is not None


# The first scan after "Download existing now" reads the whole listing and queues the skipped rows
# it lists, and a new video between them, in scan order: a channel's listing of one kind oldest
# first, even from rows recorded newest first, and a playlist's in playlist order. The worker takes
# them in that order.
@pytest.mark.parametrize('kind', ['channel', 'playlist'])
def test_backfill_scan_requeues_skipped_rows_in_scan_order(client, monkeypatch, db, make_job, kind):
    channel_id = make_channel(db)
    if kind == 'channel':
        fields = {'channel_id': channel_id, 'group_by_channel': True}
        subscription_id = make_subscription(db, channel_id=channel_id, backfill_existing=False,
                                            last_checked=CHECKED_AT)
        url, listing, recorded = CHANNEL_URL, [V4, V3, V2, V1], [V4, V2, V1]
        expected = [V1, V2, V3, V4]
    else:
        playlist_id = make_playlist(db, channel_id)
        fields = {'channel_id': channel_id, 'playlist_id': playlist_id}
        subscription_id = make_subscription(db, 'playlist', channel_id=channel_id,
                                            playlist_id=playlist_id, backfill_existing=False,
                                            last_checked=CHECKED_AT)
        url, listing, recorded = PLAYLIST_URL, [V1, V4, V2, V3], [V3, V1, V2]
        expected = [V1, V4, V2, V3]
    for offset, youtube_id in enumerate(recorded):
        video_row(make_job, youtube_id, status=SKIPPED_STATUS,
                  created_at=CHECKED_AT + timedelta(seconds=offset), **fields)
    calls = script_scans(monkeypatch, (listing, None))

    client.post(f'/subscriptions/{subscription_id}/backfill-now')
    worker.check_subscriptions()

    assert calls == [(url, None)]
    assert taken_order(db) == expected


# A periodic scan never queues a skipped row, even for an "All existing" subscription. Only a first
# scan with backfill_existing set does, and only for the unparented rows it lists, one per video.
def test_only_first_backfill_scan_requeues_skipped(monkeypatch, db, make_job):
    channel_id = make_channel(db)
    subscription_id = make_subscription(db, channel_id=channel_id, last_checked=CHECKED_AT)
    listed = channel_row(make_job, channel_id, V1, status=SKIPPED_STATUS)
    duplicate = channel_row(make_job, channel_id, V1, status=SKIPPED_STATUS)
    unlisted = channel_row(make_job, channel_id, V3, status=SKIPPED_STATUS)
    parent = make_job(url=CHANNEL_URL, request_type='channel', channel_id=channel_id,
                      status='completed')
    adopted = channel_row(make_job, channel_id, V1, parent_id=parent.id, status=SKIPPED_STATUS)
    script_scans(monkeypatch, ([V2, V1], None), ([V2, V1], None))

    worker.check_subscriptions()

    assert stored(db, listed.id).status == SKIPPED_STATUS
    [new] = [row for row in all_rows(db) if row.youtube_id == V2]
    assert new.status == 'queued'

    update_subscription(db, subscription_id, last_checked=None)
    worker.check_subscriptions()

    assert stored(db, listed.id).status == 'queued'
    assert stored(db, duplicate.id).status == SKIPPED_STATUS
    assert stored(db, unlisted.id).status == SKIPPED_STATUS
    assert stored(db, adopted.id).status == SKIPPED_STATUS


# A resolved subscription to a channel's root URL is scanned through the channel's uploads
# playlist, one listing of every kind: in full at its first scan, then its 50 newest uploads.
# Resolving it reads its own URL. A subscription to one tab keeps reading that tab, and an id that
# is no channel's has no uploads playlist.
def test_channel_subscription_scans_uploads_playlist(monkeypatch, db):
    assert worker.uploads_playlist_url(UPLOADS_CHANNEL_ID) == UPLOADS_URL
    for no_channel in (None, '', CHANNEL_YOUTUBE_ID, 'PL' + UPLOADS_CHANNEL_ID[2:]):
        assert worker.uploads_playlist_url(no_channel) is None
    for root in (CHANNEL_URL, f'{CHANNEL_URL}/', 'https://m.youtube.com/@example',
                 f'https://www.youtube.com/channel/{UPLOADS_CHANNEL_ID}',
                 'https://www.youtube.com/c/example', 'https://www.youtube.com/user/example'):
        assert worker.is_channel_root_url(root), root
    for not_root in (f'{CHANNEL_URL}/videos', f'{CHANNEL_URL}/shorts', f'{CHANNEL_URL}/about',
                     f'https://www.youtube.com/channel/{UPLOADS_CHANNEL_ID}/streams',
                     PLAYLIST_URL, 'https://example.com/@example'):
        assert not worker.is_channel_root_url(not_root), not_root
    make_subscription(db, backfill_existing=False)
    tab_url = f'{CHANNEL_URL}/videos'
    # Its channel id has a real one's shape too, so only the tab keeps it off an uploads playlist.
    make_subscription(db, url=tab_url, channel_id=make_channel(db, 'UC' + 'tab' * 7 + '1'),
                      last_checked=CHECKED_AT)
    metadata = {**CHANNEL_METADATA, 'id': UPLOADS_CHANNEL_ID, 'channel_id': UPLOADS_CHANNEL_ID}
    commands = fake_run_command(monkeypatch, metadata=metadata, outputs={
        UPLOADS_URL: [f'{V1}\n', f'{V2}\n{V1}\n'], tab_url: [f'{V3}\n', f'{V3}\n'],
    })

    worker.check_subscriptions()
    worker.check_subscriptions()

    assert commands[0][-1] == CHANNEL_URL and '--dump-single-json' in commands[0]
    scans = scan_commands(commands)
    [first, periodic] = [command for command in scans if command[-1] != tab_url]
    assert first[-1] == UPLOADS_URL and '--playlist-items' not in first
    assert periodic[-3:] == ['--playlist-items', '1:50', UPLOADS_URL]
    assert [command[-1] for command in scans if command[-1] == tab_url] == [tab_url, tab_url]
    assert row_states(db) == [(V1, SKIPPED_STATUS), (V2, 'queued'), (V3, 'queued')]


# When the uploads playlist cannot be scanned, the check reads the subscription's own URL with the
# same limit. A fallback whose listing has uploads logs WARN the first time and INFO later, until
# the uploads playlist works again; an empty one, a channel with no upload, logs INFO only. When
# both scans fail, the check is one failure, unstamped, logged once with both reasons.
def test_uploads_playlist_failure_falls_back_to_channel_url(caplog, monkeypatch, db):
    caplog.set_level(logging.INFO)
    subscription_id = make_subscription(
        db, channel_id=make_channel(db, UPLOADS_CHANNEL_ID), last_checked=CHECKED_AT,
    )
    no_uploads = ([], 'ERROR: [youtube:tab] The playlist does not exist.')
    channel_down = ([], 'ERROR: [youtube:tab] HTTP Error 503: Service Unavailable')
    calls = script_scans(
        monkeypatch, no_uploads, ([], None), no_uploads, ([V1], None), no_uploads, ([V1], None),
        ([V1], None), no_uploads, ([V1], None), no_uploads, channel_down,
    )
    fallback = f'Subscription {subscription_id}: scanning its uploads playlist failed'

    # Count the fallback lines logged at `level`.
    def fallbacks_at(level: int) -> int:
        return len([m for m in messages_at(caplog, level) if m.startswith(fallback)])

    worker.check_subscriptions()

    assert (fallbacks_at(logging.WARNING), fallbacks_at(logging.INFO)) == (0, 1)

    worker.check_subscriptions()
    worker.check_subscriptions()

    assert calls == [(UPLOADS_URL, '1:50'), (CHANNEL_URL, '1:50')] * 3
    assert row_states(db) == [(V1, 'queued')]
    assert (fallbacks_at(logging.WARNING), fallbacks_at(logging.INFO)) == (1, 2)

    worker.check_subscriptions()
    worker.check_subscriptions()

    assert fallbacks_at(logging.WARNING) == 2
    stamped = stored_subscription(db, subscription_id).last_checked
    caplog.clear()

    worker.check_subscriptions()

    assert calls[-2:] == [(UPLOADS_URL, '1:50'), (CHANNEL_URL, '1:50')]
    assert stored_subscription(db, subscription_id).last_checked == stamped
    assert worker._subscription_failures == {subscription_id: 1}
    [warning] = messages_at(caplog, logging.WARNING)
    assert 'HTTP Error 503' in warning and 'uploads playlist: ERROR' in warning


# Through the uploads playlist a channel's back catalogue is one newest-first listing of every
# kind, so "Download existing now" queues it oldest first across videos, live streams and Shorts,
# where the channel's own URL lists the kinds one after another.
def test_backfill_through_uploads_playlist_is_oldest_first(client, monkeypatch, db, make_job):
    channel_id = make_channel(db, UPLOADS_CHANNEL_ID)
    subscription_id = make_subscription(db, channel_id=channel_id, backfill_existing=False,
                                        last_checked=CHECKED_AT)
    # Videos V4 and V2, then Shorts V3 and V1, each kind newest first, as the channel URL lists
    # them and a scan of it recorded them.
    by_kind = [V4, V2, V3, V1]
    for offset, youtube_id in enumerate(by_kind):
        channel_row(make_job, channel_id, youtube_id, status=SKIPPED_STATUS,
                    created_at=CHECKED_AT + timedelta(seconds=offset))
    listings = {UPLOADS_URL: [V4, V3, V2, V1], CHANNEL_URL: by_kind}
    calls = []

    # Answer each scan with the listing of the URL it reads.
    def scan(url, playlist_items=None):
        calls.append((url, playlist_items))
        return list(listings[url]), None

    monkeypatch.setattr(worker, 'scan_video_ids', scan)

    client.post(f'/subscriptions/{subscription_id}/backfill-now')
    worker.check_subscriptions()

    assert taken_order(db) == [V1, V2, V3, V4]
    assert calls == [(UPLOADS_URL, None)]
