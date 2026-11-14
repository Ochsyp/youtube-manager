# Changelog

All notable changes to this project are listed here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and releases follow
[Semantic Versioning](https://semver.org/spec/v2.0.0.html) as CONTRIBUTING.md's Releasing section
describes.

## [Unreleased]

## [1.1.0] - 2026-11-14

Phases 9 to 11, a fix to how an upgrade adds columns, and a dependency update: SQLite connection
settings, versioned schema upgrades with daily backups, and automatic retries that survive a
restart and stop after 8. Before a host upgrades, check that the volume holding the database has
room for copies of it. The first start copies the database to `backups/` beside it
(`data/backups` by default) before it migrates the schema to version 2. The web UI answers only
once the copy and the migration are done, and it does not start if the copy fails. Upgrade the
web UI and the worker in one step, as `docker compose up -d` does: the 1.1.0 worker waits up to
300 seconds for that migration, which a 1.0.3 web UI never runs. The schema change only adds, so
1.0.3 still runs on the upgraded database and going back needs no step. If a host goes back and
then forward again, a video that 1.1.0 had given up on and that failed again after 1.0.3 queued
it stays failed, showing auto-retry 8/8, with no new ERROR line, until someone uses Retry. yt-dlp
and the bgutil plugin are unchanged, so the PO-token provider stays at 2.0.2.

After the upgrade the worker writes a snapshot of the database into the same folder soon after it
starts, between two jobs, then each day from `YTM_DB_SNAPSHOT_TIME` (set on the worker, HH:MM in
UTC, 00:30 unless set), and keeps 7. A week after a clean upgrade the folder holds 8 copies, and 9
while a snapshot is written. They sit beside the database, so a host that backs up its volume
copies them too and can set that time well before its own backup. Videos that were failing under
1.0.3 start their 8 retries afresh: any still failing about 17 hours after the upgrade are given
up, and an ERROR line counts them and names up to 20 by job id.

### Added

- The web UI's starting line and the worker's `Components:` line name the version of the SQLite
  library the process runs on. (phase 9)
- The database has a schema version, its `PRAGMA user_version`. At start the web UI copies an
  older database to `data/backups/youtube-pre-v<new version>-from-v<old version>-<UTC time>.db`,
  then migrates it one step at a time, each step and its version in one transaction, so a step
  that fails changes nothing. The log names the copy and each table's row count before and after.
  The first start of this release brings the existing schema up to date, after such a copy:
  it needs free space for a full copy of the database, and if the copy cannot be written the web
  UI does not start and logs an ERROR. Of the copies before one migration the newest 3 are kept,
  and copies from earlier upgrades stay until deleted by hand. Earlier releases ignore the
  version, so going back to one still works. (phase 10)
- The worker writes a consistent snapshot of the database to
  `data/backups/youtube-daily-<UTC time>.db` once a day, from the time `YTM_DB_SNAPSHOT_TIME` sets
  (HH:MM in UTC, 00:30 by default), and keeps the newest 7. A worker that starts with no snapshot
  since the previous day's time writes one at once. README, Backups and restore, says how to
  restore one and how to rehearse an upgrade on a copy. (phase 10)
- Each failed video keeps its automatic-retry count and the time of its next retry in the
  database, so the waits, which double from 5 minutes up to 6 hours, survive a worker restart.
  After 8 retries, about 17 hours, the worker gives up on the video: its error says so, one ERROR
  line names it, and its leftover files in staging are removed. Retry, Retry All, adding the URL
  again and Fetch Metadata give a video its 8 retries back, and so does finishing without
  failing. The job page shows "auto-retry n/8" under a video's status. (phase 11)

### Changed

- Every database connection waits up to 15 seconds for another's write lock instead of 5, and
  cuts the WAL file back to 8 MiB at the first write after a checkpoint has emptied it. Under WAL
  it also uses `synchronous=NORMAL`, which saves an fsync per commit: a power cut or an OS crash
  can roll back the last few commits but cannot corrupt the database, and a crash of the app or
  its container loses none. A connection that cannot use WAL keeps SQLite's default and logs a
  warning. (phase 9)
- The sqlite3 driver's legacy transaction control is set explicitly, so a newer Python cannot
  change when a transaction begins, and the connection pool no longer pings or recycles
  connections, which a SQLite file never needs. (phase 9)
- `YOUTUBE_DB_URI` must be a SQLite URL for Python's sqlite3 driver: the web UI and the worker
  stop at start on a URL for any other database or driver, or one that does not parse, with an
  error that never repeats more of the URL than its scheme. (phase 9)
- The worker waits up to 300 seconds instead of 60 for the web UI to create or migrate the schema.
  Both run on a newer schema that only adds to their own, with a warning, and stop with an error
  on one from a later major release. (phase 10)
- Schema version 2 adds each download's retry count and next retry time, and an index on its
  last-change time. It fills that time on old rows that lack it, from when they finished or were
  created, else 1 January 1970. The previous release runs on it. The dashboard's change check now
  reads that one indexed value instead of three. (phase 11)
- The image ships click 8.5.1 instead of 8.5.0, pycryptodomex 3.24.2 instead of 3.24.1, pydantic
  2.14.3 instead of 2.14.2, and pydantic-core 2.50.4 instead of 2.50.2.
  ([#22](https://github.com/Ochsyp/youtube-manager/pull/22))

### Fixed

- A column that an upgrade adds to an existing table now gets NOT NULL where the schema declares
  it and the column has a default, so a database from 1.0.x comes out as a new one does. Until
  now such a column was always added without NOT NULL, and a database from 1.0.x would have taken
  the new retry count that way. The table's existing rows read the column's default, and adding
  the column rewrites none of them. A column with no default for those rows is still added
  without NOT NULL, because SQLite refuses that on a table with rows.
  ([#21](https://github.com/Ochsyp/youtube-manager/pull/21))

## [1.0.3] - 2026-11-12

Phase 8, the job state machine, and a dependency update. The web app and the worker now settle a
playlist or channel by one rule, and a cancel or delete holds against auto-retry and the worker.
A playlist or channel download now fetches the videos a subscription only recorded. There is no
schema change and no new setting, so a host needs no step before it upgrades. A playlist that an
earlier version left in progress with nothing but skipped videos stays so: Cancel it, then use
Fetch Metadata for All Children to fetch its videos. yt-dlp and the bgutil plugin are unchanged,
so the PO-token provider stays at 2.0.2.

### Changed

- Fetch Metadata is refused while a job is in progress: queued, downloading or being cancelled, or
  a playlist or channel with a video still in flight. The job page and the bulk button no longer
  offer it on such a job, a request that reaches the server anyway is answered with a flash saying
  the job is still in progress, and a job the worker takes up while the request runs is left as
  the worker set it. (phase 8)
- Retry All reuses a file already on disk: a failed or cancelled video whose file is still where
  its row says, in staging or in the library, has its metadata refreshed instead of being
  downloaded again. Retry All still searches no folder, so the request stays quick on a large
  library; a file moved since is found when the worker reaches the video. (phase 8)
- Cancel acts only on jobs in progress. A bulk Cancel or Cancel All whose selection includes
  finished jobs leaves those as they are, so a failed one is still retried automatically, and the
  flash counts only the jobs actually cancelled. (phase 8)
- Retry is offered on a playlist or channel that has videos only when one of them failed or was
  cancelled, since that is all a retry resets. (phase 8)
- The image ships SQLAlchemy 2.1.8 instead of 2.1.6, and typing-inspection 0.4.5 instead of
  0.4.4. ([#14](https://github.com/Ochsyp/youtube-manager/pull/14))

### Fixed

- A playlist or channel download now downloads the videos it adopts that a subscription recorded
  as skipped, and queues again those deleted or cancelled earlier, instead of adopting them
  unchanged, never downloading them and staying in "expanding". It queues one row per video, and
  none beside a row that already holds the video. Pasting the URL of a video a subscription
  skipped into the add form downloads it instead of reporting it as already there. (phase 8)
- A playlist or channel is settled by one rule in the web app and the worker whenever one of its
  videos stops, on every path, including a video refused before it ran, a download that failed to
  start, a video cancelled at a restart, and cancelling or deleting a single video in the web UI.
  A playlist still showing queued once every video has finished is no longer held back from the
  library, and one whose video was queued again is no longer filed before that video finishes.
  (phase 8)
- A cancelled playlist or channel stays cancelled when a video finishes after the cancel or is
  fetched again, however many run at once, also when it was cancelled while one of its videos ran
  again on its own. Auto-retry no longer restarts its failed videos, nor a video whose own cancel
  was still pending when it failed. A deleted one likewise stays deleted, also when auto-retry
  reruns a video fetched again under it. A cancel flag left on a playlist that had already
  finished no longer turns it cancelled later. (phase 8)
- Cancelling a playlist, or one of its videos, while the worker refreshes the playlist's
  metadata for Fetch Metadata for All Children is no longer undone by that refresh. (phase 8)
- A playlist or channel left with nothing but videos a subscription skipped no longer stays in
  progress for good once it settles: it fails with an error saying that Fetch Metadata for All
  Children downloads them. (phase 8)
- The worker no longer writes over a download deleted in the web UI while the worker still held
  it, so a deleted video no longer comes back as completed or failed; it logs one WARN instead.
  (phase 8)
- The admin sweep settles each playlist it reaches by the same rule, so a playlist with a failed
  video is no longer marked completed, and it leaves a video still in progress to the worker.
  (phase 8)
- A youtu.be share link, or a Shorts, embed, live or /v link, that carries the playlist it was
  shared from is classified as a video. A video request downloads that video, and a playlist
  request, now accepted for it as for a watch link opened inside a playlist, downloads the
  playlist instead of the single video. YouTube's playlist embed link still counts as a playlist.
  (phase 8)

## [1.0.2] - 2026-11-11

Phase 7, subscription scan correctness, and the switch of channel subscriptions to the uploads
playlist. Before a host moves to this release, follow the deploy notes of
[#10](https://github.com/Ochsyp/youtube-manager/pull/10): copy the database with SQLite's backup
API and run its two read-only queries. At its first start the worker retires the rows whose id is
not a video id and re-baselines their subscriptions record-only; its log names how many rows it
retired and each subscription it re-baselined. yt-dlp and the bgutil plugin are unchanged, so the
PO-token provider stays at 2.0.2.

### Changed

- A subscription to a channel's own URL is scanned through the channel's uploads playlist, one
  newest-first listing of every upload: videos, live streams and Shorts alike. The channel's URL
  lists each kind in turn, so a back catalogue is now queued and numbered in upload order instead
  of kind by kind. A subscription to one tab, such as a channel's Videos tab, still reads that tab.
  When the uploads playlist cannot be read, the check reads the channel's URL instead, and logs WARN
  the first time if that URL lists uploads.
- Download existing now takes effect at the next subscription check, which runs every 30 minutes
  between downloads, instead of at once. That check scans the channel or playlist in full and
  queues every skipped video it still lists, and any it has not recorded yet, in the order it lists
  them: a channel's oldest first (kind by kind when its uploads playlist cannot be read), a
  playlist in playlist order, videos recorded before 1.0.1 included. A video recorded twice is
  queued once. The button also sets the subscription to "All existing", and a disabled
  subscription has to be enabled first. (phase 7)
- Once a channel subscription has its baseline, each check reads only the channel's 50 newest
  uploads. When 50 or more of them are new, the check reads the whole channel, so a burst of
  uploads is not missed; if that scan fails, the check keeps the newest uploads and logs WARN. A
  first scan and every playlist scan still read the whole listing. A video of the channel
  downloaded on its own moves into the channel's folder at a check only while it is among those
  newest uploads. (phase 7)

### Fixed

- A subscription's baseline comes only from a scan that succeeded. A failed scan no longer counts
  as a check, so an "Only new" subscription whose first scan failed no longer queues its whole
  back catalogue at the next one, and Last Checked shows the last scan that succeeded. (phase 7)
- A subscription check no longer holds the database's write lock while yt-dlp scans: resolving a
  new subscription is committed before its scan, and a failed resolve is rolled back, so changes
  in the web UI no longer fail with "database is locked" meanwhile. (phase 7)
- A subscription deleted while a check runs no longer stops the check of the subscriptions after
  it. (phase 7)
- A scan keeps only the lines that are video ids, each once. A subscription or channel download no
  longer records a playlist's or channel's id as a video, or one video twice, and a scan whose
  every line is dropped fails instead of recording an empty baseline. (phase 7)
- At start, the worker retires the rows such scans left: a skipped, queued or failed row a scan
  recorded under an id that is not a video id is marked deleted, and a channel or playlist download
  it belongs to is settled again, unless only skipped videos are left beside it. A subscription of
  that channel or playlist is set to "Only new" and records its current listing as skipped at its
  next check, without queuing it; the worker log names it, and Download existing now fetches its
  back catalogue. (phase 7)
- The background repairs of missing parent titles and playlist episode numbers first run one
  check interval after the worker starts, not ahead of the queue on its first pass, and a failed
  title lookup or scan during them no longer marks a finished playlist or channel failed: each logs
  WARN instead. (phase 7)

## [1.0.1] - 2026-11-10

### Changed

- CONTRIBUTING's release steps name the release signing key, its `maintainer` principal and the
  tag message `youtube-manager vX.Y.Z`, check the tag's signature before pushing it, and keep the
  image digest section in every release's notes.

### Fixed

- Channel episodes already filed keep their number. A newly filed video takes the next free number
  instead of renumbering the filed ones, so a backfill no longer files every video under the same
  number. A video that arrives after newer ones were filed takes the next number, a download that
  failed keeps its number until it is retried, and numbers already duplicated on disk stay.
  (phase 6)
- Videos from a playlist subscription get an episode number from their position in the playlist,
  or the next free number when another video holds that position, so they are filed. Rows
  recorded before this release get theirs at the next scan that still lists them. A playlist
  download numbers its videos the same way. (phase 6)
- A video with several rows is one episode: a further row of a video that already has a number in
  the playlist or channel shares it, the number it is filed under first. (phase 6)
- A channel or playlist download keeps the number of every episode it adopts that is already filed
  in its show, and leaves it filed there. (phase 6)
- Retry all no longer downloads a video again when its file is already in the library. The worker
  refreshes it in place, as a single retry does, so it is not filed a second time. A video whose
  library file is gone is downloaded again under its old number, but not while LIBRARY_SENTINEL
  reports the library missing: the job fails, and a later retry finds the file. (phase 6)
- The worker takes jobs created together in the order they were created, and a channel
  subscription records new videos oldest first, so a new subscription's backfill downloads in
  upload order. Videos a subscription recorded before this release still download newest first
  with Download existing now, and are numbered in that order. (phase 6)

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

[Unreleased]: https://github.com/Ochsyp/youtube-manager/compare/v1.1.0...HEAD
[1.1.0]: https://github.com/Ochsyp/youtube-manager/compare/v1.0.3...v1.1.0
[1.0.3]: https://github.com/Ochsyp/youtube-manager/compare/v1.0.2...v1.0.3
[1.0.2]: https://github.com/Ochsyp/youtube-manager/compare/v1.0.1...v1.0.2
[1.0.1]: https://github.com/Ochsyp/youtube-manager/compare/v1.0.0...v1.0.1
[1.0.0]: https://github.com/Ochsyp/youtube-manager/releases/tag/v1.0.0
