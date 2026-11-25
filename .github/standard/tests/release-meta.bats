#!/usr/bin/env bats
# release-meta.bats of the repository standard: the same file in every repository; never edit it
# here. release-meta check and notes against synthetic VERSION files, CHANGELOG.md files and
# tags: the states, the version and changelog rules of spec 3.8 and 3.9, the pull request rules,
# and its output. Then changelog-entry on synthetic pull requests: the CHANGELOG line a pull
# request adds, its form, and the exceptions for a release, a draft fix and Dependabot. Last,
# it tests the launcher's version rules and the -dev rule.

# The sh -c scripts are single-quoted, and bats runs each test in a subshell, both on purpose.
# shellcheck disable=SC2016,SC2030,SC2031
load lib

# The repository of the synthetic links.
URL=https://github.com/example-owner/example

setup() {
    isolate_home
    unset GITHUB_ACTIONS RELEASE_META_TODAY GITHUB_REPOSITORY BASE
    FAKE="$BATS_TEST_TMPDIR/fake"
    mkdir -p "$FAKE"
    META="$REPO_ROOT/.github/standard/bin/release-meta"
}

# Write VERSION as the one line $1 and a line feed.
write_version() {
    printf '%s\n' "$1" > "$FAKE/VERSION"
}

# Print the header of a CHANGELOG.md, with the state sentence $1.
header() {
    printf '%s\n' '# Changelog' '' \
        'All notable changes to this project are listed here. The format follows' \
        '[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow' \
        "[Semantic Versioning](https://semver.org/spec/v2.0.0.html). $1" ''
}

# A CHANGELOG.md before the first release, with one line under [Unreleased].
changelog_none() {
    { header 'There are no releases yet.'
      printf '%s\n' '## [Unreleased]' '' '### Added' '' '- A first feature.' '' \
          "[Unreleased]: $URL/commits/main"; } > "$FAKE/CHANGELOG.md"
}

# A CHANGELOG.md for the first release 1.0.0, dated $1 (2000-01-01 by default). The version
# heading is line 9.
changelog_first() {
    { header 'Each version is the GitHub release `vX.Y.Z`.'
      printf '%s\n' '## [Unreleased]' '' "## [1.0.0] - ${1:-2000-01-01}" '' '### Added' '' \
          '- The first release.' '' "[Unreleased]: $URL/compare/v1.0.0...HEAD" \
          "[1.0.0]: $URL/releases/tag/v1.0.0"; } > "$FAKE/CHANGELOG.md"
}

# A CHANGELOG.md for 1.1.0 dated $1 above 1.0.0 dated $2 (2000-01-02 and 2000-01-01 by default).
# The version headings are lines 9 and 15.
changelog_second() {
    { header 'Each version is the GitHub release `vX.Y.Z`.'
      printf '%s\n' '## [Unreleased]' '' "## [1.1.0] - ${1:-2000-01-02}" '' '### Added' '' \
          '- A second feature.' '' "## [1.0.0] - ${2:-2000-01-01}" '' '### Added' '' \
          '- The first release.' '' "[Unreleased]: $URL/compare/v1.1.0...HEAD" \
          "[1.1.0]: $URL/compare/v1.0.0...v1.1.0" "[1.0.0]: $URL/releases/tag/v1.0.0"; } \
          > "$FAKE/CHANGELOG.md"
}

# Run release-meta check ARGS on $FAKE through bats `run`, with the tags in TAGS on stdin.
check() {
    # shellcheck disable=SC2086 # TAGS holds one tag per word.
    run --separate-stderr bash "$META" check --root "$FAKE" "$@" < <(printf '%s\n' ${TAGS:-})
}

# Fail unless the run passed with nothing on stderr and printed version $1, latest $2, state $3.
assert_state() {
    if [[ $status -ne 0 || -n $stderr ]]; then
        printf 'status %s, stderr:\n%s\n' "$status" "$stderr" >&2
        return 1
    fi
    [ "$output" = "version=$1"$'\n'"latest=$2"$'\n'"state=$3" ]
}

