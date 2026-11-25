#!/usr/bin/env bats
# hooks.bats of the repository standard: the same file in every repository; never edit it here.
# The hooks as git runs them: the branch guards of pre-commit, pre-merge-commit and pre-push, the
# tag rules of pre-push, the size guard, the denylist loader, and what the hooks scan: staged
# content, path names, author and committer, the message, ref names and the pushed commits.
# Every value is synthetic; values that look private are built from fragments at run time.

# The sh -c scripts are single-quoted, ~ stays literal in git config values, and bats runs each
# test in a subshell, all on purpose.
# shellcheck disable=SC1003,SC2016,SC2030,SC2031,SC2088
load lib

setup() {
    isolate_home
}

# Commit with the hooks off, as rebase, cherry-pick, am or a skipped hook would.
commit_unchecked() {
    git -C "$REPO" -c core.hooksPath=no-hooks commit -q -m "$1"
}

# Give the test a home whose folder name a message must never show, as a Windows user name.
named_home() {
    export HOME="$BATS_TEST_TMPDIR/profile 5ca1ab1e"
    export XDG_CONFIG_HOME="$HOME/.config"
    export GIT_CONFIG_GLOBAL="$HOME/.gitconfig"
    mkdir -p "$XDG_CONFIG_HOME"
    : > "$GIT_CONFIG_GLOBAL"
    DENYLIST="$XDG_CONFIG_HOME/git-privacy/denylist"
}

@test "pre-commit refuses a commit on main" {
    make_repo
    stage notes.txt 'synthetic notes'
    try_commit "docs: add notes"
    [ "$status" -ne 0 ]
    [[ $output == *"commits on 'main' are refused"* ]]
    [ "$(commit_count)" -eq 1 ]
}

@test "pre-commit refuses a commit on master" {
    make_repo
    git -C "$REPO" branch -m master
    stage notes.txt 'synthetic notes'
    try_commit "docs: add notes"
    [ "$status" -ne 0 ]
    [[ $output == *"commits on 'master' are refused"* ]]
    [ "$(commit_count)" -eq 1 ]
}

@test "pre-commit refuses a commit on main while a tag named main exists" {
    make_repo
    git -C "$REPO" tag main
    stage notes.txt 'synthetic notes'
    try_commit "docs: add notes"
    [ "$status" -ne 0 ]
    [[ $output == *"commits on 'main' are refused"* ]]
    [ "$(commit_count)" -eq 1 ]
}

@test "a refusal on main comes before any scan" {
    make_repo
    write_denylist
    stage notes.txt "contact $DENY_TERM"
    try_commit "docs: add notes"
    [ "$status" -ne 0 ]
    [[ $output == *"commits on 'main' are refused"* ]]
    [[ $output != *privacy-scan* ]]
}

@test "pre-commit allows a clean commit on a feature branch and leaves TMPDIR empty" {
    make_repo
    write_denylist
    git -C "$REPO" switch -q -c feat/example
    stage notes.txt 'synthetic notes'
    try_commit "docs: add notes"
    [ "$status" -eq 0 ]
    [ "$(commit_count)" -eq 2 ]
    assert_tmpdir_empty
}

@test "pre-commit scans a commit in a detached HEAD" {
    make_repo
    write_denylist
    git -C "$REPO" switch -q --detach
    stage notes.txt 'synthetic notes'
    try_commit "docs: add notes"
    [ "$status" -eq 0 ]
    stage leak.txt "contact $DENY_TERM"
    try_commit "docs: add a leak"
    [ "$status" -ne 0 ]
    [[ $output == *"leak.txt:1: denylist"* ]]
    [ "$(commit_count)" -eq 2 ]
}

@test "pre-merge-commit refuses a merge commit on main" {
    make_repo
    git -C "$REPO" switch -q -c feat/example
    stage notes.txt 'synthetic notes'
    try_commit "docs: add notes"
    [ "$status" -eq 0 ]
    git -C "$REPO" switch -q main
    run git -C "$REPO" merge -q --no-ff -m "Merge feat/example" feat/example
    [ "$status" -ne 0 ]
    [[ $output == *"commits on 'main' are refused"* ]]
    [ "$(commit_count)" -eq 1 ]
}

@test "pre-merge-commit refuses a merge that brings in a denylist term" {
    make_repo
    git -C "$REPO" switch -q -c feat/leak
    stage docs/leak.md "contact $DENY_TERM"
    commit_unchecked "docs: add notes"
    write_denylist
    git -C "$REPO" switch -q -c feat/merge main
    run git -C "$REPO" merge -q --no-ff -m "Merge feat/leak" feat/leak
    [ "$status" -ne 0 ]
    [[ $output == *"docs/leak.md:1: denylist"* ]]
    refute_term_shown
    [ "$(commit_count)" -eq 1 ]
}

@test "pre-commit refuses staged content that holds a denylist term in another case" {
    make_repo
    write_denylist
    git -C "$REPO" switch -q -c feat/example
    stage docs/notes.md 'Written by FORBIDDEN-Term-Example.'
    try_commit "docs: add notes"
    [ "$status" -ne 0 ]
    [[ $output == *"docs/notes.md:1: denylist"* ]]
    refute_term_shown
    [ "$(commit_count)" -eq 1 ]
}

