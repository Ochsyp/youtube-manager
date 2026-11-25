# shellcheck shell=bash disable=SC2016 # the sh -c scripts are single-quoted on purpose.
# lib.bash of the repository standard: the same file in every repository; never edit it here.
# Test helpers for the standard's own suites, which call `load lib`, and for each repository's
# tests/helpers.bash, which sources this file first. Every value a test uses is synthetic.
#   Isolation: isolate_home, clear_git_repo_env.
#   Scratch repositories: make_repo, commit_config, add_remote, stage, stage_executables,
#     try_commit, commit_count, run_pre_push, write_denylist, utf16le.
#   Checks: refute_term_shown, refute_home_shown, assert_tmpdir_empty.
#   Other: need, use_symlinks, run_shellcheck, shellcheck_code_counts, manifest_copies.
#   Stubs: stub_init, mkstub (body on stdin), stub_refuse.

bats_require_minimum_version 1.13.0

# The repository under test: this file sits in .github/standard/tests/.
REPO_ROOT=$(cd "${BASH_SOURCE[0]%/*}/../../.." && pwd)
# The synthetic denylist term of the hook tests.
DENY_TERM=forbidden-term-example
# The home folder the tests started with, before isolate_home; only its name is compared.
STANDARD_REAL_HOME=${STANDARD_REAL_HOME:-${HOME:-}}
# The all-zero object id, which a pre-push ref line gives for a remote ref that does not exist.
ZERO_OID=0000000000000000000000000000000000000000

# Give the test its own HOME, XDG_CONFIG_HOME, TMPDIR and global git config under
# BATS_TEST_TMPDIR, keep the system git config out, and unset git's repository and identity
# variables, so that no real denylist, hook setting or identity reaches a test. Call from setup().
isolate_home() {
    clear_git_repo_env
    unset GIT_CONFIG_SYSTEM GIT_TEMPLATE_DIR GIT_AUTHOR_NAME GIT_AUTHOR_EMAIL GIT_AUTHOR_DATE \
        GIT_COMMITTER_NAME GIT_COMMITTER_EMAIL GIT_COMMITTER_DATE PRIVACY_DENYLIST \
        GIT_CONFIG_PARAMETERS GIT_CONFIG_COUNT
    export HOME="$BATS_TEST_TMPDIR/home"
    export XDG_CONFIG_HOME="$HOME/.config"
    export TMPDIR="$BATS_TEST_TMPDIR/tmp"
    export GIT_CONFIG_NOSYSTEM=1
    export GIT_CONFIG_GLOBAL="$HOME/.gitconfig"
    mkdir -p "$XDG_CONFIG_HOME" "$TMPDIR"
    : > "$GIT_CONFIG_GLOBAL"
    DENYLIST="$XDG_CONFIG_HOME/git-privacy/denylist"
}

# Unset the repository variables a hook or a linked worktree exports, such as GIT_DIR and
# GIT_INDEX_FILE, so that git in a test finds the repository by its folder.
clear_git_repo_env() {
    local var
    while IFS= read -r var; do
        unset "$var"
    done < <(git rev-parse --local-env-vars)
}

# Create a repository at $REPO on main with a test identity and one commit, made before the
# hooks are on. Then copy the hooks under test and the standard's bin/ into it and turn the hooks
# on the way CONTRIBUTING.md says: core.hooksPath .githooks. The first call per .bats file builds
# a template under BATS_FILE_TMPDIR, which later calls copy (each git call is slow on Windows).
# Sets REPO; call after isolate_home.
make_repo() {
    local template="$BATS_FILE_TMPDIR/template-repo"
    REPO="$BATS_TEST_TMPDIR/repo"
    if [[ ! -d $template ]]; then
        git init -q -b main "$template"
        git -C "$template" config user.name "Test User"
        git -C "$template" config user.email test@example.invalid
        git -C "$template" config commit.gpgsign false
        git -C "$template" config core.autocrlf false
        printf 'synthetic test repository\n' > "$template/README.md"
        git -C "$template" add README.md
        git -C "$template" commit -q -m "chore: initial commit"
        cp -R "$REPO_ROOT/.githooks" "$template/.githooks"
        mkdir -p "$template/.github/standard"
        cp -R "$REPO_ROOT/.github/standard/bin" "$template/.github/standard/bin"
        git -C "$template" config core.hooksPath .githooks
    fi
    cp -R "$template" "$REPO"
}