# Fail unless the run exited 1 and one stderr line reads "release-meta: " and $1.
assert_problem() {
    local line
    if [[ $status -eq 1 ]]; then
        for line in "${stderr_lines[@]}"; do
            [[ $line == "release-meta: $1" ]] && return 0
        done
    fi
    printf 'status %s; no stderr line: release-meta: %s\nstderr:\n%s\n' "$status" "$1" "$stderr" >&2
    return 1
}

@test "this repository passes check with its own tags" {
    run --separate-stderr bash -c 'git -C "$1" tag --list "v*" | bash "$2" check --root "$1"' \
        sh "$REPO_ROOT" "$META"
    [ "$status" -eq 0 ] || { printf '%s\n' "$stderr" >&2; return 1; }
    [[ ${lines[0]} == "version=$(cat "$REPO_ROOT/VERSION")" ]]
}

@test "0.0.0 or X.Y.Z-dev with no section is unreleased; X.Y.Z-dev must lie above every tag" {
    write_version 0.0.0
    changelog_none
    check
    assert_state 0.0.0 "" unreleased
    write_version 1.0.0-dev
    check
    assert_state 1.0.0-dev "" unreleased
    write_version 1.1.0-dev
    changelog_first
    TAGS=v1.0.0 check
    assert_state 1.1.0-dev v1.0.0 unreleased
    write_version 1.0.0-dev
    TAGS=v1.0.0 check
    assert_problem "VERSION 1.0.0-dev is not above the newest tag v1.0.0"
}

@test "a first release is pending until its tag exists, then released; a second is pending above it" {
    write_version 1.0.0
    changelog_first
    check
    assert_state 1.0.0 "" pending
    TAGS=v1.0.0 check
    assert_state 1.0.0 v1.0.0 released
    write_version 1.1.0
    changelog_second
    TAGS=v1.0.0 check
    assert_state 1.1.0 v1.0.0 pending
}

@test "tags that are not vX.Y.Z are ignored, and the newest is the highest in SemVer order" {
    write_version 1.10.0
    { header 'Each version is the GitHub release `vX.Y.Z`.'
      printf '%s\n' '## [Unreleased]' '' '## [1.10.0] - 2000-01-03' '' '- Ten.' '' \
          '## [1.9.0] - 2000-01-02' '' '- Nine.' '' "[Unreleased]: $URL/compare/v1.10.0...HEAD" \
          "[1.10.0]: $URL/compare/v1.9.0...v1.10.0" "[1.9.0]: $URL/releases/tag/v1.9.0"; } \
          > "$FAKE/CHANGELOG.md"
    TAGS='v1.9.0 v1.10.0 seal/v1.9.0 1.9.0 v1.9 latest' check
    assert_state 1.10.0 v1.10.0 released
}

@test "a section is needed for VERSION and for each tag, and a tag for each section but the pending" {
    write_version 1.2.0
    changelog_second
    TAGS=v1.0.0 check
    assert_problem "VERSION is 1.2.0, so CHANGELOG.md needs ## [1.2.0] - YYYY-MM-DD as its newest version section"
    write_version 1.1.0
    TAGS='v1.0.0 v0.9.0' check
    assert_problem "tag v0.9.0 has no ## [0.9.0] section in CHANGELOG.md"
    write_version 1.1.0-dev
    TAGS=v1.0.0 check
    assert_problem "CHANGELOG.md:9: [1.1.0] has no tag v1.1.0; only the pending VERSION may lack one"
}

@test "a date after the day after today (UTC) or off the calendar fails, and so does a bad RELEASE_META_TODAY" {
    write_version 1.0.0
    changelog_first 2000-01-02
    RELEASE_META_TODAY=2000-01-01 check
    assert_state 1.0.0 "" pending
    changelog_first 2000-01-03
    RELEASE_META_TODAY=2000-01-01 check
    assert_problem "CHANGELOG.md:9: [1.0.0] is dated 2000-01-03, after 2000-01-02, the day after today (UTC)"
    changelog_first 2000-02-30
    check
    assert_problem "CHANGELOG.md:9: [1.0.0] has date 2000-02-30, which is not a calendar date"
    changelog_first
    RELEASE_META_TODAY=yesterday check
    assert_problem "RELEASE_META_TODAY is not a calendar date: yesterday"
}