@test "pre-commit refuses binary content that holds a denylist term" {
    make_repo
    write_denylist
    git -C "$REPO" switch -q -c feat/example
    printf 'prompt$ %s\n\000\001\n' "$DENY_TERM" > "$REPO/session.bin"
    git -C "$REPO" add -- session.bin
    try_commit "test: add a recording"
    [ "$status" -ne 0 ]
    [[ $output == *"session.bin:1: denylist"* ]]
    refute_term_shown
}

@test "pre-commit refuses UTF-16 text that holds a denylist term" {
    make_repo
    write_denylist
    git -C "$REPO" switch -q -c feat/example
    utf16le "notes by $DENY_TERM" > "$REPO/notes.txt"
    git -C "$REPO" add -- notes.txt
    try_commit "docs: add notes"
    [ "$status" -ne 0 ]
    [[ $output == *"notes.txt:1: denylist"* ]]
    refute_term_shown
}

@test "pre-commit refuses a path name that holds a denylist term, and never shows it" {
    make_repo
    write_denylist
    git -C "$REPO" switch -q -c feat/example
    stage "notes-$DENY_TERM.txt" "contact $DENY_TERM"
    try_commit "docs: add notes"
    [ "$status" -ne 0 ]
    [[ $output == *"path name 1 of the scanned paths: denylist"* ]]
    [[ $output == *"path name 1 of the scanned paths:1: denylist"* ]]
    refute_term_shown
    [ "$(commit_count)" -eq 1 ]
}

@test "pre-commit refuses a file renamed to a name with spaces whose content gains a term" {
    make_repo
    write_denylist
    git -C "$REPO" switch -q -c feat/example
    stage notes.txt "$(printf 'synthetic line %s\n' 1 2 3 4 5 6 7 8)"
    try_commit "docs: add notes"
    [ "$status" -eq 0 ]
    git -C "$REPO" mv notes.txt "meeting notes.txt"
    printf 'contact %s\n' "$DENY_TERM" >> "$REPO/meeting notes.txt"
    git -C "$REPO" add -- "meeting notes.txt"
    try_commit "docs: rename the notes"
    [ "$status" -ne 0 ]
    [[ $output == *"meeting notes.txt:9: denylist"* ]]
    refute_term_shown
    [ "$(commit_count)" -eq 2 ]
}

@test "pre-commit refuses a file turned into a symlink whose target holds a denylist term" {
    local blob
    make_repo
    write_denylist
    git -C "$REPO" switch -q -c feat/example
    # A symlink's staged content is its target; update-index stages one without a real symlink.
    blob=$(printf 'docs/%s/notes.txt' "$DENY_TERM" | git -C "$REPO" hash-object -w --stdin)
    git -C "$REPO" update-index --cacheinfo "120000,$blob,README.md"
    try_commit "docs: link the readme"
    [ "$status" -ne 0 ]
    [[ $output == *"README.md:1: denylist"* ]]
    refute_term_shown
}

@test "pre-commit refuses a commit whose author holds a denylist term" {
    make_repo
    write_denylist
    git -C "$REPO" switch -q -c feat/example
    stage notes.txt 'synthetic notes'
    run git -C "$REPO" commit -q --author="${DENY_TERM^^} <author@example.invalid>" -m "docs: add notes"
    [ "$status" -ne 0 ]
    [[ $output == *"the author identity: denylist"* ]]
    refute_term_shown
    [ "$(commit_count)" -eq 1 ]
}

@test "pre-commit refuses a commit whose committer holds a denylist term" {
    make_repo
    write_denylist
    git -C "$REPO" switch -q -c feat/example
    stage notes.txt 'synthetic notes'
    run env GIT_COMMITTER_EMAIL="$DENY_TERM@example.invalid" git -C "$REPO" commit -q -m "docs: add notes"
    [ "$status" -ne 0 ]
    [[ $output == *"the committer identity: denylist"* ]]
    refute_term_shown
    [ "$(commit_count)" -eq 1 ]
}

@test "commit-msg refuses a commit message that holds a denylist term" {
    make_repo
    write_denylist
    git -C "$REPO" switch -q -c feat/example
    stage notes.txt 'synthetic notes'
    try_commit "docs: add notes from ${DENY_TERM^^}"
    [ "$status" -ne 0 ]
    [[ $output == *"message line 1: denylist"* ]]
    refute_term_shown
    [ "$(commit_count)" -eq 1 ]
}

@test "commit-msg refuses a home path in the message, and lets a noreply trailer through" {
    make_repo
    git -C "$REPO" switch -q -c feat/example
    commit_config PRIVACY_RULES=base
    stage notes.txt 'synthetic notes'
    try_commit "$(printf 'docs: add notes\n\nCo-Authored-By: Example <noreply@%s>' example.org)"
    [ "$status" -eq 0 ]
    stage more.txt 'synthetic notes'
    try_commit "$(printf 'docs: add notes from /%s/someone' home)"
    [ "$status" -ne 0 ]
    [[ $output == *"message line 1: home-path"* ]]
    [[ $output != *someone* ]]
}

