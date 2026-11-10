# -------------------------------------------------------
# Organizer
#
# Organizer for the YouTube Manager: files completed downloads out of the flat DOWNLOAD_PATH into
# a Jellyfin library layout. Playlist-linked and channel-grouped videos become episodes of a
# per-playlist/per-channel show; everything else becomes a movie. Naming follows Sonarr/Radarr
# conventions plus a [{video_id}] collision guard. Safe to re-run: organized_at, or a destination
# file that already exists, means already-organized, unless no other row uses it and its size
# differs.
# -------------------------------------------------------

import errno
import glob
import json
import logging
import os
import re
import shutil
import time
import unicodedata
from contextlib import contextmanager, suppress
from datetime import datetime
from pathlib import Path
from typing import Any, BinaryIO, Callable, Iterator, NamedTuple, Optional
from urllib.parse import urlparse
from xml.etree import ElementTree as ET

import requests
from sqlalchemy.orm import object_session

from youtube_manager.database import Channel, Download, Playlist, Session, Subscription, utcnow
from youtube_manager.jobstate import ACTIVE_STATUS_SET
from youtube_manager.media import (
    DOWNLOAD_PATH,
    MEDIA_PATH,
    STAGING_TEMP,
    YOUTUBE_ID_PATTERN,
    find_existing_media,
    is_media_file,
    is_ytdlp_intermediate,
)


# =======================================================
#              SETUP AND NAMING CONSTANTS
# =======================================================

# Root of the Jellyfin media library, as mounted into this container.
SHOWS_DIR = MEDIA_PATH
MOVIES_DIR = MEDIA_PATH
CHANNEL_DIR = MEDIA_PATH

# Jellyfin connection details for the post-organize library scan. Both optional: with either
# unset the notification is skipped rather than treated as an error, since the videos are filed.
JELLYFIN_URL = os.getenv('JELLYFIN_URL')
JELLYFIN_TOKEN = os.getenv('JELLYFIN_TOKEN')

# Characters that are unsafe (or awkward) across common filesystems used for media shares.
ILLEGAL_FILENAME_CHARS = '<>:"/\\|?*'

# Cap on one filename component. ext4/exFAT stop at 255 bytes and a longer name fails the move
# outright; the margin covers the sidecar suffixes hung off the same base name (-thumb.jpg).
MAX_NAME_LENGTH = 200

# Characters XML 1.0 forbids. One in a description makes the whole NFO unparseable for Jellyfin,
# and a lone surrogate cannot be encoded to UTF-8 at all.
ILLEGAL_XML_CHARS = re.compile(r'[\x00-\x08\x0b\x0c\x0e-\x1f\ud800-\udfff]')

# Map common yt-dlp vcodec prefixes to friendly labels for the simplified quality tag.
VCODEC_LABELS = {
    'avc1': 'H264',
    'av01': 'AV1',
    'vp9': 'VP9',
    'vp09': 'VP9',
    'hev1': 'HEVC',
    'hvc1': 'HEVC',
}

# Upper bound on <tag> elements in an NFO: YouTube tag lists run to hundreds of near-duplicate
# keywords, which clutters the library UI.
MAX_NFO_TAGS = 25

# Domains artwork may be fetched from: YouTube's thumbnail CDN and the two hosts serving channel
# avatars. Any other host is refused before a request goes out, so a foreign or redirecting URL
# fails fast with one WARN.
IMAGE_HOST_DOMAINS = ('ytimg.com', 'ggpht.com', 'googleusercontent.com')

# Cap on one artwork download. Thumbnails and avatars are far smaller; a larger body is not one.
MAX_IMAGE_BYTES = 10 * 1024 * 1024

# Seconds between repeats of the missing-library ERROR while the library stays unavailable.
LIBRARY_ERROR_INTERVAL_SECONDS = 3600


# Prefixed so organizer output is easy to grep out of the worker's logs.
def log(msg: str) -> None:
    logging.info('[organizer] %s', msg)


# WARN with the same prefix: a failure the next scan retries, or one that loses nothing.
def warn(msg: str) -> None:
    logging.warning('[organizer] %s', msg)


# ERROR with the same prefix. Deployments alert on ERROR lines from either service, so this is
# kept for what needs a human: a failed move or NFO write, a rejected Jellyfin token, a refused
# delete.
def error(msg: str) -> None:
    logging.error('[organizer] %s', msg)


# =======================================================
#             FILENAME AND FIELD SANITISING
# =======================================================
# Raw titles and yt-dlp fields reduced to something a filesystem or an NFO element can hold.

# Characters, beyond plain ASCII letters/digits, kept as-is in a sanitized filename.
SAFE_FILENAME_CHARS = " -_.,()[]!'&"

# Strip filesystem-unsafe characters from a title, collapse whitespace, cap length (150 for
# show/movie titles, 90 for episodes per Sonarr's {Episode CleanTitle:90}), and fold non-ASCII to
# ASCII, dropping the rest: Tdarr/HandBrake's ffmpeg errors on it. The NFO keeps the raw title.
def sanitize_filename(name: str, max_length: int = 150) -> str:
    transliterated = unicodedata.normalize('NFKD', name).encode('ascii', 'ignore').decode('ascii')
    cleaned = ''.join(
        c for c in transliterated
        if c not in ILLEGAL_FILENAME_CHARS and (c.isalnum() or c in SAFE_FILENAME_CHARS)
    )
    cleaned = ' '.join(cleaned.split())
    # SMB/NTFS shares mangle or reject a leading or trailing dot or space, and a name of nothing
    # but dots would address the parent directory instead.
    return cleaned[:max_length].strip(' .') or 'untitled'


# yt-dlp's YYYYMMDD upload_date in the YYYY-MM-DD form an NFO's <aired>/<premiered> needs.
def format_upload_date(raw: Optional[str]) -> Optional[str]:
    if not raw or len(str(raw)) != 8 or not str(raw).isdigit():
        return None
    raw = str(raw)
    return f'{raw[0:4]}-{raw[4:6]}-{raw[6:8]}'


# The year out of yt-dlp's YYYYMMDD upload_date, for an NFO's <year>; None if the date is invalid.
def extract_year(raw: Optional[str]) -> Optional[str]:
    formatted = format_upload_date(raw)
    return formatted[:4] if formatted else None


# The form Kodi/Jellyfin expect for <dateadded>.
def format_timestamp(value: Optional[datetime]) -> Optional[str]:
    return value.strftime('%Y-%m-%d %H:%M:%S') if value else None


# Whole minutes for the NFO's <runtime>, never rounding a short video down to zero.
def format_runtime_minutes(seconds: Optional[Any]) -> Optional[int]:
    try:
        total = float(seconds)
    except (TypeError, ValueError):
        return None
    return max(1, round(total / 60)) if total > 0 else None


# Shorten a raw yt-dlp vcodec ("avc1.640028") to a friendly label ("H264").
def normalize_vcodec(raw: Optional[str]) -> Optional[str]:
    if not raw or raw == 'none':
        return None
    prefix = str(raw).split('.')[0].lower()
    return VCODEC_LABELS.get(prefix, prefix.upper())


# Decode a JSON-encoded list column (Download.tags / Download.categories).
def decode_list_column(raw: Optional[str]) -> list:
    if not raw:
        return []
    try:
        decoded = json.loads(raw)
    except (TypeError, ValueError):
        return []
    return decoded if isinstance(decoded, list) else []


# =======================================================
#             SIDECARS AND STORED METADATA
# =======================================================

# Locate a sidecar (thumbnail, info.json, description) sharing the video file's stem.
def find_sidecar(video_path: Path, suffix: str) -> Optional[Path]:
    candidate = video_path.parent / (video_path.stem + suffix)
    if candidate.exists():
        return candidate

    # Glob metacharacters in a title ("[Official Video]") would otherwise fail to match.
    escaped_stem = glob.escape(video_path.stem)
    matches = sorted(video_path.parent.glob(f'{escaped_stem}{suffix}*'))
    return matches[0] if matches else None


