#!/usr/bin/env bats
# standard-check.bats of the repository standard: the same file in every repository; never edit
# it here. standard-check's rules against good and bad fixture repositories: the copies, blocks
# and modes the MANIFEST lists, the owned files' forms, removed paths, the pull request rules on
# copies and loosening, ROLE=source, --local, and the pre-commit hook's call of --staged.

# The sh -c scripts are single-quoted, and bats runs each test in a subshell, both on purpose.
# shellcheck disable=SC2016,SC2030,SC2031
load lib

setup() {
    isolate_home
    # Synthetic block bodies, so that the suite runs in every repository of the standard.
    BODIES="$BATS_TEST_TMPDIR/bodies"
    mkdir -p "$BODIES/_github/dependabot.yml" "$BODIES/SECURITY.md"
    printf '%s\n' '# Synthetic Dependabot head.' 'version: 2' 'updates:' \
        > "$BODIES/_github/dependabot.yml/dependabot.yml"
    printf '%s\n' '## Reporting' '' 'Do not open a public issue.' > "$BODIES/SECURITY.md/reporting@public.md"
    printf '%s\n' '## Reporting' '' 'Report to the maintainer.' > "$BODIES/SECURITY.md/reporting@private.md"
}

# Run standard-check MODE in $REPO through bats `run`, stderr apart.
check() {
    run --separate-stderr sh -c 'cd "$1" && exec sh .github/standard/bin/standard-check "$2"' \
        sh "$REPO" "$1"
}

# Print a block: its begin marker, the body file BODY of the synthetic bodies, its end
# marker. NAME and STYLE (md or hash) pick the markers.
block() {
    local name=$1 style=$2 body=$3
    if [[ $style == md ]]; then
        printf '<!-- standard:begin %s -->\n' "$name"
        cat "$BODIES/$body"
        printf '<!-- standard:end %s -->\n' "$name"
    else
        printf '# standard:begin %s\n' "$name"
        cat "$BODIES/$body"
        printf '# standard:end %s\n' "$name"
    fi
}

# Print the blob id of the synthetic block body FILE.
body_id() {
    git hash-object --no-filters "$BODIES/$1"
}

# Write and stage $REPO's MANIFEST: every copy under .githooks/ and .github/standard/, LICENSE
# (profile mit), the release workflow when present (profile release), and three blocks.
write_manifest() {
    local mode oid path profile
    {
        printf '# Repository standard 1.0.0. Written by tools/manifest; never edit it.\n'
        printf 'standard 1.0.0\n'
        while read -r mode oid _ path; do
            case $path in
                .github/standard/MANIFEST) continue ;;
                LICENSE) profile=mit ;;
                .github/workflows/release.yml) profile=release ;;
                *) profile=all ;;
            esac
            printf 'file %s %s %s %s\n' "$profile" "$oid" "$mode" "$path"
        done < <(git -C "$REPO" ls-files -s -- .githooks .github/standard .github/workflows LICENSE)
        printf 'block all %s - .github/dependabot.yml#dependabot\n' \
            "$(body_id _github/dependabot.yml/dependabot.yml)"
        printf 'block public %s - SECURITY.md#reporting@public\n' "$(body_id SECURITY.md/reporting@public.md)"
        printf 'block private %s - SECURITY.md#reporting@private\n' "$(body_id SECURITY.md/reporting@private.md)"
    } > "$REPO/.github/standard/MANIFEST"
    git -C "$REPO" add .github/standard/MANIFEST
}

# Commit what is staged in $REPO with the hooks off.
commit_unchecked() {
    git -C "$REPO" -c core.hooksPath=no-hooks commit -q -m "$1"
}

# Add three blocks to $REPO's MANIFEST and stage it: a releasing block (profile release), its
# seal variant, and a block that exists only as a seal variant (both profile seal).
add_seal_blocks() {
    mkdir -p "$BODIES/CONTRIBUTING.md"
    printf '%s\n' '## Releasing' '' 'Publish in the browser.' > "$BODIES/CONTRIBUTING.md/releasing.md"
    printf '%s\n' '## Releasing' '' 'Publish in the browser, then seal.' \
        > "$BODIES/CONTRIBUTING.md/releasing@seal.md"
    printf '%s\n' '## Sealed releases' '' 'Hosts install a sealed release only.' \
        > "$BODIES/SECURITY.md/sealed-releases@seal.md"
    {
        printf 'block release %s - CONTRIBUTING.md#releasing\n' "$(body_id CONTRIBUTING.md/releasing.md)"
        printf 'block seal %s - CONTRIBUTING.md#releasing@seal\n' \
            "$(body_id CONTRIBUTING.md/releasing@seal.md)"
        printf 'block seal %s - SECURITY.md#sealed-releases@seal\n' \
            "$(body_id SECURITY.md/sealed-releases@seal.md)"
    } >> "$REPO/.github/standard/MANIFEST"
    git -C "$REPO" add .github/standard/MANIFEST
}