# Write .github/standard.conf with one line per argument, plus NAME=example, VISIBILITY=private
# and RELEASE_KIND=tar where no argument sets them; commit_config also commits it on the current
# branch of $REPO, with the hooks off for this one commit.
write_config() {
    local key arg
    local -a lines=()
    for key in NAME=example VISIBILITY=private RELEASE_KIND=tar; do
        for arg in "$@"; do
            [[ $arg != "${key%%=*}="* ]] || continue 2
        done
        lines+=("$key")
    done
    mkdir -p "$REPO/.github"
    printf '%s\n' "${lines[@]}" "$@" > "$REPO/.github/standard.conf"
}
commit_config() {
    write_config "$@"
    git -C "$REPO" add .github/standard.conf
    git -C "$REPO" -c core.hooksPath=no-hooks commit -q -m "chore: add the config"
}

# Give $REPO a bare remote origin under BATS_TEST_TMPDIR and push main there with the hooks off,
# so that origin/main exists, as in a clone.
add_remote() {
    git init -q --bare "$BATS_TEST_TMPDIR/remote.git"
    git -C "$REPO" remote add origin "$BATS_TEST_TMPDIR/remote.git"
    git -C "$REPO" -c core.hooksPath=no-hooks push -q origin main
    git -C "$REPO" fetch -q origin
}

# Write CONTENT and a newline to FILE, a path relative to $REPO, and stage it.
stage() {
    mkdir -p "$(dirname "$REPO/$1")"
    printf '%s\n' "$2" > "$REPO/$1"
    git -C "$REPO" add -- "$1"
}

# Run `git commit -m MESSAGE` in $REPO through bats `run`, so the hooks run as git runs them.
try_commit() {
    run git -C "$REPO" commit -q -m "$1"
}

# Print the number of commits reachable from HEAD in $REPO.
commit_count() {
    git -C "$REPO" rev-list --count HEAD
}

# Run $REPO's pre-push hook through bats `run`, from $REPO's top as git runs it, with one ref
# line per REMOTE_REF argument that pushes HEAD there, as `git push origin HEAD:REMOTE_REF`
# would. Without arguments, stdin is empty.
run_pre_push() {
    local ref refs_in="" sha
    sha=$(git -C "$REPO" rev-parse HEAD)
    for ref in "$@"; do
        refs_in+="refs/heads/topic $sha $ref $ZERO_OID"$'\n'
    done
    run sh -c 'cd "$1" && exec sh .githooks/pre-push origin https://example.invalid/r.git' \
        sh "$REPO" <<< "$refs_in"
}

