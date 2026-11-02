# -------------------------------------------------------
# Job-state tests
#
# Characterization tests for jobstate.py: how a parent's status and progress roll up from its
# children, what a pending cancel displays as, and how yt-dlp speed strings parse. Every row here
# is a transient Download that never touches a session.
# -------------------------------------------------------

import pytest

from youtube_manager.database import Download
from youtube_manager.jobstate import (
    compute_effective_status,
    compute_parent_progress,
    parse_speed_value,
    resolve_parent_status,
)


# A transient Download with the given status, request type and children; `fields` sets any other
# column. Column defaults are not applied, so cancel_requested is None unless passed.
def make_row(status='queued', request_type='video', children=(), **fields) -> Download:
    return Download(status=status, request_type=request_type, children=list(children), **fields)


# Anything in flight keeps the parent in flight; once every child has settled, one failure
# outranks any number of completions, and only skipped rows leave no verdict at all.
@pytest.mark.parametrize(
    ('child_statuses', 'expected'),
    [
        (['completed', 'failed', 'completed'], 'failed'),
        (['failed', 'cancelled', 'completed'], 'failed'),
        (['completed', 'completed'], 'completed'),
        (['completed', 'cancelled'], 'completed'),
        (['cancelled', 'skipped'], 'cancelled'),
        (['failed', 'downloading'], 'downloading'),
        (['completed', 'queued'], 'queued'),
        (['deleted', 'skipped'], 'deleted'),
        (['skipped', 'skipped'], None),
        ([], None),
    ],
)
def test_resolve_parent_status_failed_outranks_completed(child_statuses, expected):
    parent = make_row('completed', 'playlist', [make_row(status) for status in child_statuses])
    assert resolve_parent_status(parent) == expected


# A cancel the worker has not acted on shows as 'cancelling' while data is moving and as
# 'cancel requested' while the job only waits; a job already stopped keeps its own status.
@pytest.mark.parametrize(
    ('status', 'expected'),
    [
        ('downloading', 'cancelling'),
        ('metadata', 'cancelling'),
        ('queued', 'cancel requested'),
        ('expanding', 'cancel requested'),
        ('redownload', 'cancel requested'),
        ('completed', 'completed'),
        ('failed', 'failed'),
        ('cancelled', 'cancelled'),
    ],
)
def test_compute_effective_status_cancel_pending(status, expected):
    assert compute_effective_status(make_row(status, cancel_requested=True)) == expected


# A parent's progress is the mean completion across all of its children, counting anything not
# completed or transferring as 0; with no children it reports its own stored value.
@pytest.mark.parametrize(
    ('children', 'expected'),
    [
        ([('downloading', '50%')] + [('queued', None)] * 9, '5.0%'),
        ([('completed', None), ('failed', None)], '50.0%'),
        ([('completed', None), ('downloading', '30.0%')], '65.0%'),
        ([('skipped', None), ('completed', None)], '50.0%'),
        ([('downloading', 'garbage')], '0.0%'),
        ([], '12.0%'),
    ],
)
def test_compute_parent_progress_counts_all_children(children, expected):
    parent = make_row(
        'downloading',
        'playlist',
        [make_row(status, progress=progress) for status, progress in children],
        progress='12.0%',
    )
    assert compute_parent_progress(parent) == expected


# Decimal and binary units in any case convert to bytes per second; anything else is None,
# never 0.
@pytest.mark.parametrize(
    ('speed', 'expected'),
    [
        ('10B/s', 10.0),
        ('500KB/s', 500_000.0),
        ('1.5KiB/s', 1536.0),
        ('1.5kib/s', 1536.0),
        ('2MiB/s', 2 * 1024 ** 2),
        (' 4.80MiB/s ', 4.8 * 1024 ** 2),
        ('3GB/s', 3e9),
        ('1GiB/s', 1024 ** 3),
        ('2TiB/s', 2 * 1024 ** 4),
        ('Unknown B/s', None),
        ('fast', None),
        ('', None),
        (None, None),
    ],
)
def test_parse_speed_value_units(speed, expected):
    result = parse_speed_value(speed)
    if expected is None:
        assert result is None
    else:
        assert result == pytest.approx(expected)
