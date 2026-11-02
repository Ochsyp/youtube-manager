# -------------------------------------------------------
# yt-dlp output parsing tests
#
# Characterization tests for worker.py's progress parsing: which yt-dlp output lines yield a
# percentage and a transfer rate, and which yield nothing.
# -------------------------------------------------------

import pytest

from youtube_manager.worker import parse_completion_speed, parse_progress_line


# A progress line yields its percentage and instantaneous rate exactly as printed; a rate yt-dlp
# cannot report yet gives None, and a line of any other kind gives (None, None).
@pytest.mark.parametrize(
    ('line', 'expected'),
    [
        ('[download]  62.5% of  15.30MiB at 4.80MiB/s ETA 00:02', ('62.5%', '4.80MiB/s')),
        ('[download]  45.0% of ~ 10.00MiB at  1.00KiB/s ETA 02:00 (frag 3/10)', ('45.0%', '1.00KiB/s')),
        ('[download] 100% of 559.84MiB in 00:00:31 at 18.01MiB/s', ('100%', '18.01MiB/s')),
        ('[download]   0.0% of ~  1.20GiB at  Unknown B/s ETA Unknown', ('0.0%', None)),
        ('[download] Destination: Title [dQw4w9WgXcQ].f137.mp4', (None, None)),
        ('[ffmpeg] Merging formats into "Title [dQw4w9WgXcQ].mkv"', (None, None)),
        ('', (None, None)),
    ],
)
def test_parse_progress_line(line, expected):
    assert parse_progress_line(line) == expected


# A rate or percentage inside a title on a Destination line is not progress, so nothing from it
# can be stored as the transfer rate.
@pytest.mark.parametrize(
    'line',
    [
        '[download] Destination: Fast at 5MB/s [dQw4w9WgXcQ].mp4',
        '[download] Destination: 50% of fans at 5MiB/s [dQw4w9WgXcQ].mp4',
    ],
)
def test_title_with_speed_on_destination_line_yields_no_speed(line):
    assert parse_progress_line(line) == (None, None)


# Only yt-dlp's per-format completion summary yields an average rate; a running progress line,
# an unfinished summary and any other line give None.
@pytest.mark.parametrize(
    ('line', 'expected'),
    [
        ('[download] 100% of 559.84MiB in 00:00:31 at 18.01MiB/s', '18.01MiB/s'),
        ('[download] 100% of    1.20GiB in 00:01:02 at 19.70MiB/s', '19.70MiB/s'),
        ('[download] 100% of 3.40KiB in 00:00:00 at 512.00B/s', '512.00B/s'),
        ('[download]  62.5% of  15.30MiB at 4.80MiB/s ETA 00:02', None),
        ('[download] 100% of 559.84MiB', None),
        ('[download] Destination: Done in 00:01 at 5MB/s [dQw4w9WgXcQ].mp4', None),
        ('', None),
    ],
)
def test_parse_completion_speed(line, expected):
    assert parse_completion_speed(line) == expected
