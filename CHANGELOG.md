# Changelog

All notable changes to this project are listed here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/). There are no releases yet.

## [Unreleased]

The first code commit imports the web UI, the download worker and the organizer, with phases 1 to
4 of the hardening plan applied. The entries compare it with the code that ran before.

### Added

- A pytest suite with a temp SQLite database per test and a fake yt-dlp, and CI that runs it on
  Python 3.13 (`.github/workflows/ci.yml`). The README's Running tests section runs it locally
  through uv. (phase 1)
- `YTM_ALLOWED_HOSTS`, a comma-separated list of extra host names the web UI answers to.
  (phase 2)
- yt-dlp keeps unfinished downloads in `/downloads/.incomplete`, and the worker records the
  path yt-dlp reports for the finished file. (phase 4)
- Git hooks that refuse commits on and pushes to `main`, `CONTRIBUTING.md`, a pull request
  template and this changelog.
- `LIBRARY_SENTINEL`, an opt-in file on the library disk. While it is missing the organizer files
  nothing and creates nothing under `/media`, logs ERROR at most once an hour, and leaves the
  jobs to be filed once the disk is back. (phase 5)

### Changed

- Importing `worker` no longer reconfigures stdout or creates `DOWNLOAD_PATH`; that happens when
  it runs. (phase 1)
- Failures log at WARN or ERROR instead of INFO. ERROR is kept for what needs a human: a failed
  move or NFO write, a refused delete, Jellyfin refusing the API key, a full disk, and a
  subscription that failed three scans in a row. (phase 3)
- yt-dlp's warnings reach the log, and a failed job keeps yt-dlp's last `ERROR:` line as its
  error. (phase 3)
- A job's file is found only by its recorded name or its `[<id>]` tag, never by part of its
  title. Single-stream `.fNNN` files and anything under `/downloads/.incomplete` are never taken
  for a finished download. (phase 4)
- An `.info.json` whose video id differs from the job's is not applied. (phase 4)
- Cancelling a download removes its leftovers from staging, and deleting a job never removes a
  library folder that contains a subfolder. (phase 4)
- Filing into the library copies to a hidden `.<name>.partial`, syncs it to disk and renames it
  into place before the staging file is removed, so a crash never leaves a truncated file under
  its final name or loses the only copy. NFOs and artwork are written the same way. (phase 5)
- When the destination already exists with a different size, the organizer replaces it if no
  other job uses it, and otherwise keeps both files and logs a WARN naming them. (phase 5)
- The check whether another job uses a file refuses, rather than allows, a delete or overwrite
  when it has no database session. (phase 5)

### Security

- The web UI answers only to the host names it knows, refuses state-changing requests from other
  origins, and sends a Content-Security-Policy and other hardening headers. Values put into
  page scripts are JSON-encoded. (phase 2)
- Artwork is fetched only over HTTPS from `*.ytimg.com`, `*.ggpht.com` and
  `*.googleusercontent.com`, without following redirects, capped at 10 MB and only when the
  response is an image. (phase 5)
- The UI's own nginx name is no longer built in. Set `YTM_ALLOWED_HOSTS` to it on the web
  service before deploying, or every request through nginx gets 400.

[Unreleased]: https://github.com/Ochsyp/youtube-manager/commits/main