# The fullest yt-dlp info dictionary available for a job. The copy on the row wins: it survives
# the sidecar cleanup that follows organizing. The .info.json on disk is the fallback.
def load_job_metadata(job: Download, video_path: Optional[Path]) -> dict:
    if job.metadata_json:
        try:
            stored = json.loads(job.metadata_json)
            if isinstance(stored, dict):
                return stored
        except (TypeError, ValueError):
            log(f'Job {job.id}: stored metadata is not valid JSON, falling back to sidecar')

    # A recorded metadata_file predates any rename of the video, so the stem beside it is tried too.
    info_path = Path(job.metadata_file) if job.metadata_file else None
    if (not info_path or not info_path.exists()) and video_path:
        info_path = find_sidecar(video_path, '.info.json')
    if not info_path or not info_path.exists():
        return {}

    # An unreadable sidecar yields {} rather than raising: every caller treats the info dictionary
    # as optional and falls back to the job's own columns.
    try:
        with open(info_path, 'r', encoding='utf-8') as fh:
            loaded = json.load(fh)
        return loaded if isinstance(loaded, dict) else {}
    except (OSError, ValueError) as exc:
        log(f'Job {job.id}: could not read {info_path} ({exc})')
        return {}


# =======================================================
#                 DESTINATION FILENAMES
# =======================================================
# The pieces every destination name is assembled from, before the plan_* functions order them.

# The "[1080p][H264]" tag standing in for the MediaInfo/Custom Format section of the
# Radarr/Sonarr naming templates.
def build_quality_tag(info: dict) -> str:
    parts = []
    height = info.get('height')
    if height:
        parts.append(f'[{height}p]')
    vcodec = normalize_vcodec(info.get('vcodec'))
    if vcodec:
        parts.append(f'[{vcodec}]')
    return ''.join(parts)


# Append `[id]` to a base filename, trimming the base so the whole name stays within
# MAX_NAME_LENGTH; the tag is dropped entirely when no ID is known.
def append_id_tag(base_name: str, youtube_id: Optional[str]) -> str:
    ident = str(youtube_id) if youtube_id else ''
    # The ID is the one part of a name that never goes through sanitize_filename, and it lands in
    # a directory component, so a separator or a dots-only value in it would escape the folder.
    if '/' in ident or '\\' in ident or not ident.strip(' .'):
        ident = ''

    tag = f' [{ident}]' if ident else ''
    return base_name[:MAX_NAME_LENGTH - len(tag)].rstrip(' .') + tag


# The tag that makes one video's filename unique. The row id stands in when the video has no
# YouTube id: untagged, every untitled video plans the same destination, and execute_plan() reads
# an existing destination as this video already filed -- so the second one to organize adopts the
# first one's file and discards its own download. The id is stable, so re-runs plan the same path.
def video_id_tag(job: Download) -> str:
    return str(job.youtube_id) if job.youtube_id else f'job-{job.id}'


# =======================================================
#               WRITING INTO THE LIBRARY
# =======================================================
# Nothing lands under its final name until it is complete, and nothing is filed while the library
# disk looks unmounted. Temps are hidden '.<name>.partial' files, which Jellyfin never indexes.

# The hidden temp a write to `dest` goes through: beside it, so the final rename stays on one
# filesystem, and ending '.partial' so neither Jellyfin nor is_media_file() reads it as media.
def partial_path(dest: Path) -> Path:
    return dest.with_name(f'.{dest.name}.partial')


# Yield partial_path(dest) for the caller to fill, then rename it over `dest` on a clean exit. On
# any exception the temp is removed and the exception re-raised, leaving `dest` as it was. A stale
# temp from an interrupted run is cleared first.
@contextmanager
def partial_file(dest: Path) -> Iterator[Path]:
    temp = partial_path(dest)
    try:
        temp.unlink(missing_ok=True)
        yield temp
        os.replace(temp, dest)
    except BaseException:
        # Suppressed, so a failed cleanup never hides the error that caused it.
        with suppress(OSError):
            temp.unlink(missing_ok=True)
        raise


# Write `dest` atomically: `write` fills the temp through a binary handle, which is fsynced before
# the rename, so a crash leaves the old file or the new one, never a truncated one. Raises on
# failure.
def write_atomically(dest: Path, write: Callable[[BinaryIO], None]) -> None:
    with partial_file(dest) as temp:
        with open(temp, 'wb') as fh:
            write(fh)
            fh.flush()
            os.fsync(fh.fileno())


# Copy `src` to `dest` with shutil.copy2's metadata (mtime and mode), its bytes fsynced to disk.
def copy_synced(src: Path, dest: Path) -> None:
    shutil.copyfile(src, dest)
    # Opened for append, which writes nothing: Windows only fsyncs a handle open for writing.
    with open(dest, 'ab') as fh:
        os.fsync(fh.fileno())
    # Last, as copy2 orders it: the mode copied here may make the file read-only.
    shutil.copystat(src, dest)


