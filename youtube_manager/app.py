# -------------------------------------------------------
# YouTube Manager web app
#
# FastAPI routes and UI helpers: page rendering, flash messages, job and subscription actions, and
# the polling APIs the pages refresh themselves from. Every route is a plain `def`, which FastAPI
# runs in a worker thread -- that is what keeps the blocking SQLite and filesystem work here off
# the event loop, so no route may become `async def`; the ASGI middlewares are async but do no I/O.
# -------------------------------------------------------

import logging
import os
import sqlite3
from collections.abc import Mapping
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional
from datetime import datetime, timezone, timedelta
from sqlalchemy import func, or_
from urllib.parse import parse_qs, urlparse, urlsplit

from youtube_manager import VERSION, applog

from fastapi import FastAPI, Form, Request
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from fastapi.responses import PlainTextResponse, RedirectResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import aliased, joinedload, selectinload
from starlette.datastructures import Headers
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from youtube_manager.database import (
    Download,
    Session,
    Subscription,
    initialize_database,
    normalize_datetime,
)

# Job-state vocabulary and progress/speed aggregation, shared with the worker so
# both processes describe a job the same way.
from youtube_manager.jobstate import (
    MAX_AUTO_RETRIES,
    ACTIVE_STATUSES,
    ACTIVE_STATUS_SET,
    CANCELABLE_EFFECTIVE_STATUSES,
    FAILED_OR_CANCELLED,
    HISTORY_STATUSES,
    PARENT_REQUEST_TYPES,
    QUEUE_STATUSES,
    QUEUED_OR_EXPANDING,
    SKIPPED_STATUS,
    TERMINAL_STATUS_SET,
    adopt_existing_download,
    apply_status_update_to_hierarchy,
    compute_effective_status,
    compute_parent_progress,
    compute_parent_speed,
    format_children_summary,
    is_in_progress,
    is_retriable,
    reset_job_for_retry,
    restart_auto_retry,
    settle_parent,
)
from youtube_manager.media import extract_youtube_id
from youtube_manager.organizer import remove_downloaded_files

# Install the shared log format before anything else emits a line, including
# uvicorn's own startup/access logging -- see applog.py.
applog.configure()

# The templates directory is relative to this file, so the app can be run from anywhere.
BASE_DIR = Path(__file__).resolve().parent
TEMPLATES_DIR = BASE_DIR / 'templates'


# Only this process creates the schema; the worker blocks until it appears
# (database.wait_for_schema), so it has to land before the first request.
# The release and the SQLite library are logged first, so every start names
# the build that served it and the SQLite it ran on.
@asynccontextmanager
async def lifespan(_app: FastAPI):
    logging.info('YouTube Manager %s web UI starting, SQLite %s', VERSION, sqlite3.sqlite_version)
    initialize_database()
    yield


# Docs/OpenAPI endpoints disabled: nothing consumes them.
app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)

# Every render goes through this one environment, which is also where the Jinja globals below are
# registered, so a template can call them without each route passing them in.
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

# What the add form may submit; validate_request_type() rejects anything else. Each names a shape
# of YouTube URL rather than a download option, and the two parent types expand into child jobs.
VALID_REQUEST_TYPES = ('video', 'playlist', 'channel')
FLASH_COOKIE_NAME = 'flash_message'

# History grows without bound, so the page renders a slice of it at a time.
HISTORY_PAGE_SIZE = 50


# =======================================================
#          REQUEST GUARDS AND SECURITY HEADERS
# =======================================================
# The app has no authentication, so these keep other sites out: Host pinning against DNS
# rebinding, an origin check on every state-changing request, and browser hardening headers.

# Hosts the UI always answers to: its Compose service name, for probes on the same network, and
# the loopback names a container healthcheck uses. The name the reverse proxy serves the UI under
# comes from YTM_ALLOWED_HOSTS, which adds to these.
DEFAULT_ALLOWED_HOSTS = ('youtube-manager', 'localhost', '127.0.0.1')

# Methods no route here changes state on, so a cross-site request using one is harmless.
SAFE_METHODS = frozenset({'GET', 'HEAD', 'OPTIONS'})

# Sec-Fetch-Site values for a request from this app's own pages, or one typed into the address bar.
TRUSTED_FETCH_SITES = frozenset({'same-origin', 'none'})

# 'unsafe-inline' because the templates' scripts, styles and event handlers are all inline.
# Referrer-Policy is same-origin, not no-referrer: no-referrer makes a same-origin form post send
# `Origin: null`, which CrossOriginGuard refuses.
SECURITY_HEADERS = (
    (
        b'content-security-policy',
        b"default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; "
        b"img-src 'self' data:; connect-src 'self'; form-action 'self'; frame-ancestors 'none'; "
        b"base-uri 'none'; object-src 'none'",
    ),
    (b'x-frame-options', b'DENY'),
    (b'x-content-type-options', b'nosniff'),
    (b'cross-origin-resource-policy', b'same-origin'),
    (b'referrer-policy', b'same-origin'),
)


# The default hosts plus each comma-separated entry of YTM_ALLOWED_HOSTS in `env`, trimmed, with
# empty entries dropped. Reads nothing but the mapping it is given.
def load_allowed_hosts(env: Mapping[str, str]) -> list[str]:
    extra = (entry.strip() for entry in env.get('YTM_ALLOWED_HOSTS', '').split(','))
    return [*DEFAULT_ALLOWED_HOSTS, *(entry for entry in extra if entry)]


# Refuses a state-changing request that did not come from this app's own pages: 403 and one WARN
# line, before any route runs. Every form here is a browser form, so a POST without Origin is
# refused too.
class CrossOriginGuard:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    # Whether `headers` show a request from this app's own pages. Sec-Fetch-Site decides when the
    # browser sends it; over plain HTTP it does not, and Origin's host[:port] must equal Host.
    @staticmethod
    def is_same_origin(headers: Headers) -> bool:
        fetch_site = headers.get('sec-fetch-site')
        if fetch_site is not None:
            return fetch_site in TRUSTED_FETCH_SITES

        origin = headers.get('origin', '')
        host = headers.get('host', '').lower()
        if not origin or origin == 'null' or not host:
            return False
        # urlsplit raises on a malformed bracketed IPv6 netloc, which is no origin of ours either.
        try:
            return urlsplit(origin).netloc.lower() == host
        except ValueError:
            return False

    # Passes safe methods and non-HTTP scopes straight through; answers anything else that fails
    # is_same_origin() itself. The log line carries no cookies or body.
    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope['type'] != 'http' or scope['method'] in SAFE_METHODS:
            await self.app(scope, receive, send)
            return

        headers = Headers(scope=scope)
        if self.is_same_origin(headers):
            await self.app(scope, receive, send)
            return

        # %r, because the path arrives percent-decoded and could otherwise forge a log line.
        logging.warning(
            'Cross-origin request refused: %s %r, Origin %r, Sec-Fetch-Site %r',
            scope['method'], scope['path'], headers.get('origin'), headers.get('sec-fetch-site'),
        )
        response = PlainTextResponse('Cross-origin request refused', status_code=403)
        await response(scope, receive, send)


