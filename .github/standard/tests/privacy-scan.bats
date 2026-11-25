#!/usr/bin/env bats
# privacy-scan.bats of the repository standard: the same file in every repository; never edit it
# here. The rule packs base, disk and vm against a catalogue of leaks and of synthetic values,
# PRIVACY_ALLOW and PRIVACY_HISTORY_ACCEPT, every mode of privacy-scan, and its failures. The
# leaks are built from fragments at run time, so this file holds no value a rule matches.

# The sh -c scripts are single-quoted, ~ stays literal in git config values, and bats runs each
# test in a subshell, all on purpose.
# shellcheck disable=SC1003,SC2016,SC2030,SC2031,SC2088
load lib

setup() {
    isolate_home
}

# Run privacy-scan with ARGS in DIR (default $REPO) through bats `run`, stderr apart.
scan_in() {
    local dir=$1
    shift
    run --separate-stderr sh -c 'cd "$1" && shift && exec sh .githooks/privacy-scan "$@"' \
        sh "$dir" "$@"
}
scan() {
    scan_in "$REPO" "$@"
}

# Print one leak of each kind per line; the comment names the kind of each line.
leaks() {
    printf 'addr %s.%s.%s.%s\n' 192 168 7 9                   # 1 ipv4-private
    printf 'addr %s.%s.%s.%s\n' 10 1 2 3                      # 2 ipv4-private
    printf 'addr %s.%s.%s.%s\n' 172 20 0 5                    # 3 ipv4-private
    printf 'addr %s.%s.%s.%s\n' 100 100 4 2                   # 4 ipv4-private
    printf 'addr %s::%s\n' fe80 1                             # 5 ipv6-private
    printf 'addr %s:%s::%s\n' fd12 3456 1                     # 6 ipv6-private
    printf 'host printer.%s\n' lan                            # 7 host-name
    printf 'host nas.home.%s\n' arpa                          # 8 host-name
    printf 'host pi.%s\n' local                               # 9 host-name
    printf 'host db.%s\n' internal                            # 10 host-name
    printf 'host laptop.tail1234.ts.%s\n' net                 # 11 host-name
    printf 'mac %s:%s:%s:%s:%s:%s\n' 3c 22 fb 12 34 56        # 12 mac-address
    printf 'mac %s-%s-%s-%s-%s-%s\n' B8 27 EB 12 34 56        # 13 mac-address
    printf 'path /%s/someone/file\n' home                     # 14 home-path
    printf 'win C:%sUsers%ssomeone\n' '\' '\'                 # 15 home-path
    printf 'win C:/%s/someone\n' Users                         # 16 home-path
    printf 'msys /c/%s/someone\n' Users                       # 17 home-path
    printf 'mac /%s/someone\n' Users                          # 18 home-path
    printf 'mail someone@%s\n' example.de                     # 19 e-mail
    printf 'uuid %s-%s-%s-%s-%s\n' d00dfeed cafe 4abc 8def 0123456789ab   # 20 uuid
    printf 'luks %s%s%s%s%s\n' d00dfeed cafe 4abc 8def 0123456789ab       # 21 uuid-dashless
    printf 'serial %s%s\n' D00DFEED CAFEF00D                  # 22 ntfs-serial
    printf 'fat %s="%s-%s"\n' UUID 1A2B 3C4D                  # 23 fat-serial
    printf 'search --fs-uuid --set=root %s-%s\n' 1a2b 3c4d    # 24 fat-serial
    printf 'lvm %s-%s-%s-%s-%s-%s-%s\n' Ab12Cd 3456 7890 abcd EfGh 1234 Zz99Yy   # 25 lvm-uuid
    printf 'disk %sUUID="%s-01"\n' PART d00dfeed              # 26 partuuid
    printf 'id /dev/disk/%s/%s\n' by-id usb-Vendor_Model_123456-0:0      # 27 disk-by-id
    printf 'vm %s %s %s %s %s %s %s %s-%s %s %s %s %s %s %s %s\n' \
        56 4d 12 34 56 78 9a bc de f0 12 34 56 78 9a bc       # 28 vmware-uuid
    printf 'nic %s="%s"\n' MACAddress 080027AB12CD            # 29 vbox-mac
    printf 'label %s\n' "${DENY_TERM^^}"                      # 30 denylist
}

# Print synthetic and reserved values that every pack lets through.
synthetic() {
    printf 'addr %s.%s.%s.%s\n' 192 0 2 10
    printf 'addr %s:%s::%s\n' 2001 db8 1
    printf 'host example-nas.home.%s\n' arpa
    printf 'host example-pi.%s\n' local
    printf 'mac %s-%s-%s-%s-%s-%s\n' 00 00 5E 00 53 01
    printf 'mac %s:%s:%s:%s:%s:%s\n' 5c a1 ab 1e 00 01
    printf 'mail test@example.%s\n' invalid
    printf 'mail someone@example.%s\n' com
    printf 'mail %s@%s.%s\n' 12345+someone users.noreply.github com
    printf 'trailer <noreply@%s.%s>\n' example org
    printf 'remote git@github.%s:owner/repo.git\n' com
    printf 'uuid %s-%s-%s-%s-%s\n' 5ca1ab1e cafe 4abc 8def 0123456789ab
    printf 'luks %s%s\n' 5ca1ab1e 0123456789abcdef01234567
    printf 'serial %s%s\n' 5CA1AB1E 01234567
    printf 'fat %s="%s"\n' UUID 5CA1-AB1E
    printf 'lvm %s-%s-%s-%s-%s-%s-%s\n' 5ca1ab 3456 7890 abcd EfGh 1234 Zz99Yy
    printf 'disk %sUUID=%s-01\n' PART 5ca1ab1e
    printf 'id /dev/disk/%s/%s\n' by-id usb-Synthetic_Stick_5CA1AB1E00000001-0:0
    printf 'id /dev/disk/%s/%s\n' by-id ata-Synthetic-part1
    printf 'glob /dev/disk/%s/*\n' by-id
    printf 'vm %s %s %s %s %s %s %s %s-%s %s %s %s %s %s %s %s\n' \
        5c a1 ab 1e 00 00 00 01 de f0 12 34 56 78 9a bc
    printf 'nic %s="%s"\n' MACAddress 5CA1AB1E0001
}

