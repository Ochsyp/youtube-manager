# -------------------------------------------------------
# URL helper tests
#
# Characterization tests for app.py's URL helpers: which hosts count as YouTube, how a pasted URL
# is classified and checked against the type requested, and the single canonical form it is
# stored and deduplicated under.
# -------------------------------------------------------

import pytest

from youtube_manager.app import (
    detect_youtube_type,
    normalize_url,
    validate_url_type,
    youtube_domain,
)

VIDEO_ID = 'dQw4w9WgXcQ'
LIST_ID = 'PLxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx'
WATCH = f'https://www.youtube.com/watch?v={VIDEO_ID}'
PLAYLIST = f'https://www.youtube.com/playlist?list={LIST_ID}'
CHANNEL_ID = 'UCuAXFkgsw1L7xaCfnd5JJOw'

# (pasted URL, kind detect_youtube_type reported, canonical form). Shared by the forms and
# idempotence tests below.
NORMALIZE_CASES = [
    (WATCH, 'video', WATCH),
    (f'youtube.com/watch?v={VIDEO_ID}&t=42s', 'video', WATCH),
    (f'http://www.youtube.com/watch?v={VIDEO_ID}', 'video', WATCH),
    (f'https://m.youtube.com/watch?v={VIDEO_ID}&feature=share', 'video', WATCH),
    (f'https://youtu.be/{VIDEO_ID}', 'video', WATCH),
    (f'https://youtu.be/{VIDEO_ID}/', 'video', WATCH),
    (f'https://youtu.be/{VIDEO_ID}?si=abc123', 'video', WATCH),
    (f'https://www.youtube.com/shorts/{VIDEO_ID}', 'video', WATCH),
    (f'https://www.youtube.com/embed/{VIDEO_ID}', 'video', WATCH),
    (f'https://www.youtube.com/live/{VIDEO_ID}?feature=share', 'video', WATCH),
    (f'https://www.youtube-nocookie.com/embed/{VIDEO_ID}', 'video', WATCH),
    (f'https://www.youtube.com/watch?v={VIDEO_ID}&list={LIST_ID}&index=3', 'video', WATCH),
    (f'https://www.youtube.com/watch?v={VIDEO_ID}&list={LIST_ID}&index=3', 'playlist', PLAYLIST),
    (f'{PLAYLIST}&si=abc123', 'playlist', PLAYLIST),
    ('https://www.youtube.com/@SomeChannel/', 'channel', 'https://www.youtube.com/@SomeChannel'),
    (
        f'https://www.youtube.com/channel/{CHANNEL_ID}',
        'channel',
        f'https://www.youtube.com/channel/{CHANNEL_ID}',
    ),
    ('https://example.com/some/path/', None, 'https://example.com/some/path'),
]


# Every accepted spelling of one target canonicalises to the same stored form, with timestamps,
# tracking parameters and playlist position dropped.
@pytest.mark.parametrize(('url', 'kind', 'expected'), NORMALIZE_CASES)
def test_normalize_url_forms(url, kind, expected):
    assert normalize_url(url, kind) == expected


# Normalising an already canonical URL leaves it unchanged, so a stored URL compares equal to a
# fresh paste of the same target.
@pytest.mark.parametrize(('url', 'kind'), [(url, kind) for url, kind, _ in NORMALIZE_CASES])
def test_normalize_url_idempotent(url, kind):
    once = normalize_url(url, kind)
    assert normalize_url(once, kind) == once


# A URL is classified by its path and query: v= wins over list= on a watch URL, the pre-handle
# channel forms still count as channels, and anything unrecognised or off YouTube is None.
@pytest.mark.parametrize(
    ('url', 'expected'),
    [
        (WATCH, 'video'),
        (f'youtube.com/watch?v={VIDEO_ID}', 'video'),
        (f'https://www.youtube.com/watch?v={VIDEO_ID}&list={LIST_ID}', 'video'),
        (f'https://youtu.be/{VIDEO_ID}', 'video'),
        (f'https://www.youtube.com/shorts/{VIDEO_ID}', 'video'),
        (f'https://www.youtube.com/embed/{VIDEO_ID}', 'video'),
        (f'https://www.youtube.com/live/{VIDEO_ID}', 'video'),
        (f'https://www.youtube.com/v/{VIDEO_ID}', 'video'),
        (f'https://www.youtube-nocookie.com/embed/{VIDEO_ID}', 'video'),
        (PLAYLIST, 'playlist'),
        ('https://www.youtube.com/@SomeChannel', 'channel'),
        ('https://www.youtube.com/@SomeChannel/videos', 'channel'),
        (f'https://www.youtube.com/channel/{CHANNEL_ID}', 'channel'),
        ('https://www.youtube.com/c/SomeChannel', 'channel'),
        ('https://www.youtube.com/user/SomeChannel', 'channel'),
        ('https://www.youtube.com/', None),
        ('https://www.youtube.com/feed/subscriptions', None),
        (f'https://example.com/watch?v={VIDEO_ID}', None),
    ],
)
def test_detect_youtube_type_cases(url, expected):
    assert detect_youtube_type(url) == expected


