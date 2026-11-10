# -------------------------------------------------------
# Episode numbering tests
#
# Pins how the worker numbers episodes and orders its queue: a channel's filed episodes keep their
# number while new ones take the next free one, playlist rows take their scan position or the next
# number past the highest held, one video keeps one number, a library file is refreshed rather
# than downloaded again, and a channel backfill runs oldest first. Database only: nothing runs
# yt-dlp.
# -------------------------------------------------------

import json
import logging
from datetime import datetime, timezone

import pytest

from youtube_manager import organizer, worker
from youtube_manager.database import Channel, Download, Playlist, Subscription
from youtube_manager.jobstate import SKIPPED_STATUS
from youtube_manager.media import DOWNLOAD_PATH, MEDIA_PATH

CHANNEL_YOUTUBE_ID = 'UCnumberingtest'
CHANNEL_URL = 'https://www.youtube.com/@numberingtest'
PLAYLIST_YOUTUBE_ID = 'PLnumberingtest'
PLAYLIST_URL = f'https://www.youtube.com/playlist?list={PLAYLIST_YOUTUBE_ID}'
V1, V2, V3, V4, V5, V6 = (f'aaaaaaaaaa{n}' for n in range(1, 7))

# When the filed rows below were filed.
FILED_AT = datetime(2026, 1, 1, tzinfo=timezone.utc)


# The channel every test video belongs to, and its id.
@pytest.fixture
def channel_id(db) -> int:
    with db() as session:
        channel = Channel(
            youtube_id=CHANNEL_YOUTUBE_ID,
            name='Test channel',
            url=f'https://www.youtube.com/channel/{CHANNEL_YOUTUBE_ID}',
        )
        session.add(channel)
        session.commit()
        return channel.id


# A playlist of that channel, and its id.
@pytest.fixture
def playlist_id(db, channel_id) -> int:
    with db() as session:
        playlist = Playlist(
            youtube_id=PLAYLIST_YOUTUBE_ID, title='Test playlist', channel_id=channel_id,
        )
        session.add(playlist)
        session.commit()
        return playlist.id


# Insert a subscription resolved to `channel_id` (and `playlist_id`). Returns its id.
def make_subscription(db, channel_id: int, playlist_id: int | None = None) -> int:
    with db() as session:
        subscription = Subscription(
            subscription_type='playlist' if playlist_id else 'channel',
            url=PLAYLIST_URL if playlist_id else CHANNEL_URL,
            channel_id=channel_id,
            playlist_id=playlist_id,
        )
        session.add(subscription)
        session.commit()
        return subscription.id


# Record one scan's `video_ids`, in scan order, and commit, as check_subscriptions does.
def scan(db, subscription_id: int, video_ids: list[str], queue: bool = True) -> None:
    with db() as session:
        subscription = session.get(Subscription, subscription_id)
        worker.create_download_jobs_for_subscription(session, subscription, video_ids, queue=queue)
        session.commit()


# Resequence the channel and commit, as the callers of resequence_channel_index do.
def resequence(db, channel_id: int) -> None:
    with db() as session:
        worker.resequence_channel_index(session, channel_id)
        session.commit()


# Insert a video row for `youtube_id`, its URL built from the id, with `fields` on top.
def video(make_job, youtube_id: str, **fields) -> Download:
    url = f'https://www.youtube.com/watch?v={youtube_id}'
    return make_job(url=url, youtube_id=youtube_id, **fields)


# Insert a video row grouped under the channel `channel_id`, with `fields` on top.
def channel_video(make_job, channel_id: int, youtube_id: str, **fields) -> Download:
    return video(make_job, youtube_id, channel_id=channel_id, group_by_channel=True, **fields)


# Insert a playlist download of PLAYLIST_URL, the parent its children hang off.
def playlist_parent(make_job, playlist_id: int) -> Download:
    return make_job(
        url=PLAYLIST_URL, request_type='playlist', playlist_id=playlist_id, status='completed',
    )


