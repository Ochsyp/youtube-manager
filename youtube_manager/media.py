# -------------------------------------------------------
# Media paths and file lookup
#
# Shared filesystem locations and media-file lookup for the YouTube Manager. yt-dlp writes into
# the staging directory (DOWNLOAD_PATH); the organizer moves finished videos into the library
# (MEDIA_PATH). Both the web app and the worker look up a job's file through here.
# -------------------------------------------------------

import glob
import os
import re
from pathlib import Path
from typing import Iterable, Optional
from urllib.parse import parse_qs, urlparse

# Staging directory yt-dlp downloads into, and the Jellyfin library the organizer moves files to.
DOWNLOAD_PATH = Path(os.getenv('DOWNLOAD_PATH', '/downloads'))
MEDIA_PATH = Path(os.getenv('MEDIA_PATH', '/media'))

# yt-dlp's temp path (-P temp:), where its .part files and per-format streams live until the merged
# file moves into staging. Inside DOWNLOAD_PATH so the move is a rename on the same mount; nothing
# under it is ever finished media.
STAGING_TEMP = DOWNLOAD_PATH / '.incomplete'

# Lookup order for a job's existing file: staging first, then the library, so a retry never
# re-downloads something already sitting on disk.
SEARCH_PATHS = (DOWNLOAD_PATH, MEDIA_PATH)

# Extensions treated as playable media. Sidecar files (.info.json, .description, .jpg) are
# deliberately excluded: a job must never be marked "completed" because its metadata file exists.
# A container missing here makes a finished download look unrecognisable, which fails the job and
# sends it into permanent retry, so the list errs towards including anything playable.
MEDIA_EXTENSIONS = frozenset({
    '.mp4', '.mkv', '.webm', '.mp3', '.m4a', '.aac', '.opus', '.flac',
    '.wav', '.avi', '.mov', '.ts', '.m2ts', '.mpg', '.mpeg', '.ogv',
    '.ogg', '.3gp', '.3g2', '.asf', '.wmv', '.f4v', '.flv', '.mp2',
    '.m4v', '.mka', '.mts', '.m4b', '.weba', '.oga', '.vob', '.divx',
})

# A YouTube video ID is exactly 11 characters from this alphabet.
YOUTUBE_ID_PATTERN = re.compile(r'^[A-Za-z0-9_-]{11}$')

# Markers yt-dlp puts on work-in-progress files, either trailing ("video.mp4.part") or in front
# of the real extension while it merges streams ("video.temp.mp4"). Neither is finished media.
PARTIAL_SUFFIXES = frozenset({'.part', '.ytdl', '.temp'})

# A per-format stream yt-dlp downloads before merging: "Title [<id>].f137.mp4" or
# "Title [<id>].f251-drc.webm". Anchored on the id tag, so a dotted title is never mistaken for one.
FORMAT_FRAGMENT_PATTERN = re.compile(r'\[[A-Za-z0-9_-]{11}\]\.f\d+(?:-[A-Za-z0-9]+)?\.[A-Za-z0-9]+$')


# =======================================================
#                 YOUTUBE ID EXTRACTION
# =======================================================
# Reading the video id out of a URL. Every path rejects rather than guesses: a wrong id would be
# written into the database and from there into organized filenames.

# Strip the "yt:" prefix some yt-dlp extractors emit so stored IDs are always the bare ID.
def normalize_youtube_id(raw_id: Optional[str]) -> Optional[str]:
    if not raw_id:
        return None
    if raw_id.startswith('yt:'):
        return raw_id.split(':')[-1]
    return raw_id


# Match a host exactly or as a subdomain of it; a substring test also accepts
# "youtube.com.example.net".
def _host_matches(host: str, domain: str) -> bool:
    return host == domain or host.endswith(f'.{domain}')


# Pull the video ID out of a YouTube URL, covering watch, youtu.be, shorts, embed and live
# forms. Returns None when the URL does not identify a single video (playlists, channels).
def extract_youtube_id(url: Optional[str]) -> Optional[str]:
    if not url:
        return None

    url = url.strip()

    # Accept bare hostnames the same way the UI does, so "youtu.be/x" parses like a full URL.
    if not url.startswith(('http://', 'https://')):
        url = f'https://{url}'

    parsed = urlparse(url)
    # hostname, not netloc: netloc feeds a "user@" prefix and the port into the host match, so
    # "https://youtu.be@example.net/<id>" would pass as a short link.
    host = parsed.hostname or ''
    path = parsed.path.rstrip('/')

    candidate = None

    # youtu.be short links carry the ID as the whole path.
    if _host_matches(host, 'youtu.be'):
        candidate = path.lstrip('/')
    # youtube-nocookie.com serves privacy-enhanced embeds in the same URL shapes.
    elif any(_host_matches(host, domain) for domain in ('youtube.com', 'youtube-nocookie.com')):
        query = parse_qs(parsed.query)
        if 'v' in query:
            candidate = query['v'][0]
        else:
            # Shorts, embeds and live streams carry the ID as the final path segment.
            for prefix in ('/shorts/', '/embed/', '/live/', '/v/'):
                if path.startswith(prefix):
                    candidate = path[len(prefix):].split('/')[0]
                    break

    candidate = normalize_youtube_id(candidate)

    # Only accept something that actually looks like a video ID, so a malformed URL does not
    # write nonsense into the database (and from there into organized filenames).
    if candidate and YOUTUBE_ID_PATTERN.match(candidate):
        return candidate
    return None


