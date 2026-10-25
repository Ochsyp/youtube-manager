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
| `YOUTUBE_DB_URI` | `sqlite:///<app dir>/data/youtube.db` | SQLAlchemy database URL |
| `POT_PROVIDER_URL` | unset | bgutil provider base URL passed to yt-dlp; when empty, no PO-token arguments are passed |
| `JELLYFIN_URL`, `JELLYFIN_TOKEN` | unset | Jellyfin base URL and API key (secret); the library refresh runs only when both are set |
| `WORKER_HEARTBEAT_PATH` | `/tmp/worker-heartbeat` | Liveness file for the worker healthcheck |
| `PUID`, `PGID` | `1000` | User and group the containers run as |
| `TZ` | | Timezone of the log timestamps |
| `HTTP_PROXY`, `HTTPS_PROXY`, `NO_PROXY` | | Egress proxy; `NO_PROXY` must cover Jellyfin and the bgutil provider |

Downloads also pass `HTTP_PROXY` to yt-dlp as `--proxy`, which ignores `NO_PROXY`, so
the proxy itself must let the worker reach the bgutil provider.

Volumes: `/app/data` (SQLite), `/downloads` (staging) and `/media` (library).

## Security notes

- The web UI has **no authentication and no CSRF protection**. Any web page a user on
  that network opens can submit its forms, including the ones that delete library
  files. Expose it only on a trusted network, or behind a reverse proxy that
  authenticates and checks the request origin.
- The Jellyfin refresh call does not verify TLS, so that a self-signed Jellyfin
  certificate on an internal network works. The API key is therefore sent without
  verifying the server, and urllib3's `InsecureRequestWarning` is silenced
  process-wide.

## Deploying in jelly-stack

The Compose definition (both services, the bgutil sidecar, healthchecks and port
mapping) lives in [jelly-stack](https://github.com/Ochsyp/jelly-stack), which clones this
repository to `<stack-dir>/youtube-manager`. Compose builds the image from that
checkout and bind-mounts it over `/app`, so a `git pull` followed by a restart of both
`youtube-manager` and `youtube-worker` deploys code changes; dependency, Dockerfile and
yt-dlp updates need a rebuild. The container names `youtube-manager`, `youtube-worker`
and `bgutil-provider` are an interface: the stack's Squid ACLs, firewall rules, probes,
log pipeline and reverse proxy address them by name.

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
data/               SQLite database (runtime, git-ignored)
Dockerfile          image build; also installs yt-dlp and the bgutil plugin
entrypoint.sh       drops root to PUID/PGID
requirements.txt    Python dependencies
.github/            secret scan and Dependabot
SECURITY.md         security policy
```

## License

MIT License.