# Where yt-dlp leaves `youtube_id` in staging.
def staging_path(youtube_id: str) -> str:
    return str(DOWNLOAD_PATH / f'Video [{youtube_id}].mp4')


# Where the organizer files `youtube_id` as the channel's episode `number`.
def episode_path(number: int, youtube_id: str, suffix: str = '.mp4') -> str:
    name = f'Test channel - S01E{number:04d} - Video [{youtube_id}]{suffix}'
    return str(MEDIA_PATH / 'Test channel' / 'Season 01' / name)


# Insert a video the organizer filed as the channel's episode `number`.
def filed_episode(
    make_job, channel_id: int, youtube_id: str, number: int, upload_date: str,
) -> Download:
    return channel_video(
        make_job, channel_id, youtube_id, playlist_index=number, upload_date=upload_date,
        status='completed', organized_at=FILED_AT, filename=episode_path(number, youtube_id),
    )


# yt-dlp's info dict for one of the channel's videos, as much of it as apply_metadata reads.
def video_metadata(youtube_id: str, upload_date: str) -> dict:
    return {
        'id': youtube_id,
        'title': f'Video {youtube_id}',
        'upload_date': upload_date,
        'channel_id': CHANNEL_YOUTUBE_ID,
        'channel': 'Test channel',
    }


# The stored row for `job_id`, read in a fresh session.
def stored(db, job_id: int) -> Download:
    with db() as session:
        return session.get(Download, job_id)


# The stored playlist_index of each row in `rows`, in order.
def stored_indexes(db, *rows: Download) -> list[int | None]:
    return [stored(db, row.id).playlist_index for row in rows]


# Every row of `youtube_id`, lowest id first.
def rows_of(db, youtube_id: str) -> list[Download]:
    with db() as session:
        query = session.query(Download).filter(Download.youtube_id == youtube_id)
        return query.order_by(Download.id).all()


# The one row recorded for `youtube_id`.
def only_row(db, youtube_id: str) -> Download:
    [row] = rows_of(db, youtube_id)
    return row


# Rows created in one flush can share created_at, and the lower id, added first, then goes first.
# The statuses make SQLite's status index list the higher id first, so only the tie-break decides.
def test_get_next_job_breaks_created_at_ties_by_id(db, make_job):
    created_at = datetime(2026, 1, 1, tzinfo=timezone.utc)
    first = video(make_job, V1, status='redownload', filename=staging_path(V1),
                  created_at=created_at)
    video(make_job, V2, status='queued', created_at=created_at)

    assert worker.get_next_job().id == first.id


# A playlist subscription's new rows are created in scan order and numbered by scan position, the
# skipped baseline of a first scan included.
@pytest.mark.parametrize('queue', [True, False], ids=['queued', 'baseline'])
def test_playlist_subscription_rows_get_index(db, channel_id, playlist_id, queue):
    subscription_id = make_subscription(db, channel_id, playlist_id)

    scan(db, subscription_id, [V1, V2, V3], queue=queue)

    rows = [only_row(db, youtube_id) for youtube_id in (V1, V2, V3)]
    assert [row.playlist_index for row in rows] == [1, 2, 3]
    assert [row.id for row in rows] == sorted(row.id for row in rows)
    assert {row.status for row in rows} == {'queued' if queue else SKIPPED_STATUS}


# A row recorded before playlist rows were numbered, already in the playlist with no index, gets
# its scan position at the next scan although nothing else about it changes, logging no WARN.
def test_adopted_row_without_index_gets_scan_position(
    caplog, db, make_job, channel_id, playlist_id,
):
    caplog.set_level(logging.INFO)
    subscription_id = make_subscription(db, channel_id, playlist_id)
    recorded = video(make_job, V2, channel_id=channel_id, playlist_id=playlist_id,
                     status='completed')

    scan(db, subscription_id, [V1, V2])

    row = stored(db, recorded.id)
    assert row.playlist_index == 2
    assert (row.channel_id, row.playlist_id) == (channel_id, playlist_id)
    assert row.group_by_channel is False
    assert only_row(db, V1).playlist_index == 1
    assert [record for record in caplog.records if record.levelno >= logging.WARNING] == []


