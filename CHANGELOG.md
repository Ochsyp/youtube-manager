# Changelog

All notable changes to this project are listed here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and releases follow
[Semantic Versioning](https://semver.org/spec/v2.0.0.html) as CONTRIBUTING.md's Releasing section
describes.

## [Unreleased]

## [1.0.0] - 2026-11-03

The first release: the web UI, the download worker and the organizer, imported with phases 1 to 4
of the maintainer's hardening plan applied, then phases 5 and 12. Each entry's phase tag names the
step it came from, and the entries compare 1.0.0 with the code that ran before this repository.

Hosts switch to the published image, `ghcr.io/ochsyp/youtube-manager:<version>`, and start the
worker with the image's `worker` command instead of `python worker.py`. The image carries the
code, so a host mounts only `/app/data`, `/downloads` and `/media`. Run the bgutil provider at the
release of the image's plugin, `brainicism/bgutil-ytdlp-pot-provider:2.0.2`, not `latest`: the
plugin refuses a provider of another major release and warns about any other difference.
`compose.example.yaml` shows the result. (phase 12)

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
- `compose.example.yaml`, an example deployment of the web UI, the worker and the bgutil provider.
- Release images on ghcr.io: `ghcr.io/ochsyp/youtube-manager:X.Y.Z` for amd64 and arm64, with
  provenance and SBOM attestations, and `latest`, which every release that is not a prerelease
  moves. (phase 12)
- The image's `web` and `worker` commands. `web`, the default, starts the web UI and `worker` the
  download worker; extra arguments pass through, and any other command runs as given. (phase 12)
- A release workflow, `.github/workflows/release.yml`. Publishing a GitHub release whose tag is
  `vX.Y.Z` on main, with a dated CHANGELOG heading, builds and smoke-tests the image, pushes it
  and adds its digest to the release notes. CONTRIBUTING.md's Releasing section gives the steps
  and the versioning rules, and the pull request template asks for the release impact.
  (phase 12)
- CI builds the image for amd64 and arm64 on pull requests and on main and smoke-tests each build
  with `scripts/smoke-test-image.sh`: the imports, yt-dlp, deno and ffmpeg, the web UI's
  `/health`, the worker's `Components:` line with nothing missing, and the worker's start and
  stop under tini. Nothing is pushed. (phase 12)
- At start, the worker logs the release, the versions of yt-dlp, yt-dlp-ejs and the bgutil plugin,
  and deno's path; the web UI logs the release. (phase 12)

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
- The git history was rewritten on 2026-10-31 and the GitHub repository was recreated. Two
  playlist IDs in the tests and a deploy path in an early README were replaced, and commit dates
  are recorded in UTC. Every commit ID changed: re-clone, or reset an existing clone with
  `git fetch` and `git reset --hard origin/main`. Do not pull or merge the old history.
- The README and SECURITY.md describe deployment on any Docker host behind any reverse proxy
  instead of one particular stack. Code comments no longer assume that stack's host, probes or
  log pipeline.
- SECURITY.md names GitHub's private vulnerability reporting as the way to report a problem.
- The modules move into the `youtube_manager` package: `uvicorn youtube_manager.app:app`,
  `python -m youtube_manager.worker` and `python -m youtube_manager.organizer --dry-run` replace
  `uvicorn app:app`, `python worker.py` and `python organizer.py --dry-run`. The database stays in
  `data/` at the repository root, `/app/data` in the image. (phase 12)
- `requirements.txt` pins every runtime dependency exactly. uv compiles it from the direct
  dependencies in `requirements.in`, and the image installs only those pins, as wheels. `pip check`
  and the tests verify that the pins fit together. Dependabot proposes updates to every pin daily,
  each held back seven days, except yt-dlp, its yt-dlp-ejs solver and the bgutil plugin, which
  skip the wait. A new major of the plugin moves by hand, together with the provider. (phase 12)
- The base image, `python:3.13-slim-trixie`, and deno, from the `denoland/deno` image, are pinned
  by digest. (phase 12)
- yt-dlp solves YouTube's JavaScript challenges with the yt-dlp-ejs solver installed in the image
  instead of downloading one from GitHub at run time, so the worker needs no egress to GitHub.
  Pulling the image needs `ghcr.io` and `pkg-containers.githubusercontent.com`. (phase 12)
- Every yt-dlp call passes `--ignore-config`, so no yt-dlp config file in the image, the
  staging directory or a home directory can change what it does. (phase 12)
- tini runs as PID 1 in every container and reaps orphaned yt-dlp and ffmpeg processes, so a host
  no longer needs `init: true`. (phase 12)
- The image holds only the package, `entrypoint.sh`, `requirements.txt` and `LICENSE` instead of
  the whole checkout, and yt-dlp and deno cache under `/tmp/.cache` instead of `/app/.cache`.
  (phase 12)

### Removed

- aiofiles, an unused dependency. (phase 12)
- The deno install script: deno comes from the pinned `denoland/deno` image instead of
  `curl -fsSL https://deno.land/install.sh | sh`, and unzip is no longer installed. (phase 12)

### Security

- The web UI answers only to the host names it knows, refuses state-changing requests from other
  origins, and sends a Content-Security-Policy and other hardening headers. Values put into
  page scripts are JSON-encoded. (phase 2)
- Artwork is fetched only over HTTPS from `*.ytimg.com`, `*.ggpht.com` and
  `*.googleusercontent.com`, without following redirects, capped at 10 MB and only when the
  response is an image. (phase 5)
- No reverse-proxy name is built in. Before deploying, set `YTM_ALLOWED_HOSTS` on the web service
  to the name the proxy serves the UI under, or every request through the proxy gets 400.

[Unreleased]: https://github.com/Ochsyp/youtube-manager/compare/v1.0.0...HEAD
[1.0.0]: https://github.com/Ochsyp/youtube-manager/releases/tag/v1.0.0