# Build a public repository at $REPO that holds the standard, committed on main, and switch to
# feat/example. The copies are this repository's hooks and bin/.
make_consumer() {
    make_repo
    write_config VISIBILITY=public PRIVACY_RULES=base
    printf '1.0.0\n' > "$REPO/VERSION"
    printf 'Synthetic licence text.\n' > "$REPO/LICENSE"
    mkdir -p "$REPO/ci"
    printf '#!/bin/sh\nexit 0\n' > "$REPO/ci/test"
    { block dependabot hash _github/dependabot.yml/dependabot.yml
      printf '  - package-ecosystem: github-actions\n    directory: /\n'
      printf '    schedule:\n      interval: weekly\n    open-pull-requests-limit: 0\n'; } \
        > "$REPO/.github/dependabot.yml"
    { printf '# Security\n\n'; block reporting md SECURITY.md/reporting@public.md; } > "$REPO/SECURITY.md"
    git -C "$REPO" add -A
    stage_executables
    write_manifest
    commit_unchecked "chore: adopt the standard"
    git -C "$REPO" switch -q -c feat/example
}

@test "a repository that holds the standard passes --ci and --staged" {
    make_consumer
    check --ci
    [ "$status" -eq 0 ]
    [ "$output" = "standard-check: ok" ]
    check --staged
    [ "$status" -eq 0 ]
}

@test "a changed copy, a wrong mode and a missing copy fail" {
    make_consumer
    printf '# edited\n' >> "$REPO/.githooks/pre-push"
    git -C "$REPO" add .githooks/pre-push
    git -C "$REPO" update-index --chmod=-x .githooks/commit-msg
    git -C "$REPO" rm -q --cached .githooks/rules/vm
    check --staged
    [ "$status" -eq 1 ]
    [[ $stderr == *".githooks/pre-push: differs from the MANIFEST"* ]]
    [[ $stderr == *".githooks/commit-msg: mode 100644, the MANIFEST says 100755"* ]]
    [[ $stderr == *".githooks/rules/vm: missing (the MANIFEST lists it)"* ]]
}

@test "a file of the standard's folders that the MANIFEST does not list fails" {
    make_consumer
    mkdir -p "$REPO/.github/workflows"
    printf 'name: own\n' > "$REPO/.github/workflows/own.yml"
    printf '#!/bin/sh\n' > "$REPO/.githooks/post-checkout"
    git -C "$REPO" add .github/workflows/own.yml .githooks/post-checkout
    check --staged
    [ "$status" -eq 1 ]
    [[ $stderr == *".github/workflows/own.yml: not in the MANIFEST"* ]]
    [[ $stderr == *".githooks/post-checkout: not in the MANIFEST"* ]]
}

@test "a block that differs, is missing or appears twice fails, and its variant follows VISIBILITY" {
    make_consumer
    sed -i 's/Do not open/Never open/' "$REPO/SECURITY.md"
    git -C "$REPO" add SECURITY.md
    check --staged
    [[ $stderr == *"SECURITY.md#reporting@public: the block differs from the MANIFEST"* ]]
    printf '# Security\n' > "$REPO/SECURITY.md"
    git -C "$REPO" add SECURITY.md
    check --staged
    [[ $stderr == *"SECURITY.md#reporting@public: the block's markers are missing"* ]]
    { block reporting md SECURITY.md/reporting@public.md; block reporting md SECURITY.md/reporting@public.md; } \
        > "$REPO/SECURITY.md"
    git -C "$REPO" add SECURITY.md
    check --staged
    [[ $stderr == *"the block's markers appear more than once or out of order"* ]]
    # A private repository needs the private variant.
    block reporting md SECURITY.md/reporting@public.md > "$REPO/SECURITY.md"
    write_config VISIBILITY=private PRIVACY_RULES=base
    git -C "$REPO" add SECURITY.md .github/standard.conf
    check --staged
    [ "$status" -eq 1 ]
    [[ $stderr == *"SECURITY.md#reporting@private: the block differs from the MANIFEST"* ]]
    block reporting md SECURITY.md/reporting@private.md > "$REPO/SECURITY.md"
    git -C "$REPO" add SECURITY.md
    check --staged
    [ "$status" -eq 0 ]
}

@test "under RELEASE_KIND=legacy a release workflow needs only to exist" {
    make_consumer
    mkdir -p "$REPO/.github/workflows"
    printf 'name: release\n' > "$REPO/.github/workflows/release.yml"
    git -C "$REPO" add .github/workflows/release.yml
    write_manifest
    commit_unchecked "ci: add the release workflow"
    printf 'name: release\non: push\n' > "$REPO/.github/workflows/release.yml"
    git -C "$REPO" add .github/workflows/release.yml
    check --staged
    [ "$status" -eq 1 ]
    [[ $stderr == *".github/workflows/release.yml: differs from the MANIFEST"* ]]
    write_config VISIBILITY=public PRIVACY_RULES=base RELEASE_KIND=legacy
    git -C "$REPO" add .github/standard.conf
    check --staged
    [ "$status" -eq 0 ]
}