@test "privacy-scan reports each kind of leak by file and line, never the value" {
    local blob hit name
    local -a want=()
    make_repo
    write_denylist
    git -C "$REPO" switch -q -c feat/example
    commit_config 'PRIVACY_RULES=base disk vm'
    leaks > "$REPO/leak.txt"
    printf 'one\r\ntwo\r\n' > "$REPO/crlf.txt"
    name=$(printf 'nas.%s.txt' lan)
    printf 'synthetic\n' > "$REPO/$name"
    git -C "$REPO" add leak.txt crlf.txt "$name"
    blob=$(printf '/%s/someone/x' home | git -C "$REPO" hash-object -w --stdin)
    git -C "$REPO" update-index --add --cacheinfo "120000,$blob,link"
    scan --staged
    [ "$status" -eq 1 ]
    want=(1 2 3 4)
    for hit in "${want[@]}"; do [[ $stderr == *"leak.txt:$hit: ipv4-private"* ]]; done
    for hit in 5 6; do [[ $stderr == *"leak.txt:$hit: ipv6-private"* ]]; done
    for hit in 7 8 9 10 11; do [[ $stderr == *"leak.txt:$hit: host-name"* ]]; done
    for hit in 12 13; do [[ $stderr == *"leak.txt:$hit: mac-address"* ]]; done
    for hit in 14 15 16 17 18; do [[ $stderr == *"leak.txt:$hit: home-path"* ]]; done
    for hit in "19: e-mail" "20: uuid" "21: uuid-dashless" "22: ntfs-serial" "23: fat-serial" \
        "24: fat-serial" "25: lvm-uuid" "26: partuuid" "27: disk-by-id" "28: vmware-uuid" \
        "29: vbox-mac" "30: denylist"; do
        [[ $stderr == *"leak.txt:$hit"* ]] || { printf 'missing: leak.txt:%s\n%s\n' "$hit" "$stderr" >&2; return 1; }
    done
    [[ $stderr == *"privacy-scan: crlf.txt: crlf"* ]]
    [[ $stderr == *"privacy-scan: link:1: home-path"* ]]
    [[ $stderr == *"privacy-scan: path name 4 of the scanned paths: host-name"* ]]
    # Exactly these 33 findings: nothing twice, nothing else.
    [ "$(grep -c '^privacy-scan: .*: [a-z0-9-]*$' <<< "$stderr")" -eq 33 ]
    [[ $stderr != *someone* && $stderr != *d00dfeed* && $stderr != *D00DFEED* && $stderr != *1A2B* ]]
    refute_term_shown
    assert_tmpdir_empty
}

@test "the synthetic and reserved values pass every pack" {
    make_repo
    write_denylist
    git -C "$REPO" switch -q -c feat/example
    commit_config 'PRIVACY_RULES=base disk vm'
    synthetic > "$REPO/fixture.txt"
    printf 'synthetic\n' > "$REPO/example-nas.lan.txt"
    git -C "$REPO" add fixture.txt example-nas.lan.txt
    scan --staged
    [ "$status" -eq 0 ]
    # privacy-scan writes findings and notes to stderr only; with a denylist in place a clean
    # scan prints nothing at all.
    [ -z "$stderr" ]
}

@test "a label that only starts with local is no host name; one after lan, internal, home.arpa or ts.net still hits" {
    local hit
    make_repo
    git -C "$REPO" switch -q -c feat/example
    commit_config PRIVACY_RULES=base
    {
        printf 'dir x.%s-tools-old\n' local                   # 1 passes
        printf 'dir y.%s-backup\n' lan                        # 2 host-name
        printf 'dir z.%s-tools\n' internal                    # 3 host-name
        printf 'dir n.home.%s-old\n' arpa                     # 4 host-name
        printf 'dir t.ts.%s-x\n' net                          # 5 host-name
        printf 'dir X.%s-AI-OLD\n' LOCAL                      # 6 passes
        printf 'host example-nas.%s-ai.%s\n' local lan        # 7 passes
        printf 'host h.%s\n' local                            # 8 host-name
        printf 'host h.%s.\n' local                           # 9 host-name
        printf 'host h.%s-ai.%s\n' local lan                  # 10 host-name
        printf 'host h.%s- end\n' local                       # 11 host-name
        printf 'host h.%s-ai.%s.%s\n' lan internal lan        # 12 host-name
        printf 'log h.%s-2026-11-20.log\n' local              # 13 host-name
        printf 'host h.%s-ai_x\n' local                       # 14 host-name
    } > "$REPO/hosts.txt"
    git -C "$REPO" add hosts.txt
    scan --staged
    [ "$status" -eq 1 ]
    for hit in 2 3 4 5 8 9 10 11 12 13 14; do [[ $stderr == *"hosts.txt:$hit: host-name"* ]]; done
    [ "$(grep -c '^privacy-scan: .*: [a-z0-9-]*$' <<< "$stderr")" -eq 11 ]
    # After the other suffixes, the hit is the host name alone, as in 1.0.1, so an exemption
    # written for it fits. The scan reads the config from HEAD, so it is committed apart.
    git -C "$REPO" reset -q -- hosts.txt
    commit_config PRIVACY_RULES=base "$(printf 'PRIVACY_ALLOW=host-name ^hosts\\.txt$ ^(y\\.%s|z\\.%s|n\\.home\\.%s|t\\.ts\\.%s)$' lan internal arpa net)"
    git -C "$REPO" add hosts.txt
    scan --staged
    [ "$status" -eq 1 ]
    for hit in 2 3 4 5; do [[ $stderr != *"hosts.txt:$hit: host-name"* ]]; done
    [ "$(grep -c '^privacy-scan: .*: [a-z0-9-]*$' <<< "$stderr")" -eq 7 ]
}

