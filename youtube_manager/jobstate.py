# -------------------------------------------------------
# Job-state vocabulary and aggregation
#
# Shared job-state vocabulary and progress/speed aggregation for the YouTube Manager.
# app.py and worker.py both import from here, so the two processes can never disagree about
# what status a job shows, how far along a parent is, or how fast it is going.
# Nothing here opens a Session or commits: every function mutates the rows it is handed and
# leaves the transaction to its caller, since SQLite admits only one writer at a time.
# -------------------------------------------------------

import re
from typing import Callable, Optional

from youtube_manager.database import utcnow
from youtube_manager.media import SEARCH_PATHS, find_existing_media

# =======================================================
#                   STATUS VOCABULARY
# =======================================================
# Every status name in the system, the groupings built from them, and the two predicates that read
# a job's shape. Nothing outside this section may hardcode a status string.

# Statuses that mean the job still has work left to do; anything in this set is "in flight".
ACTIVE_STATUSES = ('queued', 'metadata', 'redownload', 'expanding', 'downloading')

# Statuses that mean the job has stopped and will not change again on its own. 'deleted' means
# the user removed the downloaded file; the row is marked rather than dropped, but is inert.
TERMINAL_STATUSES = ('completed', 'failed', 'cancelled', 'deleted')

# Recorded by a subscription's first scan when backfill_existing is off: marks a video as already
# known so a later scan cannot mistake it for new, without queuing it. Only a first scan with
# backfill_existing on ("Download existing now") queues it. Never set on a parent, and never
# touched by the queue/worker machinery below.
SKIPPED_STATUS = 'skipped'

# Statuses the worker will pick up from the queue and act on.
WORKER_PICKUP_STATUSES = ('queued', 'metadata', 'expanding', 'redownload')

# Statuses shown on the dashboard queue view and on the history view respectively; both exclude
# 'deleted' and SKIPPED_STATUS, neither being a visible download. query_downloads() filters on
# the *effective* status, so display-only values belong here too, or a job whose cancel the
# worker has not acted on yet matches no list and vanishes from every view.
QUEUE_STATUSES = (
    'queued', 'metadata', 'redownload', 'expanding', 'downloading', 'failed', 'cancelled',
    'cancelling', 'cancel requested',
)
HISTORY_STATUSES = ('completed', 'failed', 'cancelled')

# Request types that expand into child jobs rather than downloading a single file.
PARENT_REQUEST_TYPES = ('playlist', 'channel')

# Frequently used status groupings, named so call sites read as intent rather than as literals.
FAILED_OR_CANCELLED = frozenset({'failed', 'cancelled'})
QUEUED_OR_EXPANDING = frozenset({'queued', 'expanding'})
ACTIVE_STATUS_SET = frozenset(ACTIVE_STATUSES)
TERMINAL_STATUS_SET = frozenset(TERMINAL_STATUSES)

# Statuses the queue machinery may write to a row, used to gate a derived value before storing
# it. SKIPPED_STATUS is stored too, but only by subscription scans, and is never derived here;
# compute_effective_status() also returns display-only values that must never be written back.
STORABLE_STATUSES = ACTIVE_STATUS_SET | TERMINAL_STATUS_SET

# Effective statuses, not raw stored ones, for which the UI shows the Cancel button; the two
# display-only states are included so it does not flicker away after a click. Not derived from
# ACTIVE_STATUSES: its 'redownload' always maps to 'queued' and is never an effective status.
CANCELABLE_EFFECTIVE_STATUSES = ('queued', 'metadata', 'expanding', 'downloading', 'cancelling', 'cancel requested')

# Raw statuses cleared of transient progress/speed once a job stops actively transferring data.
# 'completed' is deliberately excluded: its final progress/speed are the values worth keeping.
RESET_PROGRESS_ON_STATUSES = frozenset({'queued', 'failed', 'cancelled'})

# Raw statuses a job can be caught in if the worker dies mid-flight, recovered once at startup.
# 'downloading' is not in WORKER_PICKUP_STATUSES, so without this a job stuck there is never
# retried; the other two are pickable already but are normalized to clear stale bookkeeping.
RESTART_RECOVERY_STATUSES = frozenset({'downloading', 'metadata', 'expanding'})