@test "SEAL=yes takes a block's seal variant and needs a seal-only block, which SEAL=no refuses" {
    make_consumer
    add_seal_blocks
    block releasing md CONTRIBUTING.md/releasing.md > "$REPO/CONTRIBUTING.md"
    git -C "$REPO" add CONTRIBUTING.md
    check --staged
    [ "$status" -eq 0 ] || { printf '%s\n' "$stderr" >&2; return 1; }
    # Without SEAL=yes, a block that exists only as a seal variant must not be there.
    { printf '# Security\n\n'; block reporting md SECURITY.md/reporting@public.md
      block sealed-releases md SECURITY.md/sealed-releases@seal.md; } > "$REPO/SECURITY.md"
    git -C "$REPO" add SECURITY.md
    check --staged
    [ "$status" -eq 1 ]
    [[ $stderr == *"SECURITY.md#sealed-releases@seal: the block is here, but the config does not set SEAL=yes"* ]]
    # Under SEAL=yes the seal variant replaces the releasing block, and the seal-only block counts.
    write_config VISIBILITY=public PRIVACY_RULES=base SEAL=yes
    git -C "$REPO" add .github/standard.conf
    check --staged
    [ "$status" -eq 1 ]
    [[ $stderr == *"CONTRIBUTING.md#releasing@seal: the block differs from the MANIFEST"* ]]
    [ "$(grep -c '^standard-check: [A-Z]' <<< "$stderr")" -eq 1 ]
    block releasing md CONTRIBUTING.md/releasing@seal.md > "$REPO/CONTRIBUTING.md"
    git -C "$REPO" add CONTRIBUTING.md
    check --staged
    [ "$status" -eq 0 ] || { printf '%s\n' "$stderr" >&2; return 1; }
    { printf '# Security\n\n'; block reporting md SECURITY.md/reporting@public.md; } > "$REPO/SECURITY.md"
    git -C "$REPO" add SECURITY.md
    check --staged
    [ "$status" -eq 1 ]
    [[ $stderr == *"SECURITY.md#sealed-releases@seal: the block's markers are missing"* ]]
}

@test "LICENSE follows the LICENSE key" {
    make_consumer
    write_config VISIBILITY=public PRIVACY_RULES=base LICENSE=none
    git -C "$REPO" add .github/standard.conf
    check --staged
    [[ $stderr == *"LICENSE: exists, but the config says LICENSE=none"* ]]
    git -C "$REPO" rm -q --cached LICENSE
    check --staged
    [ "$status" -eq 0 ]
    [[ $stderr != *"LICENSE:"* ]]
}

@test "VERSION and VERSION_COPY need their form" {
    local bad
    make_consumer
    for bad in '01.0.0' '1.0' '1.0.0-rc1' $'1.0.0\r' $'\357\273\2771.0.0' $'1.0.0\nmore'; do
        printf '%s\n' "$bad" > "$REPO/VERSION"
        git -C "$REPO" add VERSION
        check --staged
        [[ $stderr == *"VERSION: not one line X.Y.Z or X.Y.Z-dev"* ]] ||
            { printf 'not refused: %q\n' "$bad" >&2; return 1; }
    done
    printf '1.1.0-dev\n' > "$REPO/VERSION"
    printf 'version=1.0.0\n' > "$REPO/version.txt"
    write_config VISIBILITY=public PRIVACY_RULES=base 'VERSION_COPY=version.txt|version=X.Y.Z'
    git -C "$REPO" add VERSION version.txt .github/standard.conf
    check --staged
    # The entry is named by its place in the config, never by its path: a commit made with the
    # hooks off could have put a term into that path.
    [[ $stderr == *"VERSION_COPY entry 1: its file does not hold the line for 1.1.0-dev"* ]]
    [[ $stderr != *version.txt* ]]
    git -C "$REPO" rm -q --cached version.txt
    check --staged
    [[ $stderr == *"VERSION_COPY entry 1: its file is missing"* ]]
    printf 'version=1.1.0-dev\n' > "$REPO/version.txt"
    git -C "$REPO" add version.txt
    check --staged
    [ "$status" -eq 0 ]
}

@test "--ci notes on a push when GitHub did not say whether the repository is private" {
    make_consumer
    EVENT=push run --separate-stderr sh -c 'cd "$1" && exec sh .github/standard/bin/standard-check --ci' \
        sh "$REPO"
    [ "$status" -eq 0 ]
    [[ $stderr == *"notice: GitHub did not say whether this repository is private"* ]]
    EVENT=push PRIVATE=false run --separate-stderr sh -c \
        'cd "$1" && exec sh .github/standard/bin/standard-check --ci' sh "$REPO"
    [ "$status" -eq 0 ]
    [[ $stderr != *"did not say"* ]]
}