@test "the Dependabot sign-off passes, and another github.com address still hits" {
    local trailer
    trailer=$(printf 'Signed-off-by: dependabot[bot] <%s@%s.%s>' support github com)
    make_repo
    git -C "$REPO" switch -q -c feat/example
    commit_config PRIVACY_RULES=base
    add_remote
    printf 'chore(deps): bump actions/checkout\n\n%s\n' "$trailer" > "$BATS_TEST_TMPDIR/msg"
    scan --message "$BATS_TEST_TMPDIR/msg"
    [ "$status" -eq 0 ]
    [[ $stderr != *e-mail* ]]
    stage notes.txt 'synthetic notes'
    git -C "$REPO" -c core.hooksPath=no-hooks commit -q -F "$BATS_TEST_TMPDIR/msg"
    scan --range HEAD --not --remotes=origin
    [ "$status" -eq 0 ]
    [[ $stderr != *e-mail* ]]
    scan --history
    [ "$status" -eq 0 ]
    [[ $stderr != *e-mail* ]]
    printf 'docs: notes\n\nfrom <%s@%s.%s>\n' someone github com > "$BATS_TEST_TMPDIR/msg"
    scan --message "$BATS_TEST_TMPDIR/msg"
    [ "$status" -eq 1 ]
    [[ $stderr == *"message line 3: e-mail"* ]]
    stage notes.txt 'more synthetic notes'
    git -C "$REPO" -c core.hooksPath=no-hooks commit -q -F "$BATS_TEST_TMPDIR/msg"
    scan --range HEAD --not --remotes=origin
    [ "$status" -eq 1 ]
    [[ $stderr == *"of git cat-file -p: e-mail"* ]]
    printf 'docs: notes\n\nfrom <%s@%s.%s>\n' support github io > "$BATS_TEST_TMPDIR/msg"
    scan --message "$BATS_TEST_TMPDIR/msg"
    [ "$status" -eq 1 ]
    [[ $stderr == *"message line 3: e-mail"* ]]
}

@test "privacy-scan refuses a bad option or argument count with exit 64" {
    make_repo
    scan --all
    [ "$status" -eq 64 ]
    [[ $stderr == usage:* ]]
    scan
    [ "$status" -eq 64 ]
    scan --staged extra
    [ "$status" -eq 64 ]
    scan --message
    [ "$status" -eq 64 ]
    scan --range
    [ "$status" -eq 64 ]
}

@test "privacy-scan --tracked finds nothing in this repository" {
    scan_in "$REPO_ROOT" --tracked
    [ "$status" -eq 0 ]
}

@test "the copies are the MANIFEST's file entries that the config selects, as standard-check does" {
    local manifest=$BATS_TEST_TMPDIR/MANIFEST conf=$BATS_TEST_TMPDIR/standard.conf
    { printf 'standard 1.0.0\n'
      printf 'file %s 0123 100644 %s\n' all a release r mit LICENSE public p private q
      printf 'block all 0123 - x.md#y\n'; } > "$manifest"
    printf 'NAME=example\nVISIBILITY=public\nRELEASE_KIND=tar\n' > "$conf"
    [ "$(manifest_copies "$manifest" "$conf" | tr '\n' ' ')" = "a r LICENSE p " ]
    printf 'NAME=example\nVISIBILITY=private\nLICENSE=none\nRELEASE_KIND=legacy\n' > "$conf"
    [ "$(manifest_copies "$manifest" "$conf" | tr '\n' ' ')" = "a q " ]
}

@test "the standard's copies pass every rule pack" {
    REPO="$BATS_TEST_TMPDIR/copy"
    # Only the copies this repository holds: a repository's own files, its legacy release files
    # among them, belong to its own --tracked scan, with its own packs and exemptions.
    manifest_copies "$REPO_ROOT/.github/standard/MANIFEST" "$REPO_ROOT/.github/standard.conf" |
        git -C "$REPO_ROOT" checkout-index --prefix="$REPO/" --stdin
    git init -q -b main "$REPO"
    git -C "$REPO" config user.name "Test User"
    git -C "$REPO" config user.email test@example.invalid
    git -C "$REPO" config core.autocrlf false
    git -C "$REPO" add -A
    commit_config 'PRIVACY_RULES=base disk vm'
    scan --tracked
    [ "$status" -eq 0 ]
}

@test "a PRIVACY_ALLOW exemption covers its own kind and paths only" {
    local home
    home=$(printf '/%s/%s' home example-user)
    make_repo
    git -C "$REPO" switch -q -c feat/example
    commit_config PRIVACY_RULES=base "PRIVACY_ALLOW=home-path ^tests/ ^$home\$"
    stage tests/a.txt "home $home"
    stage docs/b.txt "home $home"
    stage tests/c.txt "$(printf 'home /%s/someone' home)"
    scan --staged
    [ "$status" -eq 1 ]
    [[ $stderr == *"docs/b.txt:1: home-path"* ]]
    [[ $stderr == *"tests/c.txt:1: home-path"* ]]
    [[ $stderr != *"tests/a.txt"* ]]
}