@test "versions must descend and dates must not rise from top to bottom" {
    write_version 1.1.0
    changelog_second 2000-01-01 2000-01-02
    TAGS='v1.0.0 v1.1.0' check
    assert_problem "CHANGELOG.md:15: [1.0.0] is dated 2000-01-02, after [1.1.0] above it, dated 2000-01-01; dates must not increase from top to bottom"
    sed -i 's/^## \[1\.0\.0\]/## [1.2.0]/' "$FAKE/CHANGELOG.md"
    TAGS='v1.2.0 v1.1.0' check
    [ "$status" -eq 1 ]
    [[ $stderr == *"[1.2.0] is not lower than [1.1.0] above it; versions must descend from top to bottom"* ]]
}

@test "every link names the release it belongs to, once, in one block at the end" {
    write_version 1.1.0
    changelog_second
    sed -i "s|^\[1\.1\.0\]: .*|[1.1.0]: $URL/releases/tag/v1.1.0|" "$FAKE/CHANGELOG.md"
    TAGS=v1.0.0 check
    assert_problem "CHANGELOG.md:22: [1.1.0] links to $URL/releases/tag/v1.1.0; expected $URL/compare/v1.0.0...v1.1.0"
    changelog_second
    sed -i '/^\[1\.0\.0\]: /d' "$FAKE/CHANGELOG.md"
    TAGS=v1.0.0 check
    assert_problem "CHANGELOG.md has no link reference [1.0.0]: $URL/releases/tag/v1.0.0"
    changelog_second
    printf '%s\n' "[1.0.0]: $URL/releases/tag/v1.0.0" >> "$FAKE/CHANGELOG.md"
    TAGS=v1.0.0 check
    assert_problem "CHANGELOG.md:24: a second link reference for [1.0.0]"
    changelog_second
    sed -i "s|^\[Unreleased\]: .*|[Unreleased]: $URL/tree/main|" "$FAKE/CHANGELOG.md"
    TAGS=v1.0.0 check
    assert_problem "CHANGELOG.md:21: [Unreleased] links to $URL/tree/main, which ends in neither /commits/main nor /compare/vX.Y.Z...HEAD"
    changelog_second
    printf '%s\n' '' 'A line after the links.' 'Another.' >> "$FAKE/CHANGELOG.md"
    TAGS=v1.0.0 check
    assert_problem "CHANGELOG.md:25: text after the link reference on line 21; link references belong in one block at the end"
    [ "$(grep -c 'text after the link reference' <<< "$stderr")" -eq 1 ]
}

@test "with GITHUB_REPOSITORY set, every link names that repository" {
    write_version 1.0.0
    changelog_first
    GITHUB_REPOSITORY=example-owner/example check
    assert_state 1.0.0 "" pending
    GITHUB_REPOSITORY=example-owner/other check
    assert_problem "CHANGELOG.md:15: the links name $URL; this repository is https://github.com/example-owner/other"
}

@test "VERSION is one line X.Y.Z or X.Y.Z-dev and a line feed, never lower than the newest tag" {
    local bad
    changelog_none
    for bad in 'v1.0.0' '1.0.0-rc1' '01.0.0' '1.0' '1.0.0 # note' $'1.0.0\r' $'\357\273\2771.0.0' \
        $'1.0.0\n1.0.1'; do
        printf '%s\n' "$bad" > "$FAKE/VERSION"
        check
        assert_problem "VERSION is not one line X.Y.Z or X.Y.Z-dev and a line feed, without leading zeros" ||
            { printf 'passed: %q\n' "$bad" >&2; return 1; }
    done
    printf '1.0.0' > "$FAKE/VERSION"
    check
    assert_problem "VERSION is not one line X.Y.Z or X.Y.Z-dev and a line feed, without leading zeros"
    rm "$FAKE/VERSION"
    check
    assert_problem "VERSION is missing"
    write_version 0.9.0
    changelog_first
    TAGS=v1.0.0 check
    assert_problem "VERSION 0.9.0 is lower than the newest tag v1.0.0"
}