@test "a missing denylist allows the commit, with a note" {
    make_repo
    git -C "$REPO" switch -q -c feat/example
    stage notes.txt "synthetic notes that mention $DENY_TERM"
    try_commit "docs: add notes"
    [ "$status" -eq 0 ]
    [[ $output == *"note: no denylist holds a term, so only the rule packs run"* ]]
    [ "$(commit_count)" -eq 2 ]
}

@test "a list of only comments, a BOM and blank lines counts as no list" {
    make_repo
    write_denylist $'\357\273\277'"# $DENY_TERM" '' '    ' $'\r'
    git -C "$REPO" switch -q -c feat/example
    stage notes.txt "# $DENY_TERM"
    try_commit "docs: add notes"
    [ "$status" -eq 0 ]
    [[ $output == *"note: no denylist holds a term"* ]]
    git config --global privacy.requireDenylist true
    stage more.txt 'synthetic notes'
    try_commit "docs: add more notes"
    [ "$status" -ne 0 ]
    [[ $output == *"no denylist holds a term, and privacy.requireDenylist is true"* ]]
    [ "$(commit_count)" -eq 2 ]
}

@test "a denylist with a byte order mark and CRLF line ends still matches its first term" {
    make_repo
    write_denylist $'\357\273\277'"$DENY_TERM"$'\r' $'# Synthetic denylist.\r'
    git -C "$REPO" switch -q -c feat/example
    stage notes.txt "contact $DENY_TERM"
    try_commit "docs: add notes"
    [ "$status" -ne 0 ]
    [[ $output == *"notes.txt:1: denylist"* ]]
    refute_term_shown
}

@test "a denylist term with spaces around it still matches" {
    make_repo
    write_denylist "  $DENY_TERM   "
    git -C "$REPO" switch -q -c feat/example
    stage notes.txt "contact $DENY_TERM."
    try_commit "docs: add notes"
    [ "$status" -ne 0 ]
    [[ $output == *"notes.txt:1: denylist"* ]]
}

@test "a denylist saved as UTF-16 refuses the commit instead of matching nothing" {
    make_repo
    mkdir -p "${DENYLIST%/*}"
    utf16le "$DENY_TERM" > "$DENYLIST"
    git -C "$REPO" switch -q -c feat/example
    stage notes.txt 'synthetic notes'
    try_commit "docs: add notes"
    [ "$status" -ne 0 ]
    [[ $output == *"the denylist list 1 of 1 holds NUL bytes"* ]]
    [ "$(commit_count)" -eq 1 ]
}

@test "PRIVACY_DENYLIST adds another list" {
    make_repo
    export PRIVACY_DENYLIST="$BATS_TEST_TMPDIR/other lists/denylist"
    mkdir -p "${PRIVACY_DENYLIST%/*}"
    printf '%s\n' "$DENY_TERM" > "$PRIVACY_DENYLIST"
    git -C "$REPO" switch -q -c feat/example
    stage notes.txt "contact $DENY_TERM"
    try_commit "docs: add notes"
    [ "$status" -ne 0 ]
    [[ $output == *"notes.txt:1: denylist"* ]]
}

@test "the privacy.denylist lists of git config are read with ~ expanded" {
    make_repo
    mkdir -p "$HOME/lists"
    printf '%s\n' "$DENY_TERM" > "$HOME/lists/one"
    git config --global privacy.denylist '~/lists/one'
    git -C "$REPO" switch -q -c feat/example
    stage notes.txt "contact $DENY_TERM"
    try_commit "docs: add notes"
    [ "$status" -ne 0 ]
    [[ $output == *"notes.txt:1: denylist"* ]]
}

@test "a missing ~/ list refuses under privacy.requireDenylist, named as configured" {
    named_home
    make_repo
    write_denylist
    git config --global privacy.requireDenylist true
    git config --global privacy.denylist '~/missing/denylist'
    git -C "$REPO" switch -q -c feat/example
    stage notes.txt 'synthetic notes'
    try_commit "docs: add notes"
    [ "$status" -ne 0 ]
    [[ $output == *"the denylist ~/missing/denylist is missing, and privacy.requireDenylist is true"* ]]
    refute_home_shown
    [ "$(commit_count)" -eq 1 ]
}

@test "a missing absolute list is named only as list N of M, never by its path" {
    local path
    named_home
    make_repo
    write_denylist
    path="$HOME/missing/denylist"
    if command -v cygpath > /dev/null 2>&1; then
        path=$(cygpath -m "$path")
    fi
    git config --global privacy.requireDenylist true
    git config --global privacy.denylist "$path"
    git -C "$REPO" switch -q -c feat/example
    stage notes.txt 'synthetic notes'
    try_commit "docs: add notes"
    [ "$status" -ne 0 ]
    [[ $output == *"the denylist list 1 of 2 is missing"* ]]
    refute_home_shown
    git config --global privacy.requireDenylist false
    try_commit "docs: add notes"
    [ "$status" -eq 0 ]
    [[ $output == *"note: the denylist list 1 of 2 is missing"* ]]
    refute_home_shown
}

