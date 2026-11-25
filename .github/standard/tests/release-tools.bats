#!/usr/bin/env bats
# release-tools.bats of the repository standard: the same file in every repository; never edit
# it here. release-prepare, release-build, release-draft, release-promote and release-verify
# against synthetic repositories and releases, with the gh and docker stubs of stub-bin. Every
# value is synthetic.

# The sh -c scripts are single-quoted, and bats runs each test in a subshell, both on purpose.
# shellcheck disable=SC2016,SC2030,SC2031
load lib

URL=https://github.com/example-owner/example
SLUG=example-owner/example

setup() {
    isolate_home
    export STUB_LOG="$BATS_TEST_TMPDIR/stub.log"
    export STUB_GH="$BATS_TEST_TMPDIR/gh"
    export STUB_DOCKER_TAGS="$BATS_TEST_TMPDIR/docker-tags"
    mkdir -p "$STUB_GH"
    : > "$STUB_LOG"
    : > "$STUB_DOCKER_TAGS"
    unset GITHUB_ACTIONS GITHUB_REF GITHUB_ACTOR GH_TOKEN STUB_GH_READONLY VERSION COMMIT
    export GITHUB_REPOSITORY=$SLUG
}

# The name of the gh stub's reply file for the call ARGS, as stub-bin/gh builds it.
gh_key() {
    local key
    key=$(printf '%s ' "$@" | sed 's/[^A-Za-z0-9.-]\{1,\}/_/g')
    if (( ${#key} > 120 )); then key=$(printf '%s' "$key" | sha256sum | cut -c 1-64); fi
    printf '%s\n' "$key"
}

# Make the gh call ARGS print stdin, and exit with RC when that is set.
gh_reply() {
    local key
    key=$(gh_key "$@")
    cat > "$STUB_GH/$key"
    [[ -z ${RC:-} ]] || printf '%s\n' "$RC" > "$STUB_GH/$key.rc"
}

# Run the tool $1 with the other arguments in $REPO, with the stubs first on PATH, through bats
# `run`, stderr apart.
tool() {
    local name=$1
    shift
    run --separate-stderr env PATH="$REPO_ROOT/.github/standard/tests/stub-bin:$PATH" \
        sh -c 'cd "$1" && shift && exec bash ".github/standard/bin/$0" "$@"' "$name" "$REPO" "$@"
}

# Commit everything in $REPO with the message $1, as the author $2 (Test User by default).
commit_all() {
    git -C "$REPO" add -A
    git -C "$REPO" -c core.hooksPath=no-hooks -c user.name="${2:-Test User}" commit -q --allow-empty -m "$1"
}

# A repository at $REPO of the release kind $1 with the config lines that follow, at VERSION
# 1.0.0-dev, with no release yet and one line under [Unreleased].
release_repo() {
    make_repo
    write_config "RELEASE_KIND=$1" "${@:2}"
    printf '1.0.0-dev\n' > "$REPO/VERSION"
    printf '%s\n' '# Changelog' '' \
        'All notable changes to this project are listed here. The format follows' \
        '[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow' \
        '[Semantic Versioning](https://semver.org/spec/v2.0.0.html). There are no releases yet.' '' \
        '## [Unreleased]' '' '### Added' '' '- A first feature.' '' "[Unreleased]: $URL/commits/main" \
        > "$REPO/CHANGELOG.md"
    commit_all "feat: a first feature"
}

@test "release-prepare writes a first release: VERSION, its section, the state sentence and the links" {
    release_repo tar
    tool release-prepare 1.0.0
    [ "$status" -eq 0 ] || { printf '%s\n' "$stderr" >&2; return 1; }
    [[ $output == *"## What and why"* && $output == *"Release 1.0.0"* ]]
    [[ $output == *$'\nDependency updates since the start:\n'* ]]
    [ "$(cat "$REPO/VERSION")" = 1.0.0 ]
    grep -q -x -F "## [1.0.0] - $(date -u +%F)" "$REPO/CHANGELOG.md"
    grep -q -x -F 'Each version is the GitHub release `vX.Y.Z`.' "$REPO/CHANGELOG.md"
    [[ $(cat "$REPO/CHANGELOG.md") != *"There are no releases yet."* ]]
    grep -q -x -F "[Unreleased]: $URL/compare/v1.0.0...HEAD" "$REPO/CHANGELOG.md"
    grep -q -x -F "[1.0.0]: $URL/releases/tag/v1.0.0" "$REPO/CHANGELOG.md"
    [ -z "$(awk 'length($0) > 100' "$REPO/CHANGELOG.md")" ]
    run bash -c 'cd "$1" && bash .github/standard/bin/release-meta check < /dev/null' sh "$REPO"
    [ "$status" -eq 0 ] || { printf '%s\n' "$output" >&2; return 1; }
    [[ $output == *"state=pending"* ]]
}

@test "release-prepare writes a later release with VERSION_COPY, and names the Dependabot updates" {
    release_repo tar 'VERSION_COPY=version.txt|version = X.Y.Z'
    printf 'version = 1.0.0-dev\n' > "$REPO/version.txt"
    commit_all "feat: a version file"
    tool release-prepare 1.0.0
    [ "$status" -eq 0 ] || { printf '%s\n' "$stderr" >&2; return 1; }
    commit_all "chore: release 1.0.0"
    git -C "$REPO" tag v1.0.0
    commit_all "ci: bump an action" 'dependabot[bot]'
    sed -i 's/^## \[Unreleased\]$/## [Unreleased]\n\n### Fixed\n\n- A fix./' "$REPO/CHANGELOG.md"
    printf '1.0.1-dev\n' > "$REPO/VERSION"
    printf 'version = 1.0.1-dev\n' > "$REPO/version.txt"
    commit_all "fix: a fix"
    tool release-prepare 1.1.0
    [ "$status" -eq 0 ] || { printf '%s\n' "$stderr" >&2; return 1; }
    [[ $output == *$'\nDependency updates since v1.0.0:\n- ci: bump an action\n'* ]]
    [ "$(cat "$REPO/version.txt")" = "version = 1.1.0" ]
    grep -q -x -F "[Unreleased]: $URL/compare/v1.1.0...HEAD" "$REPO/CHANGELOG.md"
    grep -q -x -F "[1.1.0]: $URL/compare/v1.0.0...v1.1.0" "$REPO/CHANGELOG.md"
    grep -q -x -F "[1.0.0]: $URL/releases/tag/v1.0.0" "$REPO/CHANGELOG.md"
    run bash -c 'cd "$1" && git tag --list "v*" | bash .github/standard/bin/release-meta check' sh "$REPO"
    [[ $output == *"state=pending"* ]]
}

@test "release-prepare refuses a version that is not new, an empty [Unreleased] and a bad version" {
    release_repo tar
    tool release-prepare 1.0.0
    commit_all "chore: release 1.0.0"
    git -C "$REPO" tag v1.0.0
    tool release-prepare 1.0.0
    [ "$status" -eq 1 ]
    [[ $stderr == *"1.0.0 is not above the newest tag v1.0.0"* ]]
    tool release-prepare 1.1.0
    [ "$status" -eq 1 ]
    [[ $stderr == *"[Unreleased] holds nothing to release"* ]]
    tool release-prepare 1.2
    [ "$status" -eq 64 ]
}

@test "release-prepare rebuilds the MANIFEST with tools/manifest where ROLE=source, VERSION staged first" {
    release_repo tar ROLE=source
    mkdir -p "$REPO/tools"
    # tools/manifest reads the index, so it must find the new VERSION there.
    printf '#!/bin/sh\necho ran > "%s/manifest-ran"\ngit show :VERSION > "%s/manifest-version"\n' \
        "$BATS_TEST_TMPDIR" "$BATS_TEST_TMPDIR" > "$REPO/tools/manifest"
    commit_all "chore: a manifest tool"
    tool release-prepare 1.0.0
    [ "$status" -eq 0 ] || { printf '%s\n' "$stderr" >&2; return 1; }
    [ "$(cat "$BATS_TEST_TMPDIR/manifest-version")" = 1.0.0 ]
    [ -f "$BATS_TEST_TMPDIR/manifest-ran" ]
}

@test "release-build builds a tar archive with its manifest, RELEASE_ASSETS, SHA256SUMS and notes, twice the same" {
    local commit out=$BATS_TEST_TMPDIR/out
    release_repo tar 'RELEASE_ASSETS=example-release.json'
    tool release-prepare 1.0.0
    commit_all "chore: release 1.0.0"
    commit=$(git -C "$REPO" rev-parse HEAD)
    GITHUB_REF=refs/heads/main tool release-build --out "$out"
    [ "$status" -eq 0 ] || { printf '%s\n' "$stderr" >&2; return 1; }
    [ "$(cd "$out/assets" && find . -type f | LC_ALL=C sort | tr '\n' ' ')" = \
        "./SHA256SUMS ./example-1.0.0.tar.gz ./example-release.json " ]
    (cd "$out/assets" && sha256sum --check --strict --quiet SHA256SUMS)
    tar -xzOf "$out/assets/example-1.0.0.tar.gz" example-1.0.0/example-release.json > "$BATS_TEST_TMPDIR/manifest.json"
    cmp "$BATS_TEST_TMPDIR/manifest.json" "$out/assets/example-release.json"
    grep -q '"schema": 1,' "$out/assets/example-release.json"
    grep -q '"name": "example",' "$out/assets/example-release.json"
    grep -q '"version": "1.0.0",' "$out/assets/example-release.json"
    grep -q '"tag": "v1.0.0",' "$out/assets/example-release.json"
    grep -q "\"commit\": \"$commit\"" "$out/assets/example-release.json"
    [ "$(cat "$out/notes.md")" = $'### Added\n\n- A first feature.' ]
    tool release-build --dry-run --out "$BATS_TEST_TMPDIR/dry"
    [ "$status" -eq 0 ] || { printf '%s\n' "$stderr" >&2; return 1; }
    [[ $stderr == *"the two builds are the same, byte for byte"* ]]
    GITHUB_REF=refs/heads/feat/x tool release-build --out "$BATS_TEST_TMPDIR/other"
    [ "$status" -eq 1 ]
    [[ $stderr == *"a release is built from refs/heads/main only"* ]]
    GITHUB_REF=refs/heads/main tool release-build --out "$out"
    [ "$status" -eq 1 ]
    [[ $stderr == *"is not empty"* ]]
}

@test "release-build runs ci/release-build and ci/release-check, and holds the hook's SHA256SUMS to its assets" {
    release_repo zip
    mkdir -p "$REPO/ci"
    printf '%s\n' '#!/bin/bash' 'set -eu' 'while (( $# )); do case $1 in --out) out=$2; shift ;; esac; shift; done' \
        'mkdir -p "$out/assets"' 'printf "zip\n" > "$out/assets/example-1.0.0.zip"' \
        '(cd "$out/assets" && sha256sum example-1.0.0.zip > SHA256SUMS && echo "0  other.zip" >> SHA256SUMS)' \
        > "$REPO/ci/release-build"
    printf '#!/bin/sh\necho "$*" > "%s/check-args"\n' "$BATS_TEST_TMPDIR" > "$REPO/ci/release-check"
    chmod +x "$REPO/ci/release-build" "$REPO/ci/release-check"
    tool release-prepare 1.0.0
    commit_all "chore: release 1.0.0"
    GITHUB_REF=refs/heads/main tool release-build --out "$BATS_TEST_TMPDIR/out"
    [ "$status" -eq 1 ]
    [[ $stderr == *"SHA256SUMS must list exactly the assets"* ]]
    sed -i '/other.zip/d' "$REPO/ci/release-build"
    GITHUB_REF=refs/heads/main tool release-build --out "$BATS_TEST_TMPDIR/out2"
    [ "$status" -eq 0 ] || { printf '%s\n' "$stderr" >&2; return 1; }
    [[ $(cat "$BATS_TEST_TMPDIR/check-args") == "--archive "*"example-1.0.0.zip --version 1.0.0" ]]
}

@test "release-build --image writes image.json, SHA256SUMS and notes for the pushed digest" {
    local commit digest out=$BATS_TEST_TMPDIR/out
    digest=sha256:$(printf '4%.0s' {1..64})
    release_repo image 'IMAGE_PLATFORMS=linux/amd64 linux/arm64'
    tool release-prepare 1.0.0
    commit_all "chore: release 1.0.0"
    commit=$(git -C "$REPO" rev-parse HEAD)
    GITHUB_REF=refs/heads/main tool release-build --image --digest "$digest" --out "$out"
    [ "$status" -eq 0 ] || { printf '%s\n' "$stderr" >&2; return 1; }
    [ "$(cd "$out/assets" && find . -type f | LC_ALL=C sort | tr '\n' ' ')" = "./SHA256SUMS ./image.json " ]
    (cd "$out/assets" && sha256sum --check --strict --quiet SHA256SUMS)
    grep -q -x -F '  "tag": "v1.0.0",' "$out/assets/image.json"
    grep -q -x -F "  \"commit\": \"$commit\"," "$out/assets/image.json"
    grep -q -x -F "  \"image\": \"ghcr.io/$SLUG\"," "$out/assets/image.json"
    grep -q -x -F "  \"digest\": \"$digest\"," "$out/assets/image.json"
    grep -q -x -F '  "platforms": ["linux/amd64", "linux/arm64"]' "$out/assets/image.json"
    [ "$(cat "$out/notes.md")" = $'### Added\n\n- A first feature.' ]
    tool release-build --image --digest sha256:0123 --out "$BATS_TEST_TMPDIR/short"
    [ "$status" -eq 64 ]
    tool release-build --out "$BATS_TEST_TMPDIR/archive"
    [ "$status" -eq 1 ]
    [[ $stderr == *"RELEASE_KIND=image is built by image.yml"* ]]
}

# The assets of a synthetic release at $1/assets, with SHA256SUMS, and notes at $1/notes.md.
draft_files() {
    mkdir -p "$1/assets"
    printf 'archive\n' > "$1/assets/example-1.0.0.tar.gz"
    (cd "$1/assets" && sha256sum example-1.0.0.tar.gz > SHA256SUMS)
    printf '### Added\n\n- A first feature.\n' > "$1/notes.md"
}

@test "release-draft replaces the earlier drafts of the tag and drafts the tip of main" {
    local commit
    make_repo
    commit=$(git -C "$REPO" rev-parse HEAD)
    draft_files "$REPO/release-build"
    gh_reply api "repos/$SLUG/git/ref/heads/main" --jq .object.sha <<< "$commit"
    gh_reply api "repos/$SLUG/git/matching-refs/tags/v1.0.0" --jq '.[].ref' <<< refs/tags/v1.0.0-rc.1
    gh_reply api --paginate "repos/$SLUG/releases?per_page=100" --jq '.[] | [.id, .draft, .tag_name] | @tsv' \
        <<< $'11\ttrue\tv1.0.0\n12\tfalse\tv0.9.0'
    gh_reply api "repos/$SLUG/releases/11" --jq .draft <<< true
    VERSION=1.0.0 COMMIT=$commit tool release-draft --dir release-build
    [ "$status" -eq 0 ] || { printf '%s\n' "$stderr" >&2; return 1; }
    grep -q -x -F "gh api --method DELETE repos/$SLUG/releases/11" "$STUB_LOG"
    grep -q -F "gh release create v1.0.0 " "$STUB_LOG"
    grep -q -F -e "--draft --target $commit" "$STUB_LOG"
    grep -q -x -F -e '- A first feature.' "$STUB_LOG"
    [ "$(grep -c 'Container image' "$STUB_LOG")" -eq 0 ]
}

@test "release-draft gives the notes of the image kind the image reference with its digest" {
    local commit digest
    make_repo
    commit=$(git -C "$REPO" rev-parse HEAD)
    digest=sha256:$(printf '5%.0s' {1..64})
    mkdir -p "$REPO/release-build/assets"
    printf '{\n  "schema": 1,\n  "image": "ghcr.io/%s",\n  "digest": "%s",\n  "platforms": ["linux/amd64"]\n}\n' \
        "$SLUG" "$digest" > "$REPO/release-build/assets/image.json"
    (cd "$REPO/release-build/assets" && sha256sum image.json > SHA256SUMS)
    printf '### Added\n\n- A first feature.\n' > "$REPO/release-build/notes.md"
    gh_reply api "repos/$SLUG/git/ref/heads/main" --jq .object.sha <<< "$commit"
    : | gh_reply api "repos/$SLUG/git/matching-refs/tags/v1.0.0" --jq '.[].ref'
    : | gh_reply api --paginate "repos/$SLUG/releases?per_page=100" --jq '.[] | [.id, .draft, .tag_name] | @tsv'
    VERSION=1.0.0 COMMIT=$commit tool release-draft --dir release-build
    [ "$status" -eq 0 ] || { printf '%s\n' "$stderr" >&2; return 1; }
    grep -q -x -F '## Container image' "$STUB_LOG"
    grep -q -x -F "\`ghcr.io/$SLUG:1.0.0@$digest\`" "$STUB_LOG"
}

@test "release-draft refuses a moved main, a published release, an orphan tag and wrong sums" {
    local commit
    make_repo
    commit=$(git -C "$REPO" rev-parse HEAD)
    draft_files "$REPO/release-build"
    gh_reply api "repos/$SLUG/git/ref/heads/main" --jq .object.sha <<< 0123456789012345678901234567890123456789
    : | gh_reply api "repos/$SLUG/git/matching-refs/tags/v1.0.0" --jq '.[].ref'
    : | gh_reply api --paginate "repos/$SLUG/releases?per_page=100" --jq '.[] | [.id, .draft, .tag_name] | @tsv'
    VERSION=1.0.0 COMMIT=$commit tool release-draft --dir release-build
    [ "$status" -eq 1 ]
    [[ $stderr == *"main is at 0123456789012345678901234567890123456789 now"* ]]
    gh_reply api "repos/$SLUG/git/ref/heads/main" --jq .object.sha <<< "$commit"
    gh_reply api --paginate "repos/$SLUG/releases?per_page=100" --jq '.[] | [.id, .draft, .tag_name] | @tsv' \
        <<< $'13\tfalse\tv1.0.0'
    VERSION=1.0.0 COMMIT=$commit tool release-draft --dir release-build
    [ "$status" -eq 1 ]
    [[ $stderr == *"release v1.0.0 is published, and a published release never changes"* ]]
    : | gh_reply api --paginate "repos/$SLUG/releases?per_page=100" --jq '.[] | [.id, .draft, .tag_name] | @tsv'
    gh_reply api "repos/$SLUG/git/matching-refs/tags/v1.0.0" --jq '.[].ref' <<< refs/tags/v1.0.0
    VERSION=1.0.0 COMMIT=$commit tool release-draft --dir release-build
    [ "$status" -eq 1 ]
    [[ $stderr == *"tag v1.0.0 exists without a release"* ]]
    RC=1 gh_reply api "repos/$SLUG/git/matching-refs/tags/v1.0.0" --jq '.[].ref' < /dev/null
    VERSION=1.0.0 COMMIT=$commit tool release-draft --dir release-build
    [ "$status" -eq 1 ]
    [[ $stderr == *"cannot read the tags"* ]]
    VERSION=1.0 COMMIT=$commit tool release-draft --dir release-build
    [ "$status" -eq 64 ]
    printf 'other\n' > "$REPO/release-build/assets/example-1.0.0.tar.gz"
    VERSION=1.0.0 COMMIT=$commit tool release-draft --dir release-build
    [ "$status" -eq 1 ]
    grep -c 'release create' "$STUB_LOG" | grep -q -x 0
}

# image.json of a release VERSION with the image digest DIGEST, in the stub's assets of vVERSION.
image_release() {
    mkdir -p "$STUB_GH/assets-v$1"
    printf '{\n  "schema": 1,\n  "digest": "%s",\n  "version": "%s"\n}\n' "$2" "$1" > "$STUB_GH/assets-v$1/image.json"
}

@test "release-promote tags the published digest and points latest at the newest release that is no pre-release" {
    local d1 d2
    make_repo
    d1=sha256:$(printf '1%.0s' {1..64})
    d2=sha256:$(printf '2%.0s' {1..64})
    image_release 1.1.0 "$d1"
    image_release 1.2.0 "$d2"
    gh_reply api --paginate "repos/$SLUG/releases?per_page=100" --jq '.[] | select(.draft | not) | [.tag_name, .prerelease] | @tsv' \
        <<< $'v1.2.0\ttrue\nv1.1.0\tfalse'
    GITHUB_ACTOR=example-owner GH_TOKEN=synthetic-token-value tool release-promote --tag v1.2.0
    [ "$status" -eq 0 ] || { printf '%s\n' "$stderr" >&2; return 1; }
    grep -q -x -F "ghcr.io/$SLUG:1.2.0 $d2" "$STUB_DOCKER_TAGS"
    grep -q -x -F "ghcr.io/$SLUG:latest $d1" "$STUB_DOCKER_TAGS"
    grep -q -F 'docker login: a password came on stdin' "$STUB_LOG"
    [[ $(cat "$STUB_LOG") != *synthetic-token-value* ]]
}

@test "release-promote --rehearse tags the built digest rehearsal and nothing else" {
    local d3
    make_repo
    d3=sha256:$(printf '3%.0s' {1..64})
    mkdir -p "$REPO/release-build"
    printf '{\n  "schema": 1,\n  "digest": "%s",\n  "version": "0.0.0"\n}\n' "$d3" > "$REPO/release-build/image.json"
    GITHUB_ACTOR=example-owner GH_TOKEN=synthetic-token-value tool release-promote --rehearse --dir release-build
    [ "$status" -eq 0 ] || { printf '%s\n' "$stderr" >&2; return 1; }
    [ "$(cat "$STUB_DOCKER_TAGS")" = "ghcr.io/$SLUG:rehearsal $d3" ]
}

# A published release v1.0.0 of the tar kind in the stub: its archive with the release manifest
# of the commit $1, SHA256SUMS, and the API's view of it.
published_tar() {
    local dir=$BATS_TEST_TMPDIR/pkg
    mkdir -p "$dir/example-1.0.0" "$STUB_GH/assets-v1.0.0"
    printf '{\n  "schema": 1,\n  "name": "example",\n  "version": "1.0.0",\n  "tag": "v1.0.0",\n  "commit": "%s"\n}\n' \
        "$1" > "$dir/example-1.0.0/example-release.json"
    tar -czf "$STUB_GH/assets-v1.0.0/example-1.0.0.tar.gz" -C "$dir" example-1.0.0
    (cd "$STUB_GH/assets-v1.0.0" && sha256sum example-1.0.0.tar.gz > SHA256SUMS)
    gh_reply api "repos/$SLUG/releases/tags/v1.0.0" --jq '[.draft, .immutable, .author.login] | @tsv' \
        <<< $'false\ttrue\tgithub-actions[bot]'
    gh_reply api "repos/$SLUG/commits/v1.0.0" --jq .sha <<< "$1"
}

@test "release-verify checks a published private release without changing anything" {
    local commit
    release_repo tar
    commit=$(git -C "$REPO" rev-parse HEAD)
    published_tar "$commit"
    STUB_GH_READONLY=1 tool release-verify --tag v1.0.0
    [ "$status" -eq 0 ] || { printf '%s\n' "$stderr" >&2; return 1; }
    grep -q -F 'gh release verify v1.0.0' "$STUB_LOG"
    grep -q -F 'gh release verify-asset v1.0.0 ' "$STUB_LOG"
    [ "$(grep -c 'attestation' "$STUB_LOG")" -eq 0 ]
    STUB_GH_READONLY=1 tool release-verify --tag v1.0.0 --public
    [ "$status" -eq 0 ] || { printf '%s\n' "$stderr" >&2; return 1; }
    grep -q -F -e "gh attestation verify" "$STUB_LOG"
    grep -q -F -e "--signer-workflow $SLUG/.github/workflows/release.yml --source-ref refs/heads/main --source-digest $commit --deny-self-hosted-runners" "$STUB_LOG"
}

@test "release-verify refuses a draft, a mutable release, another author, a wrong commit and wrong sums" {
    local commit
    release_repo tar
    commit=$(git -C "$REPO" rev-parse HEAD)
    published_tar "$commit"
    gh_reply api "repos/$SLUG/releases/tags/v1.0.0" --jq '[.draft, .immutable, .author.login] | @tsv' \
        <<< $'false\tfalse\tsomeone'
    tool release-verify --tag v1.0.0
    [ "$status" -eq 1 ]
    [[ $stderr == *"v1.0.0 is not immutable"* ]]
    [[ $stderr == *"v1.0.0 was drafted by someone, not github-actions[bot]"* ]]
    published_tar 0123456789012345678901234567890123456789
    gh_reply api "repos/$SLUG/commits/v1.0.0" --jq .sha <<< "$commit"
    tool release-verify --tag v1.0.0
    [ "$status" -eq 1 ]
    [[ $stderr == *"example-release.json names commit 0123456789012345678901234567890123456789, not the tag's $commit"* ]]
    published_tar "$commit"
    printf 'other\n' >> "$STUB_GH/assets-v1.0.0/example-1.0.0.tar.gz"
    tool release-verify --tag v1.0.0
    [ "$status" -eq 1 ]
    [[ $stderr == *"SHA256SUMS does not match the assets"* ]]
}
