# -------------------------------------------------------
# Media lookup tests
#
# Characterization tests for media.py: reading a video id out of a URL, telling finished media
# from partials, format fragments and sidecars, and finding a job's file from its recorded
# filename or its exact '[id]' tag, never its title.
# -------------------------------------------------------

import pytest

from media import STAGING_TEMP, extract_youtube_id, find_existing_media, is_format_fragment, is_media_file

VIDEO_ID = 'dQw4w9WgXcQ'


# Every single-video URL shape yields the bare 11-character id, whitespace and a missing scheme
# included.
@pytest.mark.parametrize(
    'url',
    [
        f'https://www.youtube.com/watch?v={VIDEO_ID}',
        f'https://www.youtube.com/watch?v={VIDEO_ID}&t=42s',
        f'youtube.com/watch?v={VIDEO_ID}',
        f'  https://m.youtube.com/watch?v={VIDEO_ID}  ',
        f'https://music.youtube.com/watch?v={VIDEO_ID}',
        f'https://youtu.be/{VIDEO_ID}',
        f'youtu.be/{VIDEO_ID}/',
        f'https://www.youtube.com/shorts/{VIDEO_ID}',
        f'https://www.youtube.com/embed/{VIDEO_ID}',
        f'https://www.youtube.com/live/{VIDEO_ID}',
        f'https://www.youtube.com/v/{VIDEO_ID}',
        f'https://www.youtube-nocookie.com/embed/{VIDEO_ID}',
    ],
)
def test_extract_youtube_id_forms(url):
    assert extract_youtube_id(url) == VIDEO_ID


# Anything that is not one video on a YouTube host gives None rather than a guessed id: empty
# input, ids of the wrong shape, playlists, channels and lookalike hosts.
@pytest.mark.parametrize(
    'url',
    [
        None,
        '',
        'https://www.youtube.com/watch?v=tooshort',
        f'https://www.youtube.com/watch?v={VIDEO_ID}X',
        f"https://www.youtube.com/watch?v={VIDEO_ID[:-1]}'",
        f'https://youtu.be/{VIDEO_ID}/extra',
        'https://www.youtube.com/playlist?list=PLxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx',
        'https://www.youtube.com/@SomeChannel',
        f'https://example.com/watch?v={VIDEO_ID}',
        f'https://youtube.com.example.net/watch?v={VIDEO_ID}',
        f'https://youtu.be@example.net/{VIDEO_ID}',
    ],
)
def test_extract_youtube_id_rejects_malformed(url):
    assert extract_youtube_id(url) is None


# Only an existing file with a playable extension counts. Partials, merge temps, sidecars,
# directories and missing paths do not, while a dotted title is not mistaken for a partial.
@pytest.mark.parametrize(
    ('name', 'expected'),
    [
        (f'Title [{VIDEO_ID}].mp4', True),
        (f'Title [{VIDEO_ID}].mkv', True),
        (f'Title [{VIDEO_ID}].webm', True),
        (f'Title [{VIDEO_ID}].MP4', True),
        ('Talk.part.2.mp4', True),
        (f'Title [{VIDEO_ID}].mp4.part', False),
        (f'Title [{VIDEO_ID}].temp.mp4', False),
        (f'Title [{VIDEO_ID}].mp4.ytdl', False),
        (f'Title [{VIDEO_ID}].info.json', False),
        (f'Title [{VIDEO_ID}].description', False),
        (f'Title [{VIDEO_ID}].jpg', False),
        (f'Title [{VIDEO_ID}].nfo', False),
    ],
)
def test_is_media_file_rejects_partials_and_sidecars(tmp_path, name, expected):
    path = tmp_path / name
    path.touch()
    assert is_media_file(path) is expected

    folder = tmp_path / 'folder.mp4'
    folder.mkdir(exist_ok=True)
    assert is_media_file(folder) is False
    assert is_media_file(tmp_path / 'missing.mp4') is False
    assert is_media_file(None) is False


# The recorded filename finds the file relative to staging or absolute in the library, follows
# it by name once it has moved, and never returns a file outside the search roots the caller asked
# for. No youtube_id or title is set, so only the filename can match.
def test_find_existing_media_by_recorded_filename(library, make_job):
    name = f'Title [{VIDEO_ID}].mp4'
    staged = library.staging / name
    staged.touch()

    relative = make_job(filename=name)
    assert find_existing_media(relative) == staged
    assert find_existing_media(relative, search_paths=[library.staging]) == staged

    filed = library.media / 'Show' / 'Season 01' / name
    filed.parent.mkdir(parents=True)
    staged.rename(filed)

    # The recorded staging path is gone; the same name is found under the library instead.
    assert find_existing_media(relative) == filed
    absolute = make_job(filename=str(filed))
    assert find_existing_media(absolute) == filed

    # Scoped to staging, a file that only exists in the library does not count.
    assert find_existing_media(relative, search_paths=[library.staging]) is None
    assert find_existing_media(absolute, search_paths=[library.staging]) is None