# A scan position another video's row holds, here a child of a playlist download, gives the next
# number past the highest held; that number is held from then on, so the next video's own position
# is taken as well and it gets the number after.
def test_playlist_index_collision_takes_next_free(db, make_job, channel_id, playlist_id):
    subscription_id = make_subscription(db, channel_id, playlist_id)
    parent = playlist_parent(make_job, playlist_id)
    holder = video(make_job, V6, parent_id=parent.id, playlist_id=playlist_id, playlist_index=1,
                   status='completed')

    scan(db, subscription_id, [V1, V2])

    assert only_row(db, V1).playlist_index == 2
    assert only_row(db, V2).playlist_index == 3
    assert stored(db, holder.id).playlist_index == 1


# A number a row already holds in the playlist stays, and a position held only by a row of the
# same video, here its child of a playlist download, is that video's to reuse rather than taken.
def test_playlist_scan_keeps_index_and_reuses_own_position(db, make_job, channel_id, playlist_id):
    subscription_id = make_subscription(db, channel_id, playlist_id)
    parent = playlist_parent(make_job, playlist_id)
    child = video(make_job, V2, parent_id=parent.id, playlist_id=playlist_id, playlist_index=2,
                  status='completed')
    holding = video(make_job, V1, channel_id=channel_id, playlist_id=playlist_id, playlist_index=7,
                    status='completed')

    scan(db, subscription_id, [V1, V2])

    assert stored(db, holding.id).playlist_index == 7
    [subscription_row] = [row for row in rows_of(db, V2) if row.parent_id is None]
    assert subscription_row.playlist_index == 2
    assert stored(db, child.id).playlist_index == 2


# A row the scan moves into the playlist leaves its number behind with the show it came from, and
# takes its scan position here; organized_at is cleared so it is refiled under this show.
def test_row_joining_playlist_gets_new_number(db, make_job, channel_id, playlist_id):
    with db() as session:
        other = Playlist(youtube_id='PLnumberingother', title='Other playlist',
                         channel_id=channel_id)
        session.add(other)
        session.commit()
        other_id = other.id
    subscription_id = make_subscription(db, channel_id, playlist_id)
    moved = video(make_job, V2, channel_id=channel_id, playlist_id=other_id, playlist_index=9,
                  status='completed', organized_at=FILED_AT)

    scan(db, subscription_id, [V1, V2])

    row = stored(db, moved.id)
    assert (row.playlist_id, row.playlist_index, row.organized_at) == (playlist_id, 2, None)


# A channel scan lists newest first. Its rows are created oldest first, so the worker takes the
# oldest video's row first.
def test_channel_subscription_rows_created_oldest_first(db, channel_id):
    subscription_id = make_subscription(db, channel_id)

    scan(db, subscription_id, [V3, V2, V1])

    with db() as session:
        created = [row.youtube_id for row in session.query(Download).order_by(Download.id)]
    assert created == [V1, V2, V3]
    assert worker.get_next_job().youtube_id == V1


# A channel backfill run as the worker runs it, each video dated by its metadata and filed before
# the next starts, files every video under its own number, oldest first.
def test_channel_backfill_files_each_video_under_its_own_number(db, channel_id):
    subscription_id = make_subscription(db, channel_id)
    upload_dates = {V3: '20200103', V2: '20200102', V1: '20200101'}
    scan(db, subscription_id, list(upload_dates))

    filed = []
    for _ in upload_dates:
        job = worker.get_next_job()
        worker.apply_metadata(job, video_metadata(job.youtube_id, upload_dates[job.youtube_id]))
        with db() as session:
            row = session.get(Download, job.id)
            row.status = 'completed'
            row.organized_at = FILED_AT
            session.commit()
            filed.append((row.youtube_id, row.playlist_index))

    assert filed == [(V1, 1), (V2, 2), (V3, 3)]
    assert worker.get_next_job() is None


