#!/bin/sh
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

# yt-dlp caches its JS challenge solver under $HOME/.cache, and appuser's home is /app, which
# the image build leaves root-owned; without this the cache is unwritable and never reused.
mkdir -p /app/.cache
chown -R "$PUID:$PGID" /app/.cache

exec gosu "$PUID:$PGID" "$@"
