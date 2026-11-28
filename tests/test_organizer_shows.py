# -------------------------------------------------------
# Organizer show tests
#
# The library layout the README promises for playlists and channels: a show folder with
# tvshow.nfo and posters, each video an episode in "Season 01" with its NFO and thumbnail beside
# it. Covers a playlist's episode, a playlist under a subscribed channel's folder, a channel-
# grouped video, the wait for an episode number, the refile when a show's folder moves, and the
# re-fetch of a lost thumbnail. Each runs the organizer's own scan, as the worker does.
# -------------------------------------------------------

import io
import json
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest
import requests

from youtube_manager import organizer
from youtube_manager.database import Channel, Download, Playlist, Subscription

VIDEO_ID = 'dQw4w9WgXcQ'
CHANNEL_ID = 'UCexampleexampleexample1'
PLAYLIST_ID = 'PLexampleshow'
AVATAR_URL = 'https://yt3.ggpht.com/example-avatar'
THUMB_URL = f'https://i.ytimg.com/vi/{VIDEO_ID}/maxresdefault.jpg'
THUMB_BYTES = b'episode thumbnail'
AVATAR_BYTES = b'channel avatar'


# Stands in for requests.get and requests.post: a request no test arranged fails that test.
def refuse_network(url: str, *args, **kwargs) -> None:
    pytest.fail(f'unexpected network request to {url}')