@test "--message checks a file or stdin and names message lines" {
    make_repo
    write_denylist
    commit_config PRIVACY_RULES=base
    printf 'feat: add notes\n\nseen at printer.%s\nby %s\n' lan "$DENY_TERM" > "$BATS_TEST_TMPDIR/msg"
    scan --message "$BATS_TEST_TMPDIR/msg"
    [ "$status" -eq 1 ]
    [[ $stderr == *"message line 3: host-name"* ]]
    [[ $stderr == *"message line 4: denylist"* ]]
    refute_term_shown
    run --separate-stderr sh -c 'cd "$1" && printf "fix: a clean title\n" | sh .githooks/privacy-scan --message -' \
        sh "$REPO"
    [ "$status" -eq 0 ]
}

@test "--range scans what the commits bring in, not what the remote already has" {
    make_repo
    git -C "$REPO" switch -q -c feat/example
    commit_config PRIVACY_RULES=base
    git -C "$REPO" switch -q main
    git -C "$REPO" merge -q --ff-only feat/example
    stage old.txt "$(printf 'old /%s/someone' home)"
    git -C "$REPO" -c core.hooksPath=no-hooks commit -q -m "docs: old notes"
    add_remote
    git -C "$REPO" switch -q feat/example
    git -C "$REPO" merge -q --ff-only main
    stage notes.txt 'synthetic notes'
    git -C "$REPO" -c core.hooksPath=no-hooks commit -q -m "docs: notes"
    scan --range HEAD --not --remotes=origin
    [ "$status" -eq 0 ]
    stage new.txt "$(printf 'new /%s/someone' home)"
    git -C "$REPO" -c core.hooksPath=no-hooks commit -q -m "docs: new notes"
    scan --range HEAD --not --remotes=origin
    [ "$status" -eq 1 ]
    [[ $stderr == *"new.txt:1: home-path"* ]]
    [[ $stderr != *"old.txt"* ]]
}

@test "--history honours PRIVACY_HISTORY_ACCEPT for that commit and kind only" {
    local sha
    make_repo
    git -C "$REPO" switch -q -c feat/example
    commit_config PRIVACY_RULES=base
    stage old.txt "$(printf 'mail someone@%s\nhome /%s/someone' example.de home)"
    git -C "$REPO" -c core.hooksPath=no-hooks commit -q -m "docs: old notes"
    sha=$(git -C "$REPO" rev-parse HEAD)
    scan --history
    [ "$status" -eq 1 ]
    [[ $stderr == *"old.txt:1: e-mail"* ]]
    commit_config PRIVACY_RULES=base "PRIVACY_HISTORY_ACCEPT=$sha e-mail"
    scan --history
    [ "$status" -eq 1 ]
    [[ $stderr == *"old.txt:2: home-path"* ]]
    [[ $stderr != *"e-mail"* ]]
}

@test "--history still finds a term in a committed file whose blob a tag also points at" {
    make_repo
    write_denylist
    git -C "$REPO" switch -q -c feat/example
    commit_config PRIVACY_RULES=base
    stage notes.txt "notes by $DENY_TERM"
    git -C "$REPO" -c core.hooksPath=no-hooks commit -q -m "docs: notes"
    git -C "$REPO" tag example-blob "$(git -C "$REPO" rev-parse HEAD:notes.txt)"
    scan --history
    [ "$status" -eq 1 ] || { printf 'exit %s\n' "$status" >&2; return 1; }
    [[ $stderr == *": denylist"* ]]
    # No record can accept a denylist term, so the scan names no commit for one.
    [[ $stderr != *"brought in"* && $stderr != *"PRIVACY_HISTORY_ACCEPT"* ]]
}

@test "a PRIVACY_ALLOW line for the empty path does not exempt a blob that a tag names directly" {
    local blob
    make_repo
    git -C "$REPO" switch -q -c feat/example
    commit_config PRIVACY_RULES=base 'PRIVACY_ALLOW=e-mail ^$ ^someone@example\.de$'
    blob=$(printf 'mail someone@%s\n' example.de | git -C "$REPO" hash-object -w --stdin)
    git -C "$REPO" tag example-blob "$blob"
    scan --history
    [ "$status" -eq 1 ] || { printf 'exit %s\n%s\n' "$status" "$stderr" >&2; return 1; }
    [[ $stderr == *"privacy-scan: blob ${blob:0:12}, line 1: e-mail"* ]]
}

@test "CRLF in the index is refused, except in -text paths" {
    make_repo
    git -C "$REPO" switch -q -c feat/example
    printf '*.bin -text\n' > "$REPO/.gitattributes"
    printf 'one\r\ntwo\r\n' > "$REPO/crlf.txt"
    printf 'one\r\ntwo\r\n' > "$REPO/data.bin"
    git -C "$REPO" add .gitattributes crlf.txt data.bin
    scan --staged
    [ "$status" -eq 1 ]
    [[ $stderr == *"crlf.txt: crlf"* ]]
    [[ $stderr != *"data.bin"* ]]
}

@test "a committed config that does not parse makes every pack run, with a note" {
    make_repo
    git -C "$REPO" switch -q -c feat/example
    commit_config BOGUS=1
    stage disk.txt "$(printf 'uuid %s-%s-%s-%s-%s' d00dfeed cafe 4abc 8def 0123456789ab)"
    scan --staged
    [ "$status" -eq 1 ]
    [[ $stderr == *"does not parse, so every rule pack runs"* ]]
    [[ $stderr == *"disk.txt:1: uuid"* ]]
}

