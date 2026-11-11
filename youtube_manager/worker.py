# -------------------------------------------------------
# YouTube download worker
#
# Runs yt-dlp, updates job state in the database, discovers metadata for parent and video jobs,
# and recovers or retries stalled jobs. Runs as its own process alongside the web app, taking one
# job at a time; both share one SQLite file, which allows a single writer.
# -------------------------------------------------------

import importlib.metadata
import itertools
import json
import logging
import os
import queue
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from datetime import timedelta
from pathlib import Path
from typing import Iterable, Optional
from urllib.parse import parse_qs, urlsplit

from sqlalchemy import or_

from youtube_manager import VERSION, applog
from youtube_manager.database import (
    Channel,
    Download,
    Playlist,
    Session,
    Subscription,
    utcnow,
    wait_for_schema,
)
from youtube_manager.jobstate import (
    ACTIVE_STATUS_SET,
    PARENT_REQUEST_TYPES,
    RESET_PROGRESS_ON_STATUSES,
    RESTART_RECOVERY_STATUSES,
    SKIPPED_STATUS,
    TERMINAL_STATUS_SET,
    WORKER_PICKUP_STATUSES,
    apply_status_update_to_hierarchy,
    compute_parent_progress,
    compute_parent_speed,
    is_in_progress,
    reset_job_for_retry,
    resolve_parent_status,
    settle_parent,
)
from youtube_manager.media import (
    DOWNLOAD_PATH,
    MEDIA_PATH,
    STAGING_TEMP,
    YOUTUBE_ID_PATTERN,
    extract_youtube_id,
    find_existing_media,
    is_media_file,
    normalize_youtube_id,
)
from youtube_manager.organizer import library_ready as organizer_library_ready
from youtube_manager.organizer import organize_single_job as organizer_organize_single_job
from youtube_manager.organizer import remove_staging_leftovers as organizer_remove_staging_leftovers
from youtube_manager.organizer import scan_and_process as organizer_scan_and_process


# =======================================================
#              SETUP AND TUNING CONSTANTS
# =======================================================

PROXY = os.getenv('HTTP_PROXY') or os.getenv('http_proxy')

# Base URL of the bgutil PO token provider, usually a sidecar container. YouTube withholds its
# full DASH format list from a client with no PO token, leaving yt-dlp on low-bitrate HLS.
# Left unset, no token arguments are passed at all.
POT_PROVIDER_URL = os.getenv('POT_PROVIDER_URL', '')


# Arguments every yt-dlp invocation shares, built fresh per call so a caller extending them into
# its own command list cannot mutate shared state. --ignore-config always leads, so no yt-dlp
# config file in the image, the staging directory or a home directory can change what a call does;
# the PO-token extractor args follow when POT_PROVIDER_URL is set.
def ytdlp_common_args() -> list[str]:
    args = ['--ignore-config']
    if POT_PROVIDER_URL:
        args += ['--extractor-args', f'youtubepot-bgutilhttp:base_url={POT_PROVIDER_URL}']
    return args

# Sleep intervals for the worker loop and its sub-tasks. The organizer runs every 5 minutes, and
# subscription checks every 30 minutes, so a failed job is retried at the next organizer pass.
ORGANIZER_INTERVAL_SECONDS = 300
SUBSCRIPTION_CHECK_INTERVAL_SECONDS = 1800
# How soon a failed job is first retried, so a transient network failure clears itself without
# the user having to notice and click Retry.
RETRY_INTERVAL_SECONDS = ORGANIZER_INTERVAL_SECONDS

# Ceiling on the doubling delay between auto-retries of the same job.
RETRY_BACKOFF_MAX_SECONDS = 6 * 3600

# Consecutive pickups of one job before it is failed outright; a job no branch can move out of
# the queue otherwise spins the loop at full CPU.
MAX_JOB_PICKUP_REPEATS = 4

# Keys dropped before storing yt-dlp's info dictionary in the database. They are large
# (hundreds of kilobytes of format/caption listings) and none of them feed the NFO.
BULKY_METADATA_KEYS = (
    'formats',
    'requested_formats',
    'requested_downloads',
    'thumbnails',
    'automatic_captions',
    'subtitles',
    'heatmap',
    'fragments',
    'http_headers',
    'entries',
)

# Only channel downloads get a per-uploader subfolder in staging; playlist episodes and
# standalone videos stay flat, since the organizer builds its own structure for those.
OUTPUT_TEMPLATE_CHANNEL = '%(uploader)s/%(title)s [%(id)s].%(ext)s'
OUTPUT_TEMPLATE_FLAT = '%(title)s [%(id)s].%(ext)s'

# Timeout for synchronous yt-dlp metadata/filename lookups. Without one a stalled connection
# blocks the single-threaded loop indefinitely, with no chance for a cancel to be noticed.
COMMAND_TIMEOUT_SECONDS = 120

# A download printing nothing for this long is treated as dead. yt-dlp reports progress
# continuously while transferring; the gap only has to clear a slow ffmpeg merge on a small
# ARM host.
DOWNLOAD_STALL_TIMEOUT_SECONDS = 1800

# Longest wait for a signalled process group to actually die before escalating to SIGKILL.
PROCESS_KILL_TIMEOUT_SECONDS = 15

# yt-dlp emits progress dozens of lines per second. Persisting each one costs two SQLite
# transactions on a database the web app shares, so progress is written at most this often.
PROGRESS_UPDATE_INTERVAL_SECONDS = 1.0

# How often a running download re-reads its cancel flag, which only the web process sets.
CANCEL_POLL_INTERVAL_SECONDS = 2.0

# Cap on a job's stored log, which the UI renders whole and a fragment-heavy download would
# otherwise grow to megabytes.
MAX_STORED_LOG_CHARS = 256 * 1024

# Set by SIGTERM/SIGINT so the loop and any running download stop at the next check instead of
# being killed mid-write by the SIGKILL that follows `docker stop`.
_shutdown = threading.Event()

# Liveness marker for the container healthcheck: this process serves nothing over HTTP, so it
# proves itself by touching a file. /tmp is a tmpfs in compose.example.yaml, so this stays off
# the disk, and the file being absent after a restart is correct -- a process that has not
# reached its loop yet has not proved anything. The healthcheck's staleness window has to clear
# the longest gap between touches, which is one COMMAND_TIMEOUT_SECONDS-bounded yt-dlp call.
HEARTBEAT_PATH = Path(os.getenv('WORKER_HEARTBEAT_PATH', '/tmp/worker-heartbeat'))


# =======================================================
#              LOGGING AND JOB-ROW UPDATES
# =======================================================

# Refresh the liveness marker. Called from every loop that can run for a long time, so a download
# in progress keeps the container healthy rather than looking wedged. Failure is swallowed: an
# unwritable marker means the healthcheck assumes the worst, which is never a reason to abandon
# a download that is otherwise fine.
def touch_heartbeat() -> None:
    try:
        HEARTBEAT_PATH.touch()
    except OSError as exc:
        warn(f'Could not update the heartbeat marker: {exc}')


# Log a message at INFO level, which is what the worker's stdout handler writes to the
# container log.
def log(msg: str) -> None:
    logging.info(msg)


# Log something worth seeing that needs no human: a failure that clears itself or is retried
# later, or a warning yt-dlp relayed. WARN never pages; see log_error() for ERROR.
def warn(msg: str) -> None:
    logging.warning(msg)


# Log a failure with its traceback, indented so it stays one entry downstream.
# Deployments alert on ERROR lines from this service, so ERROR is kept for what needs a human:
# unexpected exceptions, a full disk, a subscription failing check after check.
def log_error(msg: str, exc: BaseException | None = None) -> None:
    logging.error(msg, exc_info=exc)


# Ids of the jobs update_job() refused a write to in this worker run, so each is warned about once:
# a download whose row was deleted keeps reporting progress until it stops.
_refused_write_job_ids: set[int] = set()


# Set columns on one job row in its own session and commit. One conditional UPDATE refuses a row
# marked 'deleted' (WARN once per job per run); a row gone from the table is a no-op. Outside that
# transaction, a terminal status settles a child's parent and a status or progress write
# re-aggregates it. organized_at is deliberately left alone here: a progress tick is no reason to
# send the organizer back over a video it already filed. Only retry and redownload clear it.
def update_job(job_id: int, **values) -> None:
    if values.get('status') in RESET_PROGRESS_ON_STATUSES:
        values.setdefault('progress', None)
        values.setdefault('speed', None)

    with Session() as db:
        row = db.query(Download.parent_id).filter(Download.id == job_id).first()
        if row is None:
            return

        # Conditional rather than read-then-set, so a delete the app commits meanwhile is never
        # overwritten. updated_at still moves, through its onupdate.
        written = (
            db.query(Download)
            .filter(Download.id == job_id, Download.status != 'deleted')
            .update(values, synchronize_session=False)
        )
        if not written:
            if job_id not in _refused_write_job_ids:
                _refused_write_job_ids.add(job_id)
                fields = ', '.join(sorted(values))
                warn(f'Job {job_id}: the row is deleted, so the worker writes nothing more to it '
                     f'({fields} refused)')
            return
        db.commit()
    parent_id = row.parent_id

    # Settled and aggregated after the session closes: SQLite takes one writer, so the parent's
    # own commit has no business nesting inside this transaction.
    if not parent_id:
        return
    try:
        # A parent that settles has its aggregate persisted by finalize_parent_if_done() already.
        if values.get('status') in TERMINAL_STATUS_SET and finalize_parent_if_done(job_id):
            return
        if any(key in values for key in ('progress', 'speed', 'status')):
            compute_and_persist_parent_progress(parent_id)
    except Exception as exc:
        log_error(f'Failed to settle or aggregate parent {parent_id}', exc)


# Append lines to a job's stored log, which is the only place download output is kept. Opens its own
# session and commits; any failure is logged and swallowed rather than raised into a download.
def append_log(job_id: int, *lines: str) -> None:
    if not lines:
        return

    try:
        with Session() as db:
            job = db.get(Download, job_id)

            if not job:
                return

            # The whole column is rewritten per call, so it is capped from the front: a long
            # download would otherwise grow one row until every read of it is expensive.
            text = (job.log or '') + ''.join(f'{line}\n' for line in lines)
            if len(text) > MAX_STORED_LOG_CHARS:
                text = '[log truncated]\n' + text[-MAX_STORED_LOG_CHARS:]
            job.log = text
            db.commit()
    except Exception as exc:
        # Logging must never take the worker down with it.
        log_error(f'Failed to append log for job {job_id}', exc)


# =======================================================
#                     JOB DISPATCH
# =======================================================

# True when a video job's recorded file is in the library rather than in staging. Such a file is
# only ever refreshed in place (see redownload_or_rename), never downloaded again. The path is not
# checked on disk.
def has_library_file(job: Download) -> bool:
    if job.request_type != 'video' or not job.filename:
        return False
    return Path(job.filename).is_relative_to(MEDIA_PATH)


# Run one picked-up job to its next resting state, blocking for as long as that takes: refuse a
# cancelled or unsupported job, refresh a video or parent that already has files on disk, expand a
# parent that has none, and otherwise download the video. Every branch has to leave the row in a
# status get_next_job() no longer selects, or the loop picks the same job up again forever.
def process_job(job: Download) -> None:
    # Logged unconditionally: a branch below that no-ops would otherwise make a stuck job
    # indistinguishable from the worker not running at all.
    log(f'process_job: picked up job {job.id} ({job.request_type}, status={job.status})')

    if job.cancel_requested:
        update_job(job.id, status='cancelled', error='Cancelled by user')
        return

    if not is_safe_url(job.url):
        warn(f'Job {job.id}: refusing unsupported URL {job.url!r}')
        update_job(job.id, status='failed', error=f'Unsupported URL: {job.url}')
        return

    # A video whose file is already on disk is refreshed in place rather than downloaded again, and
    # so is a queued video whose recorded file is in the library: "Retry all" queues one whose file
    # is not where the row says, and downloading it into staging again would file a second copy.
    # Either way the terminal status update_job() writes settles the parent.
    if job.status in {'metadata', 'redownload'} or has_library_file(job):
        if job.request_type == 'video' and job.filename:
            log(f'Job {job.id}: performing worker-side redownload/rename')
            append_log(job.id, 'Worker performing redownload/rename...')
            handled = redownload_or_rename(job)
            if not handled:
                # No error= here: discover_metadata has already stored yt-dlp's own reason, and
                # a generic message would overwrite it.
                update_job(job.id, status='failed')
            return
        if job.request_type in PARENT_REQUEST_TYPES:
            log(f'Job {job.id}: performing worker-side parent metadata refresh')
            append_log(job.id, 'Worker performing parent metadata refresh...')
            success = redownload_or_rename(job)
            if success:
                return
            update_job(job.id, status='failed', error='Parent metadata refresh failed')
            return

    # A parent that already has children was expanded before; its children are picked up
    # individually via the normal queue. Re-expanding loops forever: expand_playlist() resets
    # the parent to 'queued' on a failed re-scan and get_next_job() hands it straight back.
    if job.request_type in PARENT_REQUEST_TYPES:
        with Session() as db:
            db_job = db.get(Download, job.id)
            already_has_children = bool(db_job.children) if db_job else False
            if already_has_children:
                settle_parent(db_job)
                db.commit()
        if already_has_children:
            return
        expand_playlist(job)
        return

    download_video(job)


# Oldest job in any status the worker can act on, or None. A created_at tie goes to the lower id,
# so rows created in one flush run in the order they were added.
def get_next_job() -> Optional[Download]:
    with Session() as db:
        # A parent with children has nothing actionable of its own, and predates them, so
        # leaving it eligible lets it win this "oldest first" query forever.
        parents_with_children = db.query(Download.parent_id).filter(Download.parent_id.isnot(None)).distinct()
        return (
            db.query(Download)
            .filter(
                Download.status.in_(WORKER_PICKUP_STATUSES),
                or_(
                    ~Download.id.in_(parents_with_children),
                    # ...except a user-requested metadata refresh, which a parent with children
                    # could never run otherwise. 'redownload' is one-shot, so it cannot starve.
                    Download.status == 'redownload',
                ),
            )
            .order_by(Download.created_at, Download.id)
            .first()
        )


# =======================================================
#        CHANNEL, PLAYLIST AND SUBSCRIPTION ROWS
# =======================================================