@test "dependabot.yml needs the github-actions entry for / at limit 0" {
    make_consumer
    sed -i 's/open-pull-requests-limit: 0/open-pull-requests-limit: 5/' "$REPO/.github/dependabot.yml"
    git -C "$REPO" add .github/dependabot.yml
    check --staged
    [[ $stderr == *"the github-actions entry needs open-pull-requests-limit: 0"* ]]
    sed -i 's/package-ecosystem: github-actions/package-ecosystem: npm/' "$REPO/.github/dependabot.yml"
    git -C "$REPO" add .github/dependabot.yml
    check --staged
    [[ $stderr == *"no github-actions entry for /"* ]]
}

@test "a stage needs its ci/ script, and tests/helpers.bash must source lib.bash" {
    make_consumer
    write_config VISIBILITY=public PRIVACY_RULES=base 'CI_STAGES=lint test'
    mkdir -p "$REPO/tests"
    printf '# shellcheck shell=bash\n' > "$REPO/tests/helpers.bash"
    git -C "$REPO" add .github/standard.conf tests/helpers.bash
    check --staged
    [[ $stderr == *"ci/lint or ci/lint.ps1: missing (CI_STAGES lists lint)"* ]]
    [[ $stderr == *"tests/helpers.bash: does not source .github/standard/tests/lib.bash"* ]]
    printf '#!/bin/sh\nexit 0\n' > "$REPO/ci/lint.ps1"
    printf 'source "${BASH_SOURCE[0]%%/*}/../.github/standard/tests/lib.bash"\n' > "$REPO/tests/helpers.bash"
    git -C "$REPO" add ci/lint.ps1 tests/helpers.bash
    check --staged
    [ "$status" -eq 0 ]
}

@test "gitleaks' inline allow marker and a removed path fail" {
    make_consumer
    printf 'key = 1 # %s:%s\n' gitleaks allow > "$REPO/notes.txt"
    printf '[extend]\n' > "$REPO/.gitleaks.toml"
    git -C "$REPO" add notes.txt .gitleaks.toml
    check --staged
    [ "$status" -eq 1 ]
    [[ $stderr == *"notes.txt: holds gitleaks' inline allow marker"* ]]
    [[ $stderr == *".gitleaks.toml: removed by the standard; delete it"* ]]
}

@test "--ci compares VISIBILITY with GitHub" {
    make_consumer
    PRIVATE=true check --ci
    [ "$status" -eq 1 ]
    [[ $stderr == *"VISIBILITY=public, but GitHub says private"* ]]
    PRIVATE=false check --ci
    [ "$status" -eq 0 ]
}

@test "--ci on a pull request: a copy changes only on a chore/standard-* branch of this repository" {
    local base
    make_consumer
    base=$(git -C "$REPO" rev-parse main)
    printf '# edited\n' >> "$REPO/.githooks/pre-push"
    git -C "$REPO" add .githooks/pre-push
    write_manifest
    commit_unchecked "chore: change a copy"
    export EVENT=pull_request BASE=$base REPO_NAME=owner/example
    REPO_SLUG=owner/example
    HEAD_REF=feat/example HEAD_REPO=$REPO_SLUG REPO=$REPO_SLUG run --separate-stderr sh -c \
        'cd "$1" && exec sh .github/standard/bin/standard-check --ci' sh "$BATS_TEST_TMPDIR/repo"
    [ "$status" -eq 1 ]
    [[ $stderr == *"a copy of the standard changes outside a chore/standard-* branch"* ]]
    HEAD_REF=chore/standard-1.0.1 HEAD_REPO=someone/example REPO=$REPO_SLUG run --separate-stderr \
        sh -c 'cd "$1" && exec sh .github/standard/bin/standard-check --ci' sh "$BATS_TEST_TMPDIR/repo"
    [ "$status" -eq 1 ]
    [[ $stderr == *"changes on a branch of another repository"* ]]
    HEAD_REF=chore/standard-1.0.1 HEAD_REPO=$REPO_SLUG REPO=$REPO_SLUG run --separate-stderr \
        sh -c 'cd "$1" && exec sh .github/standard/bin/standard-check --ci' sh "$BATS_TEST_TMPDIR/repo"
    [ "$status" -eq 0 ]
}

@test "--ci on a pull request: loosening needs a chore/privacy-* pull request of its own" {
    local base dir=$BATS_TEST_TMPDIR/repo
    make_consumer
    base=$(git -C "$REPO" rev-parse main)
    write_config VISIBILITY=public PRIVACY_RULES=
    git -C "$REPO" add .github/standard.conf
    commit_unchecked "chore: drop the base pack"
    export EVENT=pull_request BASE=$base
    HEAD_REF=feat/example HEAD_REPO=o/r REPO=o/r run --separate-stderr sh -c \
        'cd "$1" && exec sh .github/standard/bin/standard-check --ci' sh "$dir"
    [ "$status" -eq 1 ]
    [[ $stderr == *"loosens a check (a dropped rule pack"* ]]
    HEAD_REF=chore/privacy-drop HEAD_REPO=o/r REPO=o/r run --separate-stderr sh -c \
        'cd "$1" && exec sh .github/standard/bin/standard-check --ci' sh "$dir"
    [ "$status" -eq 0 ]
    printf 'notes\n' > "$dir/notes.txt"
    git -C "$dir" add notes.txt
    git -C "$dir" -c core.hooksPath=no-hooks commit -q -m "docs: add notes"
    HEAD_REF=chore/privacy-drop HEAD_REPO=o/r REPO=o/r run --separate-stderr sh -c \
        'cd "$1" && exec sh .github/standard/bin/standard-check --ci' sh "$dir"
    [ "$status" -eq 1 ]
    [[ $stderr == *"may change only .github/standard.conf, .gitleaksignore and CHANGELOG.md"* ]]
}

