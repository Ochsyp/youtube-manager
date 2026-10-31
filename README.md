# youtube-manager

Self-hosted YouTube downloader that files everything into a Jellyfin library. Queue
videos, playlists and channels from a web UI, subscribe to channels, and get properly
named movies and shows with NFO metadata and artwork.

> **Status:** repository bootstrap. The source is imported in the next commit; full
> documentation and diagrams follow.

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

One image, two containers, one SQLite database in WAL mode.

| Component | Role |
|---|---|
| `app.py` (FastAPI + Jinja2, uvicorn on `:8080`) | Web UI and the polling endpoints it uses |
| `worker.py` | Job loop that drives yt-dlp, subscriptions and the organizer |
| `organizer.py` | Moves finished files into the library layout and writes metadata |
| `bgutil-provider` sidecar | Mints the PO tokens YouTube requires |

## Requirements

- Docker with Compose on Linux; the worker relies on POSIX process groups. Runs on a
  Raspberry Pi (arm64).
- Recommended: a `brainicism/bgutil-ytdlp-pot-provider` container the worker can reach.
  Without PO tokens YouTube withholds the DASH formats and yt-dlp falls back to
  low-bitrate HLS.
- Egress to YouTube, its CDNs and GitHub: yt-dlp downloads its JavaScript challenge
  solver from GitHub at run time (`--remote-components ejs:github`).
- Optional: Jellyfin for the library refresh, and an HTTP(S) proxy for egress

## Configuration

| Variable | Default | Purpose |
|---|---|---|
| `DOWNLOAD_PATH` | `/downloads` | Staging directory yt-dlp writes into |
| `MEDIA_PATH` | `/media` | Library root the organizer files into |
| `LIBRARY_SENTINEL` | unset | Worker only: a file on the library disk, such as `/media/.library-mounted`, that must exist before anything is filed. While it is missing the organizer files nothing and logs ERROR at most once an hour. Create the file before setting the variable |
| `YOUTUBE_DB_URI` | `sqlite:///<app dir>/data/youtube.db` | SQLAlchemy database URL |
| `POT_PROVIDER_URL` | unset | bgutil provider base URL passed to yt-dlp; when empty, no PO-token arguments are passed |
| `JELLYFIN_URL`, `JELLYFIN_TOKEN` | unset | Jellyfin base URL and API key (secret); the library refresh runs only when both are set |
| `WORKER_HEARTBEAT_PATH` | `/tmp/worker-heartbeat` | Liveness file for the worker healthcheck |
| `YTM_ALLOWED_HOSTS` | unset | Web service: comma-separated host names or IPs the web UI answers to in addition to `youtube-manager`, `localhost` and `127.0.0.1`. Set it to the name nginx serves the UI under, such as `ytdl.home.arpa`; any other `Host` gets 400 |
| `PUID`, `PGID` | `1000` | User and group the containers run as |
| `TZ` | | Timezone of the log timestamps |
| `HTTP_PROXY`, `HTTPS_PROXY`, `NO_PROXY` | | Egress proxy; `NO_PROXY` must cover Jellyfin and the bgutil provider |

Downloads also pass `HTTP_PROXY` to yt-dlp as `--proxy`, which ignores `NO_PROXY`, so
the proxy itself must let the worker reach the bgutil provider.

Volumes: `/app/data` (SQLite), `/downloads` (staging) and `/media` (library).

## Security notes

- The web UI has **no authentication**: anyone who can reach it can use it. Keep it
  LAN-only. In jelly-stack, publish no host port for it (close `8085`) and let the
  nginx server for the UI's name `allow` the LAN and the stack's Docker subnets, which
  the blackbox probe comes from, before `deny all`.
- Web pages a user opens elsewhere cannot drive it through that user's browser:
  - **Host pinning.** The app answers only to `youtube-manager` (the blackbox probe),
    `localhost` and `127.0.0.1` (the healthcheck), plus the entries of
    `YTM_ALLOWED_HOSTS`, such as the UI's nginx name, whatever the port. Any other
    `Host` gets 400, which defeats DNS rebinding.
  - **Cross-origin guard.** Every request other than GET, HEAD and OPTIONS must come
    from the UI's own pages. A browser that sends `Sec-Fetch-Site` must send
    `same-origin` or `none`; otherwise, as over plain HTTP behind nginx, the `Origin`
    header's host and port must equal `Host`. Anything else, including a missing or
    `null` Origin, gets 403 and one WARN line in the app log.
  - **Headers.** Every response carries a Content-Security-Policy limited to the app's
    own origin (inline scripts and styles allowed), `X-Frame-Options: DENY`,
    `X-Content-Type-Options: nosniff`, and same-origin Referrer and
    Cross-Origin-Resource policies.
- A manual POST must therefore send the UI's origin:
  `curl -X POST -H 'Origin: http://ytdl.home.arpa' http://ytdl.home.arpa/retry-all`,
  with your UI's name in place of `ytdl.home.arpa`.
- Until jelly-stack closes port 8085, `http://<pi-ip>:8085` answers 400 unless
  `YTM_ALLOWED_HOSTS` lists the Pi's address.
- The Jellyfin refresh call does not verify TLS, so that a self-signed Jellyfin
  certificate on an internal network works. The API key is therefore sent without
  verifying the server, and urllib3's `InsecureRequestWarning` is silenced
  process-wide.

## Deploying in jelly-stack

The Compose definition (both services, the bgutil sidecar, healthchecks and port
mapping) lives in [jelly-stack](https://github.com/Ochsyp/jelly-stack), which clones this
repository into a `youtube-manager` folder of its checkout. Compose builds the image from that
checkout and bind-mounts it over `/app`, so a `git pull` followed by a restart of both
`youtube-manager` and `youtube-worker` deploys code changes; dependency, Dockerfile and
yt-dlp updates need a rebuild. The container names `youtube-manager`, `youtube-worker`
and `bgutil-provider` are an interface: the stack's Squid ACLs, firewall rules, probes,
log pipeline and reverse proxy address them by name. nginx must forward the `Host` the
browser sent: `proxy_set_header Host $host` matches `Origin` only while nginx listens on
port 80, and on any other port it needs `$http_host`, or every POST gets 403.

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

## Repository layout

```text
app.py              web UI and API
worker.py           download worker
organizer.py        library filing, NFOs and artwork
database.py         SQLAlchemy models and migrations
jobstate.py         job status and progress helpers
media.py            paths and media lookup
applog.py           shared log formatter
templates/          Jinja2 templates
tests/              pytest suite and its fixtures (conftest.py)
data/               SQLite database (runtime, git-ignored)
Dockerfile          image build; also installs yt-dlp and the bgutil plugin
entrypoint.sh       drops root to PUID/PGID
requirements.txt    Python dependencies
requirements-dev.txt  test-only dependencies (pytest, httpx)
pytest.ini          pytest settings and the posix marker
.github/            CI tests, secret scan, Dependabot and the pull request template
.githooks/          hooks that keep commits and pushes off main
CHANGELOG.md        changes, newest first
CONTRIBUTING.md     branch and pull request workflow
SECURITY.md         security policy
```

## Contributing

Changes go through feature branches and pull requests; [CONTRIBUTING.md](CONTRIBUTING.md)
describes the workflow, and [CHANGELOG.md](CHANGELOG.md) lists what changed.

## License

MIT License.