@test "the clone's own list is found from a linked worktree" {
    make_repo
    git -C "$REPO" add .githooks .github/standard/bin
    commit_unchecked "chore: track the hooks"
    printf '%s\n' "$DENY_TERM" > "$REPO/.git/info/privacy-denylist"
    git -C "$REPO" worktree add -q -b feat/tree "$BATS_TEST_TMPDIR/tree"
    printf 'contact %s\n' "$DENY_TERM" > "$BATS_TEST_TMPDIR/tree/notes.txt"
    git -C "$BATS_TEST_TMPDIR/tree" add notes.txt
    run git -C "$BATS_TEST_TMPDIR/tree" commit -q -m "docs: add notes"
    [ "$status" -ne 0 ]
    [[ $output == *"notes.txt:1: denylist"* ]]
    refute_term_shown
}

@test "a list that git config and --git-path both name counts once" {
    local path
    make_repo
    utf16le "$DENY_TERM" > "$REPO/.git/info/privacy-denylist"
    path=$(git -C "$REPO" rev-parse --path-format=absolute --git-path info/privacy-denylist)
    git config --global privacy.denylist "$path"
    git -C "$REPO" switch -q -c feat/example
    stage notes.txt 'synthetic notes'
    try_commit "docs: add notes"
    [ "$status" -ne 0 ]
    [[ $output == *"the denylist list 1 of 1 holds NUL bytes"* ]]
}

@test "a non-ASCII denylist term matches in another case" {
    make_repo
    write_denylist "$(printf 'verbotenes-w\303\266rt')"
    git -C "$REPO" switch -q -c feat/example
    stage notes.txt "$(printf 'VERBOTENES-W\303\226RT')"
    try_commit "docs: add notes"
    [ "$status" -ne 0 ]
    [[ $output == *"notes.txt:1: denylist"* ]]
}

@test "the hooks run through the relative core.hooksPath from a subdirectory" {
    make_repo
    write_denylist
    git -C "$REPO" switch -q -c feat/example
    stage docs/leak.md "contact $DENY_TERM"
    run git -C "$REPO/docs" commit -q -m "docs: add notes"
    [ "$status" -ne 0 ]
    [[ $output == *"docs/leak.md:1: denylist"* ]]
    refute_term_shown
}

@test "a refusal names the offending files but never the term" {
    make_repo
    write_denylist
    git -C "$REPO" switch -q -c feat/example
    stage docs/leak.md "contact $DENY_TERM"
    stage docs/clean.md 'synthetic notes'
    stage "$DENY_TERM/notes.md" 'synthetic notes'
    try_commit "docs: add notes"
    [ "$status" -ne 0 ]
    [[ $output == *"docs/leak.md:1: denylist"* ]]
    [[ $output != *"docs/clean.md"* ]]
    [[ $output == *"path name 3 of the scanned paths: denylist"* ]]
    [[ $output == *"path name N is line N of: git diff --cached"* ]]
    refute_term_shown
    assert_tmpdir_empty
}

@test "pre-commit refuses a staged blob above MAX_BLOB_KB, named by its object id" {
    local blob
    make_repo
    git -C "$REPO" switch -q -c feat/example
    commit_config MAX_BLOB_KB=1
    head -c 2048 /dev/zero | tr '\0' a > "$REPO/big.txt"
    git -C "$REPO" add big.txt
    blob=$(git -C "$REPO" rev-parse :big.txt)
    try_commit "chore: add a large file"
    [ "$status" -ne 0 ]
    [[ $output == *"the staged blob ${blob:0:12} is 2 KB, above MAX_BLOB_KB=1"* ]]
    [[ $output != *big.txt* ]]
}

@test "a staged PRIVACY_ALLOW line does not exempt the staged content" {
    local home
    home=$(printf '/%s/%s' home example-user)
    make_repo
    git -C "$REPO" switch -q -c feat/example
    commit_config PRIVACY_RULES=base
    write_config PRIVACY_RULES=base "PRIVACY_ALLOW=home-path ^tests/ ^$home\$"
    git -C "$REPO" add .github/standard.conf
    stage tests/notes.txt "home $home"
    try_commit "chore: allow the synthetic home"
    [ "$status" -ne 0 ]
    [[ $output == *"tests/notes.txt:1: home-path"* ]]
    # The config's own PRIVACY_ALLOW line passes the packs, so it can be committed alone.
    [[ $output != *"standard.conf"* ]]
    git -C "$REPO" restore --staged tests/notes.txt
    try_commit "chore: allow the synthetic home"
    [ "$status" -eq 0 ]
    git -C "$REPO" add tests/notes.txt
    try_commit "test: use the synthetic home"
    [ "$status" -eq 0 ]
}

@test "pre-push refuses a push to main" {
    make_repo
    run_pre_push refs/heads/main
    [ "$status" -eq 1 ]
    [[ $output == *"pushing to 'main' is refused"* ]]
}

@test "pre-push refuses a push to master" {
    make_repo
    run_pre_push refs/heads/master
    [ "$status" -eq 1 ]
    [[ $output == *"pushing to 'master' is refused"* ]]
}

@test "pre-push refuses when one of several ref lines targets main" {
    make_repo
    run_pre_push refs/heads/feat/example refs/heads/main refs/heads/feat/other
    [ "$status" -eq 1 ]
    [[ $output == *"pushing to 'main' is refused"* ]]
}

@test "pre-push allows a push to a feature branch, and an empty ref list" {
    make_repo
    add_remote
    git -C "$REPO" switch -q -c feat/example
    stage notes.txt 'synthetic notes'
    try_commit "docs: add notes"
    run_pre_push refs/heads/feat/example
    [ "$status" -eq 0 ]
    run_pre_push
    [ "$status" -eq 0 ]
}