# A per-format stream yt-dlp has not merged yet is a fragment, never finished media, whatever its
# extension, plain or with a -drc style suffix, finished or still .part.
@pytest.mark.parametrize(
    'name',
    [
        f'Title [{VIDEO_ID}].f137.mp4',
        f'Title [{VIDEO_ID}].f251-drc.webm',
        f'Title [{VIDEO_ID}].f140.m4a',
        f'Title [{VIDEO_ID}].f137.mp4.part',
    ],
)
def test_is_media_file_rejects_format_fragments(tmp_path, name):
    path = tmp_path / name
    path.touch()
    assert is_media_file(path) is False
    if not name.endswith('.part'):
        assert is_format_fragment(path) is True


# Dots in a title are not a fragment marker: only '.f<digits>' straight after the '[id]' tag is.
@pytest.mark.parametrize(
    'name',
    [
        f'Talk v1.f2 remix [{VIDEO_ID}].mp4',
        f'Mr. Smith goes to f137 [{VIDEO_ID}].mkv',
        f'Episode 1.f [{VIDEO_ID}].webm',
        'Talk.f137.mp4',
    ],
)
def test_is_media_file_accepts_dotted_titles(tmp_path, name):
    path = tmp_path / name
    path.touch()
    assert is_format_fragment(path) is False
    assert is_media_file(path) is True


# yt-dlp's temp path holds only intermediates, so even a merged .mp4 waiting there for its move,
# at the top or in an uploader subfolder, is not finished media.
def test_is_media_file_rejects_anything_under_incomplete(library):
    assert STAGING_TEMP.parent == library.staging
    waiting = [STAGING_TEMP / f'Title [{VIDEO_ID}].mp4', STAGING_TEMP / 'Uploader' / f'Title [{VIDEO_ID}].mkv']
    for path in waiting:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()
        assert is_media_file(path) is False

    moved = library.staging / f'Title [{VIDEO_ID}].mp4'
    moved.touch()
    assert is_media_file(moved) is True


# A title match is no identity: a job whose recorded file is gone and whose id tag is nowhere is
# not found, even with a file of exactly its title in staging and the library.
def test_find_existing_media_has_no_title_fallback(library, make_job):
    title = 'Exactly The Same Title'
    (library.staging / f'{title} [aaaaaaaaaaa].mp4').touch()
    (library.media / f'{title}.mkv').touch()

    assert find_existing_media(make_job(title=title)) is None
    assert find_existing_media(make_job(title=title, filename='gone.mp4')) is None
    assert find_existing_media(make_job(title=title, youtube_id=VIDEO_ID)) is None


# The id is matched as its bracketed tag: a bare id inside another name, or one with extra
# characters inside the brackets, does not count, and the tag is found in a nested library folder.
def test_find_existing_media_requires_bracketed_id(library, make_job):
    job = make_job(youtube_id=VIDEO_ID)
    (library.staging / f'Reaction to {VIDEO_ID}.mp4').touch()
    (library.staging / f'Title [x{VIDEO_ID}].mp4').touch()
    (library.staging / f'Title [{VIDEO_ID}].info.json').touch()
    assert find_existing_media(job) is None

    filed = library.media / 'Show' / 'Season 01' / f'Show - S01E01 - Title [{VIDEO_ID}].mkv'
    filed.parent.mkdir(parents=True)
    filed.touch()
    assert find_existing_media(job) == filed
    assert find_existing_media(job, search_paths=[library.staging]) is None


# An id that is not 11 YouTube characters is never searched for, so a glob character or a
# playlist id in the column cannot match some other file.
@pytest.mark.parametrize('bad_id', ['*', 'short', 'PLyyyyyyyyyyyyyyyyyyyyyyyyyyyy-yyy', f'{VIDEO_ID[:-1]}*'])
def test_find_existing_media_skips_invalid_ids(library, make_job, bad_id):
    (library.staging / 'Anything [short].mp4').touch()
    (library.staging / f'Title [{VIDEO_ID}].mp4').touch()
    (library.media / 'Playlist [PLyyyyyyyyyyyyyyyyyyyyyyyyyyyy-yyy].mp4').touch()

    assert find_existing_media(make_job(youtube_id=bad_id)) is None