@test "the header, the headings and the subsections follow spec 3.8" {
    write_version 1.0.0
    changelog_first
    sed -i '1s/.*/# Change log/' "$FAKE/CHANGELOG.md"
    check
    assert_problem "CHANGELOG.md:1: the first line must be # Changelog"
    changelog_first
    sed -i 's|keepachangelog.com/en/1.1.0/|keepachangelog.com/en/1.0.0/|' "$FAKE/CHANGELOG.md"
    check
    assert_problem "CHANGELOG.md: the header must name Keep a Changelog 1.1.0 and Semantic Versioning 2.0.0, as templates/CHANGELOG.md in repo-standard writes them"
    changelog_first
    sed -i 's/Each version is the GitHub release `vX.Y.Z`./There are no releases yet./' "$FAKE/CHANGELOG.md"
    check
    assert_problem "CHANGELOG.md: the header says there are no releases yet, but it holds version sections"
    changelog_none
    sed -i 's/There are no releases yet\./Not yet./' "$FAKE/CHANGELOG.md"
    write_version 0.0.0
    check
    assert_problem "CHANGELOG.md: the header must say There are no releases yet. until the first version section"
    write_version 1.0.0
    changelog_first
    sed -i 's/^## \[Unreleased\]$/## Unreleased/' "$FAKE/CHANGELOG.md"
    check
    assert_problem "CHANGELOG.md:7: a heading other than ## [Unreleased] or ## [X.Y.Z] - YYYY-MM-DD: ## Unreleased"
    [[ $stderr == *"CHANGELOG.md has no ## [Unreleased] heading"* ]]
    changelog_first
    printf '%s\n' '## [Unreleased]' >> "$FAKE/CHANGELOG.md"
    check
    [ "$status" -eq 1 ]
}

@test "subsections are Migration first, then Added, Changed, Deprecated, Removed, Fixed and Security, each once" {
    write_version 1.0.0
    changelog_first
    sed -i 's/^### Added$/### Notes/' "$FAKE/CHANGELOG.md"
    check
    assert_problem "CHANGELOG.md:11: ### Notes is not a subsection of Keep a Changelog; use Migration, Added, Changed, Deprecated, Removed, Fixed or Security"
    changelog_first
    sed -i 's/^- The first release\.$/- The first release.\n\n### Migration\n\n- A step./' "$FAKE/CHANGELOG.md"
    check
    assert_problem "CHANGELOG.md:15: ### Migration comes after ### Added in [1.0.0]; the order is Migration, Added, Changed, Deprecated, Removed, Fixed, Security"
    changelog_first
    sed -i 's/^- The first release\.$/- The first release.\n\n### Added\n\n- Again./' "$FAKE/CHANGELOG.md"
    check
    assert_problem "CHANGELOG.md:15: a second ### Added in [1.0.0]"
}

@test "under GITHUB_ACTIONS each problem is an error annotation, and every problem is reported" {
    write_version 1.0.0
    changelog_first 2000-02-30
    sed -i '1s/.*/# Change log/' "$FAKE/CHANGELOG.md"
    GITHUB_ACTIONS=true check
    [ "$status" -eq 1 ]
    [ -z "$output" ]
    [ "${#stderr_lines[@]}" -eq 2 ]
    [[ ${stderr_lines[0]} == "::error::release-meta: CHANGELOG.md:1: the first line must be # Changelog" ]]
    [[ ${stderr_lines[1]} == "::error::release-meta: CHANGELOG.md:9: [1.0.0] has date 2000-02-30, which is not a calendar date" ]]
}