# No sentinel, no Jellyfin and no network unless a test arranges a fetch.
@pytest.fixture(autouse=True)
def quiet_organizer(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv('LIBRARY_SENTINEL', raising=False)
    monkeypatch.setattr(organizer, '_library_error_logged_at', None)
    monkeypatch.setattr(organizer, 'JELLYFIN_URL', None)
    monkeypatch.setattr(organizer.requests, 'get', refuse_network)
    monkeypatch.setattr(organizer.requests, 'post', refuse_network)


# Serve `images` (URL to bytes) as image/jpeg answers to requests.get; any other URL fails the test.
# Returns the list of URLs fetched.
def serve_images(monkeypatch: pytest.MonkeyPatch, images: dict[str, bytes]) -> list[str]:
    fetched: list[str] = []

    def get(url, *args, **kwargs):
        if url not in images:
            pytest.fail(f'unexpected network request to {url}')
        fetched.append(url)
        response = requests.Response()
        response.status_code = 200
        response.headers['Content-Type'] = 'image/jpeg'
        response.raw = io.BytesIO(images[url])
        return response

    monkeypatch.setattr(organizer.requests, 'get', get)
    return fetched


# The channel and the playlist taken from it, stored; returns their ids.
def make_show(db, playlist: bool = True) -> tuple[int, int | None]:
    with db() as session:
        channel = Channel(
            youtube_id=CHANNEL_ID, name='Example Channel',
            url=f'https://www.youtube.com/channel/{CHANNEL_ID}',
            description='A channel for tests.', thumbnail=AVATAR_URL,
        )
        session.add(channel)
        session.flush()
        playlist_id = None
        if playlist:
            show = Playlist(
                youtube_id=PLAYLIST_ID, title='Example Show', channel_id=channel.id,
                description='A playlist for tests.',
            )
            session.add(show)
            session.flush()
            playlist_id = show.id
        session.commit()
        return channel.id, playlist_id


# A completed video in staging as yt-dlp leaves it: the video, its .jpg thumbnail and its
# .info.json sidecar, under yt-dlp's own name.
def staged_video(library, make_job, **fields) -> Download:
    source = library.staging / f'Episode Title [{VIDEO_ID}].mp4'
    source.write_bytes(b'video')
    source.with_suffix('.jpg').write_bytes(THUMB_BYTES)
    info = source.with_name(source.stem + '.info.json')
    info.write_text(json.dumps({'id': VIDEO_ID, 'title': 'Episode Title'}), encoding='utf-8')
    return make_job(
        status='completed', title='Episode Title', youtube_id=VIDEO_ID, upload_date='20260301',
        filename=str(source), metadata_file=str(info), **fields,
    )


# The job's row as stored now.
def stored(db, job_id: int) -> Download:
    with db() as session:
        return session.get(Download, job_id)


# The text of each named child of an NFO's root element.
def nfo_fields(path: Path, *names: str) -> dict[str, str | None]:
    root = ET.parse(path).getroot()
    return {name: root.findtext(name) for name in names}


# A playlist's video becomes an episode of the playlist's show: show folder named after the
# playlist with its id, the episode in Season 01 under the "Show - S01E03 - Title [id]" name, the
# episode NFO and the moved thumbnail beside it, and tvshow.nfo plus show and season posters taken
# from the first episode's thumbnail. Staging is left empty and the row points at the new file.
def test_playlist_video_is_filed_as_an_episode(db, make_job, library):
    channel_id, playlist_id = make_show(db)
    job = staged_video(library, make_job, playlist_id=playlist_id, channel_id=channel_id,
                       playlist_index=3)

    assert organizer.scan_and_process() == 1

    show_dir = library.media / f'Example Show [{PLAYLIST_ID}]'
    base = show_dir / 'Season 01' / f'Example Show - S01E03 - Episode Title [{VIDEO_ID}]'
    video, nfo, thumb = (base.with_name(base.name + suffix) for suffix in ('.mp4', '.nfo', '-thumb.jpg'))
    assert video.read_bytes() == b'video'
    assert thumb.read_bytes() == THUMB_BYTES
    assert nfo.exists() and ET.parse(nfo).getroot().tag == 'episodedetails'
    assert nfo_fields(nfo, 'title', 'showtitle', 'season', 'episode', 'aired', 'year', 'studio') == {
        'title': 'Episode Title', 'showtitle': 'Example Show', 'season': '1', 'episode': '3',
        'aired': '2026-03-01', 'year': '2026', 'studio': 'Example Channel',
    }
    assert ET.parse(nfo).getroot().findtext('uniqueid') == VIDEO_ID

    show_nfo = show_dir / 'tvshow.nfo'
    assert ET.parse(show_nfo).getroot().tag == 'tvshow'
    assert nfo_fields(show_nfo, 'title', 'plot', 'studio') == {
        'title': 'Example Show', 'plot': 'A playlist for tests.', 'studio': 'Example Channel',
    }
    assert ET.parse(show_nfo).getroot().findtext('uniqueid') == PLAYLIST_ID
    assert (show_dir / 'poster.jpg').read_bytes() == THUMB_BYTES
    assert (show_dir / 'Season 01' / 'poster.jpg').read_bytes() == THUMB_BYTES

    assert list(library.staging.iterdir()) == []
    row = stored(db, job.id)
    assert row.filename == str(video)
    assert row.organized_at is not None and row.metadata_file is None


# While the playlist's channel has a channel subscription, the playlist's show folder sits inside
# the channel's own folder, so the channel's playlists show up together.
def test_playlist_of_a_subscribed_channel_nests_in_its_folder(db, make_job, library):
    channel_id, playlist_id = make_show(db)
    with db() as session:
        session.add(Subscription(subscription_type='channel', channel_id=channel_id,
                                 url=f'https://www.youtube.com/channel/{CHANNEL_ID}'))
        session.commit()
    job = staged_video(library, make_job, playlist_id=playlist_id, channel_id=channel_id,
                       playlist_index=1)

    assert organizer.scan_and_process() == 1

    show_dir = library.media / f'Example Channel [{CHANNEL_ID}]' / f'Example Show [{PLAYLIST_ID}]'
    video = show_dir / 'Season 01' / f'Example Show - S01E01 - Episode Title [{VIDEO_ID}].mp4'
    assert stored(db, job.id).filename == str(video)
    assert video.exists() and (show_dir / 'tvshow.nfo').exists()


# A channel-grouped video becomes an episode of the channel's own show, numbered with four
# digits; the show's tvshow.nfo names the channel and its posters are the channel's avatar.
def test_channel_video_is_filed_as_an_episode_of_the_channel(monkeypatch, db, make_job, library):
    channel_id, _ = make_show(db, playlist=False)
    fetched = serve_images(monkeypatch, {AVATAR_URL: AVATAR_BYTES})
    job = staged_video(library, make_job, channel_id=channel_id, group_by_channel=True,
                       playlist_index=12)

    assert organizer.scan_and_process() == 1

    show_dir = library.media / f'Example Channel [{CHANNEL_ID}]'
    base = show_dir / 'Season 01' / f'Example Channel - S01E0012 - Episode Title [{VIDEO_ID}]'
    video, nfo, thumb = (base.with_name(base.name + suffix) for suffix in ('.mp4', '.nfo', '-thumb.jpg'))
    assert video.exists() and thumb.read_bytes() == THUMB_BYTES
    assert nfo_fields(nfo, 'showtitle', 'season', 'episode', 'aired') == {
        'showtitle': 'Example Channel', 'season': '1', 'episode': '12', 'aired': '2026-03-01',
    }
    show_nfo = show_dir / 'tvshow.nfo'
    assert nfo_fields(show_nfo, 'title', 'plot', 'studio') == {
        'title': 'Example Channel', 'plot': 'A channel for tests.', 'studio': 'Example Channel',
    }
    assert ET.parse(show_nfo).getroot().findtext('uniqueid') == CHANNEL_ID
    assert (show_dir / 'poster.jpg').read_bytes() == AVATAR_BYTES
    assert (show_dir / 'Season 01' / 'poster.jpg').read_bytes() == AVATAR_BYTES
    assert set(fetched) == {AVATAR_URL}
    assert stored(db, job.id).filename == str(video)


# Without its episode number yet, a playlist or channel video waits in staging rather than being
# filed under a guessed number; the next scan files it once the number is set.
@pytest.mark.parametrize('kind', ['playlist', 'channel'])
def test_video_waits_for_its_episode_number(monkeypatch, db, make_job, library, kind):
    channel_id, playlist_id = make_show(db, playlist=kind == 'playlist')
    serve_images(monkeypatch, {AVATAR_URL: AVATAR_BYTES})
    fields = (
        {'playlist_id': playlist_id} if kind == 'playlist' else {'group_by_channel': True}
    )
    job = staged_video(library, make_job, channel_id=channel_id, **fields)

    assert organizer.scan_and_process() == 0
    row = stored(db, job.id)
    assert row.organized_at is None and Path(row.filename).parent == library.staging

    with db() as session:
        session.get(Download, job.id).playlist_index = 2
        session.commit()
    assert organizer.scan_and_process() == 1
    assert Path(stored(db, job.id).filename).name.startswith(
        'Example Show - S01E02 - ' if kind == 'playlist' else 'Example Channel - S01E0002 - '
    )


# Subscribing to a playlist's channel after its episodes were filed moves the show into the
# channel's folder on the next scan, and the old show folder goes once nothing of it is left.
def test_filed_show_moves_when_its_channel_is_subscribed(db, make_job, library):
    channel_id, playlist_id = make_show(db)
    job = staged_video(library, make_job, playlist_id=playlist_id, channel_id=channel_id,
                       playlist_index=1)
    assert organizer.scan_and_process() == 1
    old_show = library.media / f'Example Show [{PLAYLIST_ID}]'
    assert old_show.is_dir()

    with db() as session:
        session.add(Subscription(subscription_type='channel', channel_id=channel_id,
                                 url=f'https://www.youtube.com/channel/{CHANNEL_ID}'))
        session.commit()
    assert organizer.scan_and_process() == 1

    new_show = library.media / f'Example Channel [{CHANNEL_ID}]' / f'Example Show [{PLAYLIST_ID}]'
    video = new_show / 'Season 01' / f'Example Show - S01E01 - Episode Title [{VIDEO_ID}].mp4'
    assert stored(db, job.id).filename == str(video)
    assert video.read_bytes() == b'video'
    assert video.with_name(video.stem + '-thumb.jpg').read_bytes() == THUMB_BYTES
    assert video.with_suffix('.nfo').exists()
    assert not old_show.exists()


# A filed video whose thumbnail went missing gets it fetched again from the URL on record, and
# keeps its place in the library: the row stays organized and nothing else is rewritten.
def test_lost_thumbnail_is_fetched_again(monkeypatch, db, make_job, library):
    channel_id, playlist_id = make_show(db)
    job = staged_video(library, make_job, playlist_id=playlist_id, channel_id=channel_id,
                       playlist_index=1, thumbnail=THUMB_URL)
    assert organizer.scan_and_process() == 1
    video = Path(stored(db, job.id).filename)
    thumb = video.with_name(video.stem + '-thumb.jpg')
    thumb.unlink()
    organized_at = stored(db, job.id).organized_at

    fetched = serve_images(monkeypatch, {THUMB_URL: b'fetched again'})
    assert organizer.scan_and_process() == 0

    assert fetched == [THUMB_URL]
    assert thumb.read_bytes() == b'fetched again'
    row = stored(db, job.id)
    assert row.filename == str(video) and row.organized_at == organized_at