# Fetch the channel row for `youtube_id`, creating it if absent and refreshing whichever fields the
# caller supplies; returns None for a falsy id, despite the annotation. Works inside the caller's
# open session and flushes so the id is assigned, but never commits -- the caller's transaction
# stays in charge.
def get_or_create_channel(
    db, youtube_id: str, name: Optional[str], url: Optional[str] = None,
    thumbnail: Optional[str] = None, description: Optional[str] = None,
) -> Channel:
    # thumbnail/description are only ever passed by a caller whose info dict describes the
    # channel itself -- never a video's or playlist's own, which must not be mistaken for it.
    if not youtube_id:
        return None

    channel = db.query(Channel).filter(Channel.youtube_id == youtube_id).first()

    # Each field is refreshed only when the caller supplied one and it differs, so a later scan
    # carrying partial metadata tops the row up instead of blanking what an earlier one found.
    if channel:
        if name and channel.name != name:
            channel.name = name
        if url and channel.url != url:
            channel.url = url
        if thumbnail and channel.thumbnail != thumbnail:
            channel.thumbnail = thumbnail
        if description and channel.description != description:
            channel.description = description
        return channel

    # No row yet. The name and URL fall back to placeholders rather than failing: the id is the
    # only field the organizer needs, and a real scan fills the rest in later.
    channel = Channel(
        youtube_id=youtube_id,
        name=name or 'Unknown channel',
        url=url or f'https://www.youtube.com/channel/{youtube_id}',
        thumbnail=thumbnail,
        description=description,
    )

    # Flushed so the caller sees an assigned id without committing the outer transaction.
    db.add(channel)
    db.flush()
    return channel


# Fetch the playlist row for `youtube_id`, creating it if absent and re-pointing it at `channel`
# when one is given. Returns None for a falsy id despite the annotation, and flushes into the
# caller's transaction without committing, as get_or_create_channel does.
def get_or_create_playlist(db, youtube_id: str, title: Optional[str], channel: Optional[Channel] = None) -> Playlist:
    if not youtube_id:
        return None

    playlist = db.query(Playlist).filter(Playlist.youtube_id == youtube_id).first()

    # Updated only where the caller supplied a value that differs, so a later partial scan cannot
    # blank a title or unlink a channel an earlier one resolved.
    if playlist:
        if title and playlist.title != title:
            playlist.title = title
        if channel and playlist.channel_id != channel.id:
            playlist.channel = channel
        return playlist

    playlist = Playlist(
        youtube_id=youtube_id,
        title=title or 'Unknown playlist',
        channel=channel,
    )

    # Flushed so the caller sees an assigned id without committing the outer transaction.
    db.add(playlist)
    db.flush()
    return playlist


# Resolve a subscription's own channel/playlist row and store its id. It takes a yt-dlp call the
# web process never makes, so every subscription starts with channel_id/playlist_id NULL.
# Flushes into the caller's session without committing: the caller commits before any further
# network call, or the SQLite write lock is held across it, and rolls back on False (nothing
# resolved). Each False is also counted as a failed check through record_subscription_failure().
def resolve_subscription_target(db, subscription: Subscription) -> bool:
    command = [
        'yt-dlp', '--dump-single-json', '--skip-download',
        *ytdlp_common_args(),
        '--flat-playlist', subscription.url,
    ]
    try:
        result = run_command(command)
    except (subprocess.TimeoutExpired, subprocess.CalledProcessError) as exc:
        record_subscription_failure(subscription.id, f'failed to resolve channel/playlist: {exc}')
        return False

    try:
        metadata = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        record_subscription_failure(subscription.id, f'could not parse channel/playlist metadata: {exc}')
        return False

    fields = extract_metadata_fields(metadata)

    # Same resolution apply_metadata uses: a channel request's info dict describes the channel
    # itself, so its top-level id is the channel's when no channel_id was reported.
    channel = None
    channel_id = fields['channel_id'] or (fields['id'] if subscription.subscription_type == 'channel' else None)
    if channel_id:
        channel_name = fields['channel_name'] or (fields['title'] if subscription.subscription_type == 'channel' else None)
        channel = get_or_create_channel(
            db, channel_id, channel_name, fields['channel_url'],
            thumbnail=fields['thumbnail'] if subscription.subscription_type == 'channel' else None,
            description=fields['description'] if subscription.subscription_type == 'channel' else None,
        )
        subscription.channel_id = channel.id

    # A playlist subscription additionally needs its own row, linked to the channel resolved
    # above; a channel subscription is already complete. Either way an id is mandatory -- without
    # one the subscription cannot be matched to future scans, so the resolution fails outright.
    if subscription.subscription_type == 'playlist':
        playlist_id = fields['id'] or fields['playlist_id']
        if not playlist_id:
            record_subscription_failure(subscription.id, 'playlist metadata had no id')
            return False
        playlist = get_or_create_playlist(db, playlist_id, fields['title'] or fields['playlist_title'], channel=channel)
        subscription.playlist_id = playlist.id
    elif not channel_id:
        record_subscription_failure(subscription.id, 'channel metadata had no id')
        return False

    # Flushed, not committed: check_subscriptions commits it before the scan.
    db.flush()
    log(f'Subscription {subscription.id}: resolved channel_id={subscription.channel_id} playlist_id={subscription.playlist_id}')
    return True


# =======================================================
#       YT-DLP INVOCATION AND SUBPROCESS CONTROL
# =======================================================

# Minimum gap between consecutive yt-dlp invocations. YouTube answers "Sign in to confirm you're
# not a bot" once enough requests arrive from one IP, and the loop runs jobs back-to-back.
YTDLP_MIN_INTERVAL_SECONDS = 2.0
_last_youtube_request_at = 0.0


# Sleep just long enough that consecutive yt-dlp calls stay YTDLP_MIN_INTERVAL_SECONDS apart.
# Called only from the worker thread, so a module-level timestamp is enough. Monotonic, or the
# NTP step a host without a real-time clock takes at boot skips the throttle entirely.
def throttle_youtube_requests() -> None:
    global _last_youtube_request_at

    elapsed = time.monotonic() - _last_youtube_request_at
    if elapsed < YTDLP_MIN_INTERVAL_SECONDS:
        time.sleep(YTDLP_MIN_INTERVAL_SECONDS - elapsed)
    _last_youtube_request_at = time.monotonic()


# yt-dlp takes its URL as a positional argument, so a stored value beginning with "-" is read as
# an option instead -- and --exec runs an arbitrary command. Only that shape is refused: yt-dlp
# accepts a bare "youtube.com/watch?v=..." and older rows may hold one.
def is_safe_url(url: Optional[str]) -> bool:
    return bool(url) and not url.startswith('-')


# Log each distinct WARNING: line of yt-dlp's stderr once, at WARN, in first-seen order. Returns
# the other non-blank lines joined, so a failure's ERROR: reason is not buried behind warnings.
def relay_ytdlp_warnings(stderr: Optional[str]) -> str:
    relayed = set()
    remainder = []
    for raw_line in (stderr or '').splitlines():
        line = raw_line.strip()
        if line.startswith('WARNING:'):
            if line not in relayed:
                relayed.add(line)
                warn(line)
        elif line:
            remainder.append(raw_line.rstrip())
    return '\n'.join(remainder)


# Run a yt-dlp command to completion, throttled and blocking; raises on a non-zero exit or timeout.
# encoding='utf-8' is explicit because text=True's locale default mangles non-ASCII titles into
# mojibake on a container with no UTF-8 locale, so the stored filename stops matching the file
# on disk. The timeout is what keeps a stalled call from freezing the whole worker loop.
def run_command(command: list[str], timeout: int = COMMAND_TIMEOUT_SECONDS) -> subprocess.CompletedProcess[str]:
    throttle_youtube_requests()
    result = subprocess.run(
        command,
        capture_output=True,
        text=True,
        encoding='utf-8',
        errors='replace',
        check=True,
        timeout=timeout,
    )

    # A successful call's stderr is otherwise discarded, and with it yt-dlp's warnings, such as a
    # PO-token or JS-solver fallback. yt-dlp repeats a warning per item, so each relays once.
    relay_ytdlp_warnings(result.stderr)
    return result


# Stop a download and everything it spawned. yt-dlp runs in its own session and forks ffmpeg,
# so the whole process group is signalled; SIGKILL follows a SIGTERM that goes unanswered, or a
# merge wedged inside ffmpeg would hold the job in 'downloading' forever. Waits, so no zombie.
def terminate_process_group(process: subprocess.Popen, grace_seconds: float = 0.0) -> None:
    if grace_seconds:
        try:
            process.wait(timeout=grace_seconds)
            return
        except subprocess.TimeoutExpired:
            log(f'yt-dlp closed its output but has not exited after {grace_seconds}s; terminating')

    for sig in (signal.SIGTERM, signal.SIGKILL):
        # Only ever signal a live child: once it is reaped its pid can belong to somebody else.
        if process.poll() is not None:
            return
        try:
            os.killpg(process.pid, sig)
        except (ProcessLookupError, PermissionError):
            break
        try:
            process.wait(timeout=PROCESS_KILL_TIMEOUT_SECONDS)
            return
        except subprocess.TimeoutExpired:
            continue

    process.poll()


# =======================================================
#         PROGRESS PARSING AND OUTPUT STREAMING
# =======================================================

# A yt-dlp progress line: "[download]  62.5% of  15.30MiB at 4.80MiB/s ETA 00:02". Speed comes
# only from a line that also carries a percentage, so "at 5MB/s" inside a title on a
# "[download] Destination: ..." line cannot be stored as the transfer rate.
PROGRESS_LINE_PATTERN = re.compile(
    r'\[download\]\s+(?P<progress>[0-9]+(?:\.[0-9]+)?%)\s+of\s+'
    r'(?:.*?\sat\s+(?P<speed>[0-9]+(?:\.[0-9]+)?[KMGT]?i?B/s))?'
)

# The summary yt-dlp prints when one format finishes: "[download] 100% of 559.84MiB in 00:00:31
# at 18.01MiB/s". Its speed is that stream's average rather than an instantaneous sample.
COMPLETION_LINE_PATTERN = re.compile(
    r'\[download\]\s+100%\s+of\s+.+?\s+in\s+[0-9:]+\s+at\s+([0-9]+(?:\.[0-9]+)?[KMGT]?i?B/s)'
)


# Percentage and instantaneous speed from one yt-dlp progress line, both as the strings it printed.
# (None, None) for any other line, and a None speed for a progress line that carried no rate.
def parse_progress_line(line: str) -> tuple[Optional[str], Optional[str]]:
    match = PROGRESS_LINE_PATTERN.search(line)
    if not match:
        return None, None
    return match.group('progress'), match.group('speed')


# Average rate from yt-dlp's per-format completion line, or None for any other line. One download
# produces several of these, one per stream it fetches.
def parse_completion_speed(line: str) -> Optional[str]:
    match = COMPLETION_LINE_PATTERN.search(line)
    return match.group(1) if match else None


# Queued by the reader thread at EOF; a distinct object, so no line of output can be it.
_OUTPUT_EOF = object()


# Pumps the subprocess's stdout onto a queue from its own thread, so a stalled but not-yet-exited
# subprocess can never block stream_process_output's cancellation and stall checks.
def _pump_subprocess_output(process: subprocess.Popen[str], line_queue: 'queue.Queue') -> None:
    try:
        for line in iter(process.stdout.readline, ''):
            line_queue.put(line)
    except Exception:
        # Closing the pipe under a blocked readline raises here; EOF is reported either way.
        pass
    finally:
        line_queue.put(_OUTPUT_EOF)


# Relay a subprocess's output to the log and job row until it exits, polling for a cancel
# request, a worker shutdown and a stalled transfer as it goes. Returns why it stopped --
# 'finished', 'cancelled', 'shutdown' or 'stalled' -- and the last line yt-dlp began with
# 'ERROR:', or None. Leaves no process running behind it.
def stream_process_output(process: subprocess.Popen[str], job_id: int) -> tuple[str, Optional[str]]:
    assert process.stdout

    line_queue: 'queue.Queue' = queue.Queue()
    reader_thread = threading.Thread(target=_pump_subprocess_output, args=(process, line_queue), daemon=True)
    reader_thread.start()

    # Progress is buffered and written on a timer: yt-dlp emits dozens of lines a second, and
    # each one costs an UPDATE plus a rewrite of the whole log column, on a shared SQLite file.
    pending_lines: list[str] = []
    pending_update: dict[str, str] = {}
    # Kept apart from the instantaneous samples so the stored speed is the achieved rate.
    final_speed = None

    last_flush = time.monotonic()
    last_cancel_check = time.monotonic()
    last_output_at = time.monotonic()
    outcome = 'finished'
    # yt-dlp's own reason for a failed run; download_video stores it as the job's error.
    last_error_line = None
    out_of_space_logged = False

    # Write the buffered log lines and progress values out and restart the flush timer. Each call
    # costs up to two SQLite transactions, so callers rate-limit it instead of flushing per line.
    def flush() -> None:
        nonlocal last_flush
        if pending_lines:
            append_log(job_id, *pending_lines)
            pending_lines.clear()
        if pending_update:
            update_job(job_id, **pending_update)
            pending_update.clear()
        last_flush = time.monotonic()

    # Consume what the reader thread queues. The blocking read is bounded rather than indefinite
    # so cancel, shutdown and stall are still noticed while yt-dlp is producing nothing at all.
    try:
        while True:
            now = time.monotonic()
            # A download is the longest thing this process does; without a touch here the
            # healthcheck would call a perfectly healthy multi-hour transfer dead.
            touch_heartbeat()

            # Cancel is polled rather than pushed, since the request arrives in the database from
            # the web process. On its own timer so a chatty download doesn't check it per line.
            if now - last_cancel_check >= CANCEL_POLL_INTERVAL_SECONDS:
                last_cancel_check = now
                if _shutdown.is_set():
                    outcome = 'shutdown'
                    break
                if is_cancelled(job_id):
                    outcome = 'cancelled'
                    break

            # Silence, not slowness, is the failure signal: a stalled transfer keeps the process
            # alive and the job in 'downloading' forever, where a slow one still emits progress.
            if now - last_output_at >= DOWNLOAD_STALL_TIMEOUT_SECONDS:
                warn(f'Job {job_id}: no yt-dlp output for {DOWNLOAD_STALL_TIMEOUT_SECONDS}s, giving up')
                pending_lines.append(f'No output for {DOWNLOAD_STALL_TIMEOUT_SECONDS}s; download abandoned')
                outcome = 'stalled'
                break

            # Bounded, so the checks above get another turn while the subprocess is silent.
            try:
                item = line_queue.get(timeout=1)
            except queue.Empty:
                if time.monotonic() - last_flush >= PROGRESS_UPDATE_INTERVAL_SECONDS:
                    flush()
                continue

            if item is _OUTPUT_EOF:
                break

            last_output_at = time.monotonic()
            stripped = item.rstrip()

            completion_speed = parse_completion_speed(stripped)
            if completion_speed:
                final_speed = completion_speed

            progress, speed = parse_progress_line(stripped)
            if progress:
                # Stored exactly as parsed. yt-dlp fetches the video and audio streams one after
                # the other, so a 100% here is one stream finishing rather than the job; the
                # authoritative final value is written by record_completed_download().
                pending_update['progress'] = progress
                if speed:
                    pending_update['speed'] = speed
                # Sampled, or thousands of near-identical lines land in the log column and Loki.
                if time.monotonic() - last_flush >= PROGRESS_UPDATE_INTERVAL_SECONDS:
                    log(stripped)
                    pending_lines.append(stripped)
                    flush()
                continue

            # A full staging disk fails every download after this one too, so it pages once
            # per run, whichever tool (yt-dlp or its ffmpeg) printed it.
            if 'No space left on device' in stripped and not out_of_space_logged:
                out_of_space_logged = True
                log_error(f'Job {job_id}: no space left on device writing to {DOWNLOAD_PATH}: {stripped}')

            # Routed through log() or warn() so yt-dlp's output carries the same timestamped level
            # prefix as everything else rather than arriving unlabelled. yt-dlp's own WARNING: and
            # ERROR: lines go out at WARN only: one failed download is retried, not paged.
            if stripped.startswith('ERROR:'):
                last_error_line = stripped
            if stripped.startswith(('WARNING:', 'ERROR:')):
                warn(stripped)
            else:
                log(stripped)
            pending_lines.append(stripped)
            if time.monotonic() - last_flush >= PROGRESS_UPDATE_INTERVAL_SECONDS:
                flush()
    except BaseException:
        outcome = 'error'
        raise
    finally:
        # A process still running here was cancelled, stalled, or raised past this loop; left
        # alone it keeps downloading into staging with nothing watching it. Only a stream that
        # ended at EOF gets time to exit on its own.
        terminate_process_group(process, grace_seconds=60.0 if outcome == 'finished' else 0.0)
        reader_thread.join(timeout=5)

    if final_speed:
        pending_update['speed'] = final_speed
    flush()
    return outcome, last_error_line