# The startup resequence leaves every filed number as it is, numbers already duplicated on disk
# included and organized_at alone keeping V2's, whose recorded path is gone. The unfiled dated
# videos are numbered after the highest kept one, by upload_date: a number left over from the old
# full resequence, V5's 9 on a staging file, counts for nothing. A second pass changes nothing.
def test_resequence_never_renumbers_filed_videos(db, make_job, channel_id):
    filed = [
        filed_episode(make_job, channel_id, V1, 1, '20200103'),
        channel_video(make_job, channel_id, V2, playlist_index=1, upload_date='20200101',
                      status='completed', organized_at=FILED_AT),
        filed_episode(make_job, channel_id, V3, 2, '20200102'),
    ]
    newer = channel_video(make_job, channel_id, V4, upload_date='20200105')
    older = channel_video(make_job, channel_id, V5, playlist_index=9, upload_date='20200104',
                          status='completed', filename=staging_path(V5))

    worker.resequence_all_channel_indexes()

    assert stored_indexes(db, *filed) == [1, 1, 2]
    assert stored_indexes(db, older, newer) == [3, 4]

    worker.resequence_all_channel_indexes()

    assert stored_indexes(db, *filed, older, newer) == [1, 1, 2, 3, 4]


# A video dated only after newer ones were filed takes the next free number, not the first, so no
# filed episode moves.
def test_late_older_video_gets_next_free_number(db, make_job, channel_id):
    filed = [
        filed_episode(make_job, channel_id, youtube_id, number, f'2020020{number}')
        for number, youtube_id in enumerate((V1, V2, V3), start=1)
    ]
    late = channel_video(make_job, channel_id, V4, status='metadata')

    worker.apply_metadata(late, video_metadata(V4, '20200101'))

    assert stored(db, late.id).playlist_index == 4
    assert stored_indexes(db, *filed) == [1, 2, 3]


# A parent's redownload clears organized_at on every child but leaves the files in the library,
# named with their numbers: those numbers stay, even for a file transcoded to another extension,
# and a new video takes the next one.
def test_library_file_awaiting_refile_keeps_number(db, make_job, channel_id):
    parent = make_job(url=CHANNEL_URL, request_type='channel', channel_id=channel_id,
                      status='redownload')
    awaiting = [
        channel_video(make_job, channel_id, V1, parent_id=parent.id, playlist_index=1,
                      upload_date='20200102', status='redownload',
                      filename=episode_path(1, V1, '.mkv')),
        channel_video(make_job, channel_id, V2, parent_id=parent.id, playlist_index=2,
                      upload_date='20200103', status='redownload', filename=episode_path(2, V2)),
    ]
    new = channel_video(make_job, channel_id, V3, parent_id=parent.id, upload_date='20200101')

    resequence(db, channel_id)

    assert stored_indexes(db, *awaiting) == [1, 2]
    assert stored(db, new.id).playlist_index == 3


# Deleted, cancelled, skipped and failed rows keep whatever index they have and take no number.
# Their numbers count, so a deleted episode's 5 is never handed out again and the new video, older
# than all of them, gets 6; a failed download keeps its 3 until it is retried.
def test_inert_rows_neither_renumbered_nor_shift_others(db, make_job, channel_id):
    deleted = channel_video(make_job, channel_id, V1, playlist_index=5, upload_date='20200101',
                            status='deleted')
    filed = filed_episode(make_job, channel_id, V2, 2, '20200102')
    cancelled = channel_video(make_job, channel_id, V3, upload_date='20200103', status='cancelled')
    skipped = channel_video(make_job, channel_id, V4, upload_date='20200104', status=SKIPPED_STATUS)
    failed = channel_video(make_job, channel_id, V6, playlist_index=3, upload_date='20200105',
                           status='failed')
    new = channel_video(make_job, channel_id, V5, upload_date='20191231')

    resequence(db, channel_id)

    assert stored_indexes(db, deleted, filed, cancelled, skipped, failed) == [5, 2, None, None, 3]
    assert stored(db, new.id).playlist_index == 6