# Write the per-user denylist the loader reads, one line per argument; without arguments it
# holds a comment, a blank line and DENY_TERM. Every line must be synthetic.
write_denylist() {
    (( $# )) || set -- '# Synthetic denylist for the hook tests.' '' "$DENY_TERM"
    mkdir -p "${DENYLIST%/*}"
    printf '%s\n' "$@" > "$DENYLIST"
}

# Print TEXT as UTF-16LE with a byte order mark and a CRLF line end. TEXT must be ASCII.
utf16le() {
    local text="$1"$'\r\n' i
    printf '\377\376'
    for (( i = 0; i < ${#text}; i++ )); do
        printf '%s\000' "${text:i:1}"
    done
}

# Fail, printing the output, if $output (or $stderr) shows DENY_TERM in any case.
refute_term_shown() {
    local all="${output:-}${stderr:-}"
    if [[ ${all,,} == *"$DENY_TERM"* ]]; then
        printf 'the output shows the denylist term:\n%s\n' "$all" >&2
        return 1
    fi
}

# Fail if $output (or $stderr) shows the home folder: $HOME in its POSIX, mixed or Windows form,
# or the name of the test's or the real home folder, which a user name may form.
refute_home_shown() {
    local all="${output:-}${stderr:-}" form
    local -a forms=("$HOME" "${HOME##*/}")
    if command -v cygpath > /dev/null 2>&1; then
        forms+=("$(cygpath -m "$HOME")" "$(cygpath -w "$HOME")")
    fi
    if [[ ${#STANDARD_REAL_HOME} -gt 2 ]]; then
        forms+=("$STANDARD_REAL_HOME" "${STANDARD_REAL_HOME##*/}")
    fi
    for form in "${forms[@]}"; do
        if [[ -n $form && $all == *"$form"* ]]; then
            printf 'the output shows a home folder form:\n%s\n' "$all" >&2
            return 1
        fi
    done
}

# Fail, listing what is left, unless TMPDIR is empty after the hooks ran.
assert_tmpdir_empty() {
    local left
    left=$(ls -A "$TMPDIR")
    if [[ -n $left ]]; then
        printf 'the hooks left files in TMPDIR:\n%s\n' "$left" >&2
        return 1
    fi
}

# Skip the test unless this machine has FEATURE: symlinks, modes (POSIX file modes), or
# 'command NAME'. CI fails on every skip (no-skips), so a skip only helps a local run.
need() {
    case $1 in
        symlinks)
            use_symlinks && return 0
            skip "it needs symlinks, which ln -s does not make here"
            ;;
        modes)
            local file="$BATS_TEST_TMPDIR/need-modes"
            : > "$file" && chmod 600 "$file" && [[ $(stat -c %a "$file") == 600 ]] && return 0
            skip "it needs POSIX file modes, which chmod does not set here"
            ;;
        command)
            command -v "$2" > /dev/null 2>&1 && return 0
            skip "it needs $2 on PATH"
            ;;
        *)
            printf 'need: unknown feature %s\n' "$1" >&2
            return 1
            ;;
    esac
}

# Ask Git Bash for real symlinks, and succeed when ln -s makes one, even to a missing target.
use_symlinks() {
    local dir="$BATS_TEST_TMPDIR/need-symlinks"
    export MSYS=winsymlinks:nativestrict
    mkdir -p "$dir"
    rm -f "$dir/link"
    ln -s missing-target "$dir/link" 2> /dev/null && [[ -L $dir/link ]]
}

# Run ShellCheck through bats `run` in REPO_ROOT on PATHS, relative to it: some ShellCheck
# builds cannot read the absolute paths a POSIX layer passes them, so no path is converted.
run_shellcheck() {
    run env MSYS_NO_PATHCONV=1 MSYS2_ARG_CONV_EXCL='*' sh -c 'cd "$1" && shift && exec shellcheck "$@"' \
        sh "$REPO_ROOT" "$@"
}

# Print "CODE COUNT" per ShellCheck code in $output, CRs of the Windows build removed.
shellcheck_code_counts() {
    tr -d '\r' <<< "$output" | grep -o 'SC[0-9]\{4\}' | sort | uniq -c | awk '{ print $2, $1 }'
}

# Stage every file of $REPO's index that starts with #! as executable (100755), in one call.
# Git for Windows stages a new file as 100644, since it has no executable bit to read.
stage_executables() {
    local path first
    local -a paths=()
    while IFS= read -r path; do
        first=
        IFS= read -r first < "$REPO/$path" || true
        [[ $first != "#!"* ]] || paths+=("$path")
    done < <(git -C "$REPO" ls-files)
    (( ${#paths[@]} == 0 )) || git -C "$REPO" update-index --chmod=+x -- "${paths[@]}"
}

# Create the stub folder $STUB_BIN and the log $STUB_LOG that every stub appends its call to.
# STUB_BIN is the folder as pwd prints it: a TMPDIR in C:/ form would put a drive colon into
# PATH, which splits there and keeps the stubs off it.
stub_init() {
    mkdir -p "$BATS_TEST_TMPDIR/stub-bin"
    STUB_BIN=$(CDPATH='' cd -- "$BATS_TEST_TMPDIR/stub-bin" && pwd)
    export STUB_BIN
    export STUB_LOG="$BATS_TEST_TMPDIR/stub.log"
    : > "$STUB_LOG"
}

# Print the paths of the file entries of the MANIFEST file MANIFEST that the config file CONF
# selects, as standard-check does: the profile all; release, unless RELEASE_KIND=legacy makes
# those files the repository's own; mit under LICENSE=MIT; and the profile VISIBILITY names.
manifest_copies() {
    local manifest=$1 conf=$2 vis lic kind
    vis=$(sh "$REPO_ROOT/.github/standard/bin/conf" --get VISIBILITY "$conf") || return 1
    lic=$(sh "$REPO_ROOT/.github/standard/bin/conf" --get LICENSE "$conf") || return 1
    kind=$(sh "$REPO_ROOT/.github/standard/bin/conf" --get RELEASE_KIND "$conf") || return 1
    awk -v vis="$vis" -v lic="$lic" -v kind="$kind" '
        $1 == "file" && NF == 5 && ($2 == "all" || $2 == vis || ($2 == "mit" && lic == "MIT") ||
            ($2 == "release" && kind != "legacy")) { print $5 }' "$manifest"
}

# The shared start of every stub: append "NAME ARGS" to $STUB_LOG, each argument %q-quoted.
IFS= read -r -d '' _STUB_PROLOGUE << 'EOF' || true
if (( $# )); then printf -v _stub_args ' %q' "$@"; else _stub_args=''; fi
printf '%s%s\n' "$STUB_NAME" "$_stub_args" >> "${STUB_LOG:?}"
EOF

# Write the executable stub $STUB_BIN/NAME: the shared start, then the body read from stdin.
# An empty body makes a stub that only logs and exits 0.
mkstub() {
    local name=$1 body
    IFS= read -r -d '' body || true
    printf '#!/bin/bash\nSTUB_NAME=%q\n%s%s' "$name" "$_STUB_PROLOGUE" "$body" > "$STUB_BIN/$name"
    chmod +x "$STUB_BIN/$name"
}

# Write a stub NAME that must never run: it logs the call and exits 100.
stub_refuse() {
    mkstub "$1" << 'EOF'
printf '%s: refused under test\n' "$STUB_NAME" >&2
exit 100
EOF
}