@test "pre-commit refuses a staged copy that differs from the MANIFEST" {
    make_consumer
    printf '# edited\n' >> "$REPO/.githooks/rules/vm"
    git -C "$REPO" add .githooks/rules/vm
    try_commit "chore: edit a copy"
    [ "$status" -ne 0 ]
    [[ $output == *".githooks/rules/vm: differs from the MANIFEST"* ]]
}

@test "--local reports the hooks and the denylist counts, and fails when the hooks are off" {
    make_consumer
    write_denylist
    check --local
    [ "$status" -eq 0 ]
    [[ $output == *"standard-check: hooks: on"* ]]
    [[ $output == *"denylists: 1 source(s), 1 term(s), 0 missing, 0 with NUL bytes"* ]]
    refute_term_shown
    git -C "$REPO" config --unset core.hooksPath
    check --local
    [ "$status" -eq 1 ]
    [[ $stderr == *"the hooks are off here"* ]]
}

@test "--local warns about a remote of another owner, as in a fork clone" {
    make_consumer
    write_denylist
    git -C "$REPO" remote add origin https://github.com/someone/example.git
    git -C "$REPO" remote add upstream https://github.com/owner/example.git
    check --local
    [[ $stderr == *"warning: remote upstream is example of another owner than origin"* ]]
}

@test "--local gives a notice for a tracked blob above the limit in HEAD, and the exit stays" {
    local short
    make_consumer
    write_denylist
    write_config VISIBILITY=public PRIVACY_RULES=base MAX_BLOB_KB=1
    head -c 2048 /dev/zero | tr '\0' a > "$REPO/large-name.dat"
    git -C "$REPO" add -A
    commit_unchecked "feat: a large file"
    short=$(git -C "$REPO" rev-parse --short=12 HEAD:large-name.dat)
    check --local
    [ "$status" -eq 0 ]
    [[ $stderr == *"notice: blob $short is 2 KB, above MAX_BLOB_KB=1."* ]]
    [[ $stderr == *"Find its file with: git ls-files -s | grep $short"* ]]
    [[ $stderr == *"in a commit of its own"* && $stderr == *"chore/privacy-*"* ]]
    [[ $stderr != *large-name* ]]
    check --staged
    [ "$status" -eq 0 ]
    [[ $stderr != *"MAX_BLOB_KB"* ]]
    # A limit raised only in the index does not count: the hooks read it from HEAD.
    write_config VISIBILITY=public PRIVACY_RULES=base MAX_BLOB_KB=1024
    git -C "$REPO" add .github/standard.conf
    check --local
    [ "$status" -eq 0 ]
    [[ $stderr == *"notice: blob $short is 2 KB, above MAX_BLOB_KB=1."* ]]
    commit_unchecked "chore: raise the limit"
    check --local
    [ "$status" -eq 0 ]
    [[ $stderr != *"above MAX_BLOB_KB"* ]]
}

@test "--local says when it cannot read a tracked blob's size, and the exit stays" {
    local oid
    oid=$(printf '%040d' 0 | tr 0 1)
    make_consumer
    write_denylist
    git -C "$REPO" update-index --add --cacheinfo "100644,$oid,ghost-name.dat"
    check --local
    [ "$status" -eq 0 ]
    [[ $stderr == *"notice: blob ${oid:0:12} could not be read, so its size was not compared. Find its file with: git ls-files -s | grep ${oid:0:12}"* ]]
    [[ $stderr != *ghost-name* && $stderr != *"Raise MAX_BLOB_KB"* ]]
}

@test "a staged ROLE=source does not switch off the copy checks" {
    make_consumer
    write_config VISIBILITY=public PRIVACY_RULES=base ROLE=source
    printf '# edited\n' >> "$REPO/.githooks/pre-push"
    git -C "$REPO" add .github/standard.conf .githooks/pre-push
    check --staged
    [ "$status" -eq 1 ]
    [[ $stderr == *".githooks/pre-push: differs from the MANIFEST"* ]]
}