# =======================================================
#                   MEDIA FILE LOOKUP
# =======================================================
# Finding the file a job produced, wherever it has since been moved to. Only a finished, playable
# file counts -- a partial or a sidecar matching here would mark the job completed. A file is
# identified by its recorded name or its exact '[<id>]' tag, never by title.

# Report whether a name is a per-format stream awaiting its merge. Lexical only: the file need not
# exist, and a finished stream is still a fragment.
def is_format_fragment(path: Path) -> bool:
    return bool(FORMAT_FRAGMENT_PATTERN.search(path.name))


# Report whether a name is yt-dlp work in progress: a partial ("video.mp4.part", "video.temp.mp4")
# or a format fragment. Lexical only; the one rule is_media_file() and the organizer both apply.
def is_ytdlp_intermediate(path: Path) -> bool:
    # Only the last two suffixes, so a dotted title ("Talk.part.2.mp4") is not read as partial.
    if any(suffix.lower() in PARTIAL_SUFFIXES for suffix in path.suffixes[-2:]):
        return True
    return is_format_fragment(path)


# Report whether a path points at an existing, finished media file. Anything under STAGING_TEMP
# is refused by location alone, so a merged file yt-dlp has not moved yet never counts.
def is_media_file(path: Optional[Path]) -> bool:
    if not path or not path.is_file():
        return False
    if path.is_relative_to(STAGING_TEMP) or is_ytdlp_intermediate(path):
        return False
    return path.suffix.lower() in MEDIA_EXTENSIONS


# Resolve a stored filename, which may be relative, against `base` -- staging unless the caller
# is searching a different root.
def resolve_stored_path(filename: str, base: Path = DOWNLOAD_PATH) -> Path:
    path = Path(filename)
    return path if path.is_absolute() else base / path


# Lexical containment test -- no stat call, so it cannot race with the file being moved.
def _is_within_roots(path: Path, roots: Iterable[Path]) -> bool:
    return any(path.is_relative_to(root) for root in roots)


# Find the first media file under any of the given roots whose name contains `fragment`.
def _search_roots(roots: Iterable[Path], fragment: str) -> Optional[Path]:
    # An empty fragment globs to "**", which matches the first media file anywhere under a root.
    if not fragment:
        return None

    # glob metacharacters in titles (e.g. "[Official Video]") would otherwise silently fail to match.
    escaped = glob.escape(fragment)
    # rglob reads a "/" in a title as a path separator and then matches nothing; yt-dlp writes a
    # lookalike character in the filename, so match any single character in its place.
    pattern = f'*{escaped}*'.replace('/', '?').replace('\\', '?')

    for root in roots:
        for candidate in root.rglob(pattern):
            if is_media_file(candidate):
                return candidate
    return None


# The job's recorded file where the row says it is, if that is finished media inside one of
# search_paths (staging and the library by default), else None. One stat per root and no tree
# walk, so a file that has since moved is not found; find_existing_media() looks for that one.
def find_recorded_media(job, search_paths: Optional[Iterable[Path]] = None) -> Optional[Path]:
    if not job.filename:
        return None
    roots = list(search_paths) if search_paths is not None else list(SEARCH_PATHS)

    # The recorded filename is the strongest signal, but only inside the requested roots: a
    # relative name resolved against staging, or an absolute one left by an older
    # DOWNLOAD_PATH, otherwise escapes the scope the caller asked for.
    for base in roots:
        recorded = resolve_stored_path(job.filename, base)
        if _is_within_roots(recorded, roots) and is_media_file(recorded):
            return recorded
    return None


# Locate a job's media file: by recorded filename, then by its '[<id>]' tag; None when neither
# matches, however close a title is. search_paths scopes the search -- the worker passes staging
# only, so an already-organized file doesn't count as "already downloaded"; the app's retry/sweep
# flows include the library.
def find_existing_media(job, search_paths: Optional[Iterable[Path]] = None) -> Optional[Path]:
    roots = list(search_paths) if search_paths is not None else list(SEARCH_PATHS)

    recorded = find_recorded_media(job, roots)
    if recorded:
        return recorded

    if job.filename:
        # The file may have been moved (e.g. by the organizer); look for it by name under each root.
        found = _search_roots(roots, Path(job.filename).name)
        if found:
            return found

    # The YouTube ID is embedded in every filename this application produces, so it is the
    # most reliable fallback once a file has been renamed or organized. Only the bracketed tag
    # counts: a bare id can occur inside another video's title, and a malformed one globs wide.
    if job.youtube_id and YOUTUBE_ID_PATTERN.match(job.youtube_id):
        return _search_roots(roots, f'[{job.youtube_id}]')

    return None