# Flush a rename into `folder` to disk. Best-effort: Windows cannot open a directory this way, and
# the rename itself has happened either way.
def fsync_directory(folder: Path) -> None:
    try:
        fd = os.open(folder, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


# True when `src` and the folder `dest` lands in share a filesystem, so os.replace can rename across
# them. Its own function so the tests can force safe_move's copy branch on a single disk.
def same_filesystem(src: Path, dest: Path) -> bool:
    return src.stat().st_dev == dest.parent.stat().st_dev


# Move `src` over `dest` so that one complete copy exists at every moment. On one filesystem that is
# a rename. Across two -- staging and the library are separate disks, so this is the normal case --
# a synced copy is renamed into place before `src` is unlinked. Raises on failure with `src` intact
# and no temp left. `dest`'s folder must already exist.
def safe_move(src: Path, dest: Path) -> None:
    if same_filesystem(src, dest):
        try:
            os.replace(src, dest)
            return
        except OSError as exc:
            # Two bind mounts of one filesystem share st_dev, yet a rename between them fails.
            if exc.errno != errno.EXDEV:
                raise

    with partial_file(dest) as temp:
        copy_synced(src, temp)
    fsync_directory(dest.parent)
    src.unlink()


# time.monotonic() of the last missing-library ERROR; None while the library is ready, so the first
# refused pass of each outage logs at once. Module state, which the tests reset.
_library_error_logged_at: Optional[float] = None


# True when organizing may write into the library. Opt-in: with LIBRARY_SENTINEL unset it is always
# True; set, the file it names must exist, so a USB disk that failed to mount is never filed into.
# A refusal logs ERROR at most once an hour, and the sentinel's return logs one INFO line.
def library_ready() -> bool:
    global _library_error_logged_at
    # Read per call rather than at import, so a test can set it without reloading the module.
    sentinel = os.getenv('LIBRARY_SENTINEL')
    if not sentinel:
        return True

    if Path(sentinel).exists():
        if _library_error_logged_at is not None:
            log(f'Library sentinel {sentinel} is back; organizing resumes')
            _library_error_logged_at = None
        return True

    now = time.monotonic()
    if _library_error_logged_at is None or now - _library_error_logged_at >= LIBRARY_ERROR_INTERVAL_SECONDS:
        error(f'Library sentinel {sentinel} is missing, so {MEDIA_PATH} looks unmounted; '
              'not organizing until it is back')
        _library_error_logged_at = now
    return False


# =======================================================
#                   NFO CONSTRUCTION
# =======================================================
# A whole document is described as one plain dict and rendered by append_nfo_element().

# Drop the characters that would make the NFO unparseable, whatever a title or description holds.
def clean_xml_text(value: Any) -> str:
    return ILLEGAL_XML_CHARS.sub('', str(value))


# Add one NFO element to `parent`. Values are handled by shape so callers can describe a whole
# document as plain data: None and empty strings are dropped, lists become repeated elements,
# and dicts become nested elements (with '@attrs' for XML attributes and '#text' for content).
def append_nfo_element(parent: ET.Element, tag: str, value: Any) -> None:
    if value is None:
        return

    # A sequence repeats the tag rather than nesting, which is the shape <genre> and <tag> take.
    if isinstance(value, (list, tuple, set)):
        for item in value:
            append_nfo_element(parent, tag, item)
        return

    # A dict is one element carrying attributes and text at once, as <uniqueid> and <rating> need.
    if isinstance(value, dict):
        element = ET.SubElement(parent, tag)
        for attr_name, attr_value in (value.get('@attrs') or {}).items():
            element.set(attr_name, clean_xml_text(attr_value))
        text_value = value.get('#text')
        if text_value is not None:
            element.text = clean_xml_text(text_value)
        # Every other key recurses as a child, so a nested block like <fileinfo><streamdetails>
        # needs no case of its own here.
        for child_tag, child_value in value.items():
            if child_tag in {'@attrs', '#text'}:
                continue
            append_nfo_element(element, child_tag, child_value)
        return

    text = clean_xml_text(value).strip()
    if text:
        ET.SubElement(parent, tag).text = text


# Write a Jellyfin/Kodi-compatible NFO file. ElementTree does the XML escaping, so a title or
# description containing & or < is written safely whatever it contains. Written atomically: an
# interrupted write must not leave a truncated NFO that execute_plan() would take as healthy.
def write_nfo(path: Path, root_tag: str, fields: dict) -> None:
    root = ET.Element(root_tag)
    for tag, value in fields.items():
        append_nfo_element(root, tag, value)

    ET.indent(root, space='  ')
    tree = ET.ElementTree(root)
    write_atomically(path, lambda fh: tree.write(fh, encoding='utf-8', xml_declaration=True))


# Shorten a raw yt-dlp acodec ("mp4a.40.2") to its family ("mp4a"). Unlike normalize_vcodec there
# is no label map, and yt-dlp's "none" for a video carrying no audio track becomes None.
def normalize_acodec(raw: Optional[str]) -> Optional[str]:
    if not raw or raw == 'none':
        return None
    return str(raw).split('.')[0]


# Describe the video/audio streams yt-dlp reported, for the NFO's <fileinfo> section.
def build_stream_details(info: dict) -> Optional[dict]:
    # Keys are named as Kodi's <streamdetails> expects them, not as yt-dlp reports them.
    video = {
        'codec': normalize_vcodec(info.get('vcodec')),
        'width': info.get('width'),
        'height': info.get('height'),
        'aspect': info.get('aspect_ratio'),
        'durationinseconds': info.get('duration'),
    }
    # `language` labels the audio track in Jellyfin's player; yt-dlp reports it only where the
    # upload declares one, so most videos leave it out.
    audio = {
        'codec': normalize_acodec(info.get('acodec')),
        'channels': info.get('audio_channels'),
        'language': info.get('language'),
    }

    # Drop the unknown halves so an unreported audio track leaves no empty <audio/> element.
    video = {key: value for key, value in video.items() if value is not None}
    audio = {key: value for key, value in audio.items() if value is not None}
    if not video and not audio:
        return None

    streamdetails = {}
    if video:
        streamdetails['video'] = video
    if audio:
        streamdetails['audio'] = audio
    return {'streamdetails': streamdetails}


# Describe YouTube's own rating for the NFO's <ratings> block, when yt-dlp reported one.
def build_ratings(info: dict) -> Optional[dict]:
    try:
        value = float(info['average_rating'])
    except (KeyError, TypeError, ValueError):
        return None

    rating = {
        '@attrs': {'name': 'youtube', 'max': 5, 'default': 'true'},
        'value': round(value, 2),
    }
    # Like counts stand in for vote counts, which YouTube no longer exposes directly.
    likes = info.get('like_count')
    if likes:
        rating['votes'] = likes
    return {'rating': rating}


# The NFO fields shared by episodes and movies: the curated database columns first, then
# whatever else yt-dlp reported.
def build_common_video_fields(job: Download, info: dict, fallback_title: str) -> dict:
    # The publishing channel stands in as studio, director and credits for library purposes.
    uploader = (
        (job.channel.name if job.channel else None)
        or info.get('channel')
        or info.get('uploader')
    )
    plot = job.description or info.get('description')
    upload_date = job.upload_date or info.get('upload_date') or info.get('release_date')

    # The channel is also listed as an "actor" so its avatar surfaces in Jellyfin's People
    # section; <thumb> is fetched like any actor image, so a remote avatar URL needs no local copy.
    actor = None
    if job.channel and (job.channel.name or uploader):
        actor = [{
            'name': job.channel.name or uploader,
            'role': 'Creator',
            'thumb': job.channel.thumbnail,
        }]

    return {
        'title': job.title or info.get('title') or fallback_title,
        'originaltitle': info.get('fulltitle'),
        'plot': plot,
        # <outline> is the short form Kodi shows in list views; the first paragraph serves well.
        'outline': plot.strip().split('\n')[0][:300] if plot else None,
        'runtime': format_runtime_minutes(job.duration or info.get('duration')),
        'studio': uploader,
        'director': uploader,
        'credits': uploader,
        'actor': actor,
        'genre': decode_list_column(job.categories) or info.get('categories'),
        'tag': (decode_list_column(job.tags) or info.get('tags') or [])[:MAX_NFO_TAGS],
        'ratings': build_ratings(info),
        # <dateadded> drives Jellyfin's "recently added" row, so it is when this copy was fetched
        # rather than when YouTube published it.
        'dateadded': format_timestamp(job.completed_at or job.created_at),
        'uniqueid': {
            '@attrs': {'type': 'youtube', 'default': 'true'},
            '#text': job.youtube_id or info.get('id'),
        } if (job.youtube_id or info.get('id')) else None,
        'fileinfo': build_stream_details(info),
        # Not an NFO element: each caller pops it and maps it to <aired> or <premiered> instead.
        '_upload_date': upload_date,
    }


# =======================================================
#               SHOW FOLDERS AND ARTWORK
# =======================================================
# Where a show lives and what Jellyfin puts on its tile. Artwork failures are logged, never raised.

# A stable, filesystem-safe folder name for a playlist's show, assigned on first use so it stays
# fixed when the playlist's title is refreshed later. Committed rather than flushed: SQLite takes
# one writer, and the moves and artwork downloads that follow would hold that write open.
def get_or_assign_folder_name(db, playlist: Playlist) -> str:
    if playlist.folder_name:
        return playlist.folder_name

    base = sanitize_filename(playlist.title or playlist.youtube_id or f'playlist-{playlist.id}')
    folder_name = append_id_tag(base, playlist.youtube_id)

    playlist.folder_name = folder_name
    db.commit()
    return folder_name


# Same for a channel's grouped videos, and committed for the same reason.
def get_or_assign_channel_folder_name(db, channel: Channel) -> str:
    if channel.folder_name:
        return channel.folder_name

    base = sanitize_filename(channel.name or channel.youtube_id or f'channel-{channel.id}')
    folder_name = append_id_tag(base, channel.youtube_id)

    channel.folder_name = folder_name
    db.commit()
    return folder_name


# True for an https URL on an IMAGE_HOST_DOMAINS host or a subdomain of one, with no userinfo and
# no port but the default. hostname is lowercased by urlparse, and the dot in the suffix test keeps
# a lookalike such as "evil-ytimg.com" out.
def is_allowed_image_url(url: str) -> bool:
    try:
        parsed = urlparse(url)
        port = parsed.port
    except ValueError:
        return False
    if parsed.scheme != 'https' or '@' in parsed.netloc or port not in (None, 443):
        return False
    host = parsed.hostname or ''
    return any(host == domain or host.endswith(f'.{domain}') for domain in IMAGE_HOST_DOMAINS)


# An artwork response refused for what it is rather than for a transport failure.
class ImageRejected(Exception):
    pass


# Copy a streamed response body into `fh`. Raises ImageRejected once it passes MAX_IMAGE_BYTES,
# whatever Content-Length claimed.
def write_capped_body(response: requests.Response, fh: BinaryIO) -> None:
    received = 0
    for chunk in response.iter_content(chunk_size=64 * 1024):
        received += len(chunk)
        if received > MAX_IMAGE_BYTES:
            raise ImageRejected(f'body exceeds {MAX_IMAGE_BYTES} bytes')
        fh.write(chunk)


# Download an image into `dest`. Best-effort by design: artwork is never worth failing an
# organize over, so a failure is logged at WARN and reported rather than raised. Only
# is_allowed_image_url() hosts are fetched, a redirect counts as a failure, and the body must be
# image/* within MAX_IMAGE_BYTES; it is written atomically, so a failure leaves `dest` untouched.
def download_image(url: str, dest: Path, description: str) -> bool:
    if not is_allowed_image_url(url):
        warn(f'Refusing to download {description} from {url}: not https on a YouTube image host')
        return False

    try:
        with requests.get(url, timeout=15, stream=True, allow_redirects=False) as response:
            response.raise_for_status()
            # With redirects off, a 3xx arrives as an ordinary response carrying no image.
            if response.status_code != 200:
                raise ImageRejected(f'HTTP {response.status_code}')
            content_type = response.headers.get('Content-Type', '')
            if not content_type.lower().startswith('image/'):
                raise ImageRejected(f'Content-Type {content_type!r} is not an image')
            declared = response.headers.get('Content-Length', '')
            if declared.isdigit() and int(declared) > MAX_IMAGE_BYTES:
                raise ImageRejected(f'Content-Length {declared} exceeds {MAX_IMAGE_BYTES} bytes')

            dest.parent.mkdir(parents=True, exist_ok=True)
            write_atomically(dest, lambda fh: write_capped_body(response, fh))
        return True
    except (requests.RequestException, OSError, ImageRejected) as exc:
        warn(f'Failed to download {description}: {exc}')
        return False


# Copy artwork already on disk, best-effort on the same terms as download_image: a full disk
# must not abort the organize (or, from the scan loop, every job queued behind it).
def copy_image(src: Path, dest: Path, description: str) -> bool:
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        with partial_file(dest) as temp:
            copy_synced(src, temp)
        return True
    except OSError as exc:
        log(f'Failed to copy {description}: {exc}')
        return False


# A remote URL the video's thumbnail can be re-fetched from, from the row or the info dictionary.
def resolve_thumbnail_url(job: Download, info: dict) -> Optional[str]:
    for candidate in (job.thumbnail, info.get('thumbnail')):
        if candidate and str(candidate).startswith(('http://', 'https://')):
            return str(candidate)
    return None


# Re-download the video's thumbnail sidecar when it is missing: yt-dlp may never have written
# one, and every other path here only moves an existing sidecar.
def ensure_video_thumbnail(job: Download, dest_thumb: Path, info: dict) -> None:
    if dest_thumb.exists():
        return

    url = resolve_thumbnail_url(job, info)
    if not url:
        log(f'Job {job.id}: thumbnail missing, and no URL on record to re-fetch it from')
        return

    if download_image(url, dest_thumb, f'thumbnail for job {job.id}'):
        log(f'Job {job.id}: re-downloaded missing thumbnail')


# Write tvshow.nfo and a poster for a playlist's show folder. Both are skipped if already present,
# so a routine scan never overwrites manually edited artwork; force comes from "Fetch Metadata".
def ensure_show_metadata(show_dir: Path, playlist: Playlist, first_video_path: Path, force: bool = False) -> None:
    show_dir.mkdir(parents=True, exist_ok=True)

    nfo_path = show_dir / 'tvshow.nfo'
    if force or not nfo_path.exists():
        # A playlist has no creator of its own, so the channel it came from stands in as both.
        studio = playlist.channel.name if playlist.channel else None
        write_nfo(
            nfo_path,
            'tvshow',
            {
                'title': playlist.title or playlist.youtube_id,
                'plot': playlist.description,
                'studio': studio,
                'director': studio,
                # The show's identity for Jellyfin, which is what survives a retitle of the
                # playlist; a playlist with no YouTube id gets no element rather than a blank one.
                'uniqueid': {
                    '@attrs': {'type': 'youtube', 'default': 'true'},
                    '#text': playlist.youtube_id,
                } if playlist.youtube_id else None,
                'dateadded': format_timestamp(playlist.created_at),
            },
        )
        log(f'Wrote tvshow.nfo for "{playlist.title}"')

    # Playlists carry no artwork of their own, so the poster is the first episode's thumbnail:
    # this module's "-thumb.jpg" when already organized, yt-dlp's plain ".jpg" when fresh.
    thumb = find_sidecar(first_video_path, '-thumb.jpg') or find_sidecar(first_video_path, '.jpg')
    if not thumb:
        return

    # The season needs its own poster too, or Jellyfin puts a placeholder on the "Season 01" tile.
    for path, label in (
        (show_dir / 'poster.jpg', 'poster.jpg'),
        (show_dir / 'Season 01' / 'poster.jpg', 'Season 01 poster.jpg'),
    ):
        if path.exists():
            continue
        if copy_image(thumb, path, f'{label} for "{playlist.title}"'):
            log(f'Wrote {label} for "{playlist.title}" from first episode thumbnail')


# Same as ensure_show_metadata() for a channel's own show folder, except that a channel has an
# avatar URL on record, so the poster comes from that rather than from an episode thumbnail.
def ensure_channel_show_metadata(show_dir: Path, channel: Channel, force: bool = False) -> None:
    show_dir.mkdir(parents=True, exist_ok=True)

    nfo_path = show_dir / 'tvshow.nfo'
    if force or not nfo_path.exists():
        write_nfo(
            nfo_path,
            'tvshow',
            {
                'title': channel.name or channel.youtube_id,
                'plot': channel.description,
                'studio': channel.name,
                # Same show identity as a playlist's tvshow.nfo, taken from the channel's own id.
                'uniqueid': {
                    '@attrs': {'type': 'youtube', 'default': 'true'},
                    '#text': channel.youtube_id,
                } if channel.youtube_id else None,
                'dateadded': format_timestamp(channel.created_at),
            },
        )
        log(f'Wrote tvshow.nfo for channel "{channel.name}"')

    # Season 01 gets its own copy for the same reason as a playlist's show folder above.
    if channel.thumbnail:
        for path, label in (
            (show_dir / 'poster.jpg', 'poster.jpg'),
            (show_dir / 'Season 01' / 'poster.jpg', 'Season 01 poster.jpg'),
        ):
            if path.exists():
                continue
            if download_image(channel.thumbnail, path, f'avatar for channel "{channel.name}"'):
                log(f'Wrote {label} for channel "{channel.name}" from its own avatar')


# =======================================================
#                PLANNING A DESTINATION
# =======================================================
# Every plan_* returns None for "not yet", leaving the job to a later scan rather than failing it.

# Everything needed to file one video: where it goes and what its NFO says. The episode and movie
# flows each produce one, and execute_plan carries either out.
class OrganizePlan(NamedTuple):
    dest_video: Path
    dest_nfo: Path
    dest_thumb: Path
    nfo_root: str
    nfo_fields: dict
    show_dir: Optional[Path] = None
    playlist: Optional[Playlist] = None
    # Set instead of `playlist`, never alongside it, when show_dir is a channel's own show folder:
    # execute_plan writes channel-level rather than playlist-level show metadata for it.
    channel: Optional[Channel] = None


# The show folder a playlist's episodes belong in. It nests under the channel's own folder only
# while a channel subscription exists, not merely when the channel is known -- so subscribing or
# unsubscribing changes the answer for a playlist already on disk, which relocate_misfiled_shows()
# is what acts on. Both callers must agree, or a relocation would run on every scan.
def playlist_show_dir(db, playlist: Playlist) -> Path:
    folder_name = get_or_assign_folder_name(db, playlist)

    channel_subscribed = bool(playlist.channel) and (
        db.query(Subscription)
        .filter(Subscription.subscription_type == 'channel', Subscription.channel_id == playlist.channel_id)
        .first()
        is not None
    )

    if channel_subscribed:
        return SHOWS_DIR / get_or_assign_channel_folder_name(db, playlist.channel) / folder_name
    return SHOWS_DIR / folder_name


# Plan the move for a playlist-linked video: into the show's Season 01 folder, as an episode.
def plan_episode(db, job: Download, info: dict, video_path: Path) -> Optional[OrganizePlan]:
    # Wait rather than guess at an episode number: playlist_index is backfilled by a later scan,
    # and a guess would have to be renamed once it arrives.
    if job.playlist_index is None:
        log(f'Job {job.id}: missing playlist_index, skipping until backfilled')
        return None

    playlist = job.playlist
    if not playlist:
        log(f'Job {job.id}: no playlist relationship, skipping')
        return None

    folder_name = get_or_assign_folder_name(db, playlist)
    show_dir = playlist_show_dir(db, playlist)

    # Episode names follow the playlist's current title, while the folder keeps the name assigned
    # on first use -- so a retitled playlist renames later episodes but never moves the show.
    season_dir = show_dir / 'Season 01'
    show_title = sanitize_filename(playlist.title or playlist.youtube_id or folder_name)

    episode_title = sanitize_filename(job.title or video_path.stem, max_length=90)

    # Standard Episode Format: {Series Title} - S01E{episode:00} - {Episode CleanTitle:90}
    # {tech tag} [{video_id}]
    base_name = f'{show_title} - S01E{job.playlist_index:02d} - {episode_title}'
    quality_tag = build_quality_tag(info)
    if quality_tag:
        base_name += f' {quality_tag}'
    base_name = append_id_tag(base_name, video_id_tag(job))

    # Episode fields on top of the shared ones; <aired> is what orders a season in Jellyfin.
    fields = build_common_video_fields(job, info, episode_title)
    upload_date = fields.pop('_upload_date')
    fields.update({
        'showtitle': playlist.title or show_title,
        'season': 1,
        'episode': job.playlist_index,
        'aired': format_upload_date(upload_date),
        'year': extract_year(upload_date),
    })

    # Video, NFO and thumbnail share one base name, which is what lets find_sidecar() pair them
    # up again when the video is later re-filed or deleted.
    return OrganizePlan(
        dest_video=season_dir / f'{base_name}{video_path.suffix}',
        dest_nfo=season_dir / f'{base_name}.nfo',
        dest_thumb=season_dir / f'{base_name}-thumb.jpg',
        nfo_root='episodedetails',
        nfo_fields=fields,
        show_dir=show_dir,
        playlist=playlist,
    )


# Plan the move for a standalone video: into the Movies library, as its own movie folder.
def plan_movie(db, job: Download, info: dict, video_path: Path) -> Optional[OrganizePlan]:
    title_clean = sanitize_filename(job.title or video_path.stem)

    # Movie Folder Format: {Movie Title}, release year omitted per config.
    movie_dir = MOVIES_DIR / append_id_tag(title_clean, video_id_tag(job))

    # Standard Movie Format: {Movie CleanTitle} - {tech tag} [{video_id}]
    base_name = title_clean
    quality_tag = build_quality_tag(info)
    if quality_tag:
        base_name += f' - {quality_tag}'
    base_name = append_id_tag(base_name, video_id_tag(job))

    # A movie's date is <premiered>, not the <aired> an episode carries.
    fields = build_common_video_fields(job, info, title_clean)
    upload_date = fields.pop('_upload_date')
    fields.update({
        'premiered': format_upload_date(upload_date),
        'year': extract_year(upload_date),
    })

    # No show_dir, so execute_plan() writes no show-level metadata: the video's own folder holds
    # nothing but the video, its NFO and its thumbnail.
    return OrganizePlan(
        dest_video=movie_dir / f'{base_name}{video_path.suffix}',
        dest_nfo=movie_dir / f'{base_name}.nfo',
        dest_thumb=movie_dir / f'{base_name}-thumb.jpg',
        nfo_root='movie',
        nfo_fields=fields,
    )


# Plan the move for a channel-grouped video (Download.group_by_channel): into the channel's own
# show folder as an episode, the same structure plan_episode() uses. A genuine show stops a Mixed
# Content library's scanner reading each flat per-video folder as its own season.
def plan_channel_video(db, job: Download, info: dict, video_path: Path) -> Optional[OrganizePlan]:
    channel = job.channel
    if not channel:
        log(f'Job {job.id}: marked group_by_channel but has no channel relationship, skipping')
        return None

    # Numbering comes from worker.resequence_channel_index: the channel's next free number in
    # upload-date order, never changed once filed. Wait for it rather than guess, as plan_episode()
    # does for a playlist's index.
    if job.playlist_index is None:
        log(f'Job {job.id}: missing channel playlist_index, skipping until resequenced')
        return None

    folder_name = get_or_assign_channel_folder_name(db, channel)
    show_dir = CHANNEL_DIR / folder_name
    season_dir = show_dir / 'Season 01'
    show_title = sanitize_filename(channel.name or channel.youtube_id or folder_name)

    episode_title = sanitize_filename(job.title or video_path.stem, max_length=90)

    # Same format as plan_episode() but 4 episode digits: a channel runs to thousands of uploads,
    # where 2 digits would collide past the 99th video.
    base_name = f'{show_title} - S01E{job.playlist_index:04d} - {episode_title}'
    quality_tag = build_quality_tag(info)
    if quality_tag:
        base_name += f' {quality_tag}'
    base_name = append_id_tag(base_name, video_id_tag(job))

    # The same episode fields a playlist's video gets, with the channel standing in as the show.
    fields = build_common_video_fields(job, info, episode_title)
    upload_date = fields.pop('_upload_date')
    fields.update({
        'showtitle': channel.name or show_title,
        'season': 1,
        'episode': job.playlist_index,
        'aired': format_upload_date(upload_date),
        'year': extract_year(upload_date),
    })

    # channel= rather than playlist= is what sends execute_plan() to the channel-level show
    # metadata, which takes its poster from the channel avatar instead of an episode thumbnail.
    return OrganizePlan(
        dest_video=season_dir / f'{base_name}{video_path.suffix}',
        dest_nfo=season_dir / f'{base_name}.nfo',
        dest_thumb=season_dir / f'{base_name}-thumb.jpg',
        nfo_root='episodedetails',
        nfo_fields=fields,
        show_dir=show_dir,
        channel=channel,
    )


# =======================================================
#              DELETING FILES AND FOLDERS
# =======================================================
# Nothing outside the media and staging roots is removed, and nothing another row still points at.

# Report a yt-dlp file still being written ("video.mp4.part", "video.temp.mp4") or a format stream
# awaiting its merge. Delegates to media.is_ytdlp_intermediate(), the rule is_media_file() uses to
# rule one out as finished media.
def is_partial_download(path: Path) -> bool:
    return is_ytdlp_intermediate(path)


# Delete a file, logging either outcome. Used for the sources left behind once a video is filed.
def discard_file(job: Download, path: Optional[Path], description: str) -> None:
    if not path or not path.exists():
        return
    try:
        path.unlink()
        log(f'Job {job.id}: removed {description} {path}')
    except OSError as exc:
        log(f'Job {job.id}: failed to remove {description} {path}: {exc}')


# True when `path` sits strictly inside the library or the staging directory. Both roots are bind
# mounts holding every other job's files, so a root itself, or anything outside both, is not this
# job's to delete. Everything is resolved first so a symlink or a '..' segment cannot walk out.
def is_inside_a_root(path: Path) -> bool:
    resolved = path.resolve()
    for root in (MEDIA_PATH, DOWNLOAD_PATH):
        root = root.resolve()
        if resolved != root and resolved.is_relative_to(root):
            return True
    return False


# Delete a folder that held nothing but the video just taken out of it, along with the fixed-name
# files Jellyfin leaves in it (tvshow.nfo, poster.jpg, ...) which no stem-based cleanup matches.
# Returns False, deleting nothing, while it holds media, a partial or any subdirectory.
def remove_exclusive_folder(job: Download, folder: Path) -> bool:
    if not is_inside_a_root(folder) or not folder.exists():
        return False

    entries = list(folder.iterdir())
    # A video left in here means the folder is shared, whatever the job row says. A yt-dlp
    # work-in-progress file means a download is still writing into it, and is_media_file()
    # reports that one as not-media.
    if any(is_media_file(path) or is_partial_download(path) for path in entries):
        return False
    # A subdirectory is another season, show or uploader folder, which rmtree would take with it
    # unchecked; cleanup_old_folder_if_exclusive() applies the same rule while walking up.
    if any(path.is_dir() for path in entries):
        return False

    # Whatever is left is metadata or artwork, and goes with the folder -- named in the log first,
    # since this is the one delete that takes files no job ever recorded.
    leftovers = [path.name for path in entries]
    if leftovers:
        log(f'Job {job.id}: folder {folder} has no media left but still has {leftovers}; removing anyway')
    try:
        shutil.rmtree(folder)
        log(f'Job {job.id}: removed folder {folder}')
        return True
    except OSError as exc:
        log(f'Job {job.id}: failed to remove folder {folder}: {exc}')
        return False


# True when a row other than `job` still points at `path`. Two rows share a file whenever the same
# video is requested twice -- standalone and again inside a playlist -- because execute_plan() has
# the second one adopt the file already at the destination instead of storing a copy.
# Queries through `db`, else the session `job` is attached to; with neither it fails closed and
# answers True, so no delete or overwrite proceeds unchecked.
def file_claimed_by_another_row(job: Download, path: Path, db=None) -> bool:
    if db is None:
        db = object_session(job)
    if db is None:
        error(f'Job {job.id}: no database session to check who else uses {path}; treating it as shared')
        return True

    # no_autoflush: this runs mid-deletion or mid-organize, and a flush here would open the write
    # transaction and hold it across every remaining unlink or copy, which is how the two
    # processes end up fighting for the lock.
    with db.no_autoflush:
        others = db.query(Download).filter(Download.filename == str(path), Download.status != 'deleted')
        if job.id is not None:
            others = others.filter(Download.id != job.id)
        return others.first() is not None


# Remove a job's video and every sidecar that might sit beside it, under either naming convention;
# files only, never the database row. A folder exclusive to this one video goes with it.
def remove_downloaded_files(job: Download) -> None:
    if not job.filename:
        return
    video_path = Path(job.filename)

    # A stored path can predate a change to either mount, and the sidecars are found beside it,
    # so the video is the one place this has to be checked.
    if not is_inside_a_root(video_path):
        error(f'Job {job.id}: refusing to delete {video_path}, outside the media and download roots')
        return

    # The row is still marked deleted by the caller; only the file survives, for the row using it.
    if file_claimed_by_another_row(job, video_path):
        log(f'Job {job.id}: keeping {video_path}, another download still points at it')
        return

    # The video is gone, so the sidecars are gone too. The NFO is the only one that matters, and it
    # is written only when the video is filed, so a missing NFO is not an error. The thumbnail is the
    # only other sidecar that matters, and it is re-fetched if missing, so a missing thumbnail is not
    # an error either.
    sidecars = [video_path]
    for suffix in ('.nfo', '-thumb.jpg', '.jpg', '.info.json', '.description'):
        sidecar = find_sidecar(video_path, suffix)
        if sidecar and sidecar not in sidecars:
            sidecars.append(sidecar)

    for path in sidecars:
        discard_file(job, path, 'file')

    # Playlist-linked or channel-grouped means the folder is a season shared with other videos.
    if job.playlist_id or job.group_by_channel:
        return
    remove_exclusive_folder(job, video_path.parent)


# Sidecars yt-dlp writes into staging ahead of the media: metadata, description and the thumbnail
# in whichever image format it fetched or converted to.
STAGING_SIDECAR_SUFFIXES = ('.info.json', '.description', '.jpg', '.jpeg', '.png', '.webp')


# Delete what a stopped download of `job` left in staging: every file under STAGING_TEMP carrying
# its '[<id>]' tag, plus that id's partials, format fragments and sidecars in any staging folder
# holding no finished media for it. Opens its own read-only session. Does nothing for a row
# without a valid id, or while another active row shares the id and may still resume the files.
def remove_staging_leftovers(job: Download) -> None:
    with Session() as db:
        # The row, not `job`: the worker's copy predates the metadata lookup that sets the id.
        row = db.get(Download, job.id)
        youtube_id = row.youtube_id if row else job.youtube_id
        if not youtube_id or not YOUTUBE_ID_PATTERN.match(youtube_id):
            return
        sharer = (
            db.query(Download.id)
            .filter(
                Download.youtube_id == youtube_id,
                Download.id != job.id,
                Download.status.in_(ACTIVE_STATUS_SET),
            )
            .first()
        )
    if sharer:
        log(f'Job {job.id}: keeping staging leftovers for {youtube_id}, job {sharer.id} still needs them')
        return

    pattern = f'*{glob.escape(f"[{youtube_id}]")}*'
    tagged = [path for path in DOWNLOAD_PATH.rglob(pattern) if path.is_file()]
    # A folder holding this id's finished media keeps everything: those sidecars belong to a
    # completed download, not to the one that stopped.
    finished_dirs = {path.parent for path in tagged if is_media_file(path)}
    staging_root = DOWNLOAD_PATH.resolve()

    for path in tagged:
        if not path.is_relative_to(STAGING_TEMP):
            if path.parent in finished_dirs:
                continue
            if not (is_partial_download(path) or path.name.lower().endswith(STAGING_SIDECAR_SUFFIXES)):
                continue
        # A symlink pointing out of staging is not this job's to touch.
        if not path.resolve().is_relative_to(staging_root):
            continue
        discard_file(job, path, 'staging leftover')


# =======================================================
#                  CARRYING OUT A PLAN
# =======================================================

# Record a plan as carried out: point the row at the filed video and stamp organized_at, which is
# what makes a later pass treat the job as already organized. Commits, ending the caller's
# transaction, so it runs once the moves and NFO writes have all succeeded.
def mark_organized(db, job: Download, plan: OrganizePlan) -> None:
    job.filename = str(plan.dest_video)
    # The .info.json sidecar goes once its content is in the NFO and the database, so the row must
    # stop pointing at it. Download.thumbnail keeps yt-dlp's remote URL rather than the local
    # path -- the URL is the only way repair_missing_thumbnails can re-fetch a lost image.
    job.metadata_file = None
    job.organized_at = utcnow()
    db.commit()


# Remove the folder(s) a video just moved out of when they held nothing but that video. Walks
# upwards, since emptying a Season 01 folder orphans the Show folder above it.
def cleanup_old_folder_if_exclusive(job: Download, old_video_path: Path, new_video_path: Path) -> None:
    folder = old_video_path.parent
    new_folder = new_video_path.parent

    while folder != new_folder and folder.exists():
        # A subdirectory means another season or show hangs off this folder, so neither it nor
        # anything above it may go.
        if any(path.is_dir() for path in folder.iterdir()):
            return
        if not remove_exclusive_folder(job, folder):
            return

        folder = folder.parent


# Delete the '.partial' temps an interrupted run left beside a plan's video, thumbnail and NFO.
# Nothing ever reads one, so any still there when a plan is carried out is garbage.
def remove_stale_partials(job: Download, plan: OrganizePlan) -> None:
    for dest in (plan.dest_video, plan.dest_thumb, plan.dest_nfo):
        discard_file(job, partial_path(dest), 'stale partial copy')


# Carry out a plan: move the video and its thumbnail into the library, write the NFO, discard the
# sidecars it supersedes, and record the result on the job. force_nfo rewrites an existing NFO
# instead of only a missing one; set only for a job the user targeted, never for the routine scan,
# which must not clobber a manual Jellyfin metadata edit.
def execute_plan(db, job: Download, plan: OrganizePlan, video_path: Path, info: dict, dry_run: bool, force_nfo: bool = False) -> bool:
    # Both sidecar conventions are checked -- yt-dlp's for a video fresh from staging, this
    # module's for one being re-filed -- so re-organizing leaves nothing at the old location.
    thumb_src = find_sidecar(video_path, '-thumb.jpg') or find_sidecar(video_path, '.jpg')
    desc_src = find_sidecar(video_path, '.description')
    old_nfo_src = find_sidecar(video_path, '.nfo')
    # A metadata_file recorded before the video was renamed no longer resolves, and the sidecar
    # would then be left behind in staging for good.
    info_src = Path(job.metadata_file) if job.metadata_file else None
    if not info_src or not info_src.exists():
        info_src = find_sidecar(video_path, '.info.json')

    if not dry_run:
        remove_stale_partials(job, plan)

    # An existing destination means a previous run already filed this video; clean up whatever
    # is still sitting in the staging directory and record it as organized. A source whose size
    # differs is the exception: it replaces a destination no other row claims (most likely one an
    # interrupted move truncated), and is kept beside one another row does claim.
    if plan.dest_video.exists():
        if dry_run:
            log(f'[dry-run] Job {job.id}: {plan.dest_video} already in place, would mark organized')
            return True

        source_differs = (
            video_path != plan.dest_video
            and video_path.exists()
            and video_path.stat().st_size != plan.dest_video.stat().st_size
        )
        keep_source = False
        if not source_differs:
            log(f'Job {job.id}: destination already exists ({plan.dest_video}), marking organized')
        elif file_claimed_by_another_row(job, plan.dest_video, db):
            # Two rows legitimately share one file, so a claimed copy is never replaced on a size
            # difference alone.
            warn(f'Job {job.id}: {plan.dest_video} belongs to another download and differs in size from '
                 f'{video_path}; keeping both and marking organized')
            keep_source = True
        else:
            warn(f'Job {job.id}: {plan.dest_video} differs in size from {video_path} and no other '
                 f'download uses it; replacing it')
            try:
                safe_move(video_path, plan.dest_video)
            except OSError as exc:
                error(f'Job {job.id}: failed to organize {video_path} -> {plan.dest_video} ({exc})')
                return False

        # Each source is discarded only when it is not the destination itself: re-organizing a
        # video already sitting at its final path would otherwise delete the filed copy.
        if video_path != plan.dest_video and not keep_source:
            discard_file(job, video_path, 'original source file')
        if thumb_src and thumb_src != plan.dest_thumb:
            discard_file(job, thumb_src, 'original thumbnail')
        if old_nfo_src and old_nfo_src != plan.dest_nfo:
            discard_file(job, old_nfo_src, 'original NFO')
        discard_file(job, info_src, 'original sidecar')
        discard_file(job, desc_src, 'original sidecar')
        # An NFO can go missing without the video ever moving -- deleted by hand, or by Jellyfin's
        # own "replace metadata" -- and this is the only pass that would notice. A healthy one is
        # left alone so the routine scan never rewrites a manual edit; force_nfo overrides that.
        if force_nfo or not plan.dest_nfo.exists():
            write_nfo(plan.dest_nfo, plan.nfo_root, plan.nfo_fields)
            log(f'Job {job.id}: (re)wrote destination NFO' + (' (forced)' if force_nfo else ' (was missing)'))
        # Repaired before the show metadata below, so a playlist poster that borrows this episode's
        # thumbnail has one to borrow.
        ensure_video_thumbnail(job, plan.dest_thumb, info)
        # Same gap one level up: the move branch below writes show metadata only for a fresh show
        # folder. Both helpers no-op on anything still present.
        if plan.show_dir and plan.playlist:
            ensure_show_metadata(plan.show_dir, plan.playlist, video_path, force=force_nfo)
        elif plan.show_dir and plan.channel:
            ensure_channel_show_metadata(plan.show_dir, plan.channel, force=force_nfo)
        cleanup_old_folder_if_exclusive(job, video_path, plan.dest_video)
        mark_organized(db, job, plan)
        return True

    if dry_run:
        log(f'[dry-run] Would organize job {job.id}: {video_path} -> {plan.dest_video}')
        return True

    # Make sure the show-level metadata exists before the first episode lands in the folder.
    if plan.show_dir and plan.playlist:
        ensure_show_metadata(plan.show_dir, plan.playlist, video_path)
    elif plan.show_dir and plan.channel:
        ensure_channel_show_metadata(plan.show_dir, plan.channel)

    plan.dest_video.parent.mkdir(parents=True, exist_ok=True)

    try:
        # DOWNLOAD_PATH and MEDIA_PATH are separate mounts, so this is a copy+delete rather than
        # a rename; safe_move picks that up itself, and never deletes the source before the copy
        # is complete under its final name.
        safe_move(video_path, plan.dest_video)
        if thumb_src and thumb_src.exists():
            safe_move(thumb_src, plan.dest_thumb)

        # Written before the sidecars are discarded, so a failed write loses nothing.
        write_nfo(plan.dest_nfo, plan.nfo_root, plan.nfo_fields)
    except (OSError, ValueError) as exc:
        # Named down to the destination, since a full library disk (ENOSPC) lands here too.
        error(f'Job {job.id}: failed to organize {video_path} -> {plan.dest_video} ({exc})')
        return False

    # Fetched now that the video is at its final path, in case there was no thumbnail to move.
    ensure_video_thumbnail(job, plan.dest_thumb, info)

    # The .description/.info.json sidecars and any old NFO are superseded by the NFO just written.
    discard_file(job, info_src, 'sidecar')
    discard_file(job, desc_src, 'sidecar')
    if old_nfo_src and old_nfo_src != plan.dest_nfo:
        discard_file(job, old_nfo_src, 'old NFO')
    cleanup_old_folder_if_exclusive(job, video_path, plan.dest_video)

    mark_organized(db, job, plan)
    log(f'Organized job {job.id} -> {plan.dest_video}')
    return True


# Organize one completed video job. Returns True if the job is now organized, whether by this
# call or by an earlier run. See execute_plan() for what force_nfo does.
def organize_job(db, job: Download, dry_run: bool, force_nfo: bool = False) -> bool:
    video_path = Path(job.filename)

    # Read the metadata before anything is moved or deleted out from under it.
    info = load_job_metadata(job, video_path)

    # playlist_id takes priority; group_by_channel marks an intentional channel-wide download,
    # unlike channel_id, which nearly every video carries. The channel-missing fallback to a movie
    # stays separate from plan_channel_video returning None, which means "wait for resequencing".
    if job.playlist_id:
        plan = plan_episode(db, job, info, video_path)
    elif job.group_by_channel:
        if job.channel:
            plan = plan_channel_video(db, job, info, video_path)
        else:
            plan = plan_movie(db, job, info, video_path)
    else:
        plan = plan_movie(db, job, info, video_path)

    # No plan means "not yet", not failure: organized_at stays unset and the next scan retries.
    if not plan:
        return False

    return execute_plan(db, job, plan, video_path, info, dry_run, force_nfo=force_nfo)


# =======================================================
#            ENTRY POINTS AND REPAIR PASSES
# =======================================================
# What the worker calls: one job on demand, or the periodic sweep and the repairs that go with it.

# Re-run organizing for one job, rewriting its NFO even though the video is already in place.
# Backs "Fetch Metadata": the user asked for this one job, so the scan's caution about healthy
# NFOs doesn't apply. Returns False, touching nothing, while library_ready() refuses.
def organize_single_job(job_id: int) -> bool:
    if not library_ready():
        return False
    SHOWS_DIR.mkdir(parents=True, exist_ok=True)
    MOVIES_DIR.mkdir(parents=True, exist_ok=True)
    with Session() as db:
        job = db.get(Download, job_id)
        if not job or not is_organizable(db, job):
            return False
        result = organize_job(db, job, dry_run=False, force_nfo=True)
    if result:
        notify_jellyfin()
    return result


# Ask Jellyfin to scan for library changes rather than wait on its schedule or its filesystem
# watcher (unreliable across bind mounts). An unreachable Jellyfin is not an organizing failure
# and logs WARN; a rejected token (401/403) fails every refresh until fixed, so it logs ERROR.
def notify_jellyfin() -> None:
    if not JELLYFIN_URL or not JELLYFIN_TOKEN:
        return
    try:
        response = requests.post(
            f"{JELLYFIN_URL.rstrip('/')}/Library/Refresh",
            headers={'X-Emby-Token': JELLYFIN_TOKEN},
            # Jellyfin serves a self-signed cert from jellyfin-cert and this call never leaves the
            # internal docker network, the same trust boundary Seerr uses against it.
            verify=False,
            timeout=10,
        )
        # A rejected token answers 401 rather than failing the request, and would otherwise be
        # logged as a successful refresh.
        response.raise_for_status()
        log('Requested Jellyfin library refresh')
    except requests.RequestException as exc:
        status = exc.response.status_code if exc.response is not None else None
        if status in (401, 403):
            error(f'Jellyfin rejected the library refresh token (HTTP {status}): {exc}')
        else:
            warn(f'Failed to notify Jellyfin of library changes: {exc}')


# Shared pre-checks applied before a job is organized.
def is_organizable(db, job: Download, dry_run: bool = False) -> bool:
    if not job.filename:
        log(f'Job {job.id}: no filename recorded, skipping')
        return False
    if Path(job.filename).exists():
        return True

    # The recorded path is stale, so relocate by youtube_id rather than by name: ids are plain
    # ASCII, immune to a title mangled into mojibake, and survive a rename outside this app.
    recovered = find_existing_media(job, search_paths=[MEDIA_PATH])
    if recovered:
        log(f'Job {job.id}: recorded path was stale ({job.filename}); recovered by youtube_id at {recovered}')
        job.filename = str(recovered)
        if not dry_run:
            db.commit()
        return True

    log(f'Job {job.id}: recorded file missing on disk ({job.filename}), skipping')
    return False


# Re-point already-organized jobs whose recorded path no longer resolves but whose file is still
# in the library under another name -- typically Tdarr transcoding .mkv to .mp4 in place, which
# the main scan never revisits (it selects organized_at IS NULL).
def repair_stale_filenames(db, dry_run: bool = False) -> int:
    jobs = (
        db.query(Download)
        .filter(
            Download.status == 'completed',
            Download.request_type == 'video',
            Download.organized_at.isnot(None),
            Download.filename.isnot(None),
        )
        .all()
    )

    # One commit at the end of the pass rather than one per job: SQLite takes a single writer, and
    # the worker's own transaction is waiting behind this scan.
    repaired = 0
    for job in jobs:
        if Path(job.filename).exists():
            continue

        recovered = find_existing_media(job, search_paths=[MEDIA_PATH])
        if not recovered:
            # Genuinely gone rather than renamed; leave it alone so the loss stays visible.
            continue

        if dry_run:
            log(f'[dry-run] Would repoint job {job.id} to {recovered}')
        else:
            log(f'Job {job.id}: recorded path was stale, repointed to {recovered}')
            job.filename = str(recovered)
        repaired += 1

    # A dry run reaches here with the same count and simply never commits.
    if repaired:
        if not dry_run:
            db.commit()
        log(f'Repointed {repaired} stale file path(s)')
    return repaired


# Send already-filed episodes back through the organizer when their show belongs somewhere else
# now. Subscribing to a channel moves its playlists under the channel's folder and unsubscribing
# moves them back out, but the main loop only looks at organized_at IS NULL, so episodes filed
# before the change would stay behind and split one show across two folders.
def relocate_misfiled_shows(db, dry_run: bool = False) -> int:
    jobs = (
        db.query(Download)
        .filter(
            Download.status == 'completed',
            Download.request_type == 'video',
            Download.organized_at.isnot(None),
            # playlist_id is exactly what routes a job to plan_episode, so it is also exactly the
            # set of jobs whose location this rule governs.
            Download.playlist_id.isnot(None),
            Download.filename.isnot(None),
        )
        .all()
    )

    relocated = 0
    for job in jobs:
        if not job.playlist:
            continue

        current = Path(job.filename)
        # Episodes are filed at <show>/Season 01/<file>, so the show folder is the grandparent.
        # A file somewhere else entirely is left alone: repair_stale_filenames owns that case.
        if current.parent.name != 'Season 01':
            continue

        expected = playlist_show_dir(db, job.playlist)
        if current.parent.parent == expected:
            continue

        if dry_run:
            log(f'[dry-run] Would refile job {job.id} from {current.parent.parent} to {expected}')
        else:
            # Only the flag is cleared here. The scan's own loop re-plans the job, and execute_plan
            # moves the file, rewrites the NFO and removes the folder it came from once it empties.
            log(f'Job {job.id}: show moved from {current.parent.parent} to {expected}, refiling')
            job.organized_at = None
        relocated += 1

    if relocated:
        if not dry_run:
            db.commit()
        log(f'Queued {relocated} episode(s) to be refiled under a different show folder')
    return relocated


# Re-fetch the thumbnail for any already-organized video whose sidecar has gone missing, leaving
# Jellyfin showing a placeholder. organized_at stays set: only the artwork is missing, and
# clearing it would send the whole job back through the organizer.
def repair_missing_thumbnails(db, dry_run: bool = False) -> int:
    jobs = (
        db.query(Download)
        .filter(
            Download.status == 'completed',
            Download.request_type == 'video',
            Download.organized_at.isnot(None),
            Download.filename.isnot(None),
        )
        .all()
    )

    # The count is what tells scan_and_process to notify Jellyfin: fresh artwork on an already-filed
    # video is invisible until the library is rescanned.
    repaired = 0
    for job in jobs:
        video_path = Path(job.filename)
        if not video_path.exists():
            # A path that no longer resolves is the main scan's problem, not this one's.
            continue

        dest_thumb = video_path.with_name(video_path.stem + '-thumb.jpg')
        if dest_thumb.exists():
            continue

        info = load_job_metadata(job, video_path)
        url = resolve_thumbnail_url(job, info)
        if not url:
            continue

        # A dry run counts the fetch it skips, so its summary reports what a real pass would repair.
        if dry_run:
            log(f'[dry-run] Would re-download missing thumbnail for job {job.id}')
            repaired += 1
            continue

        if download_image(url, dest_thumb, f'thumbnail for job {job.id}'):
            repaired += 1

    if repaired:
        log(f'Repaired {repaired} missing thumbnail(s)')
    return repaired


# File every completed video job that isn't organized yet. Called periodically by the worker, and
# once after each parent job finishes. Returns 0, touching nothing, while library_ready() refuses:
# the jobs stay completed and unorganized for the first pass after the library is back.
def scan_and_process(dry_run: bool = False) -> int:
    if not library_ready():
        return 0
    SHOWS_DIR.mkdir(parents=True, exist_ok=True)
    MOVIES_DIR.mkdir(parents=True, exist_ok=True)
    organized = 0

    with Session() as db:
        # Before the query below, not after: this clears organized_at on episodes whose show has
        # moved, and they have to be in the pending set this run rather than waiting for the next.
        relocated = relocate_misfiled_shows(db, dry_run)

        # organized_at IS NULL is the whole definition of pending, which is what makes a re-run
        # cheap: an already-filed video is never looked at again by this loop.
        jobs = (
            db.query(Download)
            .filter(
                Download.status == 'completed',
                Download.request_type == 'video',
                Download.organized_at.is_(None),
            )
            .all()
        )

        # Repair already-filed videos, which the loop below never revisits. Stale paths go first,
        # so the thumbnail pass looks beside the file's real location.
        repaired = repair_stale_filenames(db, dry_run)
        repaired += repair_missing_thumbnails(db, dry_run)
        # A dry run reports the relocations it would make, but leaves organized_at set, so they
        # never reach the loop below and have to be counted here to be visible at all.
        repaired += relocated

        # Nothing left to organize, but new artwork still needs picking up by Jellyfin.
        if not jobs:
            if repaired and not dry_run:
                notify_jellyfin()
            return 0

        log(f'Found {len(jobs)} completed video(s) pending organization')
        for job in jobs:
            if not is_organizable(db, job, dry_run):
                continue

            # Hold child videos back until the parent is settled, so a playlist is filed as a unit
            # -- settled, not successful, or one failed video would strand all the others.
            if job.parent_id:
                parent = job.parent
                if not parent or parent.status in ACTIVE_STATUS_SET:
                    log(f'Job {job.id}: parent {job.parent_id} still in progress, skipping')
                    continue

            # One unfilable job must not strand the jobs behind it; the next scan retries it.
            # Logged with its traceback, which applog indents into the same log entry.
            try:
                if organize_job(db, job, dry_run):
                    organized += 1
            except Exception as exc:
                logging.exception('[organizer] Job %s: unexpected failure while organizing (%s)', job.id, exc)
                db.rollback()

    # Outside the session: the refresh is an HTTP call to Jellyfin, and holding the SQLite session
    # open across it would keep the worker waiting on the single writer.
    if organized:
        log(f'Organized {organized} video(s)')
    if (organized or repaired) and not dry_run:
        notify_jellyfin()
    return organized


# Allow running the organizer standalone for testing, e.g. with
# `python -m youtube_manager.organizer --dry-run`.
if __name__ == '__main__':
    import sys

    from youtube_manager import applog

    applog.configure()

    count = scan_and_process(dry_run='--dry-run' in sys.argv)
    log(f'Done. {count} video(s) organized.')