# =======================================================
#            METADATA DISCOVERY AND STORAGE
# =======================================================

# Discover metadata for a job using yt-dlp. fail_on_error marks the job 'failed' on a discovery
# error, which parents want; a video job passes False so a download is still attempted.
def discover_metadata(
    job: Download, fail_on_error: bool = True, store_error: bool = True,
) -> Optional[dict]:
    command = [
        'yt-dlp',
        '--dump-single-json',
        '--skip-download',
        *ytdlp_common_args(),
    ]

    # Only the playlist/channel's own metadata is wanted; scan_video_ids() lists the videos.
    # Without this flag --dump-single-json resolves every video it contains and buffers that
    # blob twice (capture_output, then json.loads), enough to get the container OOM-killed.
    if job.request_type in PARENT_REQUEST_TYPES:
        command.append('--flat-playlist')

    command.append(job.url)

    # The reason is stored whatever fail_on_error says: that flag decides who owns the job's
    # status, not whether yt-dlp's message is worth keeping. Only store_error=False, for a caller
    # that owns neither, leaves the job untouched.
    def record_failure(message: str) -> None:
        # Logged as well as stored, since the stored error only ever surfaces in the web UI.
        warn(f'Job {job.id}: metadata discovery failed: {" ".join(message.split())[:500]}')
        if fail_on_error:
            update_job(job.id, status='failed', error=message)
        elif store_error:
            update_job(job.id, error=message)

    try:
        result = run_command(command)
    except subprocess.TimeoutExpired:
        # Recorded rather than raised: propagating leaves the job, and the loop, stuck.
        record_failure(f'Timed out after {COMMAND_TIMEOUT_SECONDS}s discovering metadata')
        return None
    except subprocess.CalledProcessError as exc:
        # yt-dlp prints its warnings ahead of the ERROR: line; relayed on their own, they cannot
        # crowd the reason out of the truncated log line or the front of the stored error.
        record_failure(relay_ytdlp_warnings(exc.stderr) or exc.stderr or str(exc))
        return None

    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        record_failure(str(exc))
        return None


# Drop yt-dlp's bulky list fields; what is left is still the video's descriptive record.
def strip_bulky_metadata(metadata: dict) -> dict:
    return {key: value for key, value in metadata.items() if key not in BULKY_METADATA_KEYS}


# Serialize a yt-dlp info dictionary for the metadata_json column, bulky list fields dropped.
# Returns None rather than raising when a value will not serialize, so a download is never lost to
# a metadata problem; the caller leaves the existing column alone in that case.
def serialize_metadata(metadata: dict) -> Optional[str]:
    try:
        return json.dumps(strip_bulky_metadata(metadata), ensure_ascii=False, default=str)
    except (TypeError, ValueError) as exc:
        log(f'Failed to serialize metadata for storage: {exc}')
        return None


# Serialize a list-valued metadata field (tags, categories) for storage, ignoring empty lists.
def serialize_list_field(values) -> Optional[str]:
    if not values or not isinstance(values, (list, tuple)):
        return None
    return json.dumps([str(value) for value in values], ensure_ascii=False)


# Coerce a yt-dlp numeric field to int, tolerating strings, floats, and missing values.
def as_int(value) -> Optional[int]:
    if value is None:
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


# Pick a thumbnail URL out of a yt-dlp info dictionary. Under --flat-playlist, which every
# channel scan uses, it often carries only the raw 'thumbnails' list, lowest resolution first.
def extract_thumbnail_url(metadata: dict) -> Optional[str]:
    thumbnail = metadata.get('thumbnail')
    if thumbnail:
        return thumbnail
    thumbnails = metadata.get('thumbnails')
    if thumbnails:
        best = thumbnails[-1]
        if isinstance(best, dict):
            return best.get('url')
    return None


# Extract the stored fields, normalizing the several key names yt-dlp uses for the same thing.
def extract_metadata_fields(metadata: dict) -> dict:
    return {
        'id': normalize_youtube_id(metadata.get('id')),
        'title': metadata.get('title') or metadata.get('fulltitle'),
        'description': metadata.get('description'),
        'upload_date': metadata.get('upload_date') or metadata.get('release_date'),
        'thumbnail': extract_thumbnail_url(metadata),
        'duration': as_int(metadata.get('duration')),
        'view_count': as_int(metadata.get('view_count')),
        'like_count': as_int(metadata.get('like_count')),
        'tags': serialize_list_field(metadata.get('tags')),
        'categories': serialize_list_field(metadata.get('categories')),
        'channel_id': metadata.get('channel_id') or metadata.get('uploader_id'),
        'channel_name': metadata.get('channel') or metadata.get('uploader'),
        'channel_url': metadata.get('channel_url') or metadata.get('uploader_url'),
        'playlist_id': metadata.get('playlist_id') or metadata.get('playlist'),
        'playlist_title': metadata.get('playlist_title') or metadata.get('playlist'),
        'playlist_index': metadata.get('playlist_index'),
    }


# Apply yt-dlp metadata to a job: descriptive fields, YouTube ID, channel and playlist links.
# Videos and parents share this, since they differ only in what the top-level "id" refers to.
# Opens its own session and commits; the passed job is re-read there, not written through.
def apply_metadata(job: Download, metadata: dict) -> None:
    with Session() as db:
        db_job = db.get(Download, job.id)

        if not db_job:
            return

        fields = extract_metadata_fields(metadata)
        request_type = db_job.request_type

        if fields['title']:
            db_job.title = fields['title']

        # Kept so the organizer can still build a complete NFO once the sidecar is cleaned up.
        serialized = serialize_metadata(metadata)
        if serialized:
            db_job.metadata_json = serialized

        # Resolve (or create) the channel this job belongs to. For a channel request the info
        # dictionary describes the channel itself, so its top-level id/title are the channel's.
        channel = None
        channel_id = fields['channel_id'] or (fields['id'] if request_type == 'channel' else None)
        if channel_id:
            channel_name = fields['channel_name'] or (fields['title'] if request_type == 'channel' else None)
            # Only a channel request carries channel-level description and artwork.
            channel = get_or_create_channel(
                db, channel_id, channel_name, fields['channel_url'],
                thumbnail=fields['thumbnail'] if request_type == 'channel' else None,
                description=fields['description'] if request_type == 'channel' else None,
            )
            db_job.channel = channel

        # Only a video row carries per-video detail. Each field is written only when the metadata
        # supplied one, so a partial refresh tops the row up instead of blanking it; youtube_id
        # additionally falls back to the URL, since the organizer tags every filename with it.
        if request_type == 'video':
            db_job.youtube_id = fields['id'] or db_job.youtube_id or extract_youtube_id(db_job.url)
            if fields['description']:
                db_job.description = fields['description']
            if fields['upload_date']:
                db_job.upload_date = fields['upload_date']
            if fields['thumbnail'] and not db_job.thumbnail:
                db_job.thumbnail = fields['thumbnail']
            for column in ('duration', 'view_count', 'like_count', 'tags', 'categories'):
                if fields[column] is not None:
                    setattr(db_job, column, fields[column])

            # The Playlist link is established once, at expansion (see create_child_jobs), and
            # never re-derived here: yt-dlp reports a playlist_id for a standalone video too, via
            # YouTube's implicit "uploads" playlist, which would misfile it as an episode.
            if db_job.playlist_id:
                playlist_index = as_int(fields['playlist_index'])
                if playlist_index:
                    db_job.playlist_index = playlist_index

            # A channel-grouped video has no curated position, so it takes the channel's next free
            # number once dated; resequence_channel_index never renumbers a filed one.
            elif db_job.group_by_channel and db_job.channel_id and fields['upload_date']:
                db.flush()
                resequence_channel_index(db, db_job.channel_id)

        elif request_type == 'playlist':
            # A playlist request's info dictionary describes the playlist itself.
            playlist_id = fields['id'] or fields['playlist_id']
            if playlist_id:
                db_job.youtube_id = playlist_id
                playlist = get_or_create_playlist(
                    db, playlist_id, fields['title'] or db_job.title, channel=channel
                )
                # Carry the playlist's own description and artwork over for the show NFO/poster.
                if fields['description']:
                    playlist.description = fields['description']
                if fields['thumbnail']:
                    playlist.thumbnail = fields['thumbnail']
                db_job.playlist = playlist
                db_job.description = fields['description'] or db_job.description

        elif request_type == 'channel':
            # A channel request has no playlist; its identity is the channel it points at.
            if channel_id:
                db_job.youtube_id = channel_id
            if fields['description']:
                db_job.description = fields['description']

        db.commit()


# =======================================================
#        REDOWNLOAD, RENAME AND RESTART RECOVERY
# =======================================================

# Re-discover metadata and optionally redownload/rename files for a job. Returns True on success.
# A parent re-expands or requeues its children; a video is renamed in staging, re-organized where it
# already sits in the library, or falls through to a full download_video(), so this can block for
# the length of a download.
def redownload_or_rename(job: Download) -> bool:
    log(f'Redoing metadata/filename for job {job.id} ({job.request_type})')
    append_log(job.id, 'Redownload/rename requested...')

    # A parent refreshes its own metadata first, then pushes the work down to its children. It
    # never downloads anything itself. Unlike the video path below, a failure here is fatal to the
    # call: without fresh metadata there is nothing to propagate.
    if job.request_type in PARENT_REQUEST_TYPES:
        metadata = discover_metadata(job)

        if not metadata:
            append_log(job.id, 'Redownload/rename failed for parent job.')
            log(f'Job {job.id}: redownload/rename failed')
            return False

        apply_metadata(job, metadata)
        append_log(job.id, 'Redownload/metadata applied successfully for parent.')

        # Re-fetched in its own session: `job` came from the queue and its children may have
        # changed since, and the whole child rewrite below has to commit as one transaction.
        with Session() as db:
            db_parent = db.get(Download, job.id)
            if not db_parent:
                return False

            # A parent with no children was never expanded -- the redownload becomes that first
            # expansion instead of a refresh.
            if not db_parent.children:
                append_log(job.id, 'No children found; expanding parent to create child jobs')
                try:
                    expand_playlist(db_parent)
                except Exception as exc:
                    append_log(job.id, f'Failed to expand parent: {exc}')
                    return False
                return True

            # A Cancel or Delete the user sent while the metadata was fetched stands: the rows are
            # left as the app wrote them, and the parent settles from them -- a cancel still
            # pending as 'cancelled' once nothing runs. Rewriting them would undo the request.
            if db_parent.status != 'redownload' or db_parent.cancel_requested:
                append_log(job.id, 'Stopped by the user during the refresh; children left as is')
                settle_parent(db_parent)
                db.commit()
                return True

            # A child with a file on disk gets a rename-first refresh; the rest are queued.
            for child in db_parent.children:
                # The redownload request cleared every child's flag, so one set now is a Cancel of
                # that video sent during the refresh, and the app's write stands.
                if child.cancel_requested:
                    append_log(job.id, f'Child {child.id} cancelled during the refresh; left as is')
                    continue
                child.organized_at = None
                child.error = None
                child.progress = None
                child.speed = None
                if child.filename:
                    child.status = 'redownload'
                    append_log(job.id, f'Marked child {child.id} for redownload')
                else:
                    child.status = 'queued'
                    append_log(job.id, f'Marked child {child.id} queued for redownload')

            # The parent shows queued while its children work; settle_parent settles it again
            # once they finish. One commit covers the whole hierarchy.
            db_parent.cancel_requested = False
            db_parent.status = 'queued'
            db_parent.error = None
            db_parent.progress = None
            db_parent.speed = None
            db.commit()
        return True

    # Past here the job is a single video. fail_on_error=False so a metadata lookup that fails
    # leaves the existing row untouched rather than half-updated.
    metadata = discover_metadata(job, fail_on_error=False)

    if not metadata:
        append_log(job.id, 'Redownload/rename failed for video job.')
        log(f'Job {job.id}: redownload/rename failed')
        return False

    apply_metadata(job, metadata)
    append_log(job.id, 'Redownload/metadata applied successfully for video.')

    # Resolve what the row points at now. A relative path is read against staging, and a path
    # that no longer resolves is discarded so the recovery below can search for the real file.
    current_file = None
    if job.filename:
        current_file = Path(job.filename)
        if not current_file.is_absolute():
            current_file = DOWNLOAD_PATH / current_file
        if not current_file.exists():
            current_file = None

    # The recorded path goes stale while the file itself is fine when Tdarr transcodes in place
    # under a different extension. Recovered by youtube_id, which survives a remux.
    if current_file is None:
        recovered = find_existing_media(job)
        if recovered:
            log(f'Job {job.id}: recorded path was stale ({job.filename}); found {recovered}')
            append_log(job.id, f'Recorded path was stale; using {recovered}')
            update_job(job.id, filename=str(recovered))
            job.filename = str(recovered)
            current_file = recovered

    # A file already in the Jellyfin library must never be renamed back into flat staging, which
    # is all resolve_expected_output_path() below can produce. Re-organize this one job instead,
    # rewriting its NFO -- correct only because the user targeted this job explicitly.
    if current_file and current_file.is_relative_to(MEDIA_PATH):
        append_log(job.id, 'Existing file is already in the media library; refreshing metadata in place')
        update_job(job.id, status='completed', completed_at=utcnow(), progress='100%', organized_at=None)
        try:
            organizer_organize_single_job(job.id)
        except Exception as exc:
            log_error(f'Job {job.id}: organizer run failed', exc)
        return True

    # A file still in staging is renamed to the filename yt-dlp would produce now.
    expected_path = resolve_expected_output_path(job)
    if current_file and expected_path:
        # The .info.json travels with the video: leaving it under the old stem would strand it,
        # and find_sidecar() matches on the stem, so the organizer would stop finding it.
        expected_info = expected_path.with_name(expected_path.stem + '.info.json')
        current_info = current_file.with_name(current_file.stem + '.info.json')
        renamed = True

        # resolve() on both sides so a path that only differs by symlink or case is recognised
        # as already correct rather than renamed onto itself.
        if current_file.resolve() != expected_path.resolve():
            expected_path.parent.mkdir(parents=True, exist_ok=True)
            try:
                current_file.rename(expected_path)
                if current_info.exists():
                    current_info.rename(expected_info)
                append_log(job.id, f'Renamed existing file to {expected_path}')
            except Exception as exc:
                append_log(job.id, f'Failed to rename existing file: {exc}')
                log_error(f'Job {job.id}: failed to rename {current_file} to {expected_path}', exc)
                renamed = False

        # Completing on a failed rename would point the job at a path nothing ever created, so
        # that case drops through to the full download below instead.
        if renamed:
            update_job(
                job.id,
                status='completed',
                completed_at=utcnow(),
                progress='100%',
                filename=str(expected_path),
                metadata_file=str(expected_info) if expected_info.exists() else None,
            )
            return True

    # Not while the library is unavailable: a file recorded there is only out of reach, and a new
    # copy would be filed beside it once the disk is back. The job fails for now, and a later retry
    # finds the file again.
    if has_library_file(job) and not organizer_library_ready():
        append_log(job.id, 'Library unavailable; not downloading its recorded file again')
        update_job(job.id, error='Library unavailable: the recorded file is out of reach')
        return False

    # No existing file was found or it could not be renamed; download it normally.
    append_log(job.id, 'No existing file found; queuing for full redownload')
    update_job(job.id, status='queued')
    download_video(job)
    return True


