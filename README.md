# youtube-manager

Self-hosted YouTube downloader that files everything into a Jellyfin library. Queue
videos, playlists and channels from a web UI, subscribe to channels, and get properly
named movies and shows with NFO metadata and artwork.

## Features

- Web UI to queue single videos, playlists and whole channels, with live progress and
  per-job logs
- Channel and playlist subscriptions, checked every 30 minutes, with optional backfill
  of existing uploads
- Downloads through yt-dlp, with PO tokens from a bgutil provider and JavaScript
  challenges solved by deno
- Files finished downloads as movies or Sonarr/Radarr-style shows with Kodi/Jellyfin
  NFOs, posters and thumbnails, then asks Jellyfin to rescan
- Automatic retry with exponential backoff, restart recovery, and cancel and stall
  handling

## Architecture

One image, two containers (the image's `web` and `worker` commands), one SQLite database
in WAL mode.

| Component | Role |
|---|---|
| `youtube_manager/app.py` (FastAPI + Jinja2, uvicorn on `:8080`) | Web UI and the polling endpoints it uses |
| `youtube_manager/worker.py` | Job loop that drives yt-dlp, subscriptions and the organizer |
| `youtube_manager/organizer.py` | Moves finished files into the library layout and writes metadata |
| `bgutil-provider` sidecar | Mints the PO tokens YouTube requires |

## Requirements

- Docker with Compose on Linux, on amd64 or arm64: the release image is built for both.
  The worker relies on POSIX process groups.
- Recommended: a `brainicism/bgutil-ytdlp-pot-provider` container the worker can reach,
  on the release of the image's bgutil plugin, which the worker's `Components:` line
  names. The plugin refuses a provider of another major release and warns about any
  other difference. Without PO tokens YouTube withholds the DASH formats and yt-dlp
  falls back to low-bitrate HLS.
- Egress to YouTube and its CDNs only. yt-dlp's JavaScript challenge solver (yt-dlp-ejs)
  and deno, which runs it, ship in the image, so nothing is fetched from GitHub at run
  time. Pulling the image needs `ghcr.io` and `pkg-containers.githubusercontent.com`.
- Optional: Jellyfin for the library refresh, and an HTTP(S) proxy for egress

## Configuration

| Variable | Default | Purpose |
|---|---|---|
| `DOWNLOAD_PATH` | `/downloads` | Staging directory yt-dlp writes into |
| `MEDIA_PATH` | `/media` | Library root the organizer files into |
| `LIBRARY_SENTINEL` | unset | Worker only: a file on the library disk, such as `/media/.library-mounted`, that must exist before anything is filed. While it is missing the organizer files nothing and logs ERROR at most once an hour. Create the file before setting the variable |
| `YOUTUBE_DB_URI` | `sqlite:////app/data/youtube.db` | SQLAlchemy database URL; by default `data/youtube.db` at the repository root, which is `/app` in the image |
| `POT_PROVIDER_URL` | unset | bgutil provider base URL passed to yt-dlp; when empty, no PO-token arguments are passed |
| `JELLYFIN_URL`, `JELLYFIN_TOKEN` | unset | Jellyfin base URL and API key (secret); the library refresh runs only when both are set |
| `WORKER_HEARTBEAT_PATH` | `/tmp/worker-heartbeat` | Liveness file for the worker healthcheck |
| `YTM_ALLOWED_HOSTS` | unset | Web service: comma-separated host names or IPs the web UI answers to in addition to `youtube-manager`, `localhost` and `127.0.0.1`. Set it to the name the reverse proxy serves the UI under, such as `ytdl.home.arpa`; any other `Host` gets 400 |
| `PUID`, `PGID` | `1000` | User and group the containers run as |
| `TZ` | | Timezone of the log timestamps |
| `HTTP_PROXY`, `HTTPS_PROXY`, `NO_PROXY` | | Egress proxy; `NO_PROXY` must cover Jellyfin and the bgutil provider |

Downloads also pass `HTTP_PROXY` to yt-dlp as `--proxy`, which ignores `NO_PROXY`, so
the proxy itself must let the worker reach the bgutil provider.

Volumes: `/app/data` (SQLite), `/downloads` (staging) and `/media` (library).

## Security notes

- The web UI has **no authentication**: anyone who can reach it can use it. Keep it on a
  trusted network behind a reverse proxy (see Deploying), and add a login in the proxy
  if you need one.
- Web pages a user opens elsewhere cannot drive it through that user's browser:
  - **Host pinning.** The app answers only to `youtube-manager` (its Compose service
    name), `localhost` and `127.0.0.1` (healthchecks), plus the entries of
    `YTM_ALLOWED_HOSTS`, such as the name the proxy serves the UI under, whatever the
    port. Any other `Host` gets 400, which defeats DNS rebinding.
  - **Cross-origin guard.** Every request other than GET, HEAD and OPTIONS must come
    from the UI's own pages. A browser that sends `Sec-Fetch-Site` must send
    `same-origin` or `none`; otherwise, as over plain HTTP, the `Origin` header's host
    and port must equal `Host`. Anything else, including a missing or `null` Origin,
    gets 403 and one WARN line in the app log.
  - **Headers.** Every response carries a Content-Security-Policy limited to the app's
    own origin (inline scripts and styles allowed), `X-Frame-Options: DENY`,
    `X-Content-Type-Options: nosniff`, and same-origin Referrer and
    Cross-Origin-Resource policies.
- A manual POST must therefore send the UI's origin:
  `curl -X POST -H 'Origin: http://ytdl.home.arpa' http://ytdl.home.arpa/retry-all`,
  with your UI's name in place of `ytdl.home.arpa`.
- A client that reaches the published port directly, not through the proxy, needs the
  address it uses in `YTM_ALLOWED_HOSTS`, or it gets 400.
- The Jellyfin refresh call does not verify TLS, so that a self-signed Jellyfin
  certificate on an internal network works. The API key is therefore sent without
  verifying the server, and urllib3's `InsecureRequestWarning` is silenced
  process-wide.

## Deploying

Run one web container and one worker from the same image, next to a bgutil PO-token
provider. [compose.example.yaml](compose.example.yaml) is a complete example. It runs the
release that `YTM_VERSION` names, and every Compose command for that file needs the
variable, so keep it in an `.env` file beside the example:

```sh
echo 'YTM_VERSION=1.0.0' > .env
docker compose -f compose.example.yaml up -d
```

- **Image.** Each release is published as `ghcr.io/ochsyp/youtube-manager:<version>`,
  such as `1.0.0`, for amd64 and arm64, and `latest` moves with every release that is not
  a prerelease. In production, pin the digest the release notes list as well, as in
  `YTM_VERSION=1.0.0@sha256:<digest>`: a tag can be moved to another image, a digest
  cannot.
- **Commands.** The image has two: `web`, the default, serves the UI on port 8080, and
  `worker` runs the download worker. Arguments after either go to its process, and any
  other command runs as given. Every command runs as the `PUID`/`PGID` user, not root.
  Both services use the SQLite database under `/app/data`, so run exactly one of each.
- **Init.** tini is PID 1 in both containers: it passes `docker stop`'s SIGTERM on and
  reaps orphaned yt-dlp and ffmpeg processes, so neither container needs `init: true`.
- **Volumes.** `/app/data` holds the database, so back it up. `/downloads` is the staging
  area and `/media` the library. They may sit on different disks: filing copies to a
  hidden temporary file, syncs it and renames it into place.
- **Reverse proxy.** Serve the UI through any reverse proxy and set `YTM_ALLOWED_HOSTS` to
  the name it serves the UI under. The proxy must pass on the `Host` header the browser
  sent, port included, or every POST gets 403. In nginx that is
  `proxy_set_header Host $http_host;`, because `$host` drops the port; Caddy and Traefik
  pass it on by default. Let the proxy admit only the clients and probes that should
  reach the UI, and publish the container's port to nothing else.
- **Healthchecks.** The web UI answers `GET /health` without touching the database, and
  the image includes curl to ask it. The worker touches `WORKER_HEARTBEAT_PATH` from
  every long-running loop; treat it as dead once that file is more than 600 seconds old.
- **Stopping.** On SIGTERM the worker stops at its next check. Give it a stop grace
  period of 60 seconds, since Docker's default of 10 can kill a step mid-write.
- **Updating.** Set `YTM_VERSION` to the new release, with its digest, and run
  `docker compose -f compose.example.yaml up -d`, which pulls the image and recreates
  both containers together. Read the release's CHANGELOG section first: a major release
  lists what the host has to change. Going back to the previous release works the same
  way, unless that section has a `Migration (cannot be rolled back)` part.
- **Startup log.** The web UI logs `YouTube Manager <version> web UI starting`. The
  worker logs a `Components:` line with the release, the versions of yt-dlp, yt-dlp-ejs
  and the bgutil plugin, and deno's path.
- **Building from a checkout.** `docker build -t youtube-manager .` builds the image for
  the local architecture, with `dev` as its version (`--build-arg YTM_VERSION=<version>`
  sets another). It needs BuildKit, Docker's default builder since version 23. To run
  such a build with the example, set both `image:` lines to `youtube-manager`.

## Running tests

From the repository root, with [uv](https://docs.astral.sh/uv/), which fetches a
standalone CPython 3.13, so no system Python is needed (on Windows this also sidesteps
the Microsoft Store Python):

```sh
uv run --no-project --python 3.13 --with-requirements requirements.txt --with-requirements requirements-dev.txt python -m pytest -q
```

Tests marked `posix` need `os.killpg` and `SIGKILL`. They are skipped on Windows and run
in CI on Linux (`.github/workflows/ci.yml`) along with the rest of the suite. The suite
writes only under pytest's temp directories, never to `data/`, `/downloads` or `/media`.

CI also builds the image for amd64 and arm64 and runs `scripts/smoke-test-image.sh` on
each build. On Linux with Docker, `bash scripts/smoke-test-image.sh youtube-manager` runs
the same checks on a local build (Deploying, Building from a checkout).

## Repository layout

```text
youtube_manager/    the application package
  app.py            web UI and API
  worker.py         download worker
  organizer.py      library filing, NFOs and artwork
  database.py       SQLAlchemy models and migrations
  jobstate.py       job status and progress helpers
  media.py          paths and media lookup
  applog.py         shared log formatter
  templates/        Jinja2 templates
tests/              pytest suite and its fixtures (conftest.py)
scripts/            release check and image smoke test, run by the workflows
data/               SQLite database (runtime, git-ignored)
Dockerfile          image build: base images pinned by digest, deno, the pinned packages
entrypoint.sh       drops root to PUID/PGID and maps the web and worker commands
compose.example.yaml  example deployment: web UI, worker and bgutil provider
requirements.in     direct runtime dependencies, unpinned
requirements.txt    exact pins uv compiles from requirements.in; the image installs these
requirements-dev.txt  test-only dependencies (pytest, httpx)
pytest.ini          pytest settings and the posix marker
.github/workflows/  CI (tests, image build and smoke test), release and secret scan
.github/            Dependabot and the pull request template
.githooks/          hooks that keep commits and pushes off main
CHANGELOG.md        changes, newest first
CONTRIBUTING.md     branch, pull request and release workflow
SECURITY.md         security policy
```

## Contributing

Changes go through feature branches and pull requests; [CONTRIBUTING.md](CONTRIBUTING.md)
describes the workflow, and [CHANGELOG.md](CHANGELOG.md) lists what changed.

## License

MIT License.

## Third-party software

The image carries this licence as `/app/LICENSE`. It also contains software by others,
each under its own licence: Python, ffmpeg and the other Debian packages, deno, and the
Python packages, yt-dlp among them. Release images carry a provenance attestation of the
build and an SBOM that lists the image's packages. This prints the SBOM:

```sh
docker buildx imagetools inspect ghcr.io/ochsyp/youtube-manager:<version> --format '{{ json .SBOM }}'
```