@test "a rule pack line that does not parse stops the scan with exit 2" {
    make_repo
    printf 'broken|rule\n' >> "$REPO/.githooks/rules/base"
    git -C "$REPO" switch -q -c feat/example
    commit_config PRIVACY_RULES=base
    stage notes.txt 'synthetic notes'
    scan --staged
    [ "$status" -eq 2 ]
    [[ $stderr == *"not kind|ERE|exempt-ERE"* ]]
    [[ $stderr == *"a rule pack does not parse, so this is refused"* ]]
}

@test "a missing rule pack stops the scan with exit 2" {
    make_repo
    git -C "$REPO" switch -q -c feat/example
    commit_config PRIVACY_RULES=vm
    rm "$REPO/.githooks/rules/vm"
    stage notes.txt 'synthetic notes'
    scan --staged
    [ "$status" -eq 2 ]
    [[ $stderr == *"the rule pack vm is missing"* ]]
}

@test "the packs skip only the PRIVACY_ALLOW lines that conf accepts, and only in the config" {
    local home
    home=$(printf '/%s/%s' home example-user)
    make_repo
    git -C "$REPO" switch -q -c feat/example
    commit_config PRIVACY_RULES=base
    # An unanchored value ERE: conf refuses the line, so the packs scan it.
    write_config PRIVACY_RULES=base "PRIVACY_ALLOW=home-path ^tests/ $home\$"
    git -C "$REPO" add .github/standard.conf
    scan --staged
    [ "$status" -eq 1 ]
    [[ $stderr == *".github/standard.conf:5: home-path"* ]]
    # An accepted line passes in the config, but the same blob at another path is scanned.
    write_config PRIVACY_RULES=base "PRIVACY_ALLOW=home-path ^tests/ ^$home\$"
    cp "$REPO/.github/standard.conf" "$REPO/copy.conf"
    git -C "$REPO" add .github/standard.conf copy.conf
    scan --staged
    [ "$status" -eq 1 ]
    [[ $stderr == *"copy.conf:5: home-path"* ]]
    [[ $stderr != *"standard.conf:5"* ]]
}

@test "an accepted PRIVACY_ALLOW line still reports hits of other kinds and in its path ERE" {
    make_repo
    git -C "$REPO" switch -q -c feat/example
    commit_config PRIVACY_RULES=base
    write_config PRIVACY_RULES=base \
        "$(printf 'PRIVACY_ALLOW=home-path ^tests/ ^(/%s/example-user|someone@%s)$' home example.de)"
    git -C "$REPO" add .github/standard.conf
    scan --staged
    [ "$status" -eq 1 ]
    [[ $stderr == *".github/standard.conf:5: e-mail"* ]]
    [[ $stderr != *"home-path"* ]]
    # A value of the line's own kind in its path ERE: conf refuses the line, so nothing skips it.
    write_config PRIVACY_RULES=base \
        "$(printf 'PRIVACY_ALLOW=home-path ^/%s/quuxly ^/%s/example-user$' home home)"
    git -C "$REPO" add .github/standard.conf
    scan --staged
    [ "$status" -eq 1 ]
    [[ $stderr == *".github/standard.conf:5: home-path"* ]]
}

@test "--history keeps reporting a later value at a path whose old finding is accepted" {
    local sha
    make_repo
    git -C "$REPO" switch -q -c feat/example
    commit_config PRIVACY_RULES=base
    stage hosts.txt "$(printf 'backup target: nas.%s' lan)"
    git -C "$REPO" -c core.hooksPath=no-hooks commit -q -m "docs: old hosts"
    sha=$(git -C "$REPO" rev-parse HEAD)
    commit_config PRIVACY_RULES=base "PRIVACY_HISTORY_ACCEPT=$sha host-name"
    scan --history
    [ "$status" -eq 0 ]
    printf 'new target: printer.%s\n' lan >> "$REPO/hosts.txt"
    git -C "$REPO" add hosts.txt
    git -C "$REPO" -c core.hooksPath=no-hooks commit -q -m "docs: new host"
    scan --history
    [ "$status" -eq 1 ]
    [[ $stderr == *"hosts.txt:2: host-name"* ]]
}

# Commit what is staged in $REPO with the hooks off, and print the new commit's full SHA.
commit_off() {
    git -C "$REPO" -c core.hooksPath=no-hooks commit -q -m "$1"
    git -C "$REPO" rev-parse HEAD
}

@test "--history names the commits that brought in each version behind a file finding" {
    local c1 c2 c3 c v
    make_repo
    git -C "$REPO" switch -q -c feat/example
    commit_config PRIVACY_RULES=base
    stage notes.txt "$(printf 'mail someone@%s' example.de)"
    c1=$(commit_off "docs: notes")
    printf 'more\n' >> "$REPO/notes.txt"
    git -C "$REPO" add notes.txt
    c2=$(commit_off "docs: more notes")
    printf 'end\n' >> "$REPO/notes.txt"
    git -C "$REPO" add notes.txt
    c3=$(commit_off "docs: end of the notes")
    scan --history
    [ "$status" -eq 1 ]
    [ "$(grep -c -F 'privacy-scan: notes.txt:1: e-mail' <<< "$stderr")" -eq 1 ]
    for c in "$c1" "$c2" "$c3"; do
        v=$(git -C "$REPO" rev-parse "$c:notes.txt")
        [[ $stderr == *"privacy-scan:   in version ${v:0:12}, brought in by commit $c"$'\n'* ]]
    done
    [ "$(grep -c 'brought in by' <<< "$stderr")" -eq 3 ]
    # Each finding line ends in ": KIND": tools/status takes the kind from there.
    [ "$(sed -n 's/^privacy-scan: .*: \([a-z0-9-]*\)$/\1/p' <<< "$stderr" | sort -u)" = e-mail ]
    [[ $stderr != *"someone@"* ]]
    # Only the history scan names commits.
    scan --range HEAD
    [ "$status" -eq 1 ]
    [[ $stderr != *"brought in by"* ]]
    scan --tracked
    [ "$status" -eq 1 ]
    [[ $stderr != *"brought in by"* ]]
}