# True while a video job still has no title, which is the whole test download_video() applies before
# spending a metadata lookup. Any other field missing from the row goes unnoticed.
def needs_video_metadata(job: Download) -> bool:
    return not job.title


# Re-read from the database on every call: the flag is set by the web process, not this one.
def is_cancelled(job_id: int) -> bool:
    with Session() as db:
        job = db.get(Download, job_id)
        return bool(job and job.cancel_requested)


# Re-home jobs left in RESTART_RECOVERY_STATUSES by an unclean restart. A job with a cancel
# pending is cancelled instead, and its parent settled in the same commit.
def recover_stuck_jobs() -> None:
    with Session() as db:
        stuck_jobs = db.query(Download).filter(Download.status.in_(RESTART_RECOVERY_STATUSES)).all()

        if not stuck_jobs:
            log('No stuck jobs to recover')
            return

        cancelled_parent_ids = set()
        for job in stuck_jobs:
            log(f'Recovering stuck job {job.id} ({job.request_type}) status={job.status}')

            # Whatever the interrupted job reported last is stale by definition.
            job.progress = None
            job.speed = None

            if job.cancel_requested:
                job.status = 'cancelled'
                job.error = 'Cancelled during restart'
                if job.parent_id is not None:
                    cancelled_parent_ids.add(job.parent_id)
            else:
                job.error = None
                # Interrupted mid-expansion; 'expanding' keeps the UI showing discovery pending.
                if job.request_type in PARENT_REQUEST_TYPES and not job.children:
                    job.status = 'expanding'
                else:
                    job.status = 'queued'

        # A child cancelled here stopped without update_job(), which would otherwise settle its
        # parent; left alone, a parent with its cancel pending shows 'cancel requested' for good.
        for parent_id in cancelled_parent_ids:
            parent = db.get(Download, parent_id)
            if parent is not None:
                settle_parent(parent)

        db.commit()
        log(f'Recovered {len(stuck_jobs)} stuck job(s)')



# Populate metadata for parent jobs still missing a title, e.g. queued before discovery ran.
def populate_missing_parent_metadata() -> None:
    with Session() as db:
        parents = (
            db.query(Download)
            .filter(Download.request_type.in_(PARENT_REQUEST_TYPES), Download.title.is_(None))
            .all()
        )

        # Only parents that already expanded: one still waiting to expand gets its title from
        # that pass anyway, and discovering it here would spend a network call to duplicate it.
        candidates = [p for p in parents if p.children]
        # Logged unconditionally: each candidate costs a network round trip bounded only by
        # COMMAND_TIMEOUT_SECONDS, which would otherwise be a silent gap in the log.
        if candidates:
            log(f'populate_missing_parent_metadata: {len(candidates)} parent(s) missing a title, discovering...')

        updated = 0
        for parent in candidates:
            log(f'populate_missing_parent_metadata: discovering metadata for parent {parent.id} ({parent.url})')

            # Guarded per parent so one unreachable URL doesn't abandon the rest of the batch;
            # a parent that fails simply keeps its missing title and is retried next cycle. Its
            # status and error are not this repair's to change: a settled parent stays settled,
            # and a failed lookup only logs WARN.
            metadata = None
            try:
                metadata = discover_metadata(parent, fail_on_error=False, store_error=False)
            except Exception as exc:
                log_error(f'populate_missing_parent_metadata: parent {parent.id} failed', exc)

            if metadata:
                apply_metadata(parent, metadata)
                updated += 1

        if updated:
            log(f'Populated metadata for {updated} parent job(s)')


# =======================================================
#               PARENT STATUS AGGREGATION
# =======================================================

# Settle a child's parent now that the child has stopped (jobstate.settle_parent), in its own
# session, marking the parent finished once none of its children have work left. update_job()
# calls it for every terminal status. Returns True once the parent has settled into a terminal
# state with nothing in flight.
def finalize_parent_if_done(child_job_id: int) -> bool:
    with Session() as db:
        child = db.get(Download, child_job_id)

        if not child or not child.parent_id:
            return False

        parent = db.get(Download, child.parent_id)

        if not parent:
            return False

        # Any child still active -- 'redownload' and 'expanding' too -- keeps the parent going.
        finished = settle_parent(parent)
        db.commit()

    if finished:
        try:
            compute_and_persist_parent_progress(parent.id)
        except Exception as exc:
            log_error(f'Failed to aggregate progress for parent {parent.id}', exc)

    return finished


# Persist a parent's aggregate progress and speed so a reader of the row sees the same numbers
# the UI computes. The aggregation rules live in jobstate.py, shared with the web app.
def compute_and_persist_parent_progress(parent_id: int) -> None:
    with Session() as db:
        parent = db.get(Download, parent_id)
        if not parent or not parent.children:
            return

        progress_str = compute_parent_progress(parent)
        speed_str = compute_parent_speed(parent)

        # Persist only if values changed to avoid noisy DB commits
        changed = False
        if progress_str != parent.progress:
            parent.progress = progress_str
            changed = True
        if speed_str != parent.speed:
            parent.speed = speed_str
            changed = True
        if changed:
            db.commit()


# =======================================================
#            PLAYLIST AND CHANNEL EXPANSION
# =======================================================

# Scan a playlist or channel URL for the video IDs it contains, without downloading anything;
# `playlist_items` is passed to yt-dlp's --playlist-items when given. Returns (ids, error_message),
# at most one of them meaningful: the ids are video ids only, once each, in listing order. Output
# with lines but no video id among them is an error; no output at all is an empty listing.
# Dropped lines are logged in one WARN.
def scan_video_ids(
    url: str, playlist_items: Optional[str] = None,
) -> tuple[list[str], Optional[str]]:
    log(f'Scanning: {url}' + (f' (items {playlist_items})' if playlist_items else ''))

    command = [
        'yt-dlp',
        *ytdlp_common_args(),
        '--flat-playlist',
        '--print',
        '%(id)s',
    ]
    # See PERIODIC_CHANNEL_SCAN_LIMIT.
    if playlist_items:
        command += ['--playlist-items', playlist_items]
    command.append(url)

    try:
        result = run_command(command)
    except subprocess.TimeoutExpired:
        # Reported rather than raised: propagating leaves the job, and the loop, stuck.
        return [], f'Timed out after {COMMAND_TIMEOUT_SECONDS}s scanning playlist/channel'
    except subprocess.CalledProcessError as exc:
        return [], exc.stderr or str(exc)

    # Normalized so they match stored youtube_id values. A listing can carry ids that are no
    # video's, such as a playlist's or a channel's, and yt-dlp prints NA for an entry with no id;
    # recorded as videos, each would become a row. A video listed twice is kept once, or it would
    # get two rows.
    ids = []
    seen = set()
    rejected = []
    for raw_line in result.stdout.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        video_id = normalize_youtube_id(line)
        if video_id and YOUTUBE_ID_PATTERN.match(video_id):
            if video_id not in seen:
                seen.add(video_id)
                ids.append(video_id)
        else:
            rejected.append(line)

    if rejected:
        sample = ', '.join(repr(line[:40]) for line in rejected[:3])
        warn(
            f'Scan of {url}: dropped {len(rejected)} line(s) that are not video ids, e.g. {sample}'
        )
    # An error rather than an empty listing, so it cannot be recorded as a subscription's baseline.
    if rejected and not ids:
        return [], f'No video ids in the listing: all {len(rejected)} line(s) were dropped'
    return ids, None


# Re-derive a parent's status from its known children without touching YouTube, so a failed
# expansion leaves it reflecting reality rather than stuck mid-transition.
def resume_from_existing_children(job_id: int, reason: str) -> None:
    log(f'Job {job_id}: {reason}, resuming with existing children')
    with Session() as db:
        parent = db.get(Download, job_id)
        if parent:
            settle_parent(parent)
            db.commit()


# Expand a playlist or channel job into one child download job per video it contains.
def expand_playlist(job: Download) -> None:
    log(f'Expanding playlist/channel job {job.id}: {job.url}')

    # A parent that already has children is being re-scanned (retry, redownload, interrupted
    # expansion): every failure below falls back to those children rather than failing the
    # parent, so a network hiccup cannot discard a set of perfectly good queued children.
    with Session() as db:
        db_job = db.get(Download, job.id)
        already_has_children = bool(db_job.children) if db_job else False

    update_job(job.id, status='expanding')

    metadata = discover_metadata(job, fail_on_error=not already_has_children)

    # A cancel that arrived during discovery still applies.
    with Session() as db:
        db_parent = db.get(Download, job.id)
        if db_parent and db_parent.cancel_requested:
            update_job(job.id, status='cancelled', error='Cancelled by user')
            log(f'Parent job {job.id} cancelled during discovery')
            return

    if not metadata:
        if already_has_children:
            resume_from_existing_children(job.id, 'metadata refresh failed')
            return
        warn(f'Failed to discover playlist/channel metadata for job {job.id}')
        update_job(job.id, status='failed', error='Failed to discover playlist/channel')
        return

    apply_metadata(job, metadata)

    video_ids, scan_error = scan_video_ids(job.url)

    # A channel scan can run for minutes, so the cancel is re-read here rather than trusted from
    # before it: creating hundreds of children for a request the user already cancelled is worse
    # than losing the scan.
    with Session() as db:
        db_parent = db.get(Download, job.id)
        if db_parent and db_parent.cancel_requested:
            update_job(job.id, status='cancelled', error='Cancelled by user')
            log(f'Parent job {job.id} cancelled after scan')
            return

    # A failed or empty scan falls back to children from an earlier run where there are any --
    # YouTube rate-limiting a re-scan must not strip a playlist that already expanded.
    if scan_error:
        if already_has_children:
            resume_from_existing_children(job.id, f'scan failed ({scan_error})')
            return
        update_job(job.id, status='failed', error=scan_error)
        return

    if not video_ids:
        if already_has_children:
            resume_from_existing_children(job.id, 'scan returned no videos')
            return
        log(f'No videos found during scan for job {job.id}')
        update_job(job.id, status='failed', error='No videos found')
        return

    # Indexes are backfilled after the children exist, and from the same id list, so episode
    # numbers follow the order the scan returned rather than the order rows happened to be made.
    log(f'expand_playlist: creating children for parent {job.id}, video_ids_count={len(video_ids)}')
    created = create_child_jobs(job, video_ids)
    backfill_playlist_indexes(job.id, video_ids)
    log(f'Created {created} child jobs for parent job {job.id}')


# Statuses of an unparented row that an explicit playlist or channel request queues again when it
# adopts the row: recorded by a subscription without downloading, or deleted or cancelled earlier.
# A failed row is left to auto-retry.
REQUEUE_ON_ADOPTION_STATUSES = frozenset({SKIPPED_STATUS, 'deleted', 'cancelled'})