# Multipliers used to convert a yt-dlp speed unit into plain bytes per second.
SPEED_UNIT_MULTIPLIERS = {
    'B': 1,
    'KB': 1000,
    'KIB': 1024,
    'MB': 1000 ** 2,
    'MIB': 1024 ** 2,
    'GB': 1000 ** 3,
    'GIB': 1024 ** 3,
    'TB': 1000 ** 4,
    'TIB': 1024 ** 4,
}


# Whether this job's request expands into child jobs. Keyed on request_type, not on parent_id or
# on children actually existing -- a parent still expanding has none yet and must still count.
def is_parent_request(job) -> bool:
    return job.request_type in PARENT_REQUEST_TYPES


# A job's children as a plain list. Download.children is lazy='selectin', so it comes back
# already populated and reading it here costs no extra query.
def child_jobs(job) -> list:
    return list(getattr(job, 'children', None) or [])


# =======================================================
#               PROGRESS AND SPEED VALUES
# =======================================================
# Progress and speed are stored as the display strings yt-dlp emits, so each is parsed back to a
# number before any arithmetic and re-rendered afterwards. An unparseable value is None, never 0.

# Parse a progress string (e.g. "50%" or "50.4") and return its numeric value, or None if invalid.
def parse_progress_value(progress: Optional[str]) -> Optional[float]:
    if not progress:
        return None

    # First number in the string, with an optional percent sign; anything else is not progress.
    match = re.search(r'([0-9]+(?:\.[0-9]+)?)\s*%?', str(progress).strip())
    if not match:
        return None

    return float(match.group(1))


# Render a numeric progress value in the "50.0%" form parse_progress_value() reads back.
def format_progress_value(progress: float) -> str:
    return f'{progress:.1f}%'


# Parse a yt-dlp speed string (e.g. "1.23MiB/s") into plain bytes per second, or None if invalid.
def parse_speed_value(speed: Optional[str]) -> Optional[float]:
    if not speed:
        return None

    match = re.match(r'([0-9]+(?:\.[0-9]+)?)\s*([KMGT]?i?B)/s', str(speed).strip(), re.I)
    if not match:
        return None

    value = float(match.group(1))
    unit = match.group(2).upper()
    return value * SPEED_UNIT_MULTIPLIERS.get(unit, 1)


# Format a speed in bytes per second into a human-readable string (B/s, KiB/s, MiB/s, GiB/s).
def format_speed_value(bytes_per_second: float) -> str:
    for factor, suffix in ((1024 ** 3, 'GiB/s'), (1024 ** 2, 'MiB/s'), (1024, 'KiB/s')):
        if bytes_per_second >= factor:
            return f'{bytes_per_second / factor:.1f}{suffix}'
    return f'{bytes_per_second:.1f}B/s'


# =======================================================
#                   EFFECTIVE STATUS
# =======================================================
# The status a job displays as, which the stored one only sometimes matches.

# Derive a parent's status from its children's outcomes, or None when there is nothing to derive
# it from. The single definition of "is the parent done, and how did it end", shared by the worker
# settling a parent row and by the UI rendering one. One failed child outranks any number of
# completed ones, so a mostly-succeeded playlist reads 'failed' until every failure is cleared.
def resolve_parent_status(job) -> Optional[str]:
    children = child_jobs(job)
    if not children:
        return None

    # Anything still in flight keeps the parent in flight.
    if any(c.status == 'downloading' for c in children):
        return 'downloading'
    if any(c.status in ACTIVE_STATUS_SET for c in children):
        return 'queued'

    # Every child has settled; a failure anywhere outranks the successes, per the rule above.
    if any(c.status == 'failed' for c in children):
        return 'failed'
    if any(c.status == 'completed' for c in children):
        return 'completed'
    if any(c.status == 'cancelled' for c in children):
        return 'cancelled'

    # Only inert rows are left. Children whose files were deleted make the parent deleted too;
    # a set of nothing but SKIPPED_STATUS rows is no verdict at all, so the parent keeps its own.
    if any(c.status == 'deleted' for c in children):
        return 'deleted'
    return None