# A channel-grouped row with a playlist is that playlist's episode: resequence leaves its index
# alone and does not count it.
def test_resequence_leaves_rows_with_playlist_alone(db, make_job, channel_id, playlist_id):
    episode = channel_video(make_job, channel_id, V1, playlist_id=playlist_id, playlist_index=7,
                            upload_date='20200101', status='completed')
    grouped = channel_video(make_job, channel_id, V2, upload_date='20200102')

    resequence(db, channel_id)

    assert stored_indexes(db, episode, grouped) == [7, 1]


# A channel subscription adopting a row with no playlist drops the number it had from another
# show and numbers it after the channel's filed episodes; an adopted row with a playlist keeps its
# index, its position in the playlist it is still filed under.
def test_channel_adoption_renumbers_row_without_playlist(db, make_job, channel_id, playlist_id):
    subscription_id = make_subscription(db, channel_id)
    filed = filed_episode(make_job, channel_id, V1, 5, '20200105')
    movie = video(make_job, V2, channel_id=channel_id, playlist_index=3, upload_date='20200101',
                  status='completed', organized_at=FILED_AT,
                  filename=str(MEDIA_PATH / f'Video [{V2}]' / f'Video [{V2}].mp4'))
    episode = video(make_job, V3, channel_id=channel_id, playlist_id=playlist_id, playlist_index=4,
                    upload_date='20200102', status='completed', organized_at=FILED_AT)

    scan(db, subscription_id, [V1, V3, V2])

    adopted = stored(db, movie.id)
    assert (adopted.group_by_channel, adopted.organized_at) == (True, None)
    assert adopted.playlist_index == 6
    assert stored_indexes(db, episode, filed) == [4, 5]


# A taken position gets the number past the highest held, not the lowest unused one: with 1 and 5
# held, V1's taken position 1 gives 6, while V2's free position 2 stays its own.
def test_taken_position_gets_number_past_highest_held(db, make_job, channel_id, playlist_id):
    subscription_id = make_subscription(db, channel_id, playlist_id)
    parent = playlist_parent(make_job, playlist_id)
    video(make_job, V6, parent_id=parent.id, playlist_id=playlist_id, playlist_index=1,
          status='completed')
    video(make_job, V5, channel_id=channel_id, playlist_id=playlist_id, playlist_index=5,
          status='completed')

    scan(db, subscription_id, [V1, V2])

    assert (only_row(db, V1).playlist_index, only_row(db, V2).playlist_index) == (6, 2)


# A video that already holds a number in the playlist keeps it for every further row, so one video
# is one episode: the subscription's own row of V1, whose only other row is a playlist download's
# child, takes that child's 7 although position 1 is free. Two rows of V2, whose position another
# video holds, share the one number they get.
def test_video_keeps_the_number_it_holds_in_the_playlist(db, make_job, channel_id, playlist_id):
    subscription_id = make_subscription(db, channel_id, playlist_id)
    parent = playlist_parent(make_job, playlist_id)
    video(make_job, V1, parent_id=parent.id, playlist_id=playlist_id, playlist_index=7,
          status='completed')
    video(make_job, V6, parent_id=parent.id, playlist_id=playlist_id, playlist_index=2,
          status='completed')
    twins = [
        video(make_job, V2, channel_id=channel_id, playlist_id=playlist_id, status='completed')
        for _ in range(2)
    ]

    scan(db, subscription_id, [V1, V2])

    [subscription_row] = [row for row in rows_of(db, V1) if row.parent_id is None]
    assert subscription_row.playlist_index == 7
    assert stored_indexes(db, *twins) == [8, 8]