# Create one child Download per video, without duplicating videos that already have a row: an
# unparented row is adopted instead, and one per video queued again when every row adopted for it
# is in REQUEUE_ON_ADOPTION_STATUSES.
def create_child_jobs(parent: Download, video_ids: Iterable[str]) -> int:
    # Materialized so the IDs can be iterated more than once below.
    video_ids = list(video_ids)

    if not video_ids:
        update_job(parent.id, status='failed', error='No videos found')
        return 0

    with Session() as db:
        parent = db.get(Download, parent.id)

        if not parent:
            return 0

        if parent.cancel_requested:
            parent.status = 'cancelled'
            db.commit()
            return 0

        # Every row for these videos in one query, split into two sets: already a child of this
        # parent (skip it), and present anywhere else (adopt rather than download a second copy).
        # One query, because a per-video lookup makes a 500-video playlist 500 round trips. Oldest
        # first, so "the first" row of a video below is a defined one.
        matches = (
            db.query(Download)
            .filter(Download.youtube_id.in_(video_ids))
            .order_by(Download.id)
            .all()
        )
        existing_ids_all = {d.youtube_id for d in matches if d.youtube_id}
        existing_ids_parent = {d.youtube_id for d in matches if d.youtube_id and d.parent_id == parent.id}
        log(f'create_child_jobs: parent={parent.id} video_ids={len(video_ids)} existing_all={len(existing_ids_all)} existing_parent={len(existing_ids_parent)}')

        added = 0
        adopted = 0
        requeued = 0

        # Only a playlist's scan order is a curated sequence worth keeping as playlist_index;
        # channel children get theirs from resequence_channel_index: the channel's next free
        # numbers, in upload_date order.
        index_children = parent.request_type == 'playlist'
        # Channel children share one per-channel library folder; playlist children become
        # episodes of a show via playlist_id instead.
        group_by_channel = parent.request_type == 'channel'
        # Numbered through PlaylistNumbering, so a number another video holds in the playlist (a
        # subscription's row, say) is never handed out a second time.
        numbering = None
        if index_children and parent.playlist_id is not None:
            numbering = PlaylistNumbering(db, parent.playlist_id)

        # The episode number of a playlist child at scan `position`; a channel child gets None.
        def child_number(position: int, video_id: str) -> Optional[int]:
            if not index_children:
                return None
            return numbering.assign(position, video_id) if numbering else position

        # True when an adopted `row` is already numbered in the show this parent files into: a
        # playlist row holding a number in this playlist, or a channel row of this channel.
        def numbered_in_this_show(row: Download) -> bool:
            if row.playlist_index is None:
                return False
            if index_children:
                return numbering is not None and row.playlist_id == parent.playlist_id
            return (
                group_by_channel
                and parent.channel_id is not None
                and row.group_by_channel
                and row.channel_id == parent.channel_id
                and row.playlist_id is None
            )

        # One commit at the end: committing per video turns a 500-video playlist into 500
        # transactions and 500 extra parent lookups.
        for position, video_id in enumerate(video_ids, start=1):
            if video_id in existing_ids_parent:
                existing_child = next(
                    (d for d in matches if d.youtube_id == video_id and d.parent_id == parent.id), None
                )
                if existing_child and existing_child.playlist_index is None and index_children:
                    existing_child.playlist_index = child_number(position, video_id)
                continue

            # Every unparented row for this video is adopted, not just the first: rows may share
            # a youtube_id, and skipping the rest leaves a duplicate filed separately on disk.
            # The associations mirror the fresh child below, or it organizes as a movie.
            existing_unparented_rows = [d for d in matches if d.youtube_id == video_id and d.parent_id is None]
            if existing_unparented_rows:
                # The request asked for this video, so when every row adopted for it is one nothing
                # would ever run again, the oldest is queued; one only, as twins would download it
                # twice, and none beside a row that holds the video or will fetch it anyway.
                requeue_row = None
                if all(d.status in REQUEUE_ON_ADOPTION_STATUSES for d in existing_unparented_rows):
                    requeue_row = existing_unparented_rows[0]
                for existing_unparented in existing_unparented_rows:
                    # A row already numbered in this show keeps its number and, filed there
                    # already, its organized_at. Any other number belonged to another show.
                    keeps_number = numbered_in_this_show(existing_unparented)
                    existing_unparented.parent_id = parent.id
                    existing_unparented.playlist_id = parent.playlist_id
                    existing_unparented.channel_id = parent.channel_id
                    existing_unparented.group_by_channel = group_by_channel
                    if not keeps_number:
                        existing_unparented.playlist_index = child_number(position, video_id)
                        # An adopted video may already sit in the library as its own movie; clearing
                        # organized_at refiles it under the playlist's show or the channel's folder.
                        if existing_unparented.organized_at is not None:
                            existing_unparented.organized_at = None
                    # Queued with its numbering above kept; left as it was, it is never downloaded.
                    if existing_unparented is requeue_row:
                        existing_unparented.status = 'queued'
                        existing_unparented.error = None
                        existing_unparented.cancel_requested = False
                        existing_unparented.progress = None
                        existing_unparented.speed = None
                        existing_unparented.completed_at = None
                        requeued += 1
                    adopted += 1
                    log(f'Adopted existing download {existing_unparented.id} into parent {parent.id}')
                continue

            # A fresh child, even when the video already exists under another parent.
            db.add(Download(
                url=f'https://www.youtube.com/watch?v={video_id}',
                youtube_id=video_id,
                request_type='video',
                parent_id=parent.id,
                playlist_id=parent.playlist_id,
                channel_id=parent.channel_id,
                group_by_channel=group_by_channel,
                playlist_index=child_number(position, video_id),
                status='queued',
            ))
            added += 1

        # Make the new rows visible on parent.children before the status is derived from them.
        db.flush()
        db.refresh(parent)

        # A cancel that arrived while the children were being created applies to all of them.
        if parent.cancel_requested:
            for child in parent.children:
                if child.status in ACTIVE_STATUS_SET:
                    child.status = 'cancelled'
                    child.cancel_requested = True
                    child.progress = None
                    child.speed = None
            parent.status = 'cancelled'
            db.commit()
            log(f'Parent {parent.id}: cancelled during child creation')
            return added

        # Adopted children may already carry an upload_date, so their order is known now.
        if group_by_channel and parent.channel_id:
            resequence_channel_index(db, parent.channel_id)

        # The shared rule covers freshly queued children, adopted children already finished, and
        # a playlist that was fully downloaded before.
        if parent.children:
            settle_parent(parent)
        else:
            # Nothing attached at all: queued, so the worker revisits rather than sitting in
            # 'expanding' forever.
            parent.status = 'queued'

        db.commit()
        log(
            f'create_child_jobs: parent={parent.id} added={added} adopted={adopted} '
            f'requeued={requeued} status={parent.status}'
        )

    return added


# Hands out episode numbers in one pass over a playlist's scan. A video that already holds a number
# in the playlist gets it again, its firmest (see number_rank), so one video is one episode. Any
# other video takes its scan position, or, when another video's row holds that position, the
# number after the highest held. Every number handed out counts as held from then on, so two
# videos numbered in one pass never share one.
class PlaylistNumbering:
    # Reads which videos hold which numbers in playlist `playlist_id`, rows of any parent
    # included, in one query.
    def __init__(self, db, playlist_id: int):
        self.holders: dict[int, set[Optional[str]]] = {}
        # Each video's firmest number, as (number_rank, number).
        self.best: dict[Optional[str], tuple[int, int]] = {}
        rows = (
            db.query(Download)
            .filter(Download.playlist_id == playlist_id, Download.playlist_index.isnot(None))
            .all()
        )
        for row in rows:
            self.hold(row.playlist_index, row.youtube_id, number_rank(row))
        self.highest = max(self.holders, default=0)

    # Record that `video_id` holds number `index`, as firmly as `rank` (see number_rank).
    def hold(self, index: int, video_id: Optional[str], rank: int) -> None:
        self.holders.setdefault(index, set()).add(video_id)
        if video_id not in self.best or (rank, index) < self.best[video_id]:
            self.best[video_id] = (rank, index)

    # The number for `video_id` at scan `position`, recorded as held by that video.
    def assign(self, position: int, video_id: str) -> int:
        if video_id in self.best:
            index = self.best[video_id][1]
        elif self.holders.get(position, set()) - {video_id}:
            index = self.highest + 1
        else:
            index = position
        # A number handed out now belongs to a live row.
        self.hold(index, video_id, 1)
        self.highest = max(self.highest, index)
        return index


# Create download jobs for a subscription, skipping videos that already have a row. queue=False
# still creates them, as SKIPPED_STATUS, so a later scan cannot mistake them for new, and
# requeue_skipped queues the listed videos' unparented SKIPPED_STATUS rows again. A playlist
# subscription numbers its rows by scan position, existing rows still without a number included.
def create_download_jobs_for_subscription(
    db, subscription: Subscription, video_ids: Iterable[str], queue: bool = True,
    requeue_skipped: bool = False,
) -> int:
    if not video_ids:
        return 0

    video_ids = list(video_ids)
    group_by_channel = subscription.subscription_type == 'channel'

    # Full rows, not just ids, so a standalone download can be adopted rather than skipped. A
    # list per id, since rows may share a youtube_id and only the last would ever be seen.
    existing_by_id: dict[str, list[Download]] = {}
    for row in db.query(Download).filter(Download.youtube_id.in_(video_ids)).all():
        if row.youtube_id:
            existing_by_id.setdefault(row.youtube_id, []).append(row)

    # A channel scan lists newest first, so its rows are created oldest first, and the worker
    # downloads them in that order: in upload order through the uploads playlist, or within each
    # kind, Shorts first, when a channel's root URL was read instead (see scan_subscription). A
    # playlist keeps scan order, the playlist's own, and numbers each video by its scan position.
    scan = list(enumerate(video_ids, start=1))
    if group_by_channel:
        scan.reverse()
    numbering = None
    if subscription.subscription_type == 'playlist' and subscription.playlist_id is not None:
        numbering = PlaylistNumbering(db, subscription.playlist_id)

    # Every row this pass queues, new or requeued, is stamped created_at one microsecond after the
    # last, in the order reached, so get_next_job() takes them in scan order. A requeued row would
    # otherwise keep the created_at it was recorded with, which need not follow scan order.
    queued_at = utcnow()
    queue_stamps = (queued_at + timedelta(microseconds=step) for step in itertools.count())

    added = 0
    adopted = 0
    numbered = 0
    requeued = 0
    for position, video_id in scan:
        existing_rows = existing_by_id.get(video_id)
        if existing_rows:
            any_unparented = False
            # One requeued row per video, or duplicates an older scan recorded download it twice.
            requeue_this_video = requeue_skipped
            for existing in existing_rows:
                # A video already parented by another playlist/channel expansion stays put.
                if existing.parent_id is not None:
                    continue
                any_unparented = True
                # Only associations this subscription has an opinion about: a channel one's
                # playlist_id is always None, and that None must not overwrite a real value.
                joins_playlist = (
                    subscription.playlist_id is not None
                    and existing.playlist_id != subscription.playlist_id
                )
                needs_update = (
                    (subscription.channel_id is not None and existing.channel_id != subscription.channel_id)
                    or joins_playlist
                    or existing.group_by_channel != group_by_channel
                )
                # Ahead of the check below, so every scan numbers the rows still without one, even
                # where nothing else changes. A row joining the playlist gets a new number, its old
                # one being another show's; a number it already holds in this playlist stays.
                if numbering and (joins_playlist or existing.playlist_index is None):
                    existing.playlist_index = numbering.assign(position, video_id)
                    if not needs_update:
                        numbered += 1
                # Also ahead of that check: a skipped row is queued whether or not its associations
                # change.
                if requeue_this_video and existing.status == SKIPPED_STATUS:
                    existing.status = 'queued'
                    existing.created_at = next(queue_stamps)
                    requeue_this_video = False
                    requeued += 1
                if not needs_update:
                    continue
                if subscription.channel_id is not None:
                    existing.channel_id = subscription.channel_id
                if subscription.playlist_id is not None:
                    existing.playlist_id = subscription.playlist_id
                existing.group_by_channel = group_by_channel
                # A row with no playlist leaves its number behind with the show it came from, and
                # resequence_channel_index numbers it for this channel. A row with a playlist keeps
                # its index: it is still filed as that playlist's episode.
                if group_by_channel and existing.playlist_id is None:
                    existing.playlist_index = None
                # Clearing organized_at refiles it into the shared folder, as in create_child_jobs.
                if existing.organized_at is not None:
                    existing.organized_at = None
                adopted += 1
                log(f'Adopted existing download {existing.id} into subscription {subscription.id}')
            if any_unparented:
                continue

        download = Download(
            url=f'https://www.youtube.com/watch?v={video_id}',
            request_type='video',
            youtube_id=video_id,
            channel_id=subscription.channel_id,
            playlist_id=subscription.playlist_id,
            group_by_channel=group_by_channel,
            # SKIPPED_STATUS rows too, so a baseline video is filed once it is queued later.
            playlist_index=numbering.assign(position, video_id) if numbering else None,
            status='queued' if queue else SKIPPED_STATUS,
        )
        if queue:
            download.created_at = next(queue_stamps)
        db.add(download)
        added += 1

    # Adopted videos may already carry an upload_date, as in create_child_jobs.
    if group_by_channel and subscription.channel_id and (added or adopted or requeued):
        resequence_channel_index(db, subscription.channel_id)

    # Committing is left to check_subscriptions, which shares this session and commits these rows
    # with the subscription's stamp. A second Session() here would deadlock against it: SQLite
    # takes one writer.
    if added or adopted:
        verb = 'Queued' if queue else 'Recorded (not queued)'
        log(f'{verb} {added} new and adopted {adopted} existing download(s) for subscription {subscription.id}')
    if requeued:
        log(f'Requeued {requeued} skipped download(s) for subscription {subscription.id}')
    if numbered:
        log(f'Numbered {numbered} existing download(s) for subscription {subscription.id}')

    return added


# =======================================================
#        PERIODIC TASKS: RETRY AND SUBSCRIPTIONS
# =======================================================

# Attempt count and earliest next retry per top-level job. In memory, so a restart retries at
# once -- which is what a restart should do.
_retry_backoff: dict[int, tuple[int, float]] = {}