@test "pre-push refuses a seal tag" {
    make_repo
    commit_config RELEASE_KIND=legacy
    run_pre_push refs/tags/seal/v1.0.0
    [ "$status" -eq 1 ]
    [[ $output == *"a seal tag is refused"* ]]
}

@test "pre-push refuses a tag push, and a release tag outside RELEASE_KIND=legacy" {
    make_repo
    run_pre_push refs/tags/example
    [ "$status" -eq 1 ]
    [[ $output == *"a tag push is refused"* ]]
    run_pre_push refs/tags/v1.0.0
    [ "$status" -eq 1 ]
    [[ $output == *"a release tag is refused"* ]]
}

@test "pre-push lets a release tag through under RELEASE_KIND=legacy" {
    make_repo
    add_remote
    git -C "$REPO" switch -q -c feat/example
    commit_config RELEASE_KIND=legacy
    run_pre_push refs/tags/v1.0.0
    [ "$status" -eq 0 ]
}

@test "pre-push refuses a branch whose name holds a denylist term" {
    make_repo
    write_denylist
    run_pre_push "refs/heads/feat/$DENY_TERM"
    [ "$status" -ne 0 ]
    [[ $output == *"message line 2: denylist"* ]]
    [[ $output == *"a branch or tag name above holds a finding"* ]]
    refute_term_shown
}

@test "pre-push refuses what a commit made with the hooks off brings in" {
    make_repo
    add_remote
    write_denylist
    git -C "$REPO" switch -q -c feat/example
    stage docs/leak.md "contact $DENY_TERM"
    commit_unchecked "docs: add notes"
    run_pre_push refs/heads/feat/example
    [ "$status" -ne 0 ]
    [[ $output == *"docs/leak.md:1: denylist"* ]]
    refute_term_shown
}

@test "pre-push refuses a pushed commit whose message holds a denylist term" {
    local sha
    make_repo
    add_remote
    write_denylist
    git -C "$REPO" switch -q -c feat/example
    stage notes.txt 'synthetic notes'
    commit_unchecked "docs: add notes from $DENY_TERM"
    sha=$(git -C "$REPO" rev-parse HEAD)
    run_pre_push refs/heads/feat/example
    [ "$status" -ne 0 ]
    [[ $output == *"commit ${sha:0:12}, line "*" of git cat-file -p: denylist"* ]]
    refute_term_shown
}

@test "pre-push runs ci/pre-push last, with stdin from /dev/null, and its refusal refuses" {
    make_repo
    add_remote
    git -C "$REPO" switch -q -c feat/example
    mkdir -p "$REPO/ci"
    printf '%s\n' '#!/bin/sh' 'if read -r line; then echo "ci/pre-push read stdin" >&2; exit 3; fi' \
        'echo "ci/pre-push ran for $1" >&2' 'exit 1' > "$REPO/ci/pre-push"
    run_pre_push refs/heads/feat/example
    [ "$status" -eq 1 ]
    [[ $output == *"ci/pre-push ran for origin"* ]]
    [[ $output == *"ci/pre-push refused the push"* ]]
    [[ $output != *"read stdin"* ]]
}

@test "privacy-scan runs under dash" {
    need command dash
    make_repo
    write_denylist
    git -C "$REPO" switch -q -c feat/example
    stage leak.txt "contact $DENY_TERM"
    run dash -c 'cd "$1" && exec dash .githooks/privacy-scan --staged' dash "$REPO"
    [ "$status" -eq 1 ]
    [[ $output == *"leak.txt:1: denylist"* ]]
}

@test "a configured list without a term counts as missing under privacy.requireDenylist" {
    make_repo
    write_denylist
    mkdir -p "$HOME/lists"
    printf '%s\n' '# emptied by mistake' '' > "$HOME/lists/b"
    git config --global privacy.requireDenylist true
    git config --global privacy.denylist '~/lists/b'
    git -C "$REPO" switch -q -c feat/example
    stage notes.txt 'synthetic notes'
    try_commit "docs: add notes"
    [ "$status" -ne 0 ]
    [[ $output == *"the denylist ~/lists/b holds no term, and privacy.requireDenylist is true"* ]]
}

@test "commit-msg scans only what the commit records: no -v diff, no comment lines" {
    make_repo
    write_denylist
    git -C "$REPO" switch -q -c fix/cleanup
    stage notes.txt "contact $DENY_TERM"
    commit_unchecked "docs: add notes"
    stage notes.txt 'synthetic notes'
    printf 'x\n' > "$REPO/$DENY_TERM-untracked.txt"
    run env GIT_EDITOR=true git -C "$REPO" commit -q -v -e -m "fix: remove the private term"
    [ "$status" -eq 0 ]
    refute_term_shown
}

