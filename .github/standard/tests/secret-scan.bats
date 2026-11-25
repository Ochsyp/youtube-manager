#!/usr/bin/env bats
# secret-scan.bats of the repository standard: the same file in every repository; never edit it
# here. secret-scan with the gitleaks stub of stub-bin: the positive controls, the range of each
# event, the rules for accepted findings, the refusal of .gitleaks.toml, and a summary of counts
# and rule names only. Values a rule matches are built from fragments at run time.

# The sh -c scripts are single-quoted, and bats runs each test in a subshell, both on purpose.
# shellcheck disable=SC2016,SC2030,SC2031
load lib

setup() {
    isolate_home
    export STUB_LOG="$BATS_TEST_TMPDIR/stub.log"
    : > "$STUB_LOG"
    unset STUB_GITLEAKS_BLIND STUB_GITLEAKS_FIND GITLEAKS_CONFIG GITLEAKS_CONFIG_TOML
}

# Run secret-scan in $REPO through bats `run`, with the gitleaks stub first on PATH and the event
# settings as NAME=value arguments, stderr apart.
scan_event() {
    run --separate-stderr env PATH="$REPO_ROOT/.github/standard/tests/stub-bin:$PATH" "$@" \
        sh -c 'cd "$1" && exec bash .github/standard/bin/secret-scan' sh "$REPO"
}

# Commit FILE with TEXT in $REPO, with the hooks off.
commit_file() {
    stage "$1" "$2"
    git -C "$REPO" -c core.hooksPath=no-hooks commit -q -m "docs: add $1"
}

# A repository at $REPO with the base pack on main, BASE at main, and the branch feat/x.
pr_repo() {
    make_repo
    commit_config PRIVACY_RULES=base
    BASE=$(git -C "$REPO" rev-parse HEAD)
    git -C "$REPO" switch -q -c feat/x
}

head_of() {
    git -C "$REPO" rev-parse HEAD
}

@test "secret-scan runs the positive controls, then both scans over a pull request's commits" {
    pr_repo
    commit_file clean.txt 'synthetic notes'
    scan_event EVENT=pull_request BASE="$BASE" HEAD="$(head_of)"
    [ "$status" -eq 0 ] || { printf '%s\n' "$stderr" >&2; return 1; }
    [[ $stderr == *"secret-scan: gitleaks found nothing"* ]]
    # The control first, with the scan's flags; then the scan, over the pull request's commits.
    [ "$(grep -c '^gitleaks ' "$STUB_LOG")" -eq 2 ]
    [ "$(grep -c -e '--redact' "$STUB_LOG")" -eq 2 ]
    # An inline allow marker in a scanned commit would hide its line; gitleaks must ignore it.
    [ "$(grep -c -e '--ignore-gitleaks-allow' "$STUB_LOG")" -eq 2 ]
    grep -q -F -e "--log-opts=$BASE..$(head_of)" "$STUB_LOG"
    commit_file notes.txt "$(printf 'seen at %s.%s.%s.%s' 192 168 7 9)"
    scan_event EVENT=pull_request BASE="$BASE" HEAD="$(head_of)"
    [ "$status" -eq 1 ]
    [[ $stderr == *"notes.txt:1: ipv4-private"* ]]
    assert_tmpdir_empty
}

@test "secret-scan fails unless gitleaks finds its control token and the packs a sample of every kind" {
    pr_repo
    commit_file clean.txt 'synthetic notes'
    STUB_GITLEAKS_BLIND=1 scan_event EVENT=pull_request BASE="$BASE" HEAD="$(head_of)"
    [ "$status" -eq 1 ]
    [[ $stderr == *"the gitleaks control found nothing, so gitleaks would miss a committed token"* ]]
    # Give an enabled pack a kind that the control has no sample of.
    printf 'made-up-kind|zz[0-9]{9}zz|\n' >> "$REPO/.githooks/rules/base"
    scan_event EVENT=pull_request BASE="$BASE" HEAD="$(head_of)"
    [ "$status" -eq 1 ]
    [[ $stderr == *"the privacy control has no sample of the kind made-up-kind"* ]]
}