# Automatically retry every failed top-level job. Genuine failures only, never an explicit user
# cancel, unlike the manual Retry button: a hierarchy whose top row is cancelled or has a cancel
# pending is skipped whole, and so is a child with a cancel pending. Each retry of the same job
# waits twice as long as the last, or a permanently dead video costs a YouTube request every five
# minutes forever.
def retry_failed_jobs() -> None:
    now = time.monotonic()

    with Session() as db:
        # A parent qualifies on its own stored 'failed' status or on any child's, since the
        # parent's stored status may not have caught up yet -- see resolve_parent_status. A
        # cancelled one does not: its children's failures are no reason to restart what the user
        # stopped.
        candidates = (
            db.query(Download)
            .filter(
                Download.parent_id.is_(None),
                Download.status != 'cancelled',
                Download.cancel_requested.is_(False),
                or_(
                    Download.status == 'failed',
                    Download.id.in_(
                        db.query(Download.parent_id).filter(
                            Download.parent_id.isnot(None),
                            Download.status == 'failed',
                        )
                    ),
                ),
            )
            .all()
        )

        # A job that is no longer failing starts from the shortest delay again.
        candidate_ids = {job.id for job in candidates}
        for job_id in list(_retry_backoff):
            if job_id not in candidate_ids:
                del _retry_backoff[job_id]

        retried = 0
        deferred = 0
        for job in candidates:
            # A job still inside its backoff window is counted and skipped, not reset.
            attempts, retry_after = _retry_backoff.get(job.id, (0, 0.0))
            if now < retry_after:
                deferred += 1
                continue

            # Only 'failed' is passed, never 'cancelled': an explicit cancel must not be undone
            # by an automatic retry, nor one still pending on a video that failed before the
            # worker acted on it. A parent shows queued while its children rerun.
            if apply_status_update_to_hierarchy(
                job,
                lambda target: (
                    not target.cancel_requested
                    and reset_job_for_retry(target, frozenset({'failed'}))
                ),
            ):
                # A deleted parent stays deleted around a video fetched again under it, as
                # jobstate.settle_parent keeps it.
                if job.children and job.status != 'deleted':
                    job.status = 'queued'
                retried += 1

            # The window doubles per attempt whether or not this pass reset anything, so a
            # permanently dead video backs off to RETRY_BACKOFF_MAX_SECONDS instead of retrying
            # on every cycle forever.
            delay = min(RETRY_INTERVAL_SECONDS * 2 ** attempts, RETRY_BACKOFF_MAX_SECONDS)
            _retry_backoff[job.id] = (attempts + 1, now + delay)

        if retried or deferred:
            log(f'Auto-retried {retried} failed job(s), {deferred} still backing off')
        db.commit()


# Consecutive failed checks per subscription id, cleared by a successful scan. In memory, so a
# worker restart starts every count from zero and can delay the ERROR by up to two more checks.
_subscription_failures: dict[int, int] = {}

# Every this-many consecutive failed checks of one subscription log ERROR rather than WARN: one
# failed scan is usually a proxy or rate-limit blip, a run of them needs a human.
SUBSCRIPTION_FAILURES_BEFORE_ERROR = 3


# Log one failed check of a subscription and count it: WARN, except on every
# SUBSCRIPTION_FAILURES_BEFORE_ERROR-th failure in a row, which logs ERROR so the alert pages.
def record_subscription_failure(subscription_id: int, detail: str) -> None:
    count = _subscription_failures.get(subscription_id, 0) + 1
    _subscription_failures[subscription_id] = count
    if count % SUBSCRIPTION_FAILURES_BEFORE_ERROR == 0:
        log_error(f'Subscription {subscription_id}: {count} consecutive checks failed, latest: {detail}')
    else:
        warn(f'Subscription {subscription_id}: {detail}')


# Forget a subscription's run of failed checks once a scan of it succeeds.
def clear_subscription_failures(subscription_id: int) -> None:
    _subscription_failures.pop(subscription_id, None)


# How many of a channel's newest uploads a check reads once the subscription has its baseline,
# through yt-dlp's --playlist-items: the newest this many of the uploads playlist, or, read through
# the channel URL, up to this many of each kind. A first scan and every playlist scan read the
# whole listing.
PERIODIC_CHANNEL_SCAN_LIMIT = 50

# A YouTube channel id; its uploads playlist id is the same with UU in place of UC.
CHANNEL_ID_PATTERN = re.compile(r'^UC[A-Za-z0-9_-]{22}$')

# The path of a channel URL that names no tab: /@handle, /channel/<id>, /c/<name> or /user/<name>,
# with or without a trailing slash.
CHANNEL_ROOT_PATH = re.compile(r'/(?:@[^/]+|(?:channel|c|user)/[^/]+)/?')

# Subscriptions whose uploads playlist failed in this worker run, while their own URL listed
# uploads, and has not worked since: only the first such fallback of each logs WARN, later ones
# INFO.
_uploads_playlist_fallbacks: set[int] = set()


# How many of `video_ids` have no row yet, under any request or subscription.
def count_unrecorded(db, video_ids: list[str]) -> int:
    if not video_ids:
        return 0
    listed = db.query(Download.youtube_id).filter(Download.youtube_id.in_(video_ids))
    recorded = {youtube_id for (youtube_id,) in listed}
    return sum(1 for video_id in video_ids if video_id not in recorded)


# The uploads playlist of the channel `channel_youtube_id`: every public upload of every kind
# (videos, live streams, Shorts) in one listing, newest first. None for an id that is no channel's.
def uploads_playlist_url(channel_youtube_id: Optional[str]) -> Optional[str]:
    if not channel_youtube_id or not CHANNEL_ID_PATTERN.match(channel_youtube_id):
        return None
    return f'https://www.youtube.com/playlist?list=UU{channel_youtube_id[2:]}'


# True when `url` is a youtube.com channel URL that names no tab. yt-dlp lists such a URL as all of
# the channel's uploads, its Videos, Live and Shorts tabs one after another; a URL naming one tab
# lists that tab alone.
def is_channel_root_url(url: str) -> bool:
    try:
        parts = urlsplit(url)
    except ValueError:
        return False
    host = (parts.hostname or '').lower()
    on_youtube = host == 'youtube.com' or host.endswith('.youtube.com')
    return on_youtube and CHANNEL_ROOT_PATH.fullmatch(parts.path) is not None


# Scan what `subscription` watches. A resolved subscription to a channel's root URL is read through
# the channel's uploads playlist, the same uploads in one listing in upload order, and through its
# own URL, kind by kind, when that scan fails. Anything else, a subscription to one tab included, is
# read through its own URL. Returns (ids, error) as scan_video_ids does, and the URL read; when both
# scans fail, the error names both reasons, so the check counts one failure.
def scan_subscription(
    db, subscription: Subscription, playlist_items: Optional[str] = None,
) -> tuple[list[str], Optional[str], str]:
    uploads_url = None
    if (
        subscription.subscription_type == 'channel'
        and subscription.channel_id is not None
        and is_channel_root_url(subscription.url)
    ):
        channel = db.get(Channel, subscription.channel_id)
        uploads_url = uploads_playlist_url(channel.youtube_id if channel else None)
    if uploads_url is None:
        video_ids, error = scan_video_ids(subscription.url, playlist_items)
        return video_ids, error, subscription.url

    video_ids, error = scan_video_ids(uploads_url, playlist_items)
    if not error:
        _uploads_playlist_fallbacks.discard(subscription.id)
        return video_ids, None, uploads_url

    uploads_error = ' '.join(error.split())[:500]
    video_ids, error = scan_video_ids(subscription.url, playlist_items)
    if error:
        return [], f'{error} (uploads playlist: {uploads_error})', subscription.url
    message = (
        f'Subscription {subscription.id}: scanning its uploads playlist failed, so its URL was '
        f'scanned instead: {uploads_error}'
    )
    # An empty listing explains the failure, a channel with no upload having no uploads playlist,
    # and needs no WARN; a channel with uploads warns once until its uploads playlist works again.
    if video_ids and subscription.id not in _uploads_playlist_fallbacks:
        _uploads_playlist_fallbacks.add(subscription.id)
        warn(message)
    else:
        log(message)
    return video_ids, None, subscription.url


# Scan every enabled subscription for new videos. Only a scan that succeeded stamps last_checked,
# in the commit that records its rows, and only while the subscription still holds the
# last_checked and backfill_existing the check read; a failed resolve or scan counts towards the
# subscription's run of failures (see record_subscription_failure()). No yt-dlp call runs in a
# write transaction.
def check_subscriptions() -> None:
    log('Checking subscriptions')

    with Session() as db:
        # Ids only, each row read when its turn comes: the web app may change or delete one
        # meanwhile, and a rollback would expire rows loaded up front.
        subscription_ids = [
            subscription_id
            for (subscription_id,) in (
                db.query(Subscription.id)
                .filter(Subscription.enabled.is_(True))
                .order_by(Subscription.id)
            )
        ]

        for subscription_id in subscription_ids:
            # Each subscription costs at least one yt-dlp call, so a long list keeps the main loop
            # -- and its heartbeat -- waiting for minutes at a time.
            touch_heartbeat()
            try:
                subscription = db.get(Subscription, subscription_id)
                # Deleted or disabled since the run began.
                if subscription is None or not subscription.enabled:
                    continue
                if not is_safe_url(subscription.url):
                    warn(f'Subscription {subscription.id}: refusing unsupported URL {subscription.url!r}')
                    continue

                # An unresolved subscription has both ids unset, leaving every video it creates
                # with no channel or playlist. Retried every scan until it succeeds once, and
                # committed at once: held open across the scan below, the write lock would fail
                # the web app's writes with 'database is locked' for as long as yt-dlp runs.
                if subscription.channel_id is None and subscription.playlist_id is None:
                    if not resolve_subscription_target(db, subscription):
                        db.rollback()
                        continue
                    db.commit()

                # What this check is planned on; the stamp below holds the row to it.
                last_checked = subscription.last_checked
                backfill_existing = subscription.backfill_existing
                is_first_scan = last_checked is None
                # After its baseline a channel reads only its newest uploads; a first scan (the
                # baseline or a backfill) and a playlist read the whole listing.
                limited = subscription.subscription_type == 'channel' and not is_first_scan
                playlist_items = f'1:{PERIODIC_CHANNEL_SCAN_LIMIT}' if limited else None
                video_ids, error, scan_url = scan_subscription(db, subscription, playlist_items)
                # A window whose every upload is new may hold only the newest of more uploads than
                # fit it, so as many new ids as one window holds trigger a read of the whole
                # listing. Read through the channel URL, each kind has its own window, and the
                # output does not say which kind a line is. The baseline holds everything older,
                # so whatever the whole listing has unrecorded is new too.
                if (
                    limited and not error
                    and count_unrecorded(db, video_ids) >= PERIODIC_CHANNEL_SCAN_LIMIT
                ):
                    log(
                        f'Subscription {subscription.id}: {PERIODIC_CHANNEL_SCAN_LIMIT} or more '
                        'of the newest uploads are new, scanning the whole channel'
                    )
                    full_ids, full_error = scan_video_ids(scan_url)
                    # The newest uploads are still recorded, the rest risked with a WARN: failing
                    # the check would fail every later one too for a channel too big to list in
                    # time, its window never recorded.
                    if full_error:
                        detail = ' '.join(full_error.split())[:500]
                        warn(
                            f'Subscription {subscription.id}: the whole-channel scan failed, so '
                            f'uploads older than the newest {PERIODIC_CHANNEL_SCAN_LIMIT} may be '
                            f'missed: {detail}'
                        )
                    else:
                        video_ids = full_ids

                # A failed scan is no check: last_checked stays as it was, so a first scan that
                # fails leaves the baseline to the next check instead of recording none.
                if error:
                    record_subscription_failure(subscription.id, f'scan failed: {error}')
                    continue
                clear_subscription_failures(subscription.id)

                if video_ids:
                    # "Only new videos from now on" happens on the first scan: the current
                    # lineup is recorded as a baseline, never queued.
                    queue_now = backfill_existing or not is_first_scan
                    # A first scan with backfill_existing set (a new "All existing" subscription,
                    # or "Download existing now") also queues what a baseline skipped, in scan
                    # order. The trade-off: a channel's back catalogue then takes its next free
                    # numbers after the episodes already filed, in the order it is downloaded,
                    # oldest first (see create_download_jobs_for_subscription).
                    create_download_jobs_for_subscription(
                        db, subscription, video_ids, queue=queue_now,
                        requeue_skipped=is_first_scan and backfill_existing,
                    )

                # Stamped in the same commit as the rows, and only if the row still holds what this
                # check read: "Download existing now" committed by the web app during the scan
                # wins, and the next check runs with it. The rows go with the stamp. Only the web
                # app writes meanwhile, and it only ever empties last_checked, so whether it is
                # set is all there is to compare.
                unchanged_last_checked = (
                    Subscription.last_checked.is_(None) if is_first_scan
                    else Subscription.last_checked.isnot(None)
                )
                stamped = (
                    db.query(Subscription)
                    .filter(
                        Subscription.id == subscription.id,
                        unchanged_last_checked,
                        Subscription.backfill_existing == backfill_existing,
                    )
                    .update({Subscription.last_checked: utcnow()}, synchronize_session=False)
                )
                if not stamped:
                    db.rollback()
                    # The captured id: the rollback expired the row, which may be gone.
                    log(
                        f'Subscription {subscription_id} changed during its check; '
                        'the next check repeats it'
                    )
                    continue
                # Each subscription commits as its own unit rather than one giant commit.
                db.commit()
            except Exception as exc:
                # Without the rollback, one failure breaks the session for every subscription
                # still to come in this run.
                db.rollback()
                log_error(f'Subscription {subscription_id} check failed', exc)


# =======================================================
#           INDEX BACKFILLS AND RESEQUENCING
# =======================================================

# Fill in playlist_index from scan order for children that have none; existing indexes are kept.
def backfill_playlist_indexes(parent_id: int, video_ids: Iterable[str]) -> None:
    if not video_ids:
        return

    # Scan position is the episode number unless another video holds it (see PlaylistNumbering),
    # so the mapping is built from the order yt-dlp returned rather than from anything stored.
    mapping = {video_id: idx for idx, video_id in enumerate(list(video_ids), start=1)}

    with Session() as db:
        parent = db.get(Download, parent_id)
        if not parent:
            return

        # Only children with no index yet: overwriting would renumber episodes already filed
        # under their old number in the library. Taken in scan order, as the playlist lists them.
        pending = sorted(
            (
                child for child in parent.children
                if child.playlist_index is None and child.youtube_id in mapping
            ),
            key=lambda child: (mapping[child.youtube_id], child.id),
        )
        numbering = None
        if parent.playlist_id is not None:
            numbering = PlaylistNumbering(db, parent.playlist_id)
        for child in pending:
            position = mapping[child.youtube_id]
            child.playlist_index = (
                numbering.assign(position, child.youtube_id) if numbering else position
            )

        # Committed only when something changed, so a re-scan of a settled playlist takes no
        # write lock at all.
        if pending:
            db.commit()


# Statuses whose rows resequence_channel_index leaves alone: nothing is filed for a deleted file, a
# cancelled download or a subscription's skipped baseline. A failed download keeps its number until
# it is retried, rather than pushing every newer episode filed meanwhile one number further.
INERT_STATUSES = frozenset({'deleted', 'cancelled', 'failed', SKIPPED_STATUS})