@test "--release vX.Y.Z needs that tag, VERSION equal, an empty [Unreleased] and notes" {
    write_version 1.0.0
    changelog_first
    sed -i 's/^## \[Unreleased\]$/## [Unreleased]\n   /' "$FAKE/CHANGELOG.md"
    TAGS=v1.0.0 check --release v1.0.0
    assert_state 1.0.0 v1.0.0 released
    check --release v1.0.0
    assert_problem "tag v1.0.0 is not among the tags read on stdin"
    TAGS='v1.0.0 v0.9.0' check --release v0.9.0
    [ "$status" -eq 1 ]
    [[ $stderr == *"VERSION is 1.0.0; release v0.9.0 needs VERSION 0.9.0"* ]]
    changelog_first
    sed -i 's/^## \[Unreleased\]$/## [Unreleased]\n\n- A change after the release./' "$FAKE/CHANGELOG.md"
    TAGS=v1.0.0 check --release v1.0.0
    assert_problem "CHANGELOG.md:9: [Unreleased] is not empty; release v1.0.0 moves every entry under ## [1.0.0]"
}

@test "a pending release is a notice on a pull request and an error with lines left under [Unreleased]" {
    write_version 1.1.0
    changelog_second
    TAGS=v1.0.0 check --event pull_request
    [ "$status" -eq 0 ]
    [[ $stderr == *"release-meta: notice: v1.1.0 waits to be published; merge nothing else until it is"* ]]
    sed -i 's/^## \[Unreleased\]$/## [Unreleased]\n\n- A line that waits./' "$FAKE/CHANGELOG.md"
    TAGS=v1.0.0 check --event pull_request
    [ "$status" -eq 0 ]
    TAGS=v1.0.0 check --event push
    assert_problem "CHANGELOG.md:9: [Unreleased] is not empty while v1.1.0 waits to be published"
}

@test "on a pull request a changed VERSION must rise, and its tag must not exist" {
    make_repo
    FAKE=$REPO
    write_version 1.0.0
    changelog_first
    git -C "$REPO" add VERSION CHANGELOG.md
    git -C "$REPO" -c core.hooksPath=no-hooks commit -q -m "chore: release 1.0.0"
    base=$(git -C "$REPO" rev-parse HEAD)
    write_version 1.0.1-dev
    BASE=$base TAGS=v1.0.0 check --event pull_request
    assert_state 1.0.1-dev v1.0.0 unreleased
    write_version 0.9.0-dev
    BASE=$base TAGS=v1.0.0 check --event pull_request
    assert_problem "VERSION goes from 1.0.0 to 0.9.0-dev, but a change of VERSION must raise it"
    git -C "$REPO" tag v1.0.1
    write_version 1.0.1
    BASE=$base TAGS='v1.0.0 v1.0.1' check --event pull_request
    assert_problem "tag v1.0.1 exists already; a release raises VERSION to a new version"
}

@test "notes X.Y.Z prints the section's notes without blank ends, and needs no VERSION or tags" {
    changelog_second
    run --separate-stderr bash "$META" notes 1.1.0 --root "$FAKE" < /dev/null
    [ "$status" -eq 0 ]
    [ "$output" = $'### Added\n\n- A second feature.' ]
    run --separate-stderr bash "$META" notes 1.0.0 --root "$FAKE" < /dev/null
    [ "$output" = $'### Added\n\n- The first release.' ]
    run --separate-stderr bash "$META" notes 2.0.0 --root "$FAKE" < /dev/null
    assert_problem "CHANGELOG.md has no ## [2.0.0] - YYYY-MM-DD section"
    sed -i 's/^- A second feature\.$//' "$FAKE/CHANGELOG.md"
    run --separate-stderr bash "$META" notes 1.1.0 --root "$FAKE" < /dev/null
    assert_problem "CHANGELOG.md:9: [1.1.0] has no notes, only blank lines and headings"
}

@test "usage errors exit 64 with the usage on stderr" {
    local args
    for args in '' 'list' 'check --release' 'check --release 1.0.0' 'check --event' \
        'check --root' 'notes' 'notes v1.0.0' 'notes 1.0.0 --release v1.0.0' 'check --other'; do
        # shellcheck disable=SC2086 # the words of args are the arguments.
        run --separate-stderr bash "$META" $args < /dev/null
        [ "$status" -eq 64 ] || { printf 'args: %s, status %s\n' "$args" "$status" >&2; return 1; }
        [[ $stderr == *"usage: release-meta check"* ]]
    done
}