# Compute the status shown in the UI, which is not always the raw status stored on the row:
# a cancel can be pending, and a playlist/channel parent's real state lives in its children.
def compute_effective_status(job) -> str:
    status = job.status

    # A cancel the worker has not acted on yet is surfaced ahead of everything else, so the
    # UI acknowledges the click immediately instead of continuing to claim "downloading".
    if job.cancel_requested and status in ACTIVE_STATUS_SET:
        return 'cancelling' if status in {'downloading', 'metadata'} else 'cancel requested'

    # 'redownload' is an internal worker hand-off state; users only care that it is pending.
    if status == 'redownload':
        return 'queued'

    # A single video job has no children to reconcile against, so its own status is the answer.
    if not is_parent_request(job):
        return status

    children = child_jobs(job)

    # A parent with no children yet has not finished discovery. A parent recorded as
    # 'downloading' before a restart is really still expanding, so surface that instead.
    if not children:
        return 'expanding' if status == 'downloading' else status

    settled = not any(c.status in ACTIVE_STATUS_SET for c in children)

    # Cancelling or deleting the whole parent is a decision its children cannot express, so that
    # stored status stands once nothing is left in flight.
    if settled and status in {'cancelled', 'deleted'}:
        return status

    # Every other state is derived from what the children are actually doing: a stored 'completed'
    # cannot outrank a failed child, a stored 'failed' clears once no failure is left among them,
    # and a parent can never claim to be downloading while every child sits queued.
    return resolve_parent_status(job) or status


# Whether retrying this job would do anything. A retry cascades to each child individually (see
# app.retry_job / apply_status_update_to_hierarchy), so a parent is retriable whenever any child
# is, even when the parent's own effective status is neither failed nor cancelled.
def is_retriable(job) -> bool:
    if compute_effective_status(job) in FAILED_OR_CANCELLED:
        return True
    return any(compute_effective_status(child) in FAILED_OR_CANCELLED for child in child_jobs(job))


# =======================================================
#                  PARENT AGGREGATION
# =======================================================
# What a playlist or channel row reports for itself, rolled up from its children.

# How much of a single child counts as done, as a percentage. Anything that is not completed or
# actively transferring counts 0, not 100: it has no usable output on disk right now, and a
# parent whose children all failed must not read 100%. summarize_children() breaks out the rest.
def child_completion_value(child) -> float:
    if child.status == 'completed':
        return 100.0
    if child.status == 'downloading':
        return parse_progress_value(child.progress) or 0.0
    return 0.0


# Compute a parent job's progress as completion across *all* of its children, so a playlist
# with one child at 50% and nine still queued reads 5.0% rather than 50%. Jobs without
# children (single videos, and parents still expanding) report their own progress unchanged.
def compute_parent_progress(job) -> Optional[str]:
    children = child_jobs(job)
    if not children:
        return job.progress

    return format_progress_value(
        sum(child_completion_value(child) for child in children) / len(children)
    )


# A parent's speed is the combined live speed of whichever children are downloading, so the row
# tracks the transfer actually happening; once none are, it is the average of every speed the
# children recorded, i.e. the throughput the job achieved overall.
def compute_parent_speed(job) -> Optional[str]:
    children = child_jobs(job)
    if not children:
        return job.speed

    # Unparseable and zero speeds are dropped rather than counted: a child that has not reported
    # a figure yet would otherwise drag the sum down and make the transfer look slower than it is.
    live_speeds = [
        speed
        for speed in (parse_speed_value(c.speed) for c in children if c.status == 'downloading')
        if speed
    ]
    if live_speeds:
        return format_speed_value(sum(live_speeds))

    # Children that never recorded a speed at all leave the parent's own value alone.
    recorded_speeds = [speed for speed in (parse_speed_value(c.speed) for c in children) if speed]
    if not recorded_speeds:
        return job.speed
    return format_speed_value(sum(recorded_speeds) / len(recorded_speeds))


