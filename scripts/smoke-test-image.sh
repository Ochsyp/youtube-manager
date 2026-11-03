#!/usr/bin/env bash

# -------------------------------------------------------
# Image smoke test
#
# Checks one built image on one platform: the package imports, yt-dlp, deno and ffmpeg run, the
# web UI answers /health, and the worker finds its components, reaches its main loop under tini
# and stops on SIGTERM. CI runs it for each architecture before anything is pushed.
# Usage: smoke-test-image.sh IMAGE [PLATFORM], such as ytm-smoke:arm64 linux/arm64.
# -------------------------------------------------------

set -euo pipefail

if [[ $# -lt 1 || $# -gt 2 ]]; then
    echo "usage: $0 IMAGE [PLATFORM]" >&2
    exit 2
fi

IMAGE="$1"
PLATFORM="${2:-}"

# Seconds each wait below allows, SMOKE_TIMEOUT if set. Generous, because the arm64 image runs
# under QEMU in CI and starts many times slower; a container that exits fails its wait at once.
TIMEOUT="${SMOKE_TIMEOUT:-300}"

# Unique per run, so a container left by an aborted run cannot collide with these.
WEB="ytm-smoke-web-$$"
WORKER="ytm-smoke-worker-$$"

# data/, staging/ and library/ for /app/data, /downloads and /media, owned by the user running the
# test. Both containers run as that user through PUID/PGID, so the trap can remove all they write.
WORKDIR="$(mktemp -d)"
mkdir "$WORKDIR/data" "$WORKDIR/staging" "$WORKDIR/library"
MOUNTS=(
    -v "$WORKDIR/data:/app/data"
    -v "$WORKDIR/staging:/downloads"
    -v "$WORKDIR/library:/media"
)
RUN_UID="$(id -u)"
RUN_GID="$(id -g)"
USER_ENV=(-e "PUID=$RUN_UID" -e "PGID=$RUN_GID")

# EXIT trap: prints each container's log, removes the containers and the temp tree, and exits
# with the script's own status.
cleanup() {
    local status=$?
    local name
    for name in "$WEB" "$WORKER"; do
        if docker container inspect "$name" >/dev/null 2>&1; then
            echo "----- log of $name -----"
            docker logs "$name" 2>&1 || true
            docker rm -f "$name" >/dev/null 2>&1 || true
        fi
    done
    rm -rf "$WORKDIR" || true
    exit "$status"
}
trap cleanup EXIT

# docker run, with --platform when PLATFORM is set; the arguments are docker run's own.
docker_run() {
    if [[ -n "$PLATFORM" ]]; then
        docker run --platform "$PLATFORM" "$@"
    else
        docker run "$@"
    fi
}

# Runs the remaining arguments as a command every 2 s until it succeeds. Returns 1 when CONTAINER
# stops running first or TIMEOUT seconds pass; WHAT names the awaited state in the message.
wait_until() {
    local container="$1" what="$2"
    shift 2
    local deadline=$((SECONDS + TIMEOUT))
    until "$@"; do
        if [[ "$(docker container inspect -f '{{.State.Running}}' "$container")" != true ]]; then
            echo "$container stopped before $what" >&2
            return 1
        fi
        if ((SECONDS >= deadline)); then
            echo "$container: no $what within ${TIMEOUT}s" >&2
            return 1
        fi
        sleep 2
    done
}

# True once the web UI answers /health from inside its own container.
web_healthy() {
    docker exec "$WEB" curl -fsS http://127.0.0.1:8080/health >/dev/null 2>&1
}

# True once the worker has logged its component versions and reached its main loop.
worker_ready() {
    local logs
    logs="$(docker logs "$WORKER" 2>&1)" || return 1
    [[ "$logs" == *'Components: youtube-manager'* && "$logs" == *'entering main loop'* ]]
}

echo "==> Imports"
docker_run --rm --entrypoint python "$IMAGE" \
    -c "import youtube_manager.app, youtube_manager.worker"

echo "==> Tools"
docker_run --rm --entrypoint yt-dlp "$IMAGE" --version
docker_run --rm --entrypoint deno "$IMAGE" --version
docker_run --rm --entrypoint ffmpeg "$IMAGE" -version

# The web container runs first: it creates the database schema, which the worker waits for.
echo "==> Web UI (default command)"
docker_run -d --name "$WEB" "${USER_ENV[@]}" "${MOUNTS[@]}" "$IMAGE" >/dev/null
wait_until "$WEB" "/health" web_healthy
echo "/health answers"

echo "==> Worker"
# /tmp is a tmpfs, as compose.example.yaml mounts it, so entrypoint.sh creates the cache directory
# on an empty mount and the heartbeat and path files go there as in a deployment.
docker_run -d --name "$WORKER" --tmpfs /tmp "${USER_ENV[@]}" "${MOUNTS[@]}" \
    "$IMAGE" worker >/dev/null
wait_until "$WORKER" "main loop" worker_ready
echo "worker reached its main loop"

# A component the worker cannot find reads 'not installed', 'unknown' or 'not found' in this line.
components="$(docker logs "$WORKER" 2>&1 | grep 'Components: youtube-manager')"
echo "$components"
missing='not installed|unknown|not found'
if [[ "$components" =~ $missing ]]; then
    echo "the worker is missing a component" >&2
    exit 1
fi

# tini as PID 1 forwards docker stop's SIGTERM to the worker and reaps orphaned yt-dlp and ffmpeg
# processes.
pid1="$(docker exec "$WORKER" cat /proc/1/cmdline | tr '\0' ' ')"
echo "worker PID 1: $pid1"
if [[ "$pid1" != *tini* ]]; then
    echo "PID 1 of the worker container is not tini" >&2
    exit 1
fi

echo "==> Stop"
docker stop -t 30 "$WORKER" "$WEB" >/dev/null
if [[ "$(docker logs "$WORKER" 2>&1)" != *'YouTube worker stopped'* ]]; then
    echo "the worker did not log 'YouTube worker stopped' after SIGTERM" >&2
    exit 1
fi
echo "exit codes: web $(docker container inspect -f '{{.State.ExitCode}}' "$WEB")," \
    "worker $(docker container inspect -f '{{.State.ExitCode}}' "$WORKER")"

echo "Smoke test passed: $IMAGE ${PLATFORM:-(image platform)}"