@test "--ci on a pull request: ROLE, MAX_BLOB_KB, RELEASE_KIND=legacy and a broken base are loosening" {
    local base broken dir=$BATS_TEST_TMPDIR/repo
    make_consumer
    base=$(git -C "$REPO" rev-parse main)
    export EVENT=pull_request
    write_config VISIBILITY=public PRIVACY_RULES=base ROLE=source
    printf '# edited\n' >> "$REPO/.githooks/pre-push"
    git -C "$REPO" add .github/standard.conf .githooks/pre-push
    commit_unchecked "chore: become the source"
    BASE=$base HEAD_REF=feat/example HEAD_REPO=o/r REPO=o/r run --separate-stderr sh -c \
        'cd "$1" && exec sh .github/standard/bin/standard-check --ci' sh "$dir"
    [ "$status" -eq 1 ]
    [[ $stderr == *".githooks/pre-push: differs from the MANIFEST"* ]]
    [[ $stderr == *"a copy of the standard changes outside a chore/standard-* branch"* ]]
    [[ $stderr == *"a changed ROLE"* ]]
    git -C "$dir" switch -q -c feat/other "$base"
    write_config VISIBILITY=public PRIVACY_RULES=base MAX_BLOB_KB=4096 RELEASE_KIND=legacy
    git -C "$dir" add .github/standard.conf
    commit_unchecked "chore: loosen"
    BASE=$base HEAD_REF=feat/other HEAD_REPO=o/r REPO=o/r run --separate-stderr sh -c \
        'cd "$1" && exec sh .github/standard/bin/standard-check --ci' sh "$dir"
    [ "$status" -eq 1 ]
    [[ $stderr == *"a raised MAX_BLOB_KB"* ]]
    [[ $stderr == *"RELEASE_KIND=legacy"* ]]
    git -C "$dir" switch -q -c feat/broken "$base"
    printf 'BOGUS=1\n' >> "$dir/.github/standard.conf"
    git -C "$dir" add .github/standard.conf
    commit_unchecked "chore: break the config"
    broken=$(git -C "$dir" rev-parse HEAD)
    write_config VISIBILITY=public PRIVACY_RULES=base
    git -C "$dir" add .github/standard.conf
    commit_unchecked "fix: mend the config"
    BASE=$broken HEAD_REF=feat/broken HEAD_REPO=o/r REPO=o/r run --separate-stderr sh -c \
        'cd "$1" && exec sh .github/standard/bin/standard-check --ci' sh "$dir"
    [ "$status" -eq 1 ]
    [[ $stderr == *"a base config that does not parse"* ]]
}

@test "--ci on a pull request: an exemption, .gitleaksignore and a dropped stage loosen; more packs do not" {
    local base lint dir=$BATS_TEST_TMPDIR/repo
    make_consumer
    base=$(git -C "$REPO" rev-parse main)
    export EVENT=pull_request
    write_config VISIBILITY=public PRIVACY_RULES=base \
        'PRIVACY_ALLOW=ipv4-private ^compose\.yaml$ ^172\.30\.0\.[0-9]+$'
    printf 'synthetic-fingerprint\n' > "$REPO/.gitleaksignore"
    git -C "$REPO" add .github/standard.conf .gitleaksignore
    commit_unchecked "chore: exempt a synthetic address"
    BASE=$base HEAD_REF=feat/example HEAD_REPO=o/r REPO=o/r run --separate-stderr sh -c \
        'cd "$1" && exec sh .github/standard/bin/standard-check --ci' sh "$dir"
    [ "$status" -eq 1 ]
    [[ $stderr == *"an exemption or accept record"* ]]
    [[ $stderr == *".gitleaksignore"* ]]
    BASE=$base HEAD_REF=chore/privacy-exempt HEAD_REPO=someone/r REPO=o/r run --separate-stderr \
        sh -c 'cd "$1" && exec sh .github/standard/bin/standard-check --ci' sh "$dir"
    [ "$status" -eq 1 ]
    [[ $stderr == *"loosening comes from another repository"* ]]
    git -C "$dir" switch -q -c feat/accept "$base"
    write_config VISIBILITY=public PRIVACY_RULES=base "PRIVACY_HISTORY_ACCEPT=$base e-mail"
    git -C "$dir" add .github/standard.conf
    commit_unchecked "chore: accept an old finding"
    BASE=$base HEAD_REF=feat/accept HEAD_REPO=o/r REPO=o/r run --separate-stderr sh -c \
        'cd "$1" && exec sh .github/standard/bin/standard-check --ci' sh "$dir"
    [ "$status" -eq 1 ]
    [[ $stderr == *"an exemption or accept record"* ]]
    git -C "$dir" switch -q -c feat/stages "$base"
    write_config VISIBILITY=public PRIVACY_RULES=base 'CI_STAGES=lint test'
    git -C "$dir" add .github/standard.conf
    commit_unchecked "ci: add the lint stage"
    lint=$(git -C "$dir" rev-parse HEAD)
    write_config VISIBILITY=public PRIVACY_RULES=base
    git -C "$dir" add .github/standard.conf
    commit_unchecked "ci: drop the lint stage"
    BASE=$lint HEAD_REF=feat/stages HEAD_REPO=o/r REPO=o/r run --separate-stderr sh -c \
        'cd "$1" && exec sh .github/standard/bin/standard-check --ci' sh "$dir"
    [ "$status" -eq 1 ]
    [[ $stderr == *"a dropped CI stage"* ]]
    git -C "$dir" switch -q -c feat/more-packs "$base"
    write_config VISIBILITY=public 'PRIVACY_RULES=base disk'
    git -C "$dir" add .github/standard.conf
    commit_unchecked "chore: add the disk pack"
    BASE=$base HEAD_REF=feat/more-packs HEAD_REPO=o/r REPO=o/r run --separate-stderr sh -c \
        'cd "$1" && exec sh .github/standard/bin/standard-check --ci' sh "$dir"
    [ "$status" -eq 0 ]
}