# A playlist download of a subscribed playlist numbers around what the subscription recorded: the
# rows it adopts keep the numbers they hold in the playlist, and a new video whose position a
# removed video still holds gets the number past the highest, so no two videos share an episode.
def test_playlist_download_numbers_around_held_numbers(db, make_job, channel_id, playlist_id):
    subscription_id = make_subscription(db, channel_id, playlist_id)
    scan(db, subscription_id, [V1, V2, V3])
    parent = playlist_parent(make_job, playlist_id)

    worker.create_child_jobs(parent, [V1, V3, V4])

    numbers = [only_row(db, youtube_id).playlist_index for youtube_id in (V1, V2, V3, V4)]
    assert numbers == [1, 2, 3, 4]
    assert {only_row(db, youtube_id).parent_id for youtube_id in (V1, V3, V4)} == {parent.id}


# A library file not named as this channel's episode keeps nothing: a movie the channel adopted,
# numbered 2 by an earlier pass while it awaits its refile, is renumbered with the unfiled videos
# by upload date, behind an older one, instead of holding 2 for good. Neither a playlist episode's
# two-digit name nor a movie whose title contains the pattern counts.
@pytest.mark.parametrize('filed_as', [
    MEDIA_PATH / f'Video [{V3}]' / f'Video [{V3}].mp4',
    MEDIA_PATH / 'Test playlist' / 'Season 01' / f'Test playlist - S01E02 - Video [{V3}].mp4',
    MEDIA_PATH / f'Survival - S01E0002 - Nether [{V3}]'
    / f'Survival - S01E0002 - Nether [{V3}].mp4',
], ids=['movie', 'playlist-episode', 'title-with-pattern'])
def test_library_file_without_number_keeps_nothing(db, make_job, channel_id, filed_as):
    filed = filed_episode(make_job, channel_id, V1, 1, '20200101')
    movie = channel_video(make_job, channel_id, V3, playlist_index=2, upload_date='20200103',
                          status='completed', filename=str(filed_as))
    older = channel_video(make_job, channel_id, V2, upload_date='20200102', status='completed',
                          filename=staging_path(V2))

    resequence(db, channel_id)

    assert stored_indexes(db, filed, older, movie) == [1, 2, 3]


# Rows of one video share one number: a second row of a filed video takes the filed number, and
# two unfiled rows of a new video take one new number between them.
def test_rows_of_one_video_share_channel_number(db, make_job, channel_id):
    filed = filed_episode(make_job, channel_id, V1, 1, '20200101')
    repeat = channel_video(make_job, channel_id, V1, upload_date='20200101', status='queued')
    new = [channel_video(make_job, channel_id, V2, upload_date='20200102') for _ in range(2)]

    resequence(db, channel_id)

    assert stored_indexes(db, filed, repeat, *new) == [1, 1, 2, 2]


# A queued video whose recorded file is in the library, as "Retry all" leaves a failed redownload,
# is refreshed in place and never downloaded again; one whose file is in staging is downloaded.
def test_queued_video_with_library_file_is_refreshed_in_place(monkeypatch, make_job, channel_id):
    calls = []

    # Stands in for the in-place refresh: records the job and reports success.
    def refresh(job: Download) -> bool:
        calls.append(('refresh', job.id))
        return True

    monkeypatch.setattr(worker, 'redownload_or_rename', refresh)
    monkeypatch.setattr(worker, 'download_video', lambda job: calls.append(('download', job.id)))
    in_library = channel_video(make_job, channel_id, V1, playlist_index=1, status='queued',
                               filename=episode_path(1, V1))
    in_staging = channel_video(make_job, channel_id, V2, status='queued', filename=staging_path(V2))

    worker.process_job(in_library)
    worker.process_job(in_staging)

    assert calls == [('refresh', in_library.id), ('download', in_staging.id)]