@test "--history names both commits of a version a revert brings back, and their records accept it" {
    local c1 c3 v accept=()
    make_repo
    git -C "$REPO" switch -q -c feat/example
    commit_config PRIVACY_RULES=base
    stage notes.txt "$(printf 'mail someone@%s' example.de)"
    c1=$(commit_off "docs: notes")
    stage notes.txt 'synthetic notes'
    commit_off "docs: clean the notes" > /dev/null
    git -C "$REPO" -c core.hooksPath=no-hooks revert --no-edit HEAD > /dev/null
    c3=$(git -C "$REPO" rev-parse HEAD)
    v=$(git -C "$REPO" rev-parse "$c1:notes.txt")
    [ "$(git -C "$REPO" rev-parse "$c3:notes.txt")" = "$v" ]
    scan --history
    [ "$status" -eq 1 ]
    [[ $stderr == *"privacy-scan:   in version ${v:0:12}, brought in by commit $c3 $c1"$'\n'* ]]
    # Records for exactly the commits it names make the history scan pass.
    while read -r c; do
        accept+=("PRIVACY_HISTORY_ACCEPT=$c e-mail")
    done < <(sed -n 's/^privacy-scan:   in version [0-9a-f]*, brought in by commit //p' <<< "$stderr" | tr ' ' '\n')
    [ "${#accept[@]}" -eq 2 ]
    commit_config PRIVACY_RULES=base "${accept[@]}"
    scan --history
    [ "$status" -eq 0 ] || { printf '%s\n' "$stderr" >&2; return 1; }
}

@test "--history names the commits in HEAD first, and marks those outside it, which secret-scan refuses" {
    local t1 t2 m1 v1 v2
    make_repo
    git -C "$REPO" switch -q -c feat/example
    commit_config PRIVACY_RULES=base
    # A topic branch brings in a version, and a squash brings the same version into HEAD.
    git -C "$REPO" switch -q -c feat/topic
    stage notes.txt "$(printf 'mail someone@%s' example.de)"
    t1=$(commit_off "docs: notes on the topic")
    printf 'more\n' >> "$REPO/notes.txt"
    git -C "$REPO" add notes.txt
    t2=$(commit_off "docs: more notes on the topic")
    git -C "$REPO" switch -q feat/example
    stage notes.txt "$(printf 'mail someone@%s' example.de)"
    m1=$(commit_off "docs: notes")
    v1=$(git -C "$REPO" rev-parse "$m1:notes.txt")
    v2=$(git -C "$REPO" rev-parse "$t2:notes.txt")
    scan --history
    [ "$status" -eq 1 ]
    [[ $stderr == *"privacy-scan:   in version ${v1:0:12}, brought in by commit $m1, and outside HEAD by commit $t1"$'\n'* ]]
    [[ $stderr == *"privacy-scan:   in version ${v2:0:12}, brought in outside HEAD only, by commit $t2"$'\n'* ]]
    [[ $stderr == *"pick a commit that the base branch holds"* ]]
}

@test "--history names commits under a hidden path name, and shows neither the name nor a value" {
    local c v
    make_repo
    git -C "$REPO" switch -q -c feat/example
    commit_config PRIVACY_RULES=base
    stage "$(printf 'someone@%s.txt' example.de)" "$(printf 'mail other@%s' example.de)"
    c=$(commit_off "docs: a file with a bad name")
    v=$(git -C "$REPO" rev-parse "$c:$(printf 'someone@%s.txt' example.de)")
    scan --history
    [ "$status" -eq 1 ]
    [[ $stderr == *"path name "*" of the scanned paths:1: e-mail"* ]]
    [[ $stderr == *"privacy-scan:   in version ${v:0:12}, brought in by commit $c"* ]]
    [[ $stderr != *"someone@"* && $stderr != *"other@"* ]]
}

@test "--history says when no commit can accept a version: a blob that only a ref or a merge brings in" {
    local blob v
    make_repo
    git -C "$REPO" switch -q -c feat/example
    commit_config PRIVACY_RULES=base
    blob=$(printf 'mail someone@%s\n' example.de | git -C "$REPO" hash-object -w --stdin)
    git -C "$REPO" tag example-blob "$blob"
    # Two branches change one line, and the merge resolves it to a third text that holds a value.
    stage notes.txt 'base'
    commit_off "docs: notes" > /dev/null
    git -C "$REPO" switch -q -c feat/other
    stage notes.txt 'other'
    commit_off "docs: other notes" > /dev/null
    git -C "$REPO" switch -q feat/example
    stage notes.txt 'ours'
    commit_off "docs: our notes" > /dev/null
    git -C "$REPO" -c core.hooksPath=no-hooks merge -q --no-edit feat/other > /dev/null 2>&1 || true
    stage notes.txt "$(printf 'home /%s/someone' home)"
    git -C "$REPO" -c core.hooksPath=no-hooks commit -q --no-edit
    v=$(git -C "$REPO" rev-parse HEAD:notes.txt)
    scan --history
    [ "$status" -eq 1 ]
    [[ $stderr == *"privacy-scan: blob ${blob:0:12}, line 1: e-mail"* ]]
    [[ $stderr == *"privacy-scan:   in version ${blob:0:12}, brought in by no commit that a PRIVACY_HISTORY_ACCEPT record can name"* ]]
    [[ $stderr == *"privacy-scan: notes.txt:1: home-path"* ]]
    [[ $stderr == *"privacy-scan:   in version ${v:0:12}, brought in by no commit that a PRIVACY_HISTORY_ACCEPT record can name"* ]]
    # As the message says, a record for the merge itself does not accept that version.
    commit_config PRIVACY_RULES=base "PRIVACY_HISTORY_ACCEPT=$(git -C "$REPO" rev-parse HEAD) home-path"
    scan --history
    [ "$status" -eq 1 ]
    [[ $stderr == *"privacy-scan: notes.txt:1: home-path"* ]]
}