# Adds each SECURITY_HEADERS entry to every HTTP response that does not already set that header.
class SecurityHeaders:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    # Wraps `send`, so the headers land on whatever produces the response, refusals included.
    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope['type'] != 'http':
            await self.app(scope, receive, send)
            return

        # Forwards `message`, adding the missing headers to the response start.
        async def send_with_headers(message: Message) -> None:
            if message['type'] == 'http.response.start':
                headers = list(message.get('headers', []))
                present = {name.lower() for name, _ in headers}
                headers.extend(header for header in SECURITY_HEADERS if header[0] not in present)
                message = {**message, 'headers': headers}
            await send(message)

        await self.app(scope, receive, send_with_headers)


# Read once at import, so a YTM_ALLOWED_HOSTS change needs a restart.
ALLOWED_HOSTS = load_allowed_hosts(os.environ)

# The middleware added last runs first, so a request passes SecurityHeaders, then TrustedHost,
# then CrossOriginGuard. Headers outermost puts them on the 400 and 403 refusals too. TrustedHost
# answers 400 to any Host not in ALLOWED_HOSTS and ignores the port.
app.add_middleware(CrossOriginGuard)
app.add_middleware(TrustedHostMiddleware, allowed_hosts=ALLOWED_HOSTS)
app.add_middleware(SecurityHeaders)


# =======================================================
#             FLASH MESSAGES AND RENDERING
# =======================================================

# The message the last response left behind, or None. Reading it does not clear the cookie --
# render_template deletes it once the message has been rendered.
def get_flash_message(request: Request) -> Optional[str]:
    return request.cookies.get(FLASH_COOKIE_NAME)


# Short-lived on purpose: it must outlive the redirect but not a later refresh.
def set_flash_message(response: RedirectResponse, message: str) -> None:
    response.set_cookie(FLASH_COOKIE_NAME, message, max_age=10, httponly=True, samesite='lax')


# Redirects to `url` with 303, so the browser follows a POSTed form action with a GET. A falsy
# `message` redirects without setting a flash cookie.
def redirect_with_flash(url: str, message: Optional[str] = None) -> RedirectResponse:
    response = RedirectResponse(url, status_code=303)
    if message:
        set_flash_message(response, message)
    return response


# Renders `name`, then drops the flash cookie so a message shows exactly once.
def render_template(request: Request, name: str, context: dict):
    flash_message = get_flash_message(request)
    if flash_message:
        context['flash_message'] = flash_message

    response = templates.TemplateResponse(
        request,
        name,
        context,
    )

    # Cleared only once the render has succeeded, so a template that raises leaves the message
    # waiting for the next page rather than swallowing it.
    if flash_message:
        response.delete_cookie(FLASH_COOKIE_NAME)

    return response


# =======================================================
#            TIMESTAMPS AND POLLING BASELINE
# =======================================================
# Parsed, normalised to UTC, and reduced to the baseline the pollers compare against.

