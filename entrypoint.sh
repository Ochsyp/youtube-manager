#!/bin/sh
# -------------------------------------------------------
# Container entrypoint
#
# Runs as root under tini: creates the PUID/PGID user, hands it the database and cache
# directories, maps the image's `web` and `worker` commands to their processes, and execs the
# command as that user. Any other command runs as given.
# -------------------------------------------------------
set -e

# Plain python:3.13-slim, not a LinuxServer.io base, so nothing honours PUID/PGID for us;
# without the drop below every file written under /media or /downloads comes out root:root.
PUID="${PUID:-1000}"
PGID="${PGID:-1000}"

if ! getent group appgroup >/dev/null 2>&1; then
    groupadd -o -g "$PGID" appgroup
fi
if ! getent passwd appuser >/dev/null 2>&1; then
    useradd -o -u "$PUID" -g "$PGID" -d /app -s /sbin/nologin appuser
fi

# Only /app/data (the SQLite database) is small enough to walk on every start. The
# bind-mounted /media and /downloads are left alone: a one-off host chown handles those.
chown -R "$PUID:$PGID" /app/data

# yt-dlp and the deno it runs cache under XDG_CACHE_HOME, which the image points into /tmp
# because appuser's home, /app, is root-owned; unset, both fall back to /app/.cache. Created and
# handed to PUID/PGID on every start, since a volume mounted there, or /app, belongs to root.
CACHE_DIR="${XDG_CACHE_HOME:-/app/.cache}"
mkdir -p "$CACHE_DIR"
chown -R "$PUID:$PGID" "$CACHE_DIR"

# `web` and `worker` are the image's public interface: hosts name a role, not the process
# behind it, so a release can change how either starts. Extra arguments pass through.
case "$1" in
    web)
        shift
        set -- uvicorn youtube_manager.app:app --host 0.0.0.0 --port 8080 "$@"
        ;;
    worker)
        shift
        set -- python -m youtube_manager.worker "$@"
        ;;
esac

exec gosu "$PUID:$PGID" "$@"