# A channel download of a subscribed channel adopts the episodes the subscription filed: they keep
# their numbers and stay filed, even where the numbers do not follow upload order.
def test_channel_download_keeps_numbers_of_filed_episodes_it_adopts(db, make_job, channel_id):
    filed = [
        filed_episode(make_job, channel_id, V1, 1, '20200102'),
        filed_episode(make_job, channel_id, V2, 2, '20200103'),
        filed_episode(make_job, channel_id, V3, 3, '20200104'),
        filed_episode(make_job, channel_id, V4, 4, '20200101'),
    ]
    parent = make_job(url=CHANNEL_URL, request_type='channel', channel_id=channel_id,
                      status='expanding')

    worker.create_child_jobs(parent, [V3, V2, V1, V4])

    assert stored_indexes(db, *filed) == [1, 2, 3, 4]
    rows = [stored(db, row.id) for row in filed]
    assert {row.parent_id for row in rows} == {parent.id}
    assert all(row.organized_at is not None for row in rows)


# A video downloaded again because its library file went missing still carries that file's name
# when its metadata is applied, so it comes back under its old number, not the next one.
def test_redownload_of_missing_library_file_keeps_number(db, make_job, library, channel_id):
    filed_episode(make_job, channel_id, V1, 1, '20200101')
    filed_episode(make_job, channel_id, V3, 3, '20200103')
    missing = channel_video(make_job, channel_id, V2, playlist_index=2, upload_date='20200102',
                            status='downloading', filename=episode_path(2, V2))
    media = library.staging / f'Video [{V2}].mp4'
    media.write_bytes(b'video')
    media.with_name(media.stem + '.info.json').write_text(
        json.dumps(video_metadata(V2, '20200102')), encoding='utf-8',
    )

    worker.record_completed_download(missing, media)

    row = stored(db, missing.id)
    assert (row.playlist_index, row.filename, row.status) == (2, str(media), 'completed')


# While LIBRARY_SENTINEL reports the library missing, a video whose library file cannot be found is
# not downloaded again: the job fails with the reason, and a later retry finds the file. With the
# library back, the same job is downloaded.
def test_missing_library_file_not_downloaded_while_library_unavailable(
    monkeypatch, tmp_path, db, make_job, library, channel_id,
):
    downloads = []
    sentinel = tmp_path / 'library-mounted'
    monkeypatch.setenv('LIBRARY_SENTINEL', str(sentinel))
    monkeypatch.setattr(organizer, '_library_error_logged_at', None)
    monkeypatch.setattr(worker, 'discover_metadata',
                        lambda job, **kwargs: video_metadata(V2, '20200102'))
    monkeypatch.setattr(worker, 'resolve_expected_output_path', lambda job, *args: None)
    monkeypatch.setattr(worker, 'download_video', lambda job: downloads.append(job.id))
    job = channel_video(make_job, channel_id, V2, playlist_index=2, upload_date='20200102',
                        status='queued', filename=episode_path(2, V2))

    worker.process_job(job)

    row = stored(db, job.id)
    assert (row.status, downloads) == ('failed', [])
    assert 'Library unavailable' in row.error

    sentinel.write_text('')
    worker.process_job(job)

    assert downloads == [job.id]


# Where a video's rows disagree, the number it is filed under wins over a deleted row's: a new row
# of V1 shares the filed 4, not the deleted row's 3, and in the playlist V2 gets its filed 5 again.
def test_shared_number_prefers_the_filed_one(db, make_job, channel_id, playlist_id):
    deleted = channel_video(make_job, channel_id, V1, playlist_index=3, upload_date='20200101',
                            status='deleted')
    filed = filed_episode(make_job, channel_id, V1, 4, '20200101')
    new = channel_video(make_job, channel_id, V1, upload_date='20200101')
    video(make_job, V2, playlist_id=playlist_id, playlist_index=2, status='deleted')
    video(make_job, V2, playlist_id=playlist_id, playlist_index=5, status='completed',
          organized_at=FILED_AT)

    resequence(db, channel_id)
    with db() as session:
        assigned = worker.PlaylistNumbering(session, playlist_id).assign(2, V2)

    assert stored_indexes(db, deleted, filed, new) == [3, 4, 4]
    assert assigned == 5