@test "a pull request and a push scan their commits; schedule, dispatch and a first push the whole history" {
    local old event
    make_repo
    commit_config PRIVACY_RULES=base
    commit_file old.txt "$(printf 'seen at %s.%s.%s.%s' 10 1 2 3)"
    old=$(head_of)
    BASE=$old
    git -C "$REPO" switch -q -c feat/x
    commit_file clean.txt 'synthetic notes'
    scan_event EVENT=pull_request BASE="$BASE" HEAD="$(head_of)"
    [ "$status" -eq 0 ] || { printf '%s\n' "$stderr" >&2; return 1; }
    scan_event EVENT=push BASE="$BASE" HEAD="$(head_of)"
    [ "$status" -eq 0 ]
    for event in schedule workflow_dispatch; do
        : > "$STUB_LOG"
        scan_event EVENT="$event" HEAD="$(head_of)"
        [ "$status" -eq 1 ]
        [[ $stderr == *"old.txt:1: ipv4-private"* ]]
        [[ $(grep -c -e '--log-opts' "$STUB_LOG") -eq 0 ]]
    done
    scan_event EVENT=push BASE=0000000000000000000000000000000000000000 HEAD="$(head_of)"
    [ "$status" -eq 1 ]
    # An accept record for that old commit settles the full-history scan.
    commit_config PRIVACY_RULES=base "PRIVACY_HISTORY_ACCEPT=$old ipv4-private"
    scan_event EVENT=schedule HEAD="$(head_of)"
    [ "$status" -eq 0 ] || { printf '%s\n' "$stderr" >&2; return 1; }
}

@test "accepted findings name old commits only, in .gitleaksignore as commit:file:rule:line" {
    local new
    pr_repo
    commit_file clean.txt 'synthetic notes'
    new=$(head_of)
    printf 'clean.txt:generic-api-key:1\n' > "$REPO/.gitleaksignore"
    git -C "$REPO" add .gitleaksignore
    scan_event EVENT=pull_request BASE="$BASE" HEAD="$(head_of)"
    [ "$status" -eq 1 ]
    [[ $stderr == *".gitleaksignore:1: not commit:file:rule:line"* ]]
    printf '%s:clean.txt:generic-api-key:1\n' "$new" > "$REPO/.gitleaksignore"
    scan_event EVENT=pull_request BASE="$BASE" HEAD="$(head_of)"
    [ "$status" -eq 1 ]
    [[ $stderr == *".gitleaksignore:1: its commit is not an ancestor of the base"* ]]
    printf '# an old finding\n%s:README.md:generic-api-key:1\n' "$BASE" > "$REPO/.gitleaksignore"
    scan_event EVENT=pull_request BASE="$BASE" HEAD="$(head_of)"
    [ "$status" -eq 0 ] || { printf '%s\n' "$stderr" >&2; return 1; }
    write_config PRIVACY_RULES=base "PRIVACY_HISTORY_ACCEPT=$new e-mail"
    scan_event EVENT=pull_request BASE="$BASE" HEAD="$(head_of)"
    [ "$status" -eq 1 ]
    [[ $stderr == *"PRIVACY_HISTORY_ACCEPT entry 1: its commit is not an ancestor of the base"* ]]
}

@test "secret-scan refuses .gitleaks.toml, and gitleaks never sees GITLEAKS_CONFIG" {
    pr_repo
    commit_file clean.txt 'synthetic notes'
    printf '[extend]\nuseDefault = true\n' > "$REPO/.gitleaks.toml"
    scan_event EVENT=pull_request BASE="$BASE" HEAD="$(head_of)"
    [ "$status" -eq 1 ]
    [[ $stderr == *".gitleaks.toml is refused"* ]]
    rm "$REPO/.gitleaks.toml"
    scan_event EVENT=pull_request BASE="$BASE" HEAD="$(head_of)" GITLEAKS_CONFIG=other.toml
    [ "$status" -eq 0 ]
    run grep -c GITLEAKS_CONFIG "$STUB_LOG"
    [ "$output" = 0 ]
}

@test "the gitleaks summary holds counts and rule names, never a value" {
    pr_repo
    commit_file clean.txt 'synthetic notes'
    STUB_GITLEAKS_FIND=$'aws-access-token\naws-access-token\ngeneric-api-key' \
        scan_event EVENT=pull_request BASE="$BASE" HEAD="$(head_of)"
    [ "$status" -eq 1 ]
    [[ $stderr == *"secret-scan: gitleaks found 3 finding(s): aws-access-token (2), generic-api-key (1)"* ]]
    [[ $stderr != *REDACTED* ]]
}

@test "secret-scan refuses a pull request without its base, and arguments" {
    pr_repo
    scan_event EVENT=pull_request HEAD="$(head_of)"
    [ "$status" -eq 2 ]
    run --separate-stderr bash "$REPO/.github/standard/bin/secret-scan" extra
    [ "$status" -eq 64 ]
}
