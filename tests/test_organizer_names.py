# -------------------------------------------------------
# Organizer naming tests
#
# Characterization tests for the pieces organizer.py builds library filenames from: the sanitized
# title, the [id] tag and its length cap, and the tag used when a video has no YouTube id.
# -------------------------------------------------------

import pytest

from youtube_manager.database import Download
from youtube_manager.organizer import (
    MAX_NAME_LENGTH,
    append_id_tag,
    sanitize_filename,
    video_id_tag,
)

VIDEO_ID = 'dQw4w9WgXcQ'


# Unsafe characters are dropped, whitespace collapses, accents fold to ASCII and anything else
# non-ASCII disappears; the result never ends in a dot or space and is never empty.
@pytest.mark.parametrize(
    ('name', 'max_length', 'expected'),
    [
        ('Hello: World?', 150, 'Hello World'),
        ('a/b\\c|d*e<f>g"h', 150, 'abcdefgh'),
        ('Café Déjà Vu', 150, 'Cafe Deja Vu'),
        ('  lots   of   space  ', 150, 'lots of space'),
        ('emoji 🎉 party', 150, 'emoji party'),
        ("Rock & Roll (Live) [HD]! It's, fine.", 150, "Rock & Roll (Live) [HD]! It's, fine"),
        ('Title.', 150, 'Title'),
        ('...', 150, 'untitled'),
        ('日本語', 150, 'untitled'),
        ('', 150, 'untitled'),
        ('a' * 200, 150, 'a' * 150),
        ('abc def', 4, 'abc'),
    ],
)
def test_sanitize_filename(name, max_length, expected):
    assert sanitize_filename(name, max_length) == expected


# The tag is appended as " [id]" with the base trimmed so the whole name fits MAX_NAME_LENGTH. An
# id that is missing, contains a path separator or is only dots and spaces adds no tag at all.
@pytest.mark.parametrize(
    ('base', 'youtube_id', 'expected'),
    [
        ('Title', VIDEO_ID, f'Title [{VIDEO_ID}]'),
        ('Title', None, 'Title'),
        ('Title', '', 'Title'),
        ('Title', 'a/b', 'Title'),
        ('Title', 'a\\b', 'Title'),
        ('Title', '..', 'Title'),
        ('Title', ' . ', 'Title'),
        ('x' * 300, VIDEO_ID, 'x' * (MAX_NAME_LENGTH - 14) + f' [{VIDEO_ID}]'),
        ('a' * 185 + '.' + 'b' * 50, VIDEO_ID, 'a' * 185 + f' [{VIDEO_ID}]'),
        ('x' * 300, None, 'x' * MAX_NAME_LENGTH),
    ],
)
def test_append_id_tag_length_and_separator_guard(base, youtube_id, expected):
    result = append_id_tag(base, youtube_id)
    assert result == expected
    assert len(result) <= MAX_NAME_LENGTH


# A video's tag is its YouTube id, or 'job-<row id>' when it has none, so two untitled videos
# without ids still plan different destinations.
@pytest.mark.parametrize(
    ('youtube_id', 'expected'),
    [
        (VIDEO_ID, VIDEO_ID),
        (None, 'job-7'),
        ('', 'job-7'),
    ],
)
def test_video_id_tag_fallback(youtube_id, expected):
    assert video_id_tag(Download(id=7, youtube_id=youtube_id)) == expected