@test "a missing or unparsable config, and a missing LICENSE under LICENSE=MIT, fail" {
    make_consumer
    git -C "$REPO" rm -q --cached LICENSE
    check --staged
    [ "$status" -eq 1 ]
    [[ $stderr == *"LICENSE: missing, and the config says LICENSE=MIT"* ]]
    printf 'BOGUS=1\n' >> "$REPO/.github/standard.conf"
    git -C "$REPO" add .github/standard.conf
    check --staged
    [ "$status" -eq 1 ]
    [[ $stderr == *".github/standard.conf: does not parse"* ]]
    git -C "$REPO" rm -q --cached .github/standard.conf
    check --staged
    [ "$status" -eq 1 ]
    [[ $stderr == *".github/standard.conf: missing"* ]]
}

# Run install-tools ARGS through bats `run`, stderr apart, with RUNNER_TEMP in the test's folder.
run_install_tools() {
    run --separate-stderr env RUNNER_TEMP="$BATS_TEST_TMPDIR/runner" \
        bash "$REPO_ROOT/.github/standard/bin/install-tools" "$@"
}

@test "install-tools prints its bin folder, and in Actions adds it to GITHUB_PATH as well" {
    local tools=$BATS_TEST_TMPDIR/runner/tools
    stub_init
    # A bats clone already in place, which git reports at the pinned commit.
    mkdir -p "$tools/bats-core-1.13.0/bin"
    printf '#!/bin/bash\necho "Bats 1.13.0"\n' > "$tools/bats-core-1.13.0/bin/bats"
    chmod +x "$tools/bats-core-1.13.0/bin/bats"
    mkstub git <<< 'echo 3bca150ec86275d6d9d5a4fd7d48ab8b6c6f3d87'
    PATH="$STUB_BIN:$PATH" run_install_tools bats
    [ "$status" -eq 0 ] || { printf '%s\n' "$stderr" >&2; return 1; }
    [ "$output" = "$tools/bin" ]
    : > "$BATS_TEST_TMPDIR/github-path"
    PATH="$STUB_BIN:$PATH" GITHUB_PATH="$BATS_TEST_TMPDIR/github-path" run_install_tools bats
    [ "$status" -eq 0 ]
    [ "$output" = "$tools/bin" ]
    [ "$(awk 'END { print NR }' "$BATS_TEST_TMPDIR/github-path")" -eq 1 ]
}