# An absent or malformed `since` is not an error: the caller just treats it as "no baseline".
def parse_iso_datetime(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None

    try:
        dt = datetime.fromisoformat(value)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except ValueError:
        return None


# The most recent time anything on this row changed, in UTC: updated_at, else completed_at, else
# created_at. None only when the row carries none of the three.
def latest_job_timestamp(job: Download) -> Optional[datetime]:
    return (
        normalize_datetime(job.updated_at)
        or normalize_datetime(job.completed_at)
        or normalize_datetime(job.created_at)
    )


# The newest timestamp across one job or a list of them -- the baseline a page sends back as
# `since`. With include_children it reads job.children, so the rows must still be attached or
# eager-loaded. None when nothing carries a timestamp at all.
def compute_latest_timestamp(
    jobs: Download | list[Download],
    include_children: bool = True,
) -> Optional[datetime]:
    job_list = [jobs] if isinstance(jobs, Download) else jobs

    timestamps = [latest_job_timestamp(job) for job in job_list]
    if include_children:
        for job in job_list:
            timestamps.extend(latest_job_timestamp(child) for child in job.children)

    return max((t for t in timestamps if t is not None), default=None)


# =======================================================
#                 URL CANONICALISATION
# =======================================================
# Which URLs this app accepts, and the single form it stores them in.

# Domains this app knows how to canonicalise and is willing to hand to the worker.
YOUTUBE_DOMAINS = ('youtube.com', 'youtu.be', 'youtube-nocookie.com')

# Path prefixes that address one video; the same set media.extract_youtube_id reads ids from.
VIDEO_PATH_PREFIXES = ('/shorts/', '/embed/', '/live/', '/v/')

# The id segment of YouTube's playlist embed, /embed/videoseries?list=<id>, which names no video.
PLAYLIST_EMBED_SEGMENT = 'videoseries'


# Canonicalises a URL so it carries a scheme, which urlparse() needs to parse the hostname.
def with_scheme(url: str) -> str:
    return url if url.startswith(('http://', 'https://')) else f'https://{url}'


# The YOUTUBE_DOMAINS entry this URL belongs to, matched exactly or as a subdomain, else None.
# hostname, not netloc: netloc carries userinfo and a port, so a substring test on it accepts
# "youtu.be@evil.example" -- and endswith without the dot accepts "notyoutube.com".
def youtube_domain(url: str) -> Optional[str]:
    host = (urlparse(with_scheme(url)).hostname or '').lower()
    for domain in YOUTUBE_DOMAINS:
        if host == domain or host.endswith(f'.{domain}'):
            return domain
    return None


# The video id a URL names in its path: a youtu.be link's first path segment, or the segment after
# one of VIDEO_PATH_PREFIXES on another YouTube domain (`domain` is youtube_domain()'s answer).
# None for any other path, an empty id, and the playlist embed, so a `list=` beside them counts.
def path_video_id(domain: Optional[str], path: str) -> Optional[str]:
    # strip, not lstrip: a trailing slash left on the id makes the same video normalise two
    # different ways, and the duplicate check in queue_download() compares these strings.
    if domain == 'youtu.be':
        segment = path.strip('/').split('/')[0]
    elif domain:
        path = path.rstrip('/')
        prefix = next((p for p in VIDEO_PATH_PREFIXES if path.startswith(p)), None)
        if prefix is None:
            return None
        segment = path[len(prefix):].split('/')[0]
    else:
        return None
    if not segment or segment == PLAYLIST_EMBED_SEGMENT:
        return None
    return segment


# Canonicalise a URL so the same target pasted in different forms compares equal
# against stored rows. `kind` is the type the URL was validated as (see
# validate_url_type): a URL carrying both a video id and `list=` is the video for
# 'video' and the playlist for 'playlist', a youtu.be share link included. Without
# a kind, a watch+list URL normalises to the playlist it was opened from.
def normalize_url(url: str, kind: Optional[str] = None) -> str:
    url = with_scheme(url)
    parsed = urlparse(url)
    domain = youtube_domain(url)
    qs = parse_qs(parsed.query)
    video_id = path_video_id(domain, parsed.path)

    # A playlist keeps only its list id, whichever video it was opened at or shared from.
    if domain and kind == 'playlist' and 'list' in qs:
        return 'https://www.youtube.com/playlist?list=' + qs['list'][0]

    # A short link carries the id as its entire path, so it rewrites without consulting the query.
    if domain == 'youtu.be' and video_id:
        return 'https://www.youtube.com/watch?v=' + video_id

    if domain:
        path = parsed.path.rstrip('/')

        # Only the id survives: playlist position, timestamps and tracking parameters are all
        # dropped, which is what makes two pastes of the same target compare equal.
        if 'v' in qs and kind == 'video':
            return 'https://www.youtube.com/watch?v=' + qs['v'][0]

        # Shorts, embeds and live URLs each address one video, so they collapse to the same watch
        # form, ahead of any `list=` they carry, as in detect_youtube_type; a channel URL falls
        # through with its path intact.
        if video_id:
            return 'https://www.youtube.com/watch?v=' + video_id

        if 'list' in qs:
            return 'https://www.youtube.com/playlist?list=' + qs['list'][0]

        if 'v' in qs:
            return 'https://www.youtube.com/watch?v=' + qs['v'][0]

        return 'https://www.youtube.com' + path

    # Nothing off YouTube can be canonicalised here, so it only loses a trailing slash.
    return url.rstrip('/')


# Classifies a YouTube URL as 'video', 'playlist' or 'channel' from its path and query. None for
# a non-YouTube URL, and for a YouTube one in no shape this recognises.
def detect_youtube_type(url: str) -> Optional[str]:
    domain = youtube_domain(url)
    if not domain:
        return None

    parsed = urlparse(with_scheme(url))
    path = parsed.path.rstrip('/')
    qs = parse_qs(parsed.query)

    # Every shape that names one video is checked before `list=`: YouTube appends a
    # channel's auto-generated Uploads playlist id to any video opened from its
    # Videos tab, a share link carries the playlist it was shared from, and reading
    # either as a playlist records the single video as a playlist of its own.
    # validate_url_type still accepts such a URL for a playlist request. The
    # playlist embed names no video, so its `list=` decides.
    names_video = path == '/watch' and 'v' in qs
    if names_video or path_video_id(domain, parsed.path):
        return 'video'

    if 'list' in qs or path == '/playlist':
        return 'playlist'

    # /c/ and /user/ are YouTube's pre-handle channel URLs, still in circulation.
    if path.startswith(('/@', '/channel/', '/c/', '/user/')):
        return 'channel'

    return None


# =======================================================
#                  REQUEST VALIDATION
# =======================================================
# Helpers for the /add and /subscriptions/add routes.
def validate_request_type(request_type: str) -> bool:
    return request_type in VALID_REQUEST_TYPES


# Returns the detected type only when it contradicts `expected_type`; an
# unrecognisable URL is not treated as a mismatch, and a URL carrying both a video
# id and `list=` is accepted as either a video or a playlist.
def validate_url_type(url: str, expected_type: str) -> Optional[str]:
    detected = detect_youtube_type(url)
    if not detected or detected == expected_type:
        return None
    # detect_youtube_type reads such a URL as its video; normalize_url keeps the list instead.
    carries_list = 'list' in parse_qs(urlparse(with_scheme(url)).query)
    if detected == 'video' and expected_type == 'playlist' and carries_list:
        return None
    return detected


# Register the shared job-state helpers and vocabulary as Jinja globals, so templates render
# exactly the same status/progress/speed values the worker persists, and check membership
# against the same status lists rather than keeping their own hardcoded copies.
templates.env.globals['MAX_AUTO_RETRIES'] = MAX_AUTO_RETRIES
templates.env.globals['compute_effective_status'] = compute_effective_status
templates.env.globals['compute_parent_progress'] = compute_parent_progress
templates.env.globals['compute_parent_speed'] = compute_parent_speed
templates.env.globals['format_children_summary'] = format_children_summary
templates.env.globals['is_in_progress'] = is_in_progress
templates.env.globals['is_retriable'] = is_retriable
templates.env.globals['CANCELABLE_EFFECTIVE_STATUSES'] = CANCELABLE_EFFECTIVE_STATUSES
templates.env.globals['FAILED_OR_CANCELLED'] = FAILED_OR_CANCELLED
templates.env.globals['PARENT_REQUEST_TYPES'] = PARENT_REQUEST_TYPES


# =======================================================
#                      JOB LOOKUPS
# =======================================================

# Eager-load every relationship the templates touch, so list views don't fire a
# lazy query per row.
def apply_eager_loads(query):
    return query.options(
        joinedload(Download.channel),
        joinedload(Download.playlist),
        selectinload(Download.children),
        joinedload(Download.parent),
    )


# The job with `job_id`, or None when no such row exists. Eager-loaded, so callers may keep
# reading it -- children and parent included -- after the session closes.
def load_job(db, job_id: int) -> Optional[Download]:
    return apply_eager_loads(db.query(Download)).filter(Download.id == job_id).first()


# One query for a whole checkbox selection, so a bulk route holds one transaction
# instead of taking SQLite's single write lock once per row while the worker waits.
def load_jobs(db, job_ids: list[int]) -> list[Download]:
    if not job_ids:
        return []
    return apply_eager_loads(db.query(Download)).filter(Download.id.in_(job_ids)).all()


# =======================================================
#                     JOB MUTATIONS
# =======================================================
# Retry, cancel and delete come in pairs: a helper that acts inside the caller's transaction, and
# an entry point taking an id that opens its own session and commits.

# Statuses /add is willing to requeue rather than reject as a duplicate. 'deleted' is
# included because pasting the URL again after removing the file is a request to fetch
# it again, and nothing else on that row offers to; SKIPPED_STATUS because a video a
# subscription only recorded has never been fetched at all.
REQUEUEABLE_STATUSES = FAILED_OR_CANCELLED | {'deleted', SKIPPED_STATUS}


# Requeues a job's failed/cancelled work in the caller's transaction, and reports whether
# anything changed. Shares reset_job_for_retry with the worker's auto-retry (jobstate.py) so
# both reset the same fields and both reuse a file already on disk; search_library=False
# finds that file by a stat of its recorded name alone, walking no tree.
def requeue_job(job: Download, statuses=FAILED_OR_CANCELLED, search_library: bool = True) -> bool:
    # Reset one row and, when it was reset, give it its automatic-retry count back.
    def reset(target) -> bool:
        if not reset_job_for_retry(target, statuses, search_library):
            return False
        restart_auto_retry(target)
        return True

    changed = apply_status_update_to_hierarchy(job, reset)
    if changed and job.parent_id is None and job.children:
        # Parent shows queued too, so the UI reflects the retry. A cancel still pending on it
        # would settle the rerun hierarchy as cancelled (jobstate.settle_parent), so it goes.
        job.status = 'queued'
        job.cancel_requested = False
    return changed


# Queues a new job, or requeues the existing one for the same URL instead of
# creating a duplicate.
def queue_download(url: str, request_type: str) -> str:
    with Session() as db:
        # Top-level rows only: a child carries its parent's video URL, so matching one would
        # requeue a single episode of a playlist as though it were the request itself.
        existing = (
            db.query(Download)
            .filter(
                Download.url == url,
                Download.request_type == request_type,
                Download.parent_id.is_(None),
            )
            .first()
        )

        # Every branch returns the wording the caller flashes; a duplicate or a stale row is
        # reported as a message, never raised.
        if existing:
            if existing.status not in REQUEUEABLE_STATUSES:
                return 'Download already exists.'
            # A row a subscription only recorded was never fetched, so queuing it is no retry.
            first_fetch = existing.status == SKIPPED_STATUS
            if not requeue_job(existing, REQUEUEABLE_STATUSES):
                return 'No failed downloads to retry.'
            db.commit()
            return f'{request_type.capitalize()} queued.' if first_fetch else 'Retry queued.'

        # Capture the video id up front -- the organizer tags every filename with it,
        # and a row that reaches it without one produces "... [None]" filenames.
        job = Download(
            url=url,
            request_type=request_type,
            youtube_id=extract_youtube_id(url) if request_type == 'video' else None,
            status='queued',
        )
        db.add(job)
        db.commit()

    return f'{request_type.capitalize()} queued.'


# Requeues one job's failed/cancelled work in its own session and returns the flash wording.
# Reports a missing id rather than raising.
def retry_job(job_id: int) -> str:
    with Session() as db:
        job = db.get(Download, job_id)
        if not job:
            return 'Job not found.'

        changed = requeue_job(job)
        # Commit even when nothing was requeued: the reset may instead have adopted an
        # already-completed child, and that mutation still needs persisting.
        db.commit()

    return 'Retry queued.' if changed else 'No failed downloads to retry.'


# The flag is for the worker to notice mid-download; a job that has not started yet
# can be cancelled outright here. A row that has stopped is left unflagged: it has nothing to
# cancel, and the flag would steer a later settle or auto-retry against what the UI shows.
def apply_cancel(target: Download) -> bool:
    if target.status not in ACTIVE_STATUS_SET:
        return False
    target.cancel_requested = True
    if target.status in QUEUED_OR_EXPANDING:
        target.status = 'cancelled'
        target.organized_at = None
        # Clear transient progress/speed so the UI shows no stale metrics.
        target.progress = None
        target.speed = None
        return True
    return False


# Settle `parent` in the caller's transaction through jobstate.settle_parent once none of its
# children is in flight, as after the app cancels or deletes the last child that kept it active.
# While one is, the parent is left alone: the worker owns in-flight states and settles the parent
# when that child stops. None is a no-op.
def settle_idle_parent(parent: Optional[Download]) -> None:
    if parent is None or any(child.status in ACTIVE_STATUS_SET for child in parent.children):
        return
    settle_parent(parent)


# Cancels a job in progress (jobstate.is_in_progress) in the caller's transaction and reports
# whether it was one; a job that has stopped is left alone. Not apply_status_update_to_hierarchy:
# that only touches the parent when a child changed, so a parent whose children are all
# mid-download would never get its own flag set. A parent with nothing left in flight is settled
# here, as is a cancelled child's parent, in the same transaction.
def request_job_cancel(job: Download) -> bool:
    if not is_in_progress(job):
        return False

    if job.parent_id is None and job.children:
        # A parent stored as settled while a video of it runs again on its own lags its children:
        # settled from them first, it is stored in flight, where the flag set below settles it
        # as 'cancelled' however many videos are still running (jobstate.settle_parent).
        in_flight = any(child.status in ACTIVE_STATUS_SET for child in job.children)
        if in_flight and job.status not in ACTIVE_STATUS_SET:
            settle_parent(job)
        for child in job.children:
            apply_cancel(child)

        job.cancel_requested = True
        # Nothing left in progress, so the parent settles here -- no other code path would move
        # it out of an active status.
        settle_idle_parent(job)
    else:
        apply_cancel(job)
        if job.parent_id is not None:
            settle_idle_parent(job.parent)
    return True


# Requests cancellation of one job in its own session. A missing id is a silent no-op -- the
# caller redirects with no message either way.
def cancel_job(job_id: int) -> None:
    with Session() as db:
        job = db.get(Download, job_id)
        if not job:
            return

        request_job_cancel(job)
        db.commit()


# Removes the files but keeps the row, marked 'deleted', so history still lists the entry, and
# reports (deleted, skipped) counts. A playlist/channel parent delegates to each child and then
# follows suit. Anything still active is skipped: the worker may be writing to those files.
# Touches the filesystem but never the database, so the caller's write lock stays unheld.
def delete_job_files(job: Download) -> tuple[int, int]:
    targets = job.children if (job.parent_id is None and job.children) else [job]
    deleted = 0
    skipped_active = 0
    for target in targets:
        if target.status in ACTIVE_STATUS_SET:
            skipped_active += 1
            continue
        # A row already emptied is neither re-swept nor counted again, so the button is safe to
        # press twice.
        if target.status == 'deleted':
            continue
        # Files first, then every path on the row that pointed at them -- a leftover filename or
        # sidecar would let find_existing_media() adopt the job as complete on its next retry.
        remove_downloaded_files(target)
        target.filename = None
        target.metadata_file = None
        target.thumbnail = None
        target.organized_at = None
        target.status = 'deleted'
        target.cancel_requested = False
        deleted += 1

    # Mirror the deletion onto the parent once no child is still active.
    if job.parent_id is None and job.children and not any(c.status in ACTIVE_STATUS_SET for c in job.children):
        job.status = 'deleted'
        job.filename = None

    return deleted, skipped_active


# Flash wording for a delete; the "left alone" clause appears only when something was skipped.
def describe_deletion(deleted: int, skipped_active: int) -> str:
    if skipped_active:
        return f'Deleted {deleted} file(s); {skipped_active} still in progress were left alone.'
    return f'Deleted {deleted} file(s).'


# Deletes one job's files in its own session and returns the flash wording. The row survives,
# marked 'deleted', and a child's parent is settled in the same commit; a missing id is reported,
# not raised.
def delete_job(job_id: int) -> str:
    with Session() as db:
        job = db.get(Download, job_id)
        if not job:
            return 'Job not found.'

        deleted, skipped_active = delete_job_files(job)
        # After the files are gone: loading the parent may autoflush and take the write lock.
        if job.parent_id is not None:
            settle_idle_parent(job.parent)
        db.commit()

    return describe_deletion(deleted, skipped_active)


# Deletes the subscription row in its own session; a missing id is a no-op. Downloads it already
# queued keep their rows -- only the subscription itself goes.
def remove_subscription(subscription_id: int) -> None:
    with Session() as db:
        subscription = db.get(Subscription, subscription_id)
        if not subscription:
            return

        db.delete(subscription)
        db.commit()


# =======================================================
#               QUEUE AND HISTORY QUERIES
# =======================================================

# Stored statuses that could make a row -- or one of its children -- display as one of
# `statuses`. compute_effective_status() returns display-only values ('cancelling') and derives
# a parent's state from its children, so this is a superset that Python then narrows exactly.
def storable_statuses(statuses: tuple[str, ...]) -> set[str]:
    return set(statuses) & (ACTIVE_STATUS_SET | TERMINAL_STATUS_SET)


# Top-level jobs whose *effective* status is one of `statuses`, newest first.
# Filtering on the stored status instead would hide a parent row that lags behind
# its children, after a restart or a retry that adopted existing downloads.
def query_downloads(statuses: tuple[str, ...], hide_stale_endings: bool = False) -> list[Download]:
    # Failed and cancelled jobs older than this leave the active queue for history.
    cutoff = datetime.now(timezone.utc) - timedelta(days=1)
    raw_statuses = storable_statuses(statuses)

    with Session() as db:
        query = apply_eager_loads(db.query(Download)).filter(Download.parent_id.is_(None))

        # Narrow in SQL first, against idx_download_parent_status: the dashboard polls this
        # every few seconds, and scanning every row ever downloaded to reject most of them in
        # Python grows with the table. Empty only if a caller asks purely for display-only
        # statuses, which no stored row can match anyway.
        if raw_statuses:
            child = aliased(Download)
            has_matching_child = (
                db.query(child.id)
                .filter(child.parent_id == Download.id, child.status.in_(raw_statuses))
                .exists()
            )
            query = query.filter(or_(Download.status.in_(raw_statuses), has_matching_child))

        # Ordered by when the request was made, not by when the worker last touched it, so a job
        # does not jump around the dashboard as its children report progress.
        parents = query.order_by(Download.created_at.desc()).all()

        results: list[Download] = []
        # A parent matched only through a child is dropped again unless its own effective status
        # belongs to `statuses`, so a job appears in the queue view or history, never both.
        for parent in parents:
            effective = compute_effective_status(parent)
            if effective not in statuses:
                continue

            # Aged out on the whole hierarchy's newest timestamp: a parent whose child failed
            # minutes ago stays visible even though the parent row itself is a day old.
            if hide_stale_endings and effective in FAILED_OR_CANCELLED:
                latest = compute_latest_timestamp(parent)
                if not latest or latest < cutoff:
                    continue

            results.append(parent)

        return results


# =======================================================
#             PAGE ROUTES AND FORM ACTIONS
# =======================================================

# GET /: the dashboard queue, with failed/cancelled entries older than a day dropped, plus the
# timestamp its poller sends back as `since`.
@app.get('/')
def index(request: Request):
    jobs = query_downloads(QUEUE_STATUSES, hide_stale_endings=True)

    last_update = compute_latest_timestamp(jobs, include_children=False)

    return render_template(
        request,
        'index.html',
        {'jobs': jobs, 'last_update': last_update.isoformat() if last_update else ''},
    )


# POST /add: validates the type, rejects anything that is not a YouTube URL, canonicalises it,
# then queues or requeues. Every path ends in a redirect to / carrying a flash message,
# rejections included.
@app.post('/add')
def add(url: str = Form(...), request_type: str = Form(...)):
    if not validate_request_type(request_type):
        return redirect_with_flash('/', 'Invalid request type.')

    # Anything queued here is handed to yt-dlp by the worker, and only YouTube URLs
    # normalise, carry an id the organizer can file under, or belong on this service.
    url = url.strip()
    if not youtube_domain(url):
        return redirect_with_flash('/', 'Not a YouTube URL.')

    # Classified before normalising, not after: normalising first rewrites a
    # watch+list URL to its playlist and destroys the `v=` that decides the type.
    detected = validate_url_type(url, request_type)
    if detected:
        return redirect_with_flash('/', f'Wrong type selected. URL is a {detected}.')

    url = normalize_url(url, request_type)
    message = queue_download(url, request_type)
    return redirect_with_flash('/', message)


# POST /retry/{job_id}: requeues that job's failed/cancelled work, then back to the dashboard.
@app.post('/retry/{job_id}')
def retry(job_id: int):
    message = retry_job(job_id)
    return redirect_with_flash('/', message)


# POST /cancel/{job_id}: requests cancellation of the job if it is still in progress, and returns
# to the dashboard with no message, whether or not the id matched a row.
@app.post('/cancel/{job_id}')
def cancel(job_id: int):
    cancel_job(job_id)
    return redirect_with_flash('/')


# Cancels every top-level job in progress, and each of its children still in flight, in one
# session, and reports how many hierarchies were cancelled. Selection mirrors query_downloads():
# a parent whose own status lags behind an active child still shows as active on the dashboard,
# so matching only the stored parent status would skip a hierarchy the user can see running. A
# parent stored as active whose children have all stopped is not in progress, and is left alone.
def cancel_all_jobs() -> int:
    with Session() as db:
        child = aliased(Download)
        has_active_child = (
            db.query(child.id)
            .filter(child.parent_id == Download.id, child.status.in_(ACTIVE_STATUS_SET))
            .exists()
        )

        # Children come along eagerly because request_job_cancel() reads each one, flagging those
        # in flight: lazy-loading them would fire a query per parent from inside the write
        # transaction below.
        jobs = (
            db.query(Download)
            .options(selectinload(Download.children))
            .filter(
                Download.parent_id.is_(None),
                or_(Download.status.in_(ACTIVE_STATUSES), has_active_child),
            )
            .all()
        )

        # request_job_cancel, not apply_status_update_to_hierarchy: the latter updates a parent
        # only when a child's status actually changed, so a playlist whose children are all
        # mid-download would have its children flagged and its own flag left unset.
        count = sum(1 for job in jobs if request_job_cancel(job))
        db.commit()
    return count


# Requeues every failed/cancelled item, child rows included, and reports how many hierarchies it
# reached. Routed as one Retry is, through requeue_job(), so a row whose recorded file is on disk
# goes to 'redownload' -- but with search_library=False: this acts on the whole table, and a
# tree walk of staging and the library per row is too much for one request.
def retry_all_jobs() -> int:
    with Session() as db:
        # Every top-level row, not just the failed ones: a parent can read completed while a child
        # underneath it failed, and reset_job_for_retry() is what filters row by row.
        parents = (
            db.query(Download)
            .options(selectinload(Download.children))
            .filter(Download.parent_id.is_(None))
            .all()
        )

        count = 0
        for job in parents:
            if requeue_job(job, search_library=False):
                count += 1
        db.commit()
    return count


# POST /cancel-all: requests cancellation of every active job and reports the count.
@app.post('/cancel-all')
def cancel_all():
    count = cancel_all_jobs()
    return redirect_with_flash('/', f'Cancel requested for {count} active job(s).')


# POST /retry-all: requeues every failed/cancelled job in the table and reports the count.
@app.post('/retry-all')
def retry_all():
    count = retry_all_jobs()
    if count:
        return redirect_with_flash('/', f'Retry queued for {count} failed/cancelled job(s).')
    return redirect_with_flash('/', 'No failed or cancelled jobs to retry.')


# GET /history: one page of finished jobs, most recent activity first. The sort and the paging
# both run over the full result set, so `page` is clamped into range rather than trusted.
@app.get('/history')
def history(request: Request, page: int = 1):
    jobs = query_downloads(HISTORY_STATUSES)

    jobs.sort(key=lambda job: compute_latest_timestamp(job) or datetime.min.replace(tzinfo=timezone.utc), reverse=True)

    # The status filter and this sort both run in Python, so the slice bounds what gets
    # rendered rather than what gets loaded -- rendering is what a full history costs most.
    page_count = max(1, -(-len(jobs) // HISTORY_PAGE_SIZE))
    page = min(max(page, 1), page_count)
    start = (page - 1) * HISTORY_PAGE_SIZE

    return render_template(request, 'history.html', {
        'jobs': jobs[start:start + HISTORY_PAGE_SIZE],
        'total': len(jobs),
        'page': page,
        'page_count': page_count,
        # request.url.path, the fallback in the table partial, would drop the page number and
        # send every bulk action back to page 1.
        'return_to': f'/history?page={page}' if page > 1 else '/history',
    })


# =======================================================
#             SUBSCRIPTION PAGE AND ACTIONS
# =======================================================

# GET /subscriptions: every subscription, newest first, with the channel or playlist the worker
# resolved it to.
@app.get('/subscriptions')
def subscriptions(request: Request):
    with Session() as db:
        subs = (
            db.query(Subscription)
            .options(joinedload(Subscription.channel), joinedload(Subscription.playlist))
            .order_by(Subscription.created_at.desc())
            .all()
        )

    return render_template(request, 'subscriptions.html', {'subscriptions': subs})


# POST /subscriptions/add: validates and canonicalises the URL as /add does, then records the
# subscription unless one already exists for it. Queues nothing itself -- the worker's periodic
# check resolves the target and downloads from it.
@app.post('/subscriptions/add')
def add_subscription(
    url: str = Form(...),
    subscription_type: str = Form(...),
    backfill_existing: str = Form('true'),
):
    if not validate_request_type(subscription_type):
        return redirect_with_flash('/subscriptions', 'Invalid subscription type.')

    # Only YouTube URLs, as in /add.
    url = url.strip()
    if not youtube_domain(url):
        return redirect_with_flash('/subscriptions', 'Not a YouTube URL.')

    # Classified before normalising -- see /add.
    detected = validate_url_type(url, subscription_type)
    if detected:
        return redirect_with_flash(
            '/subscriptions',
            f'Wrong type selected. URL is a {detected}, not a {subscription_type}.',
        )

    url = normalize_url(url, subscription_type)

    # The subscription table has a unique constraint on (url, subscription_type), so this
    # check is just for a friendlier flash message. The worker's periodic check also skips
    # duplicates, so a race between two submissions is harmless: one will succeed, the other
    # will be ignored.
    with Session() as db:
        exists = db.query(Subscription).filter(Subscription.url == url).first()

        if exists:
            return redirect_with_flash('/subscriptions', 'Subscription already exists.')

        # backfill_existing arrives as the form's string, so it is compared rather than cast --
        # bool('false') is True and would silently subscribe with backfill on.
        sub = Subscription(
            url=url,
            subscription_type=subscription_type,
            enabled=True,
            backfill_existing=(backfill_existing == 'true'),
        )
        db.add(sub)
        db.commit()

    return redirect_with_flash('/subscriptions', 'Subscription added.')


# POST /subscriptions/delete/{id}: drops the subscription row, reporting success even when the id
# matched nothing.
@app.post('/subscriptions/delete/{subscription_id}')
def delete_subscription_route(subscription_id: int):
    remove_subscription(subscription_id)

    return redirect_with_flash('/subscriptions', 'Subscription deleted.')


# Paused subscriptions are never checked for new videos, but keep their history
# and settings.
@app.post('/subscriptions/{subscription_id}/toggle-enabled')
def toggle_subscription_enabled(subscription_id: int):
    with Session() as db:
        sub = db.get(Subscription, subscription_id)
        if not sub:
            return redirect_with_flash('/subscriptions', 'Subscription not found.')
        sub.enabled = not sub.enabled
        message = f"Subscription {'enabled' if sub.enabled else 'disabled'}."
        db.commit()
    return redirect_with_flash('/subscriptions', message)


# The "download all existing" preference only governs a subscription's first scan
# (see worker.check_subscriptions), so flipping it later does nothing on its own --
# "Download existing now" below, which sets it too, is what queues what was skipped.
@app.post('/subscriptions/{subscription_id}/toggle-backfill')
def toggle_subscription_backfill(subscription_id: int):
    with Session() as db:
        sub = db.get(Subscription, subscription_id)
        if not sub:
            return redirect_with_flash('/subscriptions', 'Subscription not found.')
        sub.backfill_existing = not sub.backfill_existing
        message = f"Backfill preference set to {'download all existing' if sub.backfill_existing else 'only new'}."
        db.commit()
    return redirect_with_flash('/subscriptions', message)


# Asks for this subscription's skipped videos: sets backfill_existing and clears last_checked, so
# the worker's next check is a full first scan that queues them in scan order (see
# worker.check_subscriptions). Queues nothing itself, and changes nothing for a disabled
# subscription, which is never checked.
@app.post('/subscriptions/{subscription_id}/backfill-now')
def backfill_subscription_now(subscription_id: int):
    with Session() as db:
        sub = db.get(Subscription, subscription_id)
        if not sub:
            return redirect_with_flash('/subscriptions', 'Subscription not found.')

        if not sub.enabled:
            return redirect_with_flash(
                '/subscriptions',
                'Enable the subscription first -- a disabled subscription is never checked.',
            )

        # Both stay empty until the worker's periodic check resolves them (see
        # worker.resolve_subscription_target), which a new subscription may not have
        # run yet.
        if sub.channel_id is None and sub.playlist_id is None:
            return redirect_with_flash(
                '/subscriptions',
                'Subscription has not finished its first scan yet -- try again in a minute.',
            )

        # A fresh scan rather than requeuing the skipped rows here: the scan queues them in its own
        # order, which the order the rows were recorded in need not follow. Both columns are
        # written whatever they held: during a first scan last_checked reads empty already, and an
        # UPDATE without it would leave the stamp that scan may commit before this one.
        db.query(Subscription).filter(Subscription.id == sub.id).update(
            {Subscription.backfill_existing: True, Subscription.last_checked: None},
            synchronize_session=False,
        )
        db.commit()

    return redirect_with_flash(
        '/subscriptions',
        'Skipped videos are queued at the next subscription check, which runs every 30 minutes '
        'between downloads.',
    )


# =======================================================
#              JOB DETAIL, LOG AND ACTIONS
# =======================================================
# The job detail page, its log, and the redownload/delete actions the job and history pages post.

# Number of trailing log lines the job page shows inline; the log page shows all of them.
JOB_LOG_TAIL_LINES = 150


# The tail of a job's log for the inline view, or None when it has logged nothing yet.
def job_log_tail(job: Download) -> Optional[str]:
    if not job.log:
        return None
    return '\n'.join(job.log.splitlines()[-JOB_LOG_TAIL_LINES:])


# GET /job/{job_id}: the job detail page, with the tail of its log and the baseline its poller
# compares against. A missing job redirects to / instead of rendering.
@app.get('/job/{job_id}')
def job_details(request: Request, job_id: int):
    with Session() as db:
        job = load_job(db, job_id)

    # job.html and its partials dereference the job unconditionally, so a missing one has to
    # be turned away here rather than rendered as an empty page.
    if not job:
        return redirect_with_flash('/', 'Job not found.')

    # The baseline covers the children too, so a playlist page keeps repolling while only its
    # children are moving.
    latest = compute_latest_timestamp(job, include_children=True)

    # Rendered with the session already closed; load_job()'s eager loading is what makes the
    # template's job.children and job.parent reads safe here.
    return render_template(
        request,
        'job.html',
        {
            'job': job,
            'job_last_update': latest.isoformat() if latest else '',
            'job_log': job_log_tail(job),
        },
    )


# Clears a job's transient state and puts it back into one the worker acts on (`status` when
# given), through one conditional UPDATE that applies only while the row still holds the status
# this request read, so a row the worker took up meanwhile is left alone. Returns whether the row
# was reset; its updated_at moves with it, so polling and list views reflect the change.
def reset_for_redownload(db, target: Download, status: Optional[str] = None) -> bool:
    # An existing file means 'redownload', which runs the worker's rename/refresh
    # path; otherwise this is just a fresh download.
    new_status = status or ('redownload' if target.filename else 'queued')
    reset = (
        db.query(Download)
        .filter(Download.id == target.id, Download.status == target.status)
        .update(
            {
                'error': None,
                'organized_at': None,
                'progress': None,
                'speed': None,
                'cancel_requested': False,
                'status': new_status,
                # A person asked for it again, so the worker's retry schedule starts over.
                'auto_retry_attempts': 0,
                'next_auto_retry_at': None,
            },
            synchronize_session=False,
        )
    )
    return bool(reset)


# Queue a redownload for one job in the caller's transaction, cascading to its children when it is
# a playlist/channel parent. Refused, changing nothing and returning False, while the job is in
# progress (jobstate.is_in_progress) or once the worker has changed it since this request read it.
def request_job_redownload(db, job: Download) -> bool:
    if is_in_progress(job):
        return False

    # The parent goes first and decides for the whole hierarchy. It owns no file, so it is sent to
    # 'redownload' outright; its empty filename would send it to 'queued' instead.
    if not reset_for_redownload(db, job, 'redownload' if job.children else None):
        return False

    # Reset every child, or the worker only redoes the parent's bookkeeping and
    # no video is actually fetched again.
    for child in job.children:
        reset_for_redownload(db, child)

    job.log = (job.log or '') + 'Redownload requested by user\n'
    return True


# The flash for a redownload refused while the job is in progress.
REDOWNLOAD_REFUSED_MESSAGE = 'Still in progress; Fetch Metadata is available once it has stopped.'


# Flash wording for a redownload of several jobs; the "left alone" clause appears only when some
# were refused as still in progress.
def describe_redownload(requested: int, in_progress: int) -> str:
    if in_progress:
        return (
            f'Metadata fetch requested for {requested} item(s); '
            f'{in_progress} still in progress were left alone.'
        )
    return f'Metadata fetch requested for {requested} item(s).'


# POST /job/{job_id}/redownload: queues a fresh fetch for the job and its children, then back to
# the job page; refused with a flash while the job is in progress.
@app.post('/job/{job_id}/redownload')
def request_redownload(job_id: int):
    with Session() as db:
        job = load_job(db, job_id)
        if not job:
            return redirect_with_flash('/', 'Job not found.')

        requested = request_job_redownload(db, job)
        db.commit()

    if not requested:
        return redirect_with_flash(f'/job/{job_id}', REDOWNLOAD_REFUSED_MESSAGE)
    return redirect_with_flash(f'/job/{job_id}', 'Metadata fetch requested.')


# POST /job/{job_id}/delete: removes the job's files, keeping the row as 'deleted'.
@app.post('/job/{job_id}/delete')
def delete_job_route(job_id: int):
    message = delete_job(job_id)
    # A deleted job leaves both the queue and history views, so land on history
    # whichever page triggered this.
    return redirect_with_flash('/history', message)


# Only ever redirect a bulk action back to a page this app actually serves it from, so a bulk
# form's return_to field can't be abused to redirect somewhere else.
BULK_RETURN_PATHS = frozenset({'/', '/history'})


# The target is rebuilt from parts that are each validated on their own -- the path against the
# allowlist, the page through int() -- so no part of the submitted value reaches the redirect.
def safe_return_path(path: str) -> str:
    parsed = urlparse(path)
    # A scheme or host means it addresses somewhere else, whatever path it carries.
    if parsed.scheme or parsed.netloc or parsed.path not in BULK_RETURN_PATHS:
        return '/'

    page = parse_qs(parsed.query).get('page', [''])[0]
    return f'{parsed.path}?page={int(page)}' if page.isdigit() and int(page) > 1 else parsed.path


# POST /history/redownload-all: queues a fresh fetch for every entry the history view lists, in
# one session and one commit, leaving alone any that went back into progress meanwhile.
@app.post('/history/redownload-all')
def redownload_all():
    # Reuse the history query so the button acts on every entry that view lists, which is
    # more than the page on screen once history is paginated.
    job_ids = [job.id for job in query_downloads(HISTORY_STATUSES)]

    with Session() as db:
        jobs = load_jobs(db, job_ids)

        # Same path the per-job button uses.
        requested = 0
        for job in jobs:
            if request_job_redownload(db, job):
                requested += 1

        db.commit()
        count = len(jobs)

    if not count:
        return redirect_with_flash('/history', 'No history entries to refresh.')
    entries = 'entry' if requested == 1 else 'entries'
    message = f'Metadata fetch requested for {requested} history {entries}.'
    if requested < count:
        message += f' {count - requested} still in progress were left alone.'
    return redirect_with_flash('/history', message)


# GET /job/{job_id}/log: the whole log for one job on its own page. A missing job still renders
# the page, with "Job not found." where the log would be.
@app.get('/job/{job_id}/log')
def view_job_log(request: Request, job_id: int):
    with Session() as db:
        job = load_job(db, job_id)

    if not job:
        return render_template(request, 'log.html', {'job': None, 'log_content': 'Job not found.', 'job_last_update': None})

    job_last_update = compute_latest_timestamp(job, include_children=True)

    return render_template(request, 'log.html', {
        'job': job,
        'log_content': job.log or 'No log available.',
        'job_last_update': job_last_update.isoformat() if job_last_update else None,
    })


# Marks failed/cancelled jobs completed when their file is already on disk, e.g. after a crash
# lost the row update the download had earned, then settles each parent it reached through the
# shared rule (jobstate.settle_parent). Walks the library per job, so it is a manual button
# rather than something the page does on its own.
@app.post('/admin/sweep_existing')
def sweep_existing():
    updated = 0

    # The worker's adopt_existing_download() is the same lookup the retry flow uses, so it
    # refuses parent rows, which own no file of their own. The sweep checks every failed/cancelled
    # row, and of a parent only its failed/cancelled children, so the same rows are checked as in
    # the retry flow and a child in flight is left to the worker.
    with Session() as db:
        items = (
            db.query(Download)
            .options(selectinload(Download.children), joinedload(Download.parent))
            .filter(Download.status.in_(FAILED_OR_CANCELLED))
            .all()
        )

        logging.info('Admin sweep: checking %d failed/cancelled items', len(items))

        parents: dict[int, Download] = {}
        for item in items:
            is_parent = item.parent_id is None and bool(item.children)
            targets = item.children if is_parent else [item]

            # The same lookup the worker and the retry flow use; it refuses parent rows, which
            # own no file of their own. A child reached both as an item and through its parent
            # is completed by the first pass, so the second skips it.
            for target in (t for t in targets if t.status in FAILED_OR_CANCELLED):
                if adopt_existing_download(target):
                    updated += 1
                    logging.info('Marked job %s completed -> %s', target.id, target.filename)

            parent = item if is_parent else item.parent
            if parent is not None:
                parents[parent.id] = parent

        # Each parent the sweep reached settles by the shared rule once its children are done,
        # so a failed child still outranks completed ones.
        for parent in parents.values():
            settle_idle_parent(parent)

        db.commit()

    return redirect_with_flash('/', f'Sweep completed, {updated} items marked completed.')


# =======================================================
#                  BULK ACTION ROUTES
# =======================================================
# Apply retry/cancel/redownload/delete to the job IDs checked in the shared downloads table. Each
# loads the whole selection and commits once -- a session per row would take SQLite's single write
# lock once per row, with the worker queued behind every one.

# POST /bulk/retry: requeues the failed/cancelled work in every selected job.
@app.post('/bulk/retry')
def bulk_retry(job_ids: list[int] = Form(default=[]), return_to: str = Form('/')):
    with Session() as db:
        jobs = load_jobs(db, job_ids)
        for job in jobs:
            requeue_job(job)
        db.commit()
        count = len(jobs)
    message = f'Retry applied to {count} item(s).' if count else 'No items selected.'
    return redirect_with_flash(safe_return_path(return_to), message)


# POST /bulk/cancel: requests cancellation of every selected job still in progress; the rest of
# a selection has stopped already and is left alone.
@app.post('/bulk/cancel')
def bulk_cancel(job_ids: list[int] = Form(default=[]), return_to: str = Form('/')):
    with Session() as db:
        jobs = load_jobs(db, job_ids)
        cancelled = sum(1 for job in jobs if request_job_cancel(job))
        db.commit()
    message = f'Cancel requested for {cancelled} item(s).' if jobs else 'No items selected.'
    return redirect_with_flash(safe_return_path(return_to), message)


# POST /bulk/redownload: queues a fresh fetch for every selected job not still in progress.
@app.post('/bulk/redownload')
def bulk_redownload(job_ids: list[int] = Form(default=[]), return_to: str = Form('/')):
    with Session() as db:
        jobs = load_jobs(db, job_ids)
        requested = 0
        for job in jobs:
            if request_job_redownload(db, job):
                requested += 1
        db.commit()
        count = len(jobs)
    message = describe_redownload(requested, count - requested) if count else 'No items selected.'
    return redirect_with_flash(safe_return_path(return_to), message)


# POST /bulk/delete: removes the files for every selected job, skipping any still active.
@app.post('/bulk/delete')
def bulk_delete(job_ids: list[int] = Form(default=[]), return_to: str = Form('/history')):
    with Session() as db:
        # Every row is loaded before the first file goes, so nothing here re-queries mid-loop
        # and autoflushes -- that would open the write transaction and hold it across the
        # deletions, which is exactly when the worker starts failing on "database is locked".
        jobs = load_jobs(db, job_ids)
        deleted = 0
        skipped_active = 0
        for job in jobs:
            job_deleted, job_skipped = delete_job_files(job)
            deleted += job_deleted
            skipped_active += job_skipped
        # A deleted child may leave its parent settled; settled only once every file is gone, as
        # loading a parent may autoflush and take the write lock.
        for job in jobs:
            if job.parent_id is not None:
                settle_idle_parent(job.parent)
        db.commit()

    message = describe_deletion(deleted, skipped_active) if jobs else 'No items selected.'
    return redirect_with_flash(safe_return_path(return_to), message)


# =======================================================
#                     POLLING APIS
# =======================================================
# The `since` endpoints only answer "has anything changed?"; the pages fetch the matching /section
# partial and swap it in themselves once the answer is yes.

# GET /api/queue: whether any row changed since `since`, plus the current baseline. Without a
# `since` it reports no change, so a first poll only picks the baseline up.
@app.get('/api/queue')
def api_queue(since: Optional[str] = None):
    # One value, read from the updated_at index on every poll from every open tab. Every row has
    # an updated_at since schema version 2, and every write moves it, through the column's
    # onupdate.
    with Session() as db:
        latest_update = normalize_datetime(db.query(func.max(Download.updated_at)).scalar())
    since_dt = parse_iso_datetime(since)
    changed = bool(since_dt and latest_update and latest_update > since_dt)

    return JSONResponse(
        {
            'changed': changed,
            'latest': latest_update.isoformat() if latest_update else None,
        }
    )


# GET /api/queue/section: re-renders the queue for a dashboard poller that has already been told
# something changed.
@app.get('/api/queue/section')
def api_queue_section(request: Request):
    jobs = query_downloads(QUEUE_STATUSES, hide_stale_endings=True)
    # A fragment, not a page: the dashboard drops it straight into #queue-content. `return_to`
    # is passed explicitly because this request's own path is not one a bulk form may post back.
    return templates.TemplateResponse(
        request,
        'partials/queue_section.html',
        {'jobs': jobs, 'return_to': '/'},
    )


# GET /api/job/{job_id}: whether this job changed since `since`, plus the fields the job page
# updates in place. 404 with changed=False once the row is gone.
@app.get('/api/job/{job_id}')
def api_job(job_id: int, since: Optional[str] = None):
    with Session() as db:
        job = load_job(db, job_id)

    if not job:
        return JSONResponse({'changed': False}, status_code=404)

    # The job page's poller compares the last update timestamp of the job and all its children
    # against the last poll's timestamp, so a parent whose own status lags behind an active child
    # still shows as changed. The page then fetches the /section partial to swap in.
    latest_update = compute_latest_timestamp(job, include_children=True)
    since_dt = parse_iso_datetime(since)
    changed = bool(latest_update and since_dt and latest_update > since_dt)

    # Status, error, progress and speed ride along with the answer, so a poll that only moves a
    # progress bar updates from this response without fetching the /section partial at all.
    return JSONResponse(
        {
            'changed': changed,
            'latest': latest_update.isoformat() if latest_update else None,
            'status': compute_effective_status(job),
            'error': job.error,
            'progress': job.progress,
            'speed': job.speed,
        }
    )


# GET /api/job/{job_id}/section: the job's content block as a fragment for the job page's poller
# to swap in; 404 JSON when the row is gone.
@app.get('/api/job/{job_id}/section')
def api_job_section(request: Request, job_id: int):
    with Session() as db:
        job = load_job(db, job_id)

    if not job:
        return JSONResponse({'error': 'Job not found.'}, status_code=404)

    return templates.TemplateResponse(
        request,
        'partials/job_content.html',
        {'job': job, 'job_log': job_log_tail(job)},
    )


# Carries the whole log, so the log page's poller renders straight from this response rather
# than fetching it a second time.
@app.get('/api/job/{job_id}/log')
def api_job_log(job_id: int, since: Optional[str] = None):
    with Session() as db:
        job = load_job(db, job_id)

    if not job:
        return JSONResponse({'error': 'Job not found.'}, status_code=404)

    # Change detection as in /api/job/{job_id}, except the log travels with this response, so the
    # page renders from it instead of fetching a partial.
    latest_update = compute_latest_timestamp(job, include_children=True)
    since_dt = parse_iso_datetime(since)
    changed = bool(latest_update and since_dt and latest_update > since_dt)

    return JSONResponse(
        {
            'changed': changed,
            'latest': latest_update.isoformat() if latest_update else None,
            'log_content': job.log or '',
        }
    )


# =======================================================
#                        HEALTH
# =======================================================

# GET /health: what the container healthcheck calls. It touches neither the database nor the
# filesystem on purpose -- it answers "is uvicorn still serving requests?", and a slow query or a
# locked SQLite file must not be reported as a dead container and restarted.
@app.get('/health')
def health():
    return JSONResponse({'status': 'ok'})
