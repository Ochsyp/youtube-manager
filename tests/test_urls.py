# -------------------------------------------------------
# URL helper tests
#
# Characterization tests for app.py's URL helpers: which hosts count as YouTube, how a pasted URL
# is classified, and the single canonical form it is stored and deduplicated under.
# -------------------------------------------------------

import pytest

from app import detect_youtube_type, normalize_url, youtube_domain

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
