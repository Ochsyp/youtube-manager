# -------------------------------------------------------
# Organizer delete tests
#
# Pins what the organizer's folder cleanup may remove: a folder left with nothing but metadata
# goes, and one that still has a subdirectory, which rmtree would take along, stays.
# -------------------------------------------------------

from youtube_manager import organizer
from youtube_manager.database import Download

VIDEO_ID = 'dQw4w9WgXcQ'


# A folder holding a subdirectory is refused and left exactly as it was; the same folder without
# the subdirectory is removed with its leftover metadata.
def test_remove_exclusive_folder_refuses_nested_dirs(library):
    job = Download(id=1, url=f'https://www.youtube.com/watch?v={VIDEO_ID}', request_type='video')
    folder = library.media / f'Movie [{VIDEO_ID}]'
    nfo = folder / f'Movie [{VIDEO_ID}].nfo'
    nested = folder / 'Season 01' / 'tvshow.nfo'
    nested.parent.mkdir(parents=True)
    nfo.write_text('<movie/>', encoding='utf-8')
    nested.write_text('<tvshow/>', encoding='utf-8')

    assert organizer.remove_exclusive_folder(job, folder) is False
    assert nfo.exists() and nested.exists()

    nested.unlink()
    nested.parent.rmdir()
    assert organizer.remove_exclusive_folder(job, folder) is True
    assert not folder.exists()