@test "commit-msg refuses a # line of -m and -F, which git keeps without an editor" {
    make_repo
    write_denylist
    git -C "$REPO" switch -q -c feat/example
    stage notes.txt 'synthetic notes'
    try_commit "$(printf 'docs: add notes\n\n#12 seen by %s' "$DENY_TERM")"
    [ "$status" -ne 0 ]
    [[ $output == *"message line 3: denylist"* ]]
    printf 'docs: add notes\n\n#7 seen by %s\n' "$DENY_TERM" > "$BATS_TEST_TMPDIR/message"
    run git -C "$REPO" commit -q -F "$BATS_TEST_TMPDIR/message"
    [ "$status" -ne 0 ]
    [[ $output == *"message line 3: denylist"* ]]
    refute_term_shown
    [ "$(commit_count)" -eq 1 ]
}

@test "commit-msg follows commit.cleanup: only strip drops # lines, with an editor or without" {
    local mode
    make_repo
    write_denylist
    git -C "$REPO" switch -q -c feat/example
    stage notes.txt 'synthetic notes'
    for mode in whitespace verbatim scissors; do
        run env GIT_EDITOR=true git -C "$REPO" -c commit.cleanup="$mode" commit -q -e \
            -m "docs: add notes" -m "#$DENY_TERM"
        [ "$status" -ne 0 ] || { printf 'commit.cleanup=%s let the line through\n' "$mode" >&2; return 1; }
        [[ $output == *"message line 3: denylist"* ]]
    done
    run env GIT_EDITOR=true git -C "$REPO" commit -q -e -m "docs: add notes" -m "#$DENY_TERM"
    [ "$status" -eq 0 ]
    [[ $(git -C "$REPO" log -1 --format=%B) != *"$DENY_TERM"* ]]
    stage more.txt 'synthetic notes'
    run git -C "$REPO" -c commit.cleanup=strip commit -q -m "docs: add more" -m "#$DENY_TERM"
    [ "$status" -eq 0 ]
    refute_term_shown
    [ "$(commit_count)" -eq 3 ]
}

@test "commit-msg cuts only at git's cut line, written with git's comment string" {
    local cut='------------------------ >8 ------------------------'
    make_repo
    write_denylist
    git -C "$REPO" switch -q -c feat/example
    stage notes.txt 'synthetic notes'
    # Another comment character: git does not cut there.
    try_commit "$(printf 'docs: add notes\n\nx %s\ncontact %s' "$cut" "$DENY_TERM")"
    [ "$status" -ne 0 ]
    [[ $output == *"message line 4: denylist"* ]]
    # git's cut line typed in an editor commit without -v: strip drops the line, keeps the rest.
    run env GIT_EDITOR=true git -C "$REPO" commit -q -e -m "docs: add notes" -m "# $cut" \
        -m "contact $DENY_TERM"
    [ "$status" -ne 0 ]
    [[ $output == *": denylist"* ]]
    refute_term_shown
    [ "$(commit_count)" -eq 1 ]
}

@test "commit-msg cuts the -v diff under a comment string of two characters" {
    make_repo
    write_denylist
    git -C "$REPO" switch -q -c fix/cleanup
    stage notes.txt "contact $DENY_TERM"
    commit_unchecked "docs: add notes"
    # In a subshell: MSYS_NO_PATHCONV keeps Git Bash from reading ;; as a path list, and would
    # also keep a -C path from being converted.
    (cd "$REPO" && MSYS_NO_PATHCONV=1 git config core.commentString ';;')
    [ "$(git -C "$REPO" config --get core.commentString)" = ';;' ]
    stage notes.txt 'synthetic notes'
    run env GIT_EDITOR=true git -C "$REPO" commit -q -v -e -m "fix: remove the private term"
    [ "$status" -eq 0 ]
    refute_term_shown
}

@test "commit-msg hands privacy-scan a copy of the message that only the user can read" {
    local probe=$BATS_TEST_TMPDIR/probe
    # Where the file system keeps no modes, as in Git Bash, there is nothing to see.
    : > "$probe"
    chmod 600 "$probe"
    [[ $(stat -c %a "$probe" 2> /dev/null) == 600 ]] || return 0
    make_repo
    git -C "$REPO" switch -q -c feat/example
    printf '#!/bin/sh\n[ "$1" = --message ] || exit 0\nstat -c %%a "$2" > "%s/mode"\n' \
        "$BATS_TEST_TMPDIR" > "$REPO/.githooks/privacy-scan"
    stage notes.txt 'synthetic notes'
    (umask 022 && GIT_EDITOR=true git -C "$REPO" commit -q -e -m "docs: add notes")
    [ "$(cat "$BATS_TEST_TMPDIR/mode")" = 600 ]
}

@test "commit-msg keeps every line under core.commentChar=auto" {
    make_repo
    write_denylist
    git -C "$REPO" switch -q -c feat/example
    git -C "$REPO" config core.commentChar auto
    stage notes.txt 'synthetic notes'
    run env GIT_EDITOR=true git -C "$REPO" commit -q -e -m "#$DENY_TERM notes"
    [ "$status" -ne 0 ]
    [[ $output == *"message line 1: denylist"* ]]
    refute_term_shown
    [ "$(commit_count)" -eq 1 ]
}

@test "pre-commit refuses a commit on main checked out as MAIN" {
    make_repo
    git -C "$REPO" symbolic-ref HEAD refs/heads/MAIN
    stage notes.txt 'synthetic notes'
    try_commit "docs: add notes"
    [ "$status" -ne 0 ]
    [[ $output == *"commits on 'MAIN' are refused"* ]]
}

