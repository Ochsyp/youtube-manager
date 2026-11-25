#!/usr/bin/env bats
# conf.bats of the repository standard: the same file in every repository; never edit it here.
# The parser of .github/standard.conf: keys, value forms, repeats, required keys, defaults, the
# modes, the limits on PRIVACY_ALLOW and PRIVACY_HISTORY_ACCEPT, and that no message shows a
# value from the file. Every value is synthetic.

# The sh -c scripts are single-quoted, ~ stays literal in git config values, and bats runs each
# test in a subshell, all on purpose.
# shellcheck disable=SC1003,SC2016,SC2030,SC2031,SC2088
load lib

setup() {
    isolate_home
    CONF="$REPO_ROOT/.github/standard/bin/conf"
    REPO="$BATS_TEST_TMPDIR"
    FILE="$REPO/.github/standard.conf"
    # A synthetic home and a home prefix, built from fragments so this file passes the packs.
    SYN_HOME=$(printf '/%s/%s' home example-user)
    HOME_PREFIX=$(printf '/%s/' home)
}

run_conf() {
    run --separate-stderr sh "$CONF" "$@"
}

@test "a minimal config parses, and --print fills in the defaults" {
    write_config
    run_conf --print "$FILE"
    [ "$status" -eq 0 ]
    [ "$output" = "NAME=example
VISIBILITY=private
ROLE=consumer
LICENSE=MIT
CI_RUNNER=ubuntu
CI_STAGES=test
CI_WEEKLY=no
PYTHON=
PS_MODULES=
IMAGE_PLATFORMS=
RELEASE_KIND=tar
RELEASE_RUNNER=ubuntu
RELEASE_MANIFEST=example-release.json
RELEASE_ASSETS=
SEAL=no
PRIVACY_RULES=
MAX_BLOB_KB=1024" ]
}

@test "--defaults prints the defaults without a file" {
    run_conf --defaults
    [ "$status" -eq 0 ]
    [[ $output == *$'\n'"ROLE=consumer"$'\n'* ]]
    [[ $output == *$'\n'"MAX_BLOB_KB=1024" ]]
    [[ $output == "NAME="$'\n'* ]]
}

@test "--get prints a value, a default, or each line of a repeatable key" {
    write_config 'CI_STAGES=lint test' 'VERSION_COPY=a/b|V="X.Y.Z"' 'VERSION_COPY=c|X.Y.Z'
    run_conf --get CI_STAGES "$FILE"
    [ "$output" = "lint test" ]
    run_conf --get CI_WEEKLY "$FILE"
    [ "$output" = no ]
    run_conf --get VERSION_COPY "$FILE"
    [ "$output" = $'a/b|V="X.Y.Z"\nc|X.Y.Z' ]
    run_conf --get PRIVACY_ALLOW "$FILE"
    [ "$status" -eq 0 ]
    [ -z "$output" ]
}

@test "every key takes the values of the standard" {
    write_config NAME=example-2 VISIBILITY=public ROLE=source LICENSE=none CI_RUNNER=windows \
        'CI_STAGES=lint test system' CI_WEEKLY=yes PYTHON=3.13 'PS_MODULES=pester psscriptanalyzer' \
        'IMAGE_PLATFORMS=linux/amd64 linux/arm64' RELEASE_KIND=image RELEASE_RUNNER=windows \
        RELEASE_MANIFEST=example.json 'RELEASE_ASSETS=src/install.ps1 example.json' \
        'VERSION_COPY=share/example/VERSION|X.Y.Z' SEAL=yes 'PRIVACY_RULES=base disk vm' \
        "PRIVACY_ALLOW=home-path ^tests/ ^$SYN_HOME$" \
        "PRIVACY_HISTORY_ACCEPT=$(printf '%040d' 0 | tr 0 a) e-mail" MAX_BLOB_KB=2048
    run_conf --check "$FILE"
    [ "$status" -eq 0 ]
    [ -z "$stderr" ]
}

@test "an unknown key, a line that is not KEY=value and a key set twice are refused by line" {
    write_config BOGUS=1 'not a setting' CI_WEEKLY=yes CI_WEEKLY=no '# a comment' ''
    run_conf --check "$FILE"
    [ "$status" -eq 1 ]
    [[ $stderr == *"conf: line 4: the key BOGUS is unknown"* ]]
    [[ $stderr == *"conf: line 5: not a KEY=value line"* ]]
    [[ $stderr == *"conf: line 7: CI_WEEKLY is set twice"* ]]
    [ "$(grep -c '^conf: ' <<< "$stderr")" -eq 3 ]
}

@test "NAME, VISIBILITY and RELEASE_KIND are required" {
    printf '# empty\n' > "$BATS_TEST_TMPDIR/empty.conf"
    run_conf --check "$BATS_TEST_TMPDIR/empty.conf"
    [ "$status" -eq 1 ]
    [[ $stderr == *"NAME is required"* ]]
    [[ $stderr == *"VISIBILITY is required"* ]]
    [[ $stderr == *"RELEASE_KIND is required"* ]]
}

@test "each key refuses a value outside its form" {
    local bad
    for bad in NAME=Example_Name VISIBILITY=internal ROLE=owner LICENSE=GPL CI_RUNNER=macos \
        'CI_STAGES=test test' 'CI_STAGES=test  lint' CI_STAGES=deploy CI_WEEKLY=maybe PYTHON=2.7 \
        PS_MODULES=pester-extra IMAGE_PLATFORMS=linux/386 RELEASE_KIND=deb 'RELEASE_KIND=zip tar' \
        RELEASE_RUNNER=mac RELEASE_MANIFEST=dir/file.json RELEASE_ASSETS=../outside \
        RELEASE_ASSETS=/abs 'VERSION_COPY=file' 'VERSION_COPY=../f|X.Y.Z' \
        'VERSION_COPY=f|X.Y.Z X.Y.Z' SEAL=maybe 'PRIVACY_RULES=base extra' MAX_BLOB_KB=0 \
        MAX_BLOB_KB=12345678 MAX_BLOB_KB=1k; do
        write_config "$bad"
        run_conf --check "$FILE"
        if [[ $status -ne 1 || $stderr != *"conf: line "*": ${bad%%=*}: expected"* ]]; then
            printf 'not refused as expected: %s\n%s\n' "$bad" "$stderr" >&2
            return 1
        fi
    done
}

@test "SEAL defaults to no, and SEAL=yes needs a release kind other than legacy" {
    write_config RELEASE_KIND=legacy
    run_conf --get SEAL "$FILE"
    [ "$status" -eq 0 ]
    [ "$output" = no ]
    write_config SEAL=yes RELEASE_KIND=legacy
    run_conf --check "$FILE"
    [ "$status" -eq 1 ]
    [[ $stderr == *"conf: SEAL=yes needs a RELEASE_KIND other than legacy"* ]]
    [ "$(grep -c '^conf: ' <<< "$stderr")" -eq 1 ]
    write_config SEAL=yes RELEASE_KIND=stack
    run_conf --get SEAL "$FILE"
    [ "$status" -eq 0 ]
    [ "$output" = yes ]
}

@test "a CR line end, a trailing space and a tab are refused" {
    mkdir -p "${FILE%/*}"
    printf 'NAME=example\r\nVISIBILITY=private\nRELEASE_KIND=tar \nPYTHON=3.1\t3\n' > "$FILE"
    run_conf --check "$FILE"
    [ "$status" -eq 1 ]
    [[ $stderr == *"line 1: ends with a CR"* ]]
    [[ $stderr == *"line 3: RELEASE_KIND: the value ends with a space"* ]]
    [[ $stderr == *"line 4: PYTHON: the value ends with a space or holds a tab"* ]]
}

@test "PRIVACY_ALLOW needs a pack kind other than denylist and an anchored value ERE" {
    local bad
    for bad in 'denylist ^x/ ^value$' 'no-such-kind ^x/ ^value$' 'home-path ^x/' \
        "home-path ^x/ $SYN_HOME$" "home-path ^x/ ^$SYN_HOME" \
        "home-path ^x/ ^$SYN_HOME\\\$"; do
        write_config "PRIVACY_ALLOW=$bad"
        run_conf --check "$FILE"
        if [[ $status -ne 1 || $stderr != *"PRIVACY_ALLOW: expected"* ]]; then
            printf 'not refused: %s\n%s\n' "$bad" "$stderr" >&2
            return 1
        fi
    done
}

@test "PRIVACY_ALLOW refuses a value ERE that would also exempt a realistic value" {
    local bad
    for bad in "home-path ^tests/ ^$HOME_PREFIX.*$" 'home-path ^tests/ ^[a-z]:[\\/]+users[\\/]+.*$' \
        'ipv4-private ^ ^192\.168\..*$' 'e-mail ^ ^.*@gmail\.com$' \
        'uuid ^ ^[0-9a-f-]+$' 'disk-by-id ^ ^/dev/disk/by-id/.*$' \
        'mac-address ^ ^..:..:..:..:..:..$' 'host-name ^ ^.*\.lan$'; do
        write_config "PRIVACY_ALLOW=$bad"
        run_conf --check "$FILE"
        if [[ $status -ne 1 || $stderr != *"the value ERE matches a realistic"* ]]; then
            printf 'not refused: %s\n%s\n' "$bad" "$stderr" >&2
            return 1
        fi
    done
    write_config "PRIVACY_ALLOW=home-path ^tests/ ^$SYN_HOME$" \
        'PRIVACY_ALLOW=ipv4-private ^compose\.yaml$ ^172\.30\.0\.[0-9]+$'
    run_conf --check "$FILE"
    [ "$status" -eq 0 ]
}

@test "PRIVACY_ALLOW refuses an ERE that does not compile" {
    # An unterminated bracket expression: balanced parentheses, but grep cannot compile it.
    write_config "PRIVACY_ALLOW=home-path ^tests/[ ^$SYN_HOME$"
    run_conf --check "$FILE"
    [ "$status" -eq 1 ]
    [[ $stderr == *"PRIVACY_ALLOW: an ERE does not compile"* ]]
}

@test "PRIVACY_HISTORY_ACCEPT needs a full commit SHA and a pack kind" {
    local sha bad
    sha=$(printf '%040d' 7)
    write_config "PRIVACY_HISTORY_ACCEPT=$sha e-mail" "PRIVACY_HISTORY_ACCEPT=$sha${sha:0:24} uuid"
    run_conf --check "$FILE"
    [ "$status" -eq 0 ]
    for bad in "${sha:0:12} e-mail" "$sha denylist" "$sha no-such-kind" "$sha" "$sha e-mail extra" \
        "${sha^^}x e-mail"; do
        write_config "PRIVACY_HISTORY_ACCEPT=$bad"
        run_conf --check "$FILE"
        if [[ $status -ne 1 ]]; then
            printf 'not refused: %s\n' "$bad" >&2
            return 1
        fi
    done
}

@test "no message shows a value from the file" {
    write_config NAME=Example_5ca1ab1e PYTHON=five-ca1ab1e "PRIVACY_ALLOW=home-path ^x/ ^$HOME_PREFIX.*5ca1ab1e.*$"
    run_conf --check "$FILE"
    [ "$status" -eq 1 ]
    [[ $stderr != *5ca1ab1e* && $stderr != *5CA1AB1E* ]]
}

@test "usage errors exit 64, and a missing file exits 2 without its path" {
    run_conf
    [ "$status" -eq 64 ]
    run_conf --bogus
    [ "$status" -eq 64 ]
    run_conf --get
    [ "$status" -eq 64 ]
    run_conf --get NO_SUCH_KEY "$FILE"
    [ "$status" -eq 64 ]
    run_conf --defaults extra
    [ "$status" -eq 64 ]
    run_conf --check "$BATS_TEST_TMPDIR/missing-5ca1ab1e.conf"
    [ "$status" -eq 2 ]
    [[ $stderr != *missing-5ca1ab1e* ]]
}

@test "conf reads the config from stdin with -" {
    write_config
    run --separate-stderr sh -c 'sh "$1" --get NAME - < "$2"' sh "$CONF" "$FILE"
    [ "$status" -eq 0 ]
    [ "$output" = example ]
}

@test "conf runs under dash" {
    need command dash
    write_config "PRIVACY_ALLOW=home-path ^tests/ ^$SYN_HOME$"
    run --separate-stderr dash "$CONF" --get PRIVACY_ALLOW "$FILE"
    [ "$status" -eq 0 ]
    [ "$output" = "home-path ^tests/ ^$SYN_HOME$" ]
}

@test "PRIVACY_ALLOW refuses a | outside parentheses, unbalanced ones, and a path ERE without ^" {
    local bad
    # GNU grep takes an unmatched ) as a literal, so ^a)|b$ would compile with b$ unanchored.
    # Escaped parentheses and parentheses in a bracket expression are literals too: the | in
    # ^\(a|b\)$ and in ^a[)]|b$ sits outside every group.
    for bad in "home-path ^x/ ^$SYN_HOME\$|b\$" "home-path ^x/ ^$SYN_HOME)|b\$" \
        "home-path ^x/ ^\\($SYN_HOME|b\\)\$" "home-path ^x/ ^${SYN_HOME}[)]|b\$" \
        "home-path ^x/|^y/ ^$SYN_HOME\$" "home-path ^x)|y/ ^$SYN_HOME\$" \
        "home-path x/ ^$SYN_HOME\$"; do
        write_config "PRIVACY_ALLOW=$bad"
        run_conf --check "$FILE"
        if [[ $status -ne 1 || $stderr != *"PRIVACY_ALLOW: expected"* ]]; then
            printf 'not refused: %s\n%s\n' "$bad" "$stderr" >&2
            return 1
        fi
    done
    write_config "PRIVACY_ALLOW=home-path ^(x|y)/ ^$HOME_PREFIX(example-user|example-admin)\$" \
        'PRIVACY_ALLOW=ipv4-private ^compose\.yaml$ ^172\.30\.0\.[0-9|]+$'
    run_conf --check "$FILE"
    [ "$status" -eq 0 ]
}

@test "--allow-lines prints the PRIVACY_ALLOW lines of a config that parses, and nothing otherwise" {
    write_config "PRIVACY_ALLOW=home-path ^tests/ ^$SYN_HOME\$" PRIVACY_RULES=base
    run_conf --allow-lines "$FILE"
    [ "$status" -eq 0 ]
    [ "$output" = "4 home-path" ]
    write_config "PRIVACY_ALLOW=home-path ^tests/ $SYN_HOME\$"
    run_conf --allow-lines "$FILE"
    [ "$status" -eq 1 ]
    [ -z "$output" ]
}

@test "PRIVACY_ALLOW refuses a path ERE that holds a value a rule pack matches" {
    write_config "$(printf 'PRIVACY_ALLOW=ipv4-private ^%s.%s.7.9 ^x$' 192 168)"
    run_conf --check "$FILE"
    [ "$status" -eq 1 ]
    [[ $stderr == *"the path ERE holds a value a rule pack matches"* ]]
    # A synthetic value that the rule's own exempt ERE covers passes, as it does in a scan.
    write_config "$(printf 'PRIVACY_ALLOW=home-path ^tests/fixtures/%s-5ca1-4000-8000-%s0001/ ^/%s/example-user$' \
        5ca1ab1e 5ca1ab1e home)"
    run_conf --check "$FILE"
    [ "$status" -eq 0 ] || { printf '%s\n' "$stderr" >&2; return 1; }
}

# Run conf --ci in $REPO through bats `run` with the event settings in the environment that come
# as arguments (EVENT=... WORKFLOW=...), stderr apart.
run_ci() {
    run --separate-stderr env "$@" sh -c 'cd "$1" && exec sh "$2" --ci' sh "$REPO" "$CONF"
}

# Print the value of output KEY of the last run_ci.
out() {
    awk -v k="$1" 'index($0, k "=") == 1 { print substr($0, length(k) + 2); exit }' <<< "$output"
}

@test "--ci prints the jobs of a public consumer on a pull request, as lowercase key=value lines" {
    write_config VISIBILITY=public 'CI_STAGES=lint test' PYTHON=3.13 'PS_MODULES=pester' \
        'IMAGE_PLATFORMS=linux/amd64 linux/arm64'
    run_ci EVENT=pull_request WORKFLOW=ci PRIVATE=false
    [ "$status" -eq 0 ] || { printf '%s\n' "$stderr" >&2; return 1; }
    [ "$(out runner)" = ubuntu-26.04 ]
    [ "$(out stages)" = "lint test" ]
    [ "$(out lint)" = true ] && [ "$(out test)" = true ] && [ "$(out system)" = false ]
    [ "$(out python)" = 3.13 ]
    [ "$(out ps-modules)" = pester ]
    [ "$(out platforms)" = linux/amd64,linux/arm64 ]
    [ "$(out image)" = true ]
    [ "$(out windows)" = true ]
    [ "$(out full-suite)" = true ]
    [ "$(out required)" = "lint test image hooks-windows" ]
    [ "$(out kind)" = tar ]
    [ "$(out release-runner)" = ubuntu-26.04 ]
    [ "$(out public)" = true ]
    [ "$(out rehearse)" = false ]
    run grep -v -E '^[a-z][a-z-]*=' <<< "$output"
    [ "$status" -eq 1 ]
}

@test "--ci runs the stages and the image on schedule only with CI_WEEKLY=yes, and hooks-windows never" {
    write_config VISIBILITY=public 'CI_STAGES=test system' 'IMAGE_PLATFORMS=linux/amd64'
    run_ci EVENT=schedule WORKFLOW=ci PRIVATE=false
    [ "$(out stages)" = "" ]
    [ "$(out test)" = false ] && [ "$(out system)" = false ] && [ "$(out image)" = false ]
    [ "$(out windows)" = false ]
    [ "$(out full-suite)" = true ]
    [ "$(out required)" = "" ]
    write_config VISIBILITY=public 'CI_STAGES=test system' 'IMAGE_PLATFORMS=linux/amd64' CI_WEEKLY=yes
    run_ci EVENT=schedule WORKFLOW=ci PRIVATE=false
    [ "$(out stages)" = "test system" ]
    [ "$(out required)" = "test system image" ]
    [ "$(out windows)" = false ]
}

@test "--ci maps the runners, builds no image for release.yml, and sets public and rehearse narrowly" {
    write_config VISIBILITY=public CI_RUNNER=windows RELEASE_RUNNER=windows 'IMAGE_PLATFORMS=linux/amd64'
    run_ci EVENT=push WORKFLOW=release PRIVATE=true
    [ "$(out runner)" = windows-2025 ]
    [ "$(out release-runner)" = windows-2025 ]
    [ "$(out image)" = false ]
    # A config that says public in a repository GitHub calls private is not public.
    [ "$(out public)" = false ]
    write_config VISIBILITY=private RELEASE_KIND=image 'IMAGE_PLATFORMS=linux/amd64'
    run_ci EVENT=workflow_dispatch WORKFLOW=release PRIVATE=true REHEARSE=true
    [ "$(out rehearse)" = true ]
    [ "$(out windows)" = false ]
    run_ci EVENT=workflow_dispatch WORKFLOW=release PRIVATE=true REHEARSE=false
    [ "$(out rehearse)" = false ]
    write_config VISIBILITY=private RELEASE_KIND=tar
    run_ci EVENT=workflow_dispatch WORKFLOW=release PRIVATE=true REHEARSE=true
    [ "$(out rehearse)" = false ]
}

@test "--ci runs the full suite of a private repository only for a change to what the MANIFEST lists" {
    local base oid
    make_repo
    commit_config VISIBILITY=private
    stage .githooks/x '# synthetic copy'
    oid=$(git -C "$REPO" rev-parse :.githooks/x)
    stage .github/standard/MANIFEST "$(printf 'standard 1.0.0\nfile all %s 100644 .githooks/x\nblock private %s - SECURITY.md#reporting@private' "$oid" "$oid")"
    stage SECURITY.md 'synthetic'
    git -C "$REPO" -c core.hooksPath=no-hooks commit -q -m "chore: adopt"
    base=$(git -C "$REPO" rev-parse HEAD)
    stage notes.txt 'own file'
    git -C "$REPO" -c core.hooksPath=no-hooks commit -q -m "docs: notes"
    run_ci EVENT=pull_request WORKFLOW=ci PRIVATE=true BASE="$base"
    [ "$(out full-suite)" = false ]
    [ "$(out windows)" = false ]
    stage .githooks/x '# changed copy'
    git -C "$REPO" -c core.hooksPath=no-hooks commit -q -m "chore: change a copy"
    run_ci EVENT=pull_request WORKFLOW=ci PRIVATE=true BASE="$base"
    [ "$(out full-suite)" = true ]
    git -C "$REPO" switch -q -c docs/other "$base"
    stage SECURITY.md 'changed block target'
    git -C "$REPO" -c core.hooksPath=no-hooks commit -q -m "docs: security"
    run_ci EVENT=push WORKFLOW=ci PRIVATE=true BASE="$base"
    [ "$(out full-suite)" = true ]
    # A first push, whose before is all zeros, cannot tell what changed.
    run_ci EVENT=push WORKFLOW=ci PRIVATE=true BASE=0000000000000000000000000000000000000000
    [ "$(out full-suite)" = true ]
    git -C "$REPO" switch -q -c docs/own "$base"
    run_ci EVENT=workflow_dispatch WORKFLOW=ci PRIVATE=true BASE="$base"
    [ "$(out full-suite)" = true ]
}

@test "--ci prints nothing on stdout for a config that does not parse, also under dash" {
    local shell
    write_config 'BOGUS=1'
    for shell in sh dash; do
        command -v "$shell" > /dev/null 2>&1 || continue
        run --separate-stderr env EVENT=push "$shell" -c 'cd "$1" && exec "$2" "$3" --ci' sh "$REPO" "$shell" "$CONF"
        [ "$status" -eq 1 ]
        [ -z "$output" ]
    done
}

@test "--samples prints realistic values of a kind, and fails for a kind it does not know" {
    run_conf --samples ipv4-private
    [ "$status" -eq 0 ]
    [ "$(awk 'END { print NR }' <<< "$output")" -ge 2 ]
    run_conf --samples no-such-kind
    [ "$status" -eq 1 ]
    run_conf --samples
    [ "$status" -eq 64 ]
}
