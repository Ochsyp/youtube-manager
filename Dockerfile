# The youtube-manager image, built for amd64 and arm64. Base images are pinned
# by digest and Python packages by exact version, in requirements.txt.

# deno runs yt-dlp's JavaScript challenge solver. This image holds only the
# binary, at /deno, for amd64 and arm64.
FROM denoland/deno:bin-2.9.8@sha256:b507583d3c68ba69f3ed2ce5969ae5b8b70f43421c781279c7aea06435263543 AS deno

# The multi-arch index digest. 3.13 is the Python the test suite runs on.
FROM python:3.13-slim-trixie@sha256:ae14d4ce8771606477a81446e7ef429edd6762305fc50c84507c1041fa8285d1

# yt-dlp and deno cache under XDG_CACHE_HOME; /tmp/.cache, which entrypoint.sh
# creates for PUID/PGID, keeps that off the root-owned /app home.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    XDG_CACHE_HOME=/tmp/.cache

# curl runs the web healthcheck; tini and gosu start the command (ENTRYPOINT).
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg curl gosu tini \
    && rm -rf /var/lib/apt/lists/*

COPY --from=deno /deno /usr/local/bin/deno

WORKDIR /app

# --no-deps installs exactly the pins, so a set missing a dependency fails
# pip check here instead of pulling in an unpinned package; --only-binary
# keeps unpinned build tools out by taking wheels only. pip check skips
# extras, so tests/test_build.py checks the pins of yt-dlp's default extra.
COPY requirements.txt ./
RUN pip install --no-deps --only-binary=:all: -r requirements.txt && pip check

COPY --chmod=755 entrypoint.sh ./
COPY LICENSE ./
COPY youtube_manager/ ./youtube_manager/
# The database directory, so the image also runs with no volume on /app/data.
RUN mkdir -p data

EXPOSE 8080

# The release, read as youtube_manager.VERSION. Set last, so a new release
# changes no layer above it.
ARG YTM_VERSION=dev
ENV YTM_VERSION=${YTM_VERSION}

# The web UI and the worker share this image, differing only in the command:
# entrypoint.sh maps `web` or `worker` to its process and execs it as
# PUID/PGID, not root. tini, as PID 1, reaps the orphaned yt-dlp and ffmpeg
# processes a Python PID 1 would leave as zombies; -s (subreaper) keeps that
# working when the host runs an init of its own.
ENTRYPOINT ["/usr/bin/tini", "-s", "--", "./entrypoint.sh"]
CMD ["web"]