# Break a parent's children down by outcome so the UI can show completion across all children
# ("8/10 done") alongside the aggregate percentage, and still surface failures on a parent
# whose overall status is 'completed'.
def summarize_children(job) -> Optional[dict]:
    children = child_jobs(job)
    if not children:
        return None

    # The buckets need not add up to 'total': a SKIPPED_STATUS child belongs to none of them.
    return {
        'total': len(children),
        'completed': sum(1 for c in children if c.status == 'completed'),
        'failed': sum(1 for c in children if c.status == 'failed'),
        'cancelled': sum(1 for c in children if c.status == 'cancelled'),
        'deleted': sum(1 for c in children if c.status == 'deleted'),
        'active': sum(1 for c in children if c.status in ACTIVE_STATUS_SET),
    }


# Render summarize_children() as a one-line label for table cells, e.g. "8/10 done - 2 failed".
def format_children_summary(job) -> Optional[str]:
    summary = summarize_children(job)
    if not summary:
        return None

    # Only non-zero outcomes are appended, so a clean playlist reads as the count alone.
    parts = [f"{summary['completed']}/{summary['total']} done"]
    if summary['failed']:
        parts.append(f"{summary['failed']} failed")
    if summary['cancelled']:
        parts.append(f"{summary['cancelled']} cancelled")
    if summary['deleted']:
        parts.append(f"{summary['deleted']} deleted")
    return ' · '.join(parts)


# =======================================================
#                  RETRY AND ADOPTION
# =======================================================
# Putting a stopped job back to work. The app's buttons and the worker's auto-retry both land here,
# so a retry means the same thing whichever side asked for it.

# Adopt a file already sitting on disk (staging or library), found by its recorded name or its
# '[<id>]' tag, as a completed download rather than fetching it again. Shared by the app's Retry
# button and the worker's auto-retry so the shortcut cannot exist on one side only.
def adopt_existing_download(job) -> bool:
    # A playlist/channel row owns no media file of its own: anything a lookup found for it would
    # be one video's file, and adopting it would complete the whole request.
    if is_parent_request(job):
        return False

    found_path = find_existing_media(job, search_paths=SEARCH_PATHS)
    if not found_path:
        return False

    # The row is pointed at the file where it already sits: nothing is copied or moved, so a file
    # the organizer has filed into the library is adopted in place.
    job.status = 'completed'
    job.completed_at = utcnow()
    job.filename = str(found_path)
    info_path = found_path.with_name(found_path.stem + '.info.json')
    if info_path.exists():
        job.metadata_file = str(info_path)
    return True


# Apply update_fn to a parent's children, then to the parent itself only if at least one child
# actually changed, so a parent with nothing left needing the update is not forced anyway. A
# plain video just gets update_fn applied directly.
def apply_status_update_to_hierarchy(job, update_fn: Callable[[object], bool]) -> bool:
    children = child_jobs(job)
    if job.parent_id is None and children:
        any_updated = False
        for child in children:
            if update_fn(child):
                any_updated = True
        if any_updated:
            update_fn(job)
        return any_updated
    return update_fn(job)


# Reset one job so the worker picks it up again, if eligible under allowed_statuses: the manual
# Retry button passes failed and cancelled alike, scheduled auto-retry only {'failed'} so it
# never overrides an explicit cancel. A file already on disk, found by recorded name or '[<id>]'
# tag, routes to 'redownload' rather than straight to completed, since the metadata may be what
# failed; the file itself is reused.
def reset_job_for_retry(job, allowed_statuses=FAILED_OR_CANCELLED) -> bool:
    if job.status not in allowed_statuses:
        return False

    job.cancel_requested = False
    job.error = None
    job.progress = None
    job.speed = None
    job.completed_at = None

    # Parents are excluded for the same reason as in adopt_existing_download(): a video's file
    # found for one would send a playlist for a metadata refresh instead of re-expanding it.
    if not is_parent_request(job) and find_existing_media(job, search_paths=SEARCH_PATHS):
        job.status = 'redownload'
        return True

    job.status = 'queued'
    job.organized_at = None
    return True