# A youtu.be share link, or a /shorts, /embed, /live or /v link, that carries the playlist it was
# shared from names one video: it is classified as a video before list= is read, so a video
# request for it is accepted, a channel request refused, and it normalises to the watch URL.
@pytest.mark.parametrize(
    'url',
    [
        f'https://youtu.be/{VIDEO_ID}?list={LIST_ID}',
        f'https://youtu.be/{VIDEO_ID}?si=abc123&list={LIST_ID}',
        f'https://www.youtube.com/shorts/{VIDEO_ID}?list={LIST_ID}',
        f'https://www.youtube.com/embed/{VIDEO_ID}?list={LIST_ID}',
        f'https://www.youtube.com/live/{VIDEO_ID}?list={LIST_ID}',
        f'https://www.youtube.com/v/{VIDEO_ID}?list={LIST_ID}',
    ],
)
def test_youtu_be_with_list_is_video(url):
    assert detect_youtube_type(url) == 'video'
    assert validate_url_type(url, 'video') is None
    assert validate_url_type(url, 'channel') == 'video'
    assert normalize_url(url, 'video') == WATCH


# Such a link, like a watch URL opened inside a playlist, is accepted for a playlist request too,
# and then normalises to the playlist alone, whichever video it was shared from.
@pytest.mark.parametrize(
    'url',
    [
        f'https://youtu.be/{VIDEO_ID}?list={LIST_ID}',
        f'https://youtu.be/{VIDEO_ID}?list={LIST_ID}&si=abc123',
        f'https://www.youtube.com/watch?v={VIDEO_ID}&list={LIST_ID}&index=3',
    ],
)
def test_youtu_be_with_list_as_playlist_normalises_to_list(url):
    assert validate_url_type(url, 'playlist') is None
    assert normalize_url(url, 'playlist') == PLAYLIST


# The playlist embed, and a share link with no video id, name no video: list= decides, so each is
# a playlist, refused for a video request, and normalises to the playlist.
@pytest.mark.parametrize(
    'url',
    [
        f'https://www.youtube.com/embed/videoseries?list={LIST_ID}',
        f'https://www.youtube-nocookie.com/embed/videoseries?list={LIST_ID}',
        f'https://youtu.be/?list={LIST_ID}',
    ],
)
def test_list_without_video_is_playlist(url):
    assert detect_youtube_type(url) == 'playlist'
    assert validate_url_type(url, 'video') == 'playlist'
    assert normalize_url(url) == normalize_url(url, 'playlist') == PLAYLIST


# Only the YouTube hosts themselves and their subdomains match. Lookalike hosts, userinfo
# prefixes and a YouTube name buried in the query or a longer label all resolve to None.
@pytest.mark.parametrize(
    ('url', 'expected'),
    [
        (WATCH, 'youtube.com'),
        ('youtube.com/@SomeChannel', 'youtube.com'),
        ('https://music.youtube.com/watch?v=x', 'youtube.com'),
        ('https://WWW.YouTube.com/watch?v=x', 'youtube.com'),
        (f'https://youtu.be/{VIDEO_ID}', 'youtu.be'),
        (f'https://www.youtube-nocookie.com/embed/{VIDEO_ID}', 'youtube-nocookie.com'),
        (f'https://youtube.com.example.net/watch?v={VIDEO_ID}', None),
        (f'https://notyoutube.com/watch?v={VIDEO_ID}', None),
        (f'https://fakeyoutu.be/{VIDEO_ID}', None),
        (f'https://youtu.be@example.net/{VIDEO_ID}', None),
        ('https://www.youtube.com:443@evil.example/watch', None),
        ('https://example.net/?next=https://www.youtube.com/', None),
        ('https://youtube.co/watch', None),
        ('', None),
    ],
)
def test_youtube_domain_rejects_lookalikes(url, expected):
    assert youtube_domain(url) == expected