@test "pre-push scans the message of an annotated release tag under RELEASE_KIND=legacy" {
    make_repo
    add_remote
    write_denylist
    git -C "$REPO" switch -q -c feat/example
    commit_config RELEASE_KIND=legacy
    git -C "$REPO" -c core.hooksPath=no-hooks tag -a v1.0.0 -m "release by $DENY_TERM"
    run sh -c 'cd "$1" && exec sh .githooks/pre-push origin https://example.invalid/r.git' sh "$REPO" \
        <<< "refs/tags/v1.0.0 $(git -C "$REPO" rev-parse v1.0.0) refs/tags/v1.0.0 $ZERO_OID"
    [ "$status" -ne 0 ]
    [[ $output == *"tag object "*": denylist"* ]]
    refute_term_shown
}

@test "pre-push finds a denylist term in a blob that a release tag points at directly" {
    local blob
    make_repo
    add_remote
    write_denylist
    git -C "$REPO" switch -q -c feat/example
    commit_config RELEASE_KIND=legacy
    blob=$(printf 'notes by %s\n' "$DENY_TERM" | git -C "$REPO" hash-object -w --stdin)
    git -C "$REPO" tag v1.0.0 "$blob"
    run sh -c 'cd "$1" && exec sh .githooks/pre-push origin https://example.invalid/r.git' sh "$REPO" \
        <<< "refs/tags/v1.0.0 $blob refs/tags/v1.0.0 $ZERO_OID"
    [[ $status -ne 0 && $output == *": denylist"* ]] ||
        { printf 'exit %s\n%s\n' "$status" "$output" >&2; return 1; }
    refute_term_shown
}

@test "pre-push finds a denylist term in a blob that a ref outside heads and tags points at" {
    local blob
    make_repo
    add_remote
    write_denylist
    git -C "$REPO" switch -q -c feat/example
    commit_config RELEASE_KIND=tar
    blob=$(printf 'notes by %s\n' "$DENY_TERM" | git -C "$REPO" hash-object -w --stdin)
    git -C "$REPO" update-ref refs/example/notes "$blob"
    run sh -c 'cd "$1" && exec sh .githooks/pre-push origin https://example.invalid/r.git' sh "$REPO" \
        <<< "refs/example/notes $blob refs/example/notes $ZERO_OID"
    [[ $status -ne 0 && $output == *": denylist"* ]] ||
        { printf 'exit %s\n%s\n' "$status" "$output" >&2; return 1; }
    refute_term_shown
}

@test "pre-push still finds a term in a committed file whose blob a pushed tag also points at" {
    local blob
    make_repo
    add_remote
    write_denylist
    git -C "$REPO" switch -q -c feat/example
    commit_config RELEASE_KIND=legacy
    stage notes.txt "notes by $DENY_TERM"
    commit_unchecked "docs: add notes"
    blob=$(git -C "$REPO" rev-parse HEAD:notes.txt)
    git -C "$REPO" tag v1.0.0 "$blob"
    run sh -c 'cd "$1" && exec sh .githooks/pre-push origin https://example.invalid/r.git' sh "$REPO" \
        <<< "refs/tags/v1.0.0 $blob refs/tags/v1.0.0 $ZERO_OID
refs/heads/feat/example $(git -C "$REPO" rev-parse HEAD) refs/heads/feat/example $ZERO_OID"
    [[ $status -ne 0 && $output == *": denylist"* ]] ||
        { printf 'exit %s\n%s\n' "$status" "$output" >&2; return 1; }
    refute_term_shown
}

@test "a relative list named like an awk assignment or an option is still read" {
    local name
    make_repo
    git -C "$REPO" switch -q -c feat/example
    for name in 'deny=list' '-deny'; do
        printf '%s\n' "$DENY_TERM" > "$REPO/$name"
    done
    git -C "$REPO" config privacy.denylist 'deny=list'
    git -C "$REPO" config --add privacy.denylist '-deny'
    git -C "$REPO" config privacy.requireDenylist true
    stage notes.txt "contact $DENY_TERM"
    try_commit "docs: add notes"
    [ "$status" -ne 0 ]
    [[ $output == *"notes.txt:1: denylist"* ]]
    [[ $output != *"holds no term"* ]]
    refute_term_shown
}

@test "pre-push scans the names a root commit adds, whatever log.showRoot says" {
    make_repo
    add_remote
    write_denylist
    git config --global log.showRoot false
    git -C "$REPO" switch -q --orphan feat/example
    stage "docs/notes-$DENY_TERM.md" 'synthetic notes'
    commit_unchecked "docs: add notes"
    # Renamed: the pushed tree names only the new path, so only the root commit's diff shows
    # the first one.
    git -C "$REPO" mv "docs/notes-$DENY_TERM.md" docs/notes.md
    commit_unchecked "docs: rename the notes"
    run_pre_push refs/heads/feat/example
    [ "$status" -ne 0 ]
    [[ $output == *"path name "*" of the scanned paths: denylist"* ]]
    refute_term_shown
}