# True when `filename` is a library file filed as channel episode `index`: plan_channel_video
# writes "<show>/Season 01/<show> - S01E0005 - <title>", four digits where plan_episode writes two,
# and a transcode keeps that name under another extension. A movie or another show's episode does
# not match. The path is not checked on disk.
def filed_under_number(filename: Optional[str], index: Optional[int]) -> bool:
    if not filename or index is None:
        return False
    path = Path(filename)
    if not path.is_relative_to(MEDIA_PATH) or path.parent.name != 'Season 01':
        return False
    return f' - S01E{index:04d} - ' in path.name


# How firmly a row's number belongs to its video, lower first: 0 when the video is filed under it,
# 1 for a live row, 2 for an inert one. Where a video's rows disagree, the firmest number wins, so
# a deleted row's stale number never beats the one the video is filed under.
def number_rank(video: Download) -> int:
    if video.organized_at is not None or filed_under_number(video.filename, video.playlist_index):
        return 0
    return 2 if video.status in INERT_STATUSES else 1


# True when resequence_channel_index must leave a channel row's number alone: the video is filed,
# its library file carries that number while it awaits a refile (a parent's redownload clears
# organized_at and leaves the file), the row is inert, or it has no upload_date to order it by. A
# library file without the number, such as a movie this channel just adopted, keeps nothing.
def keeps_channel_index(video: Download) -> bool:
    if video.organized_at is not None:
        return True
    if filed_under_number(video.filename, video.playlist_index):
        return True
    return video.status in INERT_STATUSES or video.upload_date is None


# Number a channel's grouped videos without changing one a filed video carries: rows that keep
# their number (see keeps_channel_index) stay, and the rest follow in (upload_date, id) order from
# one past the highest kept. A further row of a video that already has a number shares its firmest
# one (see number_rank), so one video is one episode. The trade-off: a video older than filed ones
# takes the next free number, numbers already duplicated on disk stay, and gaps can appear. A
# number is handed out once, except the highest after its episode moves to a playlist's show,
# which the next new video takes. Takes an open session and does not commit, so it composes with
# the caller.
def resequence_channel_index(db, channel_id: int) -> None:
    # A row with a playlist is an episode of that playlist, and its index is its position there.
    videos = (
        db.query(Download)
        .filter(
            Download.channel_id == channel_id,
            Download.group_by_channel.is_(True),
            Download.playlist_id.is_(None),
        )
        .order_by(Download.upload_date, Download.id)
        .all()
    )

    kept = []
    renumbered = []
    for video in videos:
        if keeps_channel_index(video):
            kept.append(video)
        else:
            renumbered.append(video)

    highest = max((video.playlist_index for video in kept if video.playlist_index), default=0)
    # The number each video already has: the firmest a kept row carries, lowest id first on a tie.
    numbers: dict[str, int] = {}
    for video in sorted(kept, key=lambda video: (number_rank(video), video.id)):
        if video.youtube_id and video.playlist_index:
            numbers.setdefault(video.youtube_id, video.playlist_index)

    # Only values that change are written, so a settled channel leaves nothing to flush.
    for video in renumbered:
        index = numbers.get(video.youtube_id) if video.youtube_id else None
        if index is None:
            highest += 1
            index = highest
            if video.youtube_id:
                numbers[video.youtube_id] = index
        if video.playlist_index != index:
            video.playlist_index = index


# Backfill playlist_index for playlist parents whose children lack one. Each parent costs a
# network round trip. Playlists only: channel children keep it NULL, so they would match forever.
# A failed scan logs WARN and leaves the parent alone: a background repair never fails a parent.
def backfill_all_playlist_indexes() -> None:
    with Session() as db:
        parents = (
            db.query(Download)
            .filter(
                Download.request_type == 'playlist',
                Download.id.in_(
                    db.query(Download.parent_id).filter(
                        Download.parent_id.isnot(None),
                        Download.playlist_index.is_(None),
                    )
                ),
            )
            .all()
        )

    # Logged unconditionally, same reasoning as populate_missing_parent_metadata.
    if parents:
        log(f'backfill_all_playlist_indexes: {len(parents)} playlist(s) missing an index, scanning...')

    for parent in parents:
        log(f'backfill_all_playlist_indexes: scanning parent {parent.id} ({parent.url})')
        try:
            video_ids, error = scan_video_ids(parent.url)
            if error:
                # A parent that settled long ago is still settled; the next pass scans it again.
                detail = ' '.join(error.split())[:500]
                warn(f'backfill_all_playlist_indexes: scan of parent {parent.id} failed: {detail}')
                continue
            if video_ids:
                backfill_playlist_indexes(parent.id, video_ids)
        except Exception as exc:
            log_error(f'Failed to backfill playlist indexes for parent {parent.id}', exc)


# Resequence every channel with at least one group_by_channel video, catching up any never
# resequenced; a filed video keeps its number (see resequence_channel_index). Pure database work,
# so it is cheap enough to run for every channel on startup.
def resequence_all_channel_indexes() -> None:
    with Session() as db:
        channel_ids = [
            row[0]
            for row in db.query(Download.channel_id)
            .filter(Download.group_by_channel.is_(True), Download.channel_id.isnot(None))
            .distinct()
            .all()
        ]
        for channel_id in channel_ids:
            resequence_channel_index(db, channel_id)
        if channel_ids:
            db.commit()


# Backfill youtube_id for video jobs created before the ID was recorded at queue time. Without
# one the organizer's "[id]" filename tag reads "[None]" and its existing-file lookups have no key.
def backfill_missing_youtube_ids() -> None:
    with Session() as db:
        jobs = (
            db.query(Download)
            .filter(Download.request_type == 'video', Download.youtube_id.is_(None))
            .all()
        )

        updated = 0
        for job in jobs:
            youtube_id = extract_youtube_id(job.url)
            if youtube_id:
                job.youtube_id = youtube_id
                updated += 1

        if updated:
            db.commit()
            log(f'Backfilled youtube_id for {updated} video job(s)')


# Statuses retire_invalid_scan_rows takes a row out of: recorded or queued by a scan, or failed
# trying to download what is no video. A row in any other status keeps it.
RETIRABLE_STATUSES = frozenset({SKIPPED_STATUS, 'queued', 'failed'})

# The error a retired row carries.
NOT_A_VIDEO_ID_ERROR = 'Not a video id (channel tab listing)'


# True when `url` is a watch URL whose v parameter is `youtube_id`, the URL a scan gives a row.
def is_watch_url_of(url: Optional[str], youtube_id: str) -> bool:
    try:
        return parse_qs(urlsplit(url or '').query).get('v') == [youtube_id]
    except ValueError:
        return False


# True when `row` sits where `subscription`'s scans file rows: grouped under a channel
# subscription's channel, or in a playlist subscription's playlist. Download records no
# subscription_id. A parented row counts too: a download of the same channel or playlist adopts the
# rows a subscription recorded, and treating its own rows as the subscription's errs on the side of
# a record-only re-baseline.
def subscription_owns_row(subscription: Subscription, row: Download) -> bool:
    if subscription.subscription_type == 'channel':
        return (
            subscription.channel_id is not None
            and row.group_by_channel
            and row.channel_id == subscription.channel_id
        )
    return subscription.playlist_id is not None and row.playlist_id == subscription.playlist_id


# Retire the video rows a scan built from a line that is no video id, such as a playlist's or a
# channel's: each in RETIRABLE_STATUSES becomes 'deleted', and the parent of one is re-synced. A
# subscription that owned one is re-baselined record-only: its next scan records the current
# listing as skipped. Local database work only, for startup; at most one commit.
def retire_invalid_scan_rows() -> None:
    with Session() as db:
        # Columns alone first: the pattern runs in Python, and nearly every row passes it.
        candidates = (
            db.query(Download.id, Download.youtube_id, Download.url)
            .filter(
                Download.request_type == 'video',
                Download.youtube_id.isnot(None),
                Download.status.in_(RETIRABLE_STATUSES),
            )
            .all()
        )
        # Only rows whose URL is a watch URL of the id, as a scan builds it: a user's own request,
        # such as a clip whose id yt-dlp reports at 26 characters, is no scan's line.
        invalid_ids = [
            row_id for row_id, youtube_id, url in candidates
            if youtube_id and not YOUTUBE_ID_PATTERN.match(youtube_id)
            and is_watch_url_of(url, youtube_id)
        ]
        rows = db.query(Download).filter(Download.id.in_(invalid_ids)).all() if invalid_ids else []

        subscriptions = db.query(Subscription).all() if rows else []
        owners: dict[int, Subscription] = {}
        parent_ids = set()
        for row in rows:
            row.status = 'deleted'
            row.error = NOT_A_VIDEO_ID_ERROR
            if row.parent_id is not None:
                parent_ids.add(row.parent_id)
            for subscription in subscriptions:
                if subscription_owns_row(subscription, row):
                    owners[subscription.id] = subscription

        # A parent whose other children are all SKIPPED_STATUS keeps its status: they give no
        # verdict, so its retired rows alone would make it deleted, gone from every view though
        # its videos were never downloaded.
        for parent_id in parent_ids:
            parent = db.get(Download, parent_id)
            if parent is None:
                continue
            leaves_live_children = any(child.status != 'deleted' for child in parent.children)
            if resolve_parent_status(parent) == 'deleted' and leaves_live_children:
                continue
            settle_parent(parent)

        # Record-only, whatever backfill_existing said: a whole back catalogue queued long after
        # the subscription began would arrive unannounced, and "Download existing now" fetches it
        # on request.
        for subscription in owners.values():
            subscription.last_checked = None
            subscription.backfill_existing = False

        if rows:
            db.commit()

    for subscription_id, subscription in sorted(owners.items()):
        warn(
            f'Subscription {subscription_id} ({subscription.url}): rows of its channel or playlist '
            'that are not videos were retired, so it is set to "Only new" and its next check '
            'records the current listing as skipped without queuing it; "Download existing now" '
            'fetches the back catalogue.'
        )
    log(f'Retired {len(rows)} row(s) whose youtube_id is not a video id')


# =======================================================
#                 DOWNLOADING ONE VIDEO
# =======================================================

# Only a child of a 'channel' request gets the per-uploader subfolder; everything else is flat.
def resolve_output_template(job: Download) -> str:
    if job.parent_id:
        with Session() as db:
            parent = db.get(Download, job.parent_id)
            if parent and parent.request_type == 'channel':
                return OUTPUT_TEMPLATE_CHANNEL
    return OUTPUT_TEMPLATE_FLAT


# The -P/-o arguments that put a download's finished files in staging and its intermediates under
# STAGING_TEMP. The template stays relative: yt-dlp ignores -P whenever -o is an absolute path.
def ytdlp_output_args(output_template: str) -> list[str]:
    return ['-P', f'home:{DOWNLOAD_PATH}', '-P', f'temp:{STAGING_TEMP}', '-o', output_template]


# Ask yt-dlp what it would name this job's output, using the download's own ytdlp_output_args() so
# both agree on the path. A caller that already resolved the template passes it in rather than
# have it looked up again.
def resolve_expected_output_path(job: Download, output_template: Optional[str] = None) -> Optional[Path]:
    output_template = output_template or resolve_output_template(job)
    filename_cmd = [
        'yt-dlp',
        *ytdlp_common_args(),
        '--get-filename',
        *ytdlp_output_args(output_template),
        job.url,
    ]

    try:
        result = run_command(filename_cmd)
    except subprocess.TimeoutExpired:
        warn(f'Job {job.id}: timed out determining expected filename after {COMMAND_TIMEOUT_SECONDS}s for {job.url}')
        return None
    except subprocess.CalledProcessError as exc:
        warn(f'Job {job.id}: failed to determine expected filename: {exc}')
        return None

    # Indexing straight into splitlines() raises IndexError when yt-dlp prints nothing at all.
    output_lines = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    if not output_lines:
        warn(f'Job {job.id}: yt-dlp returned empty expected filename for {job.url}')
        return None

    expected_path = Path(output_lines[0])
    if not expected_path.is_absolute():
        expected_path = DOWNLOAD_PATH / expected_path
    return expected_path


# Find the media file produced for a job, preferring the exact path yt-dlp said it would write.
# Confined to staging: a match in the library is a file the organizer already filed.
def find_downloaded_video_file(job: Download, expected_path: Optional[Path] = None) -> Optional[Path]:
    if expected_path and is_media_file(expected_path):
        return expected_path
    return find_existing_media(job, search_paths=[DOWNLOAD_PATH])


# Where yt-dlp reports job `job_id`'s final media path. tempfile.gettempdir() is /tmp, a tmpfs in
# compose.example.yaml, so the report stays off the disk there.
def final_path_file(job_id: int) -> Path:
    return Path(tempfile.gettempdir()) / f'ytm-job-{job_id}.path'


# The media path yt-dlp last appended to `path_file`, or None when the file is missing, unreadable
# or empty, or its last line is not finished media inside DOWNLOAD_PATH.
def read_final_path(path_file: Path) -> Optional[Path]:
    try:
        text = path_file.read_text(encoding='utf-8', errors='replace')
    except OSError:
        return None

    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines:
        return None
    # normpath first, so a '..' segment cannot pass the lexical containment test.
    reported = Path(os.path.normpath(lines[-1]))
    if reported.is_relative_to(DOWNLOAD_PATH) and is_media_file(reported):
        return reported
    return None


# Delete a job's final-path file if it exists, logging rather than raising on failure. yt-dlp
# appends to the file, so one left by an earlier attempt would be read back as this one's report.
def discard_final_path_file(path_file: Path) -> None:
    try:
        path_file.unlink(missing_ok=True)
    except OSError as exc:
        warn(f'Could not remove {path_file}: {exc}')


# The yt-dlp argv for one video, writing into staging under `output_template`. The sidecars it
# requests (description, info.json, thumbnail) are what the organizer reads to build the NFO, and
# the final media path goes to final_path_file(job_id). `url` becomes a positional argument, so
# callers must screen it through is_safe_url() first. YouTube's JS challenges are solved locally:
# deno runs the yt-dlp-ejs solver that yt-dlp[default] installs, so nothing comes from GitHub.
def build_download_command(url: str, output_template: str, job_id: int) -> list[str]:
    command = [
        'yt-dlp',
        *ytdlp_common_args(),
        '--write-description',
        '--write-info-json',
        '--write-thumbnail',
        '--embed-thumbnail',
        '--embed-metadata',
        '--convert-thumbnails',
        'jpg',
        *ytdlp_output_args(output_template),
        # after_move fires once the file sits in staging, already-downloaded ones included. Unlike
        # --print, this implies neither --quiet nor --simulate, so progress output keeps flowing.
        '--print-to-file',
        'after_move:filepath',
        str(final_path_file(job_id)),
    ]

    if PROXY:
        command.extend(['--proxy', PROXY])

    command.append(url)

    return command