# Build a pull request in $REPO: main with the CHANGELOG.md that changelog_none writes, then the
# branch $1 with the file the caller writes next; commit_pr commits it.
pr_start() {
    make_repo
    FAKE=$REPO
    write_version 0.0.0
    changelog_none
    git -C "$REPO" add VERSION CHANGELOG.md
    git -C "$REPO" -c core.hooksPath=no-hooks commit -q -m "docs: changelog"
    BASE=$(git -C "$REPO" rev-parse HEAD)
    git -C "$REPO" switch -q -c "$1"
}

commit_pr() {
    git -C "$REPO" add -A
    git -C "$REPO" -c core.hooksPath=no-hooks commit -q --allow-empty -m "feat: change"
}

# Run changelog-entry in $REPO through bats `run` with HEAD_REF $1 and AUTHOR $2.
entry() {
    run --separate-stderr env BASE="$BASE" HEAD_REF="$1" AUTHOR="${2:-someone}" \
        sh -c 'cd "$1" && exec bash .github/standard/bin/changelog-entry' sh "$REPO"
}

@test "changelog-entry passes a line added under [Unreleased], and fails a pull request that adds none" {
    pr_start feat/x
    sed -i 's/^- A first feature\.$/- A first feature.\n- A second one (phase 2)/' "$REPO/CHANGELOG.md"
    commit_pr
    entry feat/x
    [ "$status" -eq 0 ] || { printf '%s\n' "$stderr" >&2; return 1; }
    pr_start feat/y
    printf 'other\n' > "$REPO/notes.txt"
    commit_pr
    entry feat/y
    [ "$status" -eq 1 ]
    [[ $stderr == *"changelog-entry: the pull request adds no line under ## [Unreleased] in CHANGELOG.md"* ]]
}

@test "changelog-entry passes a release and the fix of a pending draft, and Dependabot only as Dependabot" {
    pr_start chore/release-1.0.0
    write_version 1.0.0
    changelog_first
    commit_pr
    entry chore/release-1.0.0
    [ "$status" -eq 0 ] || { printf '%s\n' "$stderr" >&2; return 1; }
    git -C "$REPO" switch -q -c fix/release-1.0.0-notes
    sed -i 's/^- The first release\.$/- The first release.\n- A note the draft lacked./' "$REPO/CHANGELOG.md"
    BASE=$(git -C "$REPO" rev-parse HEAD)
    commit_pr
    entry fix/release-1.0.0-notes
    [ "$status" -eq 0 ] || { printf '%s\n' "$stderr" >&2; return 1; }
    entry feat/other
    [ "$status" -eq 1 ]
    pr_start dependabot/github_actions/ci-123
    printf 'bump\n' > "$REPO/notes.txt"
    commit_pr
    entry dependabot/github_actions/ci-123 'dependabot[bot]'
    [ "$status" -eq 0 ]
    entry dependabot/github_actions/ci-123 someone
    [ "$status" -eq 1 ]
    entry feat/look-alike 'dependabot[bot]'
    [ "$status" -eq 1 ]
}

@test "changelog-entry holds the added lines to 100 columns and a trailing (phase N)" {
    pr_start feat/x
    sed -i "s/^- A first feature\.\$/- A first feature.\n- $(printf 'w%.0s' {1..99})/" "$REPO/CHANGELOG.md"
    commit_pr
    entry feat/x
    [ "$status" -eq 1 ]
    [[ $stderr == *"CHANGELOG.md:12: a line the pull request adds is longer than 100 columns"* ]]
    pr_start feat/y
    sed -i 's/^- A first feature\.$/- A first feature.\n- Built in phase 2 of the plan./' "$REPO/CHANGELOG.md"
    commit_pr
    entry feat/y
    [ "$status" -eq 1 ]
    [[ $stderr == *"CHANGELOG.md:12: name a phase only as a trailing \"(phase N)\""* ]]
}