@test "a fullwidth at sign, a mixed fat-serial line and stdin are checked too" {
    make_repo
    git -C "$REPO" switch -q -c feat/example
    commit_config 'PRIVACY_RULES=base disk'
    { printf 'mail someone\357\274\240%s\n' example.de
      printf 'search --fs-uuid --set=root %s-%s || search --fs-uuid --set=root %s-%s\n' 5CA1 AB1E 1A2B 3C4D
    } > "$REPO/notes.txt"
    git -C "$REPO" add notes.txt
    scan --staged
    [ "$status" -eq 1 ]
    [[ $stderr == *"notes.txt:1: e-mail"* ]]
    [[ $stderr == *"notes.txt:2: fat-serial"* ]]
    run --separate-stderr sh -c 'cd "$1" && printf "fix: seen at printer.%s\n" lan | sh .githooks/privacy-scan --message -' \
        sh "$REPO"
    [ "$status" -eq 1 ]
    [[ $stderr == *"message line 1: host-name"* ]]
}

@test "fat-serial covers quoted, ieee1275 and parenthesised GRUB hints, and exempts only its own serial" {
    local s=1A2B-3C4D syn=5CA1-AB1E line
    make_repo
    git -C "$REPO" switch -q -c feat/example
    commit_config 'PRIVACY_RULES=disk'
    { printf "search --no-floppy --fs-uuid --set=root --hint='hd0,msdos1'  %s\n" "$s"
      printf "search --fs-uuid --hint-ieee1275='ieee1275//vdevice/v-scsi@3000/disk@%s%s,msdos2' --hint-bios=hd0,msdos2  %s\n" \
          81000000 00000000 "$s"
      printf 'search --fs-uuid --set=root --hint=(hd0,gpt1) %s\n' "$s"
      printf 'search --fs-uuid --hint=%s --set=root %s\n' "$syn" "$s"
      printf 'search --fs-uuid --set=root %s\n' "$syn"
      printf 'search --fs-uuid --set=root\t%s\n' "$s"
    } > "$REPO/grub.cfg"
    git -C "$REPO" add grub.cfg
    scan --staged
    [ "$status" -eq 1 ]
    for line in 1 2 3 4 6; do
        [[ $stderr == *"grub.cfg:$line: fat-serial"* ]] ||
            { printf 'line %s: no fat-serial finding\n' "$line" >&2; return 1; }
    done
    [[ $stderr != *"grub.cfg:5: fat-serial"* ]]
}

@test "a denylist term written with a fullwidth at sign matches either at sign" {
    make_repo
    git -C "$REPO" switch -q -c feat/example
    commit_config 'PRIVACY_RULES='
    write_denylist "$(printf 'contact-%s\357\274\240example.invalid' "$DENY_TERM")"
    { printf 'by contact-%s@example.invalid\n' "$DENY_TERM"
      printf 'by contact-%s\357\274\240example.invalid\n' "$DENY_TERM"
    } > "$REPO/notes.txt"
    git -C "$REPO" add notes.txt
    scan --staged
    [ "$status" -eq 1 ]
    [[ $stderr == *"notes.txt:1: denylist"* ]]
    [[ $stderr == *"notes.txt:2: denylist"* ]]
    refute_term_shown
}

@test "a relative PRIVACY_DENYLIST is read from where privacy-scan starts" {
    make_repo
    git -C "$REPO" switch -q -c feat/example
    stage top.txt "contact $DENY_TERM"
    mkdir -p "$REPO/docs/lists"
    printf '%s\n' "$DENY_TERM" > "$REPO/docs/lists/mine"
    run --separate-stderr sh -c \
        'cd "$1/docs" && PRIVACY_DENYLIST=lists/mine exec sh ../.githooks/privacy-scan --staged' sh "$REPO"
    [ "$status" -eq 1 ]
    [[ $stderr == *"top.txt:1: denylist"* ]]
    refute_term_shown
}

@test "privacy-scan run from a subdirectory scans the whole tree" {
    make_repo
    write_denylist
    git -C "$REPO" switch -q -c feat/example
    stage top.txt "contact $DENY_TERM"
    git -C "$REPO" -c core.hooksPath=no-hooks commit -q -m "docs: notes"
    mkdir -p "$REPO/docs"
    run --separate-stderr sh -c 'cd "$1/docs" && exec sh ../.githooks/privacy-scan --tracked' sh "$REPO"
    [ "$status" -eq 1 ]
    [[ $stderr == *"top.txt:1: denylist"* ]]
}

@test "an object to scan that is missing fails closed with exit 2" {
    local oid
    make_repo
    write_denylist
    stage notes.txt 'synthetic notes'
    oid=$(git -C "$REPO" rev-parse :notes.txt)
    rm "$REPO/.git/objects/${oid:0:2}/${oid:2}"
    scan --tracked
    [ "$status" -eq 2 ]
    [[ $stderr == *"an object to scan is missing"* ]]
}