@test "pre-push reads no path name from the signature check that log.showSignature adds" {
    local key=$BATS_TEST_TMPDIR/signing-key
    need command ssh-keygen
    make_repo
    add_remote
    write_denylist
    git config --global log.showSignature true
    ssh-keygen -q -t ed25519 -N '' -C '' -f "$key"
    # The check names the signer from this list: here the denylist term.
    printf '%s %s\n' "$DENY_TERM" "$(cat "$key.pub")" > "$BATS_TEST_TMPDIR/allowed-signers"
    git -C "$REPO" config gpg.format ssh
    git -C "$REPO" config user.signingkey "$key"
    git -C "$REPO" config gpg.ssh.allowedSignersFile "$BATS_TEST_TMPDIR/allowed-signers"
    git -C "$REPO" switch -q -c feat/example
    stage notes.txt 'synthetic notes'
    git -C "$REPO" -c core.hooksPath=no-hooks commit -q -S -m "docs: add signed notes"
    [[ $(git -C "$REPO" log -1 --format= --name-only) == *"$DENY_TERM"* ]]
    run_pre_push refs/heads/feat/example
    [ "$status" -eq 0 ]
    refute_term_shown
}

@test "the hooks run under an exported CDPATH" {
    make_repo
    write_denylist
    git -C "$REPO" switch -q -c feat/example
    stage notes.txt 'synthetic notes'
    run env CDPATH=. git -C "$REPO" commit -q -m "docs: add notes"
    [ "$status" -eq 0 ]
    [ "$(commit_count)" -eq 2 ]
}

@test "the copy guard never shows an indexed path name that holds a term" {
    make_repo
    write_denylist
    git -C "$REPO" switch -q -c feat/example
    commit_config
    stage .github/standard/MANIFEST 'standard 1.0.0'
    commit_unchecked "chore: adopt the standard"
    # As a cherry-pick, rebase or am would: in the index, never staged through the hooks.
    stage ".github/workflows/deploy-$DENY_TERM.yml" 'name: deploy'
    commit_unchecked "ci: add a deploy workflow"
    write_config CI_WEEKLY=yes
    git -C "$REPO" add .github/standard.conf
    try_commit "ci: run weekly"
    [ "$status" -ne 0 ]
    [[ $output == *"holds a finding"* ]]
    refute_term_shown
}

@test "stub_init gives PATH a stub folder without a drive colon" {
    # A TMPDIR in C:/ form, as Git Bash on Windows may inherit it.
    if command -v cygpath > /dev/null 2>&1; then
        BATS_TEST_TMPDIR=$(cygpath -m "$BATS_TEST_TMPDIR")
    fi
    stub_init
    [[ $STUB_BIN != *:* ]]
    mkstub git <<< 'echo stub-git'
    [ "$(PATH="$STUB_BIN:$PATH" sh -c 'git')" = stub-git ]
}

@test "pre-push gives the rename advice only for a finding, not when the scan cannot run" {
    make_repo
    write_denylist
    git -C "$REPO" switch -q -c feat/example
    commit_config 'PRIVACY_RULES=base disk'
    rm "$REPO/.githooks/rules/disk"
    run_pre_push refs/heads/feat/example
    [ "$status" -eq 2 ]
    [[ $output == *"the rule pack disk is missing"* ]]
    [[ $output != *"a branch or tag name above holds a finding"* ]]
}

@test "pre-commit scans before the copy guard, so standard-check never shows a staged path name" {
    make_repo
    write_denylist
    git -C "$REPO" switch -q -c feat/example
    commit_config
    stage .github/standard/MANIFEST 'standard 1.0.0'
    commit_unchecked "chore: adopt the standard"
    stage ".github/workflows/deploy-$DENY_TERM.yml" 'name: deploy'
    try_commit "ci: add a deploy workflow"
    [ "$status" -ne 0 ]
    [[ $output == *"path name 1 of the scanned paths: denylist"* ]]
    [[ $output != *"not in the MANIFEST"* ]]
    refute_term_shown
}

# The engine runs a fixed number of processes, so git and grep run as often for 50 staged files
# as for one. A loop per staged file would take minutes on Windows (bug 12).
@test "pre-commit runs git and grep as often for 50 staged files as for one" {
    local i one many
    make_repo
    write_denylist
    git -C "$REPO" switch -q -c feat/example
    commit_config 'PRIVACY_RULES=base disk vm'
    stub_init
    mkstub git <<< "exec $(printf '%q' "$(command -v git)") \"\$@\""
    mkstub grep <<< "exec $(printf '%q' "$(command -v grep)") \"\$@\""
    stage notes-0.txt 'synthetic notes 0'
    PATH="$STUB_BIN:$PATH" sh -c 'cd "$1" && exec sh .githooks/pre-commit' sh "$REPO"
    one=$(awk '{ print $1 }' "$STUB_LOG" | sort | uniq -c)
    : > "$STUB_LOG"
    for i in {1..49}; do
        printf 'synthetic notes %s\n' "$i" > "$REPO/notes-$i.txt"
    done
    git -C "$REPO" add -- 'notes-*.txt'
    PATH="$STUB_BIN:$PATH" sh -c 'cd "$1" && exec sh .githooks/pre-commit' sh "$REPO"
    many=$(awk '{ print $1 }' "$STUB_LOG" | sort | uniq -c)
    [[ -n $one && $one == "$many" ]] ||
        { printf 'one file:\n%s\n50 files:\n%s\n' "$one" "$many" >&2; return 1; }
}