# Fold a yt-dlp .info.json sidecar into the job's database row, which is how the full yt-dlp
# record survives for the organizer's NFO once the sidecar itself has been cleaned up. A sidecar
# whose id is not the row's is refused with a WARN (False, nothing folded in): it would rewrite
# the row's identity.
def apply_metadata_from_info_file(job: Download, info_path: Optional[Path]) -> bool:
    if not info_path or not info_path.exists():
        return False

    try:
        with open(info_path, 'r', encoding='utf-8') as fh:
            info = json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        warn(f'Job {job.id}: failed to read metadata sidecar {info_path}: {exc}')
        return False

    # Compared with the row, not `job`: apply_metadata() writes the id through its own session.
    with Session() as db:
        row = db.get(Download, job.id)
        row_id = normalize_youtube_id(row.youtube_id) if row else None
    info_id = normalize_youtube_id(info.get('id')) if isinstance(info, dict) else None
    if row_id and info_id != row_id:
        warn(f'Job {job.id}: not applying {info_path}, it describes {info_id!r} rather than {row_id!r}')
        return False

    apply_metadata(job, info)
    return True


# Record a finished download: point the job at its media file, capture the metadata yt-dlp wrote
# beside it, mark it completed. The final 100% is written here, not from the progress stream,
# which reaches 100% once per format the download merges.
def record_completed_download(job: Download, media_path: Path) -> None:
    info_path = media_path.with_name(media_path.stem + '.info.json')
    log(f'Job {job.id}: download completed, recorded filename {media_path}')

    # Before the row points at the new file: a video downloaded again because its library file was
    # missing still carries that file's name here, so the channel resequence apply_metadata runs
    # keeps its episode number for this copy.
    apply_metadata_from_info_file(job, info_path)

    update_job(
        job.id,
        status='completed',
        completed_at=utcnow(),
        progress='100%',
        filename=str(media_path),
        metadata_file=str(info_path) if info_path.exists() else None,
    )


# Shared tail for a stopped video job: file whatever is ready. The status update_job() wrote has
# settled the parent already, so this only reads it.
def finalize_after_download(job: Download) -> None:
    # Child videos are only organized once their whole parent is done, the organizer's own gate.
    if job.parent_id:
        with Session() as db:
            parent = db.get(Download, job.parent_id)
            parent_done = parent is not None and not is_in_progress(parent)
        if not parent_done:
            return

    try:
        append_log(job.id, 'Organizer: running scan after completion')
        organizer_scan_and_process(dry_run=False)
    except Exception as exc:
        log_error('Organizer run failed', exc)


# Remove what a cancelled download left in staging; see organizer.remove_staging_leftovers().
# A failure is logged at WARN and swallowed, so it never turns the cancel into a failed job.
def remove_cancelled_leftovers(job: Download) -> None:
    try:
        organizer_remove_staging_leftovers(job)
    except Exception as exc:
        warn(f'Job {job.id}: could not remove staging leftovers: {exc}')


# Download one video job to completion, in the caller's thread: adopt an existing file if staging
# already has one, fetch metadata, run yt-dlp while streaming its output into the job row, record
# the file yt-dlp reports, then hand off to the organizer. Blocks for the whole download. Reports
# outcome by writing the row's status rather than returning it, and swallows its own failures so
# the worker loop survives.
def download_video(job: Download) -> None:
    log(f'Downloading: {job.url}')

    # Resolved first so an already-downloaded file is found before any bandwidth is spent. None
    # only means the pre-check could not run.
    output_template = resolve_output_template(job)
    expected_path = resolve_expected_output_path(job, output_template)

    # Media already in the staging directory is adopted instead of downloaded again.
    actual_media_path = find_downloaded_video_file(job, expected_path)
    if actual_media_path:
        log(f'Job {job.id}: existing downloaded media found at {actual_media_path}, marking completed')
        record_completed_download(job, actual_media_path)
        finalize_after_download(job)
        return

    log(f'Job {job.id}: no existing downloaded media found for expected path {expected_path}, continuing to download')

    # Metadata before bytes, so the organizer has a title and id to file by even if the download
    # later fails. Non-fatal: yt-dlp writes an .info.json alongside the video anyway, which
    # apply_metadata_from_info_file() folds in once the download finishes.
    if needs_video_metadata(job):
        update_job(job.id, status='metadata')
        metadata = discover_metadata(job, fail_on_error=False)

        if metadata:
            apply_metadata(job, metadata)

    # The lookups above can spend two COMMAND_TIMEOUT_SECONDS on the network, long enough for a
    # cancel to arrive before a single byte is fetched.
    if is_cancelled(job.id):
        update_job(job.id, status='cancelled', error='Cancelled by user')
        remove_cancelled_leftovers(job)
        return

    update_job(job.id, status='downloading')

    command = build_download_command(job.url, output_template, job.id)
    path_file = final_path_file(job.id)
    discard_final_path_file(path_file)

    try:
        # start_new_session so the download and the ffmpeg it forks form one signallable process
        # group. encoding='utf-8' explicit for the same reason as run_command().
        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding='utf-8',
            errors='replace',
            start_new_session=True,
        )
    except Exception as exc:
        log_error('Failed to start yt-dlp', exc)
        update_job(job.id, status='failed', error=str(exc))
        return

    try:
        outcome, last_error_line = stream_process_output(process, job.id)
    except Exception as error:
        log_error(f'Job {job.id}: download failed', error)
        update_job(job.id, status='failed', error=str(error))
        return
    finally:
        # stream_process_output leaves nothing running, but a raise on the way there would
        # orphan a yt-dlp that keeps writing into staging with nothing watching it.
        terminate_process_group(process)
        if process.stdout:
            process.stdout.close()
        # The path file is read only once yt-dlp has exited, then deleted whatever the outcome.
        reported_path = read_final_path(path_file)
        discard_final_path_file(path_file)

    if outcome == 'cancelled':
        update_job(job.id, status='cancelled', error='Cancelled by user')
        remove_cancelled_leftovers(job)
        return

    # A shutdown is nobody's failure: the job goes back on the queue for the next start.
    if outcome == 'shutdown':
        update_job(job.id, status='queued')
        return

    if outcome == 'stalled':
        update_job(job.id, status='failed', error=f'No yt-dlp output for {DOWNLOAD_STALL_TIMEOUT_SECONDS}s')
    elif process.returncode == 0:
        # yt-dlp's own report is authoritative. The lookup by predicted name and '[id]' tag only
        # stands in when --print-to-file wrote nothing usable.
        media_path = reported_path
        if not media_path:
            warn(f'Job {job.id}: yt-dlp reported no usable final path in {path_file}; falls back to a lookup in staging')
            media_path = find_downloaded_video_file(job, expected_path)
        if media_path:
            record_completed_download(job, media_path)
        else:
            # Completing with no file leaves a row the organizer skips forever and auto-retry
            # never revisits.
            warn(f'Job {job.id}: yt-dlp succeeded but no media file was found')
            update_job(job.id, status='failed', error='yt-dlp reported success but produced no media file')
    else:
        # yt-dlp's own last ERROR: line says why; the exit code alone tells the user nothing.
        error = last_error_line or f'yt-dlp exited with code {process.returncode}'
        warn(f'Job {job.id}: download failed: {error}')
        update_job(job.id, status='failed', error=error)

    # However it ended, the job has stopped and its status has settled the parent: organize
    # whatever is ready.
    finalize_after_download(job)


# =======================================================
#               ENTRY POINT AND MAIN LOOP
# =======================================================

# Run one job, keeping the loop alive whatever it does. An unhandled error here ends the
# process; the container restarts, recovery requeues the job, and it happens again.
def run_job_guarded(job: Download) -> None:
    try:
        process_job(job)
    except Exception as exc:
        log_error(f'Job {job.id}: processing failed', exc)
        try:
            # Failing it moves the row out of WORKER_PICKUP_STATUSES; left pickable, the loop
            # would hand it straight back.
            update_job(job.id, status='failed', error=str(exc))
        except Exception as update_exc:
            log_error(f'Job {job.id}: could not record the failure', update_exc)


# SIGTERM/SIGINT handler. Only sets the flag: the loop and the download-output reader poll it and
# stop at their next check, so nothing is interrupted mid-write.
def _request_shutdown(signum, _frame) -> None:
    log(f'Received signal {signum}, finishing the current step and stopping')
    _shutdown.set()


# The packages whose versions decide whether YouTube downloads work, named in the startup log.
COMPONENT_PACKAGES = ('yt-dlp', 'yt-dlp-ejs', 'bgutil-ytdlp-pot-provider')


# The installed version of each COMPONENT_PACKAGES entry, then deno's path, keyed by name in that
# order. Never raises: a package that is not installed reads 'not installed', one whose metadata
# cannot be read 'unknown', and a deno missing from PATH 'not found'.
def component_versions() -> dict[str, str]:
    versions = {}
    for package in COMPONENT_PACKAGES:
        try:
            versions[package] = importlib.metadata.version(package) or 'unknown'
        except importlib.metadata.PackageNotFoundError:
            versions[package] = 'not installed'
        except Exception:
            versions[package] = 'unknown'
    versions['deno'] = shutil.which('deno') or 'not found'
    return versions


# The startup line naming this release and every component_versions() entry, comma-separated.
def components_line() -> str:
    parts = {'youtube-manager': VERSION, **component_versions()}
    return 'Components: ' + ', '.join(f'{name} {value}' for name, value in parts.items())


# The worker process's entry point, and the only caller of the job loop. Runs startup housekeeping
# once, then takes one job at a time until shutdown, interleaving the periodic tasks -- organize,
# subscription check, auto-retry, parent backfill -- between jobs on their own timers. Returns
# only when a signal sets _shutdown; every step is guarded so no single failure ends the process.
def worker() -> None:
    log('YouTube worker started')

    # `docker stop` sends SIGTERM and SIGKILLs ten seconds later; handling it kills the running
    # download and requeues its job rather than leaving the row claimed until the next startup.
    signal.signal(signal.SIGTERM, _request_shutdown)
    signal.signal(signal.SIGINT, _request_shutdown)

    # Startup housekeeping is local DB work with no network calls, so it can block here. Each
    # step is wrapped separately so a raise pinpoints which one.
    try:
        recover_stuck_jobs()
    except Exception as exc:
        log_error('recover_stuck_jobs failed', exc)

    try:
        backfill_missing_youtube_ids()
    except Exception as exc:
        log_error('backfill_missing_youtube_ids failed', exc)

    # Before the resequence, so it numbers each channel with the retired rows already inert.
    try:
        retire_invalid_scan_rows()
    except Exception as exc:
        log_error('retire_invalid_scan_rows failed', exc)

    # Cheap and network-free, unlike the playlist backfill in the loop below.
    log('Resequencing channel indexes...')
    try:
        resequence_all_channel_indexes()
    except Exception as exc:
        log_error('resequence_all_channel_indexes failed', exc)
    log('Startup housekeeping done, entering main loop')

    # Monotonic: an NTP correction on a host without a real-time clock would otherwise postpone
    # every periodic task by the size of the step.
    last_organize = time.monotonic()
    last_subscription_check = time.monotonic()
    last_retry = time.monotonic()
    # The backfills first run one interval after start, like the subscription check, not on the
    # first pass: each makes a network call per affected parent, holding up every queued job.
    last_parent_backfill = time.monotonic()

    last_job_id = None
    repeat_count = 0

    while not _shutdown.is_set():
        touch_heartbeat()
        job = get_next_job()

        if job:
            repeat_count = repeat_count + 1 if job.id == last_job_id else 0
            last_job_id = job.id

            # A job that keeps coming back is one no branch of process_job can move out of the
            # queue; left alone it spins the loop at full CPU.
            if repeat_count >= MAX_JOB_PICKUP_REPEATS:
                log(f'Job {job.id}: still queued after {repeat_count} passes, failing it')
                update_job(job.id, status='failed', error='Worker made no progress on this job')
                continue

            run_job_guarded(job)
        else:
            last_job_id = None
            _shutdown.wait(1)

        # The periodic tasks run between jobs, never during one, so none of them can contend with
        # a download for the write lock. Each is independently guarded: one failing must not stop
        # the others or end the loop.
        now = time.monotonic()

        if now - last_organize >= ORGANIZER_INTERVAL_SECONDS:
            last_organize = now
            try:
                organizer_scan_and_process(dry_run=False)
            except Exception as exc:
                log_error('Periodic organizer run failed', exc)

        if now - last_retry >= RETRY_INTERVAL_SECONDS:
            last_retry = now
            try:
                retry_failed_jobs()
            except Exception as exc:
                log_error('Periodic retry failed', exc)

        if now - last_subscription_check >= SUBSCRIPTION_CHECK_INTERVAL_SECONDS:
            last_subscription_check = now
            try:
                check_subscriptions()
            except Exception as exc:
                log_error('Periodic subscription check failed', exc)

        # Both backfills share the subscription interval: they are the two network-bound repair
        # passes, and running them on the same clock keeps their YouTube requests in one burst.
        if now - last_parent_backfill >= SUBSCRIPTION_CHECK_INTERVAL_SECONDS:
            last_parent_backfill = now
            try:
                populate_missing_parent_metadata()
            except Exception as exc:
                log_error('Periodic parent metadata backfill failed', exc)
            try:
                backfill_all_playlist_indexes()
            except Exception as exc:
                log_error('Periodic playlist index backfill failed', exc)

    log('YouTube worker stopped')


if __name__ == '__main__':
    # Line buffering, so container logs appear as the worker runs rather than in blocks.
    sys.stdout.reconfigure(line_buffering=True)
    applog.configure()
    # Logged first, so the log of every run, a failed start included, names what it ran.
    log(components_line())

    # Create the staging directory, and yt-dlp's temp dir inside it, if they do not exist, so a
    # fresh container can start without issues. It runs here rather than at import, so importing
    # this module creates nothing.
    DOWNLOAD_PATH.mkdir(parents=True, exist_ok=True)
    STAGING_TEMP.mkdir(parents=True, exist_ok=True)

    # The web process owns schema creation, so the worker waits rather than racing it.
    log('Waiting for database schema to become available...')
    if wait_for_schema(timeout_seconds=60):
        worker()
    else:
        logging.error('Timed out waiting for database schema')