@test "install-tools installs the PowerShell modules only on Windows, from a download that matches its pin" {
    stub_init
    mkstub uname <<< 'echo Linux'
    PATH="$STUB_BIN:$PATH" run_install_tools pester
    [ "$status" -eq 1 ]
    [[ $stderr == *"pester is pinned for Windows PowerShell 5.1 only"* ]]
    mkstub uname <<< 'echo MINGW64_NT-10.0'
    # A download of other bytes than the pinned package.
    mkstub curl << 'STUB'
while (( $# )); do
    if [[ $1 == --output ]]; then printf 'other bytes\n' > "$2"; break; fi
    shift
done
STUB
    PATH="$STUB_BIN:$PATH" run_install_tools psscriptanalyzer
    [ "$status" -eq 1 ]
    [[ $stderr == *"does not match its pinned SHA-256"* ]]
    [[ $(cat "$STUB_LOG") == *"api/v2/package/PSScriptAnalyzer/1.25.0"* ]]
    run_install_tools pwsh
    [ "$status" -eq 64 ]
}

# Run run-stage ARGS in the folder $1 of $REPO through bats `run`, stderr apart.
run_stage_in() {
    local dir=$1
    shift
    run --separate-stderr env PATH="${STUB_BIN:-/nonexistent}:$PATH" RUNNER_TEMP="$BATS_TEST_TMPDIR/runner" \
        bash -c 'cd "$1" && shift && exec bash "$0" "$@"' "$REPO/.github/standard/bin/run-stage" "$dir" "$@"
}

@test "no-skips passes a full run, and fails on a skip, a failure, a short run, no plan and no test" {
    local d=$BATS_TEST_TMPDIR bad
    printf '%s\n' '1..2' 'ok 1 one' 'ok 2 two' > "$d/good.tap"
    printf '%s\n' '1..2' 'ok 1 one' 'ok 2 two # skip it needs a tool' > "$d/skip.tap"
    printf '%s\n' '1..2' 'ok 1 one' 'not ok 2 two' > "$d/fail.tap"
    printf '%s\n' '1..3' 'ok 1 one' 'ok 2 two' > "$d/short.tap"
    printf '%s\n' 'ok 1 one' > "$d/noplan.tap"
    printf '%s\n' '1..0' > "$d/empty.tap"
    run sh "$REPO_ROOT/.github/standard/bin/no-skips" "$d/good.tap"
    [ "$status" -eq 0 ]
    for bad in skip fail short noplan empty; do
        run sh "$REPO_ROOT/.github/standard/bin/no-skips" "$d/good.tap" "$d/$bad.tap"
        [ "$status" -eq 1 ] || { printf '%s passed\n' "$bad" >&2; return 1; }
    done
    run sh "$REPO_ROOT/.github/standard/bin/no-skips" "$d/missing.tap"
    [ "$status" -eq 2 ]
}

@test "no-skips names each file as it was given, backslashes and all" {
    local dir file
    # A Windows runner hands out paths such as D:\a\_temp; elsewhere a folder of that name.
    if command -v cygpath > /dev/null 2>&1; then
        dir=$BATS_TEST_TMPDIR/tap
        mkdir -p "$dir"
        file="$(cygpath -w "$dir")\\a.tap"
    else
        dir=$BATS_TEST_TMPDIR/'D:\a\_temp'
        mkdir -p "$dir"
        file=$dir/a.tap
    fi
    printf '1..1\nok 1 one\n' > "$file"
    run --separate-stderr sh "$REPO_ROOT/.github/standard/bin/no-skips" "$file"
    [ "$status" -eq 0 ]
    [ "$output" = "no-skips: $file: 1 test(s), 0 skipped, 0 failed" ]
    [ -z "$stderr" ]
}

@test "run-stage runs ci/STAGE from the top of the repository, and its status carries through" {
    make_repo
    mkdir -p "$REPO/ci" "$REPO/docs"
    printf '#!/bin/sh\npwd > "%s/ran"\nexit 0\n' "$BATS_TEST_TMPDIR" > "$REPO/ci/test"
    chmod +x "$REPO/ci/test"
    run_stage_in "$REPO/docs" test
    [ "$status" -eq 0 ] || { printf '%s\n' "$stderr" >&2; return 1; }
    # The same folder, however its path is written: on a Windows runner bash reaches it through
    # /tmp and a short 8.3 name, while run-stage enters the long path that git prints.
    [ "$(cat "$BATS_TEST_TMPDIR/ran")" -ef "$REPO" ]
    printf '#!/bin/sh\nexit 3\n' > "$REPO/ci/test"
    run_stage_in "$REPO" test
    [ "$status" -eq 3 ]
    run_stage_in "$REPO" lint
    [ "$status" -eq 1 ]
    [[ $stderr == *"this repository has no ci/lint or ci/lint.ps1"* ]]
    run_stage_in "$REPO" deploy
    [ "$status" -eq 64 ]
}

@test "run-stage runs ci/STAGE first, then ci/STAGE.ps1 under Windows PowerShell 5.1, only on Windows" {
    make_repo
    stub_init
    mkdir -p "$REPO/ci"
    printf '#!/bin/sh\necho bash >> "%s/order"\n' "$BATS_TEST_TMPDIR" > "$REPO/ci/system"
    chmod +x "$REPO/ci/system"
    printf 'Write-Output "synthetic"\r\n' > "$REPO/ci/system.ps1"
    mkstub uname <<< 'echo Linux'
    run_stage_in "$REPO" system
    [ "$status" -eq 1 ]
    [[ $stderr == *"ci/system.ps1 needs Windows PowerShell 5.1"* ]]
    mkstub uname <<< 'echo MINGW64_NT-10.0'
    mkstub powershell << STUB
echo powershell >> "$BATS_TEST_TMPDIR/order"
printf '%s\n' "\$TEMP" "\$TMP" > "$BATS_TEST_TMPDIR/temp"
STUB
    : > "$BATS_TEST_TMPDIR/order"
    POWERSHELL=powershell run_stage_in "$REPO" system
    [ "$status" -eq 0 ] || { printf '%s\n' "$stderr" >&2; return 1; }
    [ "$(cat "$BATS_TEST_TMPDIR/order")" = "$(printf 'bash\npowershell')" ]
    [[ $(cat "$STUB_LOG") == *"powershell -NoProfile -ExecutionPolicy Bypass -File "*"system.ps1"* ]]
    [ "$(awk 'END { print NR }' "$BATS_TEST_TMPDIR/temp")" -eq 2 ]
    [[ $(head -n 1 "$BATS_TEST_TMPDIR/temp") == *runner* ]]
}