# Run pr-text ARGS in $REPO through bats `run`, stderr apart; stdin passes through.
run_pr_text() {
    run --separate-stderr sh -c 'cd "$1" && shift && exec bash .github/standard/bin/pr-text "$@"' \
        sh "$REPO" "$@"
}

@test "pr-text passes a Conventional Commit title, and refuses any other without showing it" {
    local title
    make_repo
    write_denylist
    printf 'Synthetic body.\n' > "$BATS_TEST_TMPDIR/body"
    run_pr_text 'feat(conf): add a mode' "$BATS_TEST_TMPDIR/body"
    [ "$status" -eq 0 ] || { printf '%s\n' "$stderr" >&2; return 1; }
    run_pr_text 'chore!: drop a key'
    [ "$status" -eq 0 ]
    for title in 'Add a mode' 'feat:no space' 'Feat: capital' 'feat(Conf): capital scope' \
        'build: a type the workflow does not name'; do
        run_pr_text "$title" "$BATS_TEST_TMPDIR/body"
        [ "$status" -eq 1 ] || { printf 'passed: %s\n' "$title" >&2; return 1; }
        [[ $stderr == *"the title is not a Conventional Commit"* ]]
        [[ $stderr != *"$title"* ]]
    done
    run_pr_text
    [ "$status" -eq 64 ]
    run_pr_text 'docs: notes' "$BATS_TEST_TMPDIR/no-such-body"
    [ "$status" -eq 2 ]
}

@test "pr-text refuses a denylist term in the body and a home path in the title, and shows neither" {
    local home
    make_repo
    write_denylist
    commit_config PRIVACY_RULES=base
    printf 'Line one.\nSeen by %s.\n' "$DENY_TERM" > "$BATS_TEST_TMPDIR/body"
    run_pr_text 'docs: notes' "$BATS_TEST_TMPDIR/body"
    [ "$status" -eq 1 ]
    [[ $stderr == *"message line 4: denylist"* ]]
    refute_term_shown
    home=$(printf '/%s/%s' home quuxly)
    run_pr_text "fix: the path $home in a tool" - <<< 'A body on stdin.'
    [ "$status" -eq 1 ]
    [[ $stderr == *"message line 1: home-path"* ]]
    [[ $stderr != *quuxly* ]]
}

@test "pr-text reads the denylists only through the hooks' loader: a ~/ list, a BOM, a fullwidth at sign" {
    make_repo
    mkdir -p "$HOME/lists"
    printf '\357\273\277%s\357\274\240example.invalid\n' "$DENY_TERM" > "$HOME/lists/extra"
    git config --global privacy.denylist '~/lists/extra'
    printf 'Contact %s@example.invalid for it.\n' "$DENY_TERM" > "$BATS_TEST_TMPDIR/body"
    run_pr_text 'docs: contact' "$BATS_TEST_TMPDIR/body"
    [ "$status" -eq 1 ]
    [[ $stderr == *"message line 3: denylist"* ]]
    refute_term_shown
}

@test "pr-title checks the current title and body of a pull request, read through gh" {
    make_repo
    write_denylist
    stub_init
    mkstub gh << STUB
[[ -e "$BATS_TEST_TMPDIR/gh-fails" ]] && exit 1
case "\$*" in
    *"--jq .title"*) cat "$BATS_TEST_TMPDIR/title" ;;
    *) cat "$BATS_TEST_TMPDIR/body" ;;
esac
STUB
    printf 'Add a mode\n' > "$BATS_TEST_TMPDIR/title"
    : > "$BATS_TEST_TMPDIR/body"
    run --separate-stderr env PATH="$STUB_BIN:$PATH" GITHUB_REPOSITORY=example-owner/example \
        GH_TOKEN=synthetic-token-value sh -c 'cd "$1" && exec bash .github/standard/bin/pr-title 7' sh "$REPO"
    [ "$status" -eq 1 ]
    [[ $(cat "$STUB_LOG") == *"repos/example-owner/example/pulls/7"* ]]
    # The pull request is renamed in the browser, and the job runs again.
    printf 'feat: add a mode\n' > "$BATS_TEST_TMPDIR/title"
    run --separate-stderr env PATH="$STUB_BIN:$PATH" GITHUB_REPOSITORY=example-owner/example \
        GH_TOKEN=synthetic-token-value sh -c 'cd "$1" && exec bash .github/standard/bin/pr-title 7' sh "$REPO"
    [ "$status" -eq 0 ] || { printf '%s\n' "$stderr" >&2; return 1; }
    printf 'Seen by %s.\n' "$DENY_TERM" > "$BATS_TEST_TMPDIR/body"
    run --separate-stderr env PATH="$STUB_BIN:$PATH" GITHUB_REPOSITORY=example-owner/example \
        GH_TOKEN=synthetic-token-value sh -c 'cd "$1" && exec bash .github/standard/bin/pr-title 7' sh "$REPO"
    [ "$status" -eq 1 ]
    refute_term_shown
    touch "$BATS_TEST_TMPDIR/gh-fails"
    run --separate-stderr env PATH="$STUB_BIN:$PATH" GITHUB_REPOSITORY=example-owner/example \
        GH_TOKEN=synthetic-token-value sh -c 'cd "$1" && exec bash .github/standard/bin/pr-title 7' sh "$REPO"
    [ "$status" -eq 2 ]
    [[ $output$stderr != *synthetic-token-value* ]]
    run --separate-stderr sh -c 'cd "$1" && exec bash .github/standard/bin/pr-title seven' sh "$REPO"
    [ "$status" -eq 64 ]
}
