# shellcheck shell=sh disable=SC2154 # std_hook and std_here come from the caller.
# lib.sh of the repository standard: the same file in every repository; never edit it here.
# Sourced by the hooks, privacy-scan and standard-check, never run. It holds the branch guard,
# the reader of the committed .github/standard.conf, the size and copy guards, the denylist
# loader and the rule engine. Nothing here prints a denylist term, a matched value, or a path
# that may hold one. A check that cannot run refuses with exit 2 (std_fail), so a broken tool
# never passes for a clean scan.
#
# The caller sets std_hook, the name its messages start with, and std_here, the folder of this
# file, before it calls a function here.

std_tmp=
# The unit separator splits the fields of the work files; no rule matches it.
std_us=$(printf '\037')

# Print "HOOK: TEXT" on stderr.
std_say() {
    printf '%s: %s\n' "$std_hook" "$*" >&2
}

# Print "HOOK: PROBLEM, so this is refused." on stderr and exit 2.
std_fail() {
    std_say "$*, so this is refused."
    exit 2
}

# Make the private work folder $std_tmp once, and remove it at exit. Sets umask 077. A TMPDIR
# with backslashes (a Windows path) is not used, since the work file names are matched with /.
std_tmpdir() {
    [ -z "$std_tmp" ] || return 0
    umask 077
    std_base=${TMPDIR:-/tmp}
    case $std_base in *\\*) std_base=/tmp ;; esac
    std_tmp=$(mktemp -d "$std_base/standard.XXXXXX" 2> /dev/null) ||
        std_fail "cannot create a temp folder"
    trap 'rm -rf -- "$std_tmp"' EXIT
    trap 'exit 1' HUP INT TERM
}

# Remove the work folder now, for a hook that hands over to another script with exec.
std_cleanup() {
    if [ -n "$std_tmp" ]; then
        rm -rf -- "$std_tmp"
    fi
    std_tmp=
    trap - EXIT HUP INT TERM
}

# Refuse a commit while the branch NAME (main or master) is checked out, and exit 1.
refuse_branch() {
    std_say "commits on '$1' are refused. Create a feature branch first, for example:"
    printf '    git switch -c fix/short-name\n' >&2
    exit 1
}

# Read .github/standard.conf from HEAD through .github/standard/bin/conf into cfg_role,
# cfg_release_kind, cfg_rules and cfg_max_blob_kb, and into the files $std_tmp/allow and
# $std_tmp/accept, one PRIVACY_ALLOW or PRIVACY_HISTORY_ACCEPT value per line. A staged or
# uncommitted edit never counts, so it cannot loosen the checks of the commit it travels with.
# Before the file's first commit the defaults apply. A committed file that does not parse gives
# the strictest settings instead (every pack, no exemption), with a note.
std_load_config() {
    std_tmpdir
    std_conf="$std_here/../.github/standard/bin/conf"
    [ -f "$std_conf" ] || std_fail ".github/standard/bin/conf is missing"
    std_conf_status=0
    if git rev-parse --quiet --verify HEAD:.github/standard.conf > /dev/null 2>&1; then
        git cat-file blob HEAD:.github/standard.conf > "$std_tmp/conf.in" 2> /dev/null ||
            std_fail "cannot read .github/standard.conf from HEAD"
        sh "$std_conf" --print "$std_tmp/conf.in" > "$std_tmp/conf.out" 2> /dev/null ||
            std_conf_status=$?
    else
        sh "$std_conf" --defaults > "$std_tmp/conf.out" 2> /dev/null || std_conf_status=$?
    fi
    case $std_conf_status in
        0) ;;
        1)
            std_say "the committed .github/standard.conf does not parse, so every rule pack runs" \
                "with no exemption. sh .github/standard/bin/conf --check .github/standard.conf" \
                "shows why."
            printf '%s\n' ROLE=consumer RELEASE_KIND= 'PRIVACY_RULES=base disk vm' MAX_BLOB_KB=1024 \
                > "$std_tmp/conf.out"
            ;;
        *) std_fail ".github/standard/bin/conf failed" ;;
    esac
    cfg_role=consumer
    cfg_release_kind=
    cfg_rules=
    cfg_max_blob_kb=1024
    : > "$std_tmp/allow"
    : > "$std_tmp/accept"
    # shellcheck disable=SC2034 # the hooks read the cfg_ values.
    while IFS= read -r std_line; do
        case $std_line in
            ROLE=*) cfg_role=${std_line#*=} ;;
            RELEASE_KIND=*) cfg_release_kind=${std_line#*=} ;;
            PRIVACY_RULES=*) cfg_rules=${std_line#*=} ;;
            MAX_BLOB_KB=*) cfg_max_blob_kb=${std_line#*=} ;;
            PRIVACY_ALLOW=*) printf '%s\n' "${std_line#*=}" >> "$std_tmp/allow" ;;
            PRIVACY_HISTORY_ACCEPT=*) printf '%s\n' "${std_line#*=}" >> "$std_tmp/accept" ;;
        esac
    done < "$std_tmp/conf.out"
}

# Refuse a staged blob above MAX_BLOB_KB, with one git diff and one git cat-file. A blob is named
# by its object id, since its path may hold a term. Call after std_load_config.
guard_blob_size_staged() {
    git diff --cached --raw --no-renames --no-abbrev --diff-filter=ACMRT > "$std_tmp/size.raw" ||
        std_fail "cannot list the staged files"
    awk '$2 != "160000" { print $4 }' "$std_tmp/size.raw" > "$std_tmp/size.oids" ||
        std_fail "cannot list the staged files"
    [ -s "$std_tmp/size.oids" ] || return 0
    git cat-file --batch-check='%(objectname) %(objectsize)' < "$std_tmp/size.oids" \
        > "$std_tmp/size.out" || std_fail "cannot read the sizes of the staged files"
    std_status=0
    awk -v limit="$cfg_max_blob_kb" -v hook="$std_hook" '
        $2 == "missing" { bad = 1; next }
        $2 > limit * 1024 {
            printf "%s: the staged blob %s is %d KB, above MAX_BLOB_KB=%d.\n", hook,
                substr($1, 1, 12), int(($2 + 1023) / 1024), limit
            big = 1
        }
        END { exit bad ? 2 : big }' "$std_tmp/size.out" >&2 || std_status=$?
    case $std_status in
        0) ;;
        1)
            std_say "find the file with: git ls-files -s | grep OBJECT-ID. Keep large files out of git."
            exit 1
            ;;
        *) std_fail "a staged blob is missing" ;;
    esac
}

# Run standard-check --staged when the commit stages a path of the standard. Without a staged
# MANIFEST it does nothing: the repository has not adopted the standard yet. In a repository that
# uses the standard (ROLE=consumer), such a path is one the staged MANIFEST lists, anything under
# .githooks/, .github/standard/ or .github/workflows/, or .github/standard.conf. In repo-standard
# (ROLE=source), which changes its canonical files between releases, it is a body in blocks/, a
# document that the staged MANIFEST names a block of, or .github/standard.conf, since the config
# selects the variants of its own blocks. Call after std_load_config.
guard_vendored_staged() {
    case $cfg_role in
        consumer | source) ;;
        *) return 0 ;;
    esac
    git cat-file -e :0:.github/standard/MANIFEST 2> /dev/null || return 0
    git cat-file blob :0:.github/standard/MANIFEST > "$std_tmp/manifest" ||
        std_fail "cannot read the staged .github/standard/MANIFEST"
    git -c core.quotePath=false diff --cached --name-only --no-renames > "$std_tmp/staged" ||
        std_fail "cannot list the staged files"
    ROLE=$cfg_role awk -v manifest="$std_tmp/manifest" '
        BEGIN {
            source = ENVIRON["ROLE"] == "source"
            while ((getline line < manifest) > 0) {
                if (split(line, f, " ") >= 5 && (f[1] == "file" || f[1] == "block")) {
                    if (source && f[1] != "block") continue
                    sub(/#.*/, "", f[5])
                    listed[f[5]] = 1
                }
            }
        }
        $0 in listed || $0 == ".github/standard.conf" { found = 1 }
        source && /^blocks\// { found = 1 }
        !source && (/^\.githooks\// || /^\.github\/standard\// || /^\.github\/workflows\//) { found = 1 }
        END { exit !found }' "$std_tmp/staged" || return 0
    # standard-check names every path of the standard's folders in the index, including any that
    # a commit made with the hooks off brought in. These names go through the scan first, so that
    # nothing shows a name that holds a finding.
    git -c core.quotePath=false ls-files -- .githooks .github/standard .github/workflows \
        > "$std_tmp/guard.names" || std_fail "cannot list the standard's folders"
    if [ -s "$std_tmp/guard.names" ]; then
        sh "$std_here/privacy-scan" --message "$std_tmp/guard.names" < /dev/null || {
            std_status=$?
            [ "$std_status" -ne 1 ] || std_say "a path name in .githooks/, .github/standard/ or" \
                ".github/workflows/ holds a finding (line N of git ls-files on those folders);" \
                "rename it before the copy guard can run."
            exit "$std_status"
        }
    fi
    sh "$std_here/../.github/standard/bin/standard-check" --staged || exit 1
}

# The denylist loader. Merges every denylist into $std_tmp/terms, one term per line, and sets
# deny_sources (the lists found or configured), deny_terms (the terms in all of them),
# deny_missing (configured lists that do not exist), deny_nul (1 when a list holds NUL bytes)
# and deny_require (1 when privacy.requireDenylist is true). Notes for deny_enforce go to
# $std_tmp/deny.notes. Sources, in this order, each once: $PRIVACY_DENYLIST; every
# privacy.denylist value of git config, which --type=path expands; the per-user list
# ${XDG_CONFIG_HOME:-$HOME/.config}/git-privacy/denylist; and the clone's
# info/privacy-denylist, which --git-path also finds from a linked worktree. A list holds fixed
# strings, matched without regard to case; a BOM, blank lines, # lines and the whitespace
# around each term are dropped. A list without a term counts as no list.
#
# A message names a list by its configured value when that starts with ~/, and otherwise only as
# "list N of M": an expanded path holds the user's home folder.
deny_load() {
    std_tmpdir
    : > "$std_tmp/deny.src"
    : > "$std_tmp/deny.kept"
    : > "$std_tmp/deny.labels"
    : > "$std_tmp/deny.notes"
    : > "$std_tmp/terms"
    deny_sources=0
    deny_terms=0
    deny_missing=0
    deny_nul=0
    deny_require=0
    std_status=0
    std_value=$(git config --type=bool --get privacy.requireDenylist 2> /dev/null) ||
        std_status=$?
    case $std_status:$std_value in
        0:true) deny_require=1 ;;
        0:false | 1:*) ;;
        *) std_fail "privacy.requireDenylist is not true or false" ;;
    esac
    # Candidates, as "configured US path US label" lines; the label - stands for "list N of M".
    if [ -n "${PRIVACY_DENYLIST:-}" ]; then
        printf '1%s%s%s-\n' "$std_us" "$PRIVACY_DENYLIST" "$std_us" >> "$std_tmp/deny.src"
    fi
    std_status=0
    git config --get-all privacy.denylist > "$std_tmp/deny.raw" 2> /dev/null || std_status=$?
    case $std_status in
        0)
            git config --type=path --get-all privacy.denylist > "$std_tmp/deny.path" \
                2> /dev/null || std_fail "cannot expand the privacy.denylist paths"
            awk -v us="$std_us" -v raw="$std_tmp/deny.raw" '
                {
                    if ((getline label < raw) <= 0) exit 1
                    if (substr(label, 1, 2) != "~/") label = "-"
                    print 1 us $0 us label
                }' "$std_tmp/deny.path" >> "$std_tmp/deny.src" ||
                std_fail "cannot read the privacy.denylist paths"
            ;;
        1) ;;
        *) std_fail "cannot read privacy.denylist from git config" ;;
    esac
    std_path="${XDG_CONFIG_HOME:-${HOME:-}/.config}/git-privacy/denylist"
    if [ -e "$std_path" ]; then
        printf '0%s%s%s-\n' "$std_us" "$std_path" "$std_us" >> "$std_tmp/deny.src"
    fi
    if std_path=$(git rev-parse --path-format=absolute --git-path info/privacy-denylist \
        2> /dev/null) && [ -e "$std_path" ]; then
        printf '0%s%s%s-\n' "$std_us" "$std_path" "$std_us" >> "$std_tmp/deny.src"
    fi
    # Keep each list once: the same file may come through git config and --git-path, in another
    # path form. -ef compares the files themselves.
    while IFS=$std_us read -r std_flag std_path std_label; do
        std_dup=0
        while IFS=$std_us read -r _ std_kept _; do
            # shellcheck disable=SC3013 # dash, bash and busybox sh all have -ef.
            if [ "$std_path" = "$std_kept" ] ||
                { [ -e "$std_path" ] && [ "$std_path" -ef "$std_kept" ]; }; then
                std_dup=1
                break
            fi
        done < "$std_tmp/deny.kept"
        if [ "$std_dup" -eq 0 ]; then
            printf '%s%s%s%s%s\n' "$std_flag" "$std_us" "$std_path" "$std_us" "$std_label" \
                >> "$std_tmp/deny.kept"
        fi
    done < "$std_tmp/deny.src"
    deny_sources=$(awk 'END { print NR }' "$std_tmp/deny.kept")
    # A configured list that is missing is a note for deny_enforce; a path that is not a readable
    # file stops the check here.
    std_n=0
    set --
    while IFS=$std_us read -r std_flag std_path std_label; do
        std_n=$((std_n + 1))
        [ "$std_label" != - ] || std_label="list $std_n of $deny_sources"
        if [ ! -e "$std_path" ]; then
            deny_missing=$((deny_missing + 1))
            printf 'missing%s%s\n' "$std_us" "$std_label" >> "$std_tmp/deny.notes"
        elif [ ! -f "$std_path" ] || [ ! -r "$std_path" ]; then
            std_fail "the denylist $std_label is not a readable file"
        else
            # A relative path gets ./, so that grep and awk below never read it as an option, as
            # stdin (-) or, in awk, as an assignment (NAME=VALUE).
            case $std_path in
                /* | [A-Za-z]:*) set -- "$@" "$std_path" ;;
                *) set -- "$@" "./$std_path" ;;
            esac
            printf '%s%s%s\n' "$std_flag" "$std_us" "$std_label" >> "$std_tmp/deny.labels"
        fi
    done < "$std_tmp/deny.kept"
    [ "$#" -gt 0 ] || return 0
    # grep -z counts NUL-terminated records: more than one means NUL bytes inside the file, as
    # in a list saved as UTF-16, whose terms would never match.
    std_status=0
    grep -H -c -z -a '' "$@" > "$std_tmp/deny.nul" 2> /dev/null || std_status=$?
    [ "$std_status" -le 1 ] || std_fail "cannot read a denylist"
    std_status=0
    awk -v us="$std_us" -v labels="$std_tmp/deny.labels" -v notes="$std_tmp/deny.notes" '
        {
            n = $0
            sub(/.*:/, "", n)
            if ((getline label < labels) <= 0) { err = 1; exit }
            label = substr(label, index(label, us) + 1)
            if (n + 0 > 1) { print "nul" us label >> notes; bad = 1 }
        }
        END { if (err) exit 1; exit bad ? 3 : 0 }' "$std_tmp/deny.nul" || std_status=$?
    case $std_status in
        0) ;;
        3)
            deny_nul=1
            return 0
            ;;
        *) std_fail "cannot read a denylist" ;;
    esac
    std_bom=$(printf '\357\273\277')
    std_fw=$(printf '\357\274\240')
    # One pass over every list: drop a BOM, the whitespace around each term, and blank and #
    # lines, and count the terms of each list. A fullwidth at sign (U+FF20) reads as @, as it
    # does in the scanned text.
    LC_ALL=C awk -v bom="$std_bom" -v fw="$std_fw" -v terms="$std_tmp/terms" '
        {
            line = $0
            if (index(line, bom) == 1) line = substr(line, 4)
            gsub(fw, "@", line)
            sub(/^[[:space:]]+/, "", line)
            sub(/[[:space:]]+$/, "", line)
            if (line == "" || substr(line, 1, 1) == "#") next
            print line > terms
            n[FILENAME]++
        }
        END { for (i = 1; i < ARGC; i++) print n[ARGV[i]] + 0 }' "$@" > "$std_tmp/deny.counts" ||
        std_fail "cannot read a denylist"
    # A configured list without a term counts as missing, so that a list emptied by mistake does
    # not pass unnoticed.
    awk -v us="$std_us" -v labels="$std_tmp/deny.labels" -v notes="$std_tmp/deny.notes" '
        {
            if ((getline line < labels) <= 0) exit 1
            i = index(line, us); flag = substr(line, 1, i - 1); label = substr(line, i + 1)
            total += $1
            if ($1 == 0 && flag == 1) { print "empty" us label >> notes; empty++ }
        }
        END { print total + 0, empty + 0 }' "$std_tmp/deny.counts" > "$std_tmp/deny.sum" ||
        std_fail "cannot read a denylist"
    read -r deny_terms std_empty < "$std_tmp/deny.sum"
    deny_missing=$((deny_missing + std_empty))
}

# Print the loader's notes and decide: a list with NUL bytes always refuses; a configured list
# that is missing, or no term in any list, refuses under privacy.requireDenylist and is a note
# otherwise. Exits 2 to refuse; returns to let the scan go on, with or without terms.
deny_enforce() {
    std_refuse=0
    while IFS=$std_us read -r std_what std_label; do
        case $std_what in
            nul)
                std_say "the denylist $std_label holds NUL bytes: save it as UTF-8 text."
                std_refuse=1
                ;;
            empty)
                if [ "$deny_require" -eq 1 ]; then
                    std_say "the denylist $std_label holds no term, and privacy.requireDenylist is true."
                    std_refuse=1
                else
                    std_say "note: the denylist $std_label holds no term."
                fi
                ;;
            missing)
                if [ "$deny_require" -eq 1 ]; then
                    std_say "the denylist $std_label is missing, and privacy.requireDenylist is true."
                    std_refuse=1
                else
                    std_say "note: the denylist $std_label is missing."
                fi
                ;;
        esac
    done < "$std_tmp/deny.notes"
    if [ "$deny_nul" -eq 0 ] && [ "$deny_terms" -eq 0 ]; then
        if [ "$deny_require" -eq 1 ]; then
            std_say "no denylist holds a term, and privacy.requireDenylist is true."
            std_refuse=1
        else
            std_say "note: no denylist holds a term, so only the rule packs run."
        fi
    fi
    [ "$std_refuse" -eq 0 ] || std_fail "the denylists cannot be checked"
}

# The rule engine. scan_begin makes the work folder $std_scan. The collectors add units of text:
# blobs, commits and tag objects by object id, and a message or the identity as a file. scan_run
# puts the path names and every unit into one stream, each behind a marker line. It strips the NUL
# bytes once, so UTF-16 text matches as UTF-8 text does. It runs each rule and the denylist once
# over the stream, maps each hit back to its unit and line, and drops exempt and accepted hits.
# It prints "privacy-scan: WHERE: KIND" per finding on stderr. A path whose name holds a hit is
# shown only as "path name N", never by name.
scan_begin() {
    std_tmpdir
    std_scan="$std_tmp/scan"
    mkdir "$std_scan" || std_fail "cannot create a work folder"
    for std_f in objs units names blobs eol accept.units accept.paths; do
        : > "$std_scan/$std_f"
    done
    printf 'second pass\n' > "$std_scan/pass2"
    std_pseudo=0
    std_listing=
    # Set by --history: name the commits behind each file finding (scan_commits).
    std_history=
}

# Add the text of FILE as a unit of TYPE (message or identity).
scan_add_file() {
    std_pseudo=$((std_pseudo + 1))
    cp -- "$2" "$std_scan/pseudo.$std_pseudo" || std_fail "cannot read the $1 to scan"
    printf 'pseudo.%s%s%s%s\n' "$std_pseudo" "$std_us" "$1" "$std_us" >> "$std_scan/units"
}

# Add the staged blobs, their path names, the author and committer identity, and the line
# endings the index records for the staged paths.
scan_staged() {
    git -c core.quotePath=false diff --cached --raw --no-renames --no-abbrev --diff-filter=ACMRT \
        > "$std_scan/raw" || std_fail "cannot list the staged files"
    awk -F '\t' -v us="$std_us" -v dir="$std_scan" '
        {
            split($1, m, " ")
            if (m[2] == "160000") next
            path = substr($0, index($0, "\t") + 1)
            print path > (dir "/names")
            print m[4] us path > (dir "/blobs")
            if (!(m[4] in seen)) { seen[m[4]] = 1; print m[4] > (dir "/objs") }
        }' "$std_scan/raw" || std_fail "cannot list the staged files"
    { git var GIT_AUTHOR_IDENT && git var GIT_COMMITTER_IDENT; } > "$std_scan/ident" 2> /dev/null ||
        std_fail "cannot read the author and committer identity"
    scan_add_file identity "$std_scan/ident"
    git -c core.quotePath=false ls-files --eol > "$std_scan/eol" ||
        std_fail "cannot read the line endings of the index"
    std_listing="git diff --cached --name-only --no-renames --diff-filter=ACMRT"
}

# Add every blob of the index, its path name and the line ending the index records for it.
scan_tracked() {
    git -c core.quotePath=false ls-files -s > "$std_scan/raw" ||
        std_fail "cannot list the tracked files"
    awk -v us="$std_us" -v dir="$std_scan" '
        {
            if ($1 == "160000") next
            path = substr($0, index($0, "\t") + 1)
            if (!(path in named)) { named[path] = 1; print path > (dir "/names") }
            print $2 us path > (dir "/blobs")
            if (!(($2) in seen)) { seen[$2] = 1; print $2 > (dir "/objs") }
        }' "$std_scan/raw" || std_fail "cannot list the tracked files"
    git -c core.quotePath=false ls-files --eol > "$std_scan/eol" ||
        std_fail "cannot read the line endings of the index"
    std_listing="git ls-files"
}

# Add what the commits of REVS... bring in: every new blob, commit and annotated tag object (so
# the content, the messages, the author and committer, and the tag messages), and the path names
# those commits add. REVS are git rev-list arguments, for example SHA --not --remotes=origin.
scan_range() {
    git rev-list --objects "$@" > "$std_scan/revs" 2> /dev/null ||
        std_fail "cannot list the commits to scan"
    git cat-file --batch-check='%(objecttype) %(objectname) %(rest)' < "$std_scan/revs" \
        > "$std_scan/types" || std_fail "cannot read the objects to scan"
    git -c core.quotePath=false log --format= --name-only --no-renames --diff-filter=ACMRT \
        --root --no-show-signature --diff-merges=first-parent "$@" > "$std_scan/lognames" 2> /dev/null ||
        std_fail "cannot list the paths the commits add"
    awk -v us="$std_us" -v dir="$std_scan" '
        FILENAME ~ /\/lognames$/ {
            if ($0 != "" && !($0 in named)) { named[$0] = 1; print $0 > (dir "/names") }
            next
        }
        $1 == "blob" || $1 == "commit" || $1 == "tag" {
            if (!(($2) in seen)) { seen[$2] = 1; print $2 > (dir "/objs") }
            if ($1 != "blob") { print $2 us $1 us substr($2, 1, 12) > (dir "/units"); next }
            path = substr($0, length($1) + length($2) + 3)
            if (path != "" && !(path in named)) { named[path] = 1; print path > (dir "/names") }
            print $2 us path > (dir "/blobs")
        }' "$std_scan/lognames" "$std_scan/types" || std_fail "cannot read the objects to scan"
}

# Mark the findings that PRIVACY_HISTORY_ACCEPT records accept: "SHA KIND" accepts the hits of
# KIND in that commit object and in the blobs and path names that commit adds. Call after
# scan_range and std_load_config.
scan_accept() {
    while read -r std_sha std_kind; do
        [ -n "$std_sha" ] || continue
        printf '%s%s%s\n' "$std_kind" "$std_us" "$std_sha" >> "$std_scan/accept.units"
        git -c core.quotePath=false diff-tree -r --root --no-commit-id --no-renames --no-abbrev \
            --diff-filter=ACMRT "$std_sha" > "$std_scan/accept.raw" 2> /dev/null ||
            std_fail "cannot read a commit that PRIVACY_HISTORY_ACCEPT names"
        awk -F '\t' -v us="$std_us" -v kind="$std_kind" -v dir="$std_scan" '
            {
                split($1, m, " ")
                print kind us m[4] >> (dir "/accept.units")
                print kind us substr($0, index($0, "\t") + 1) >> (dir "/accept.paths")
            }' "$std_scan/accept.raw" ||
            std_fail "cannot read a commit that PRIVACY_HISTORY_ACCEPT names"
    done < "$std_tmp/accept"
}

# Split the rule packs of cfg_rules into $std_scan/rules: "kind US ERE US exempt-ERE" lines.
scan_load_rules() {
    set --
    for std_pack in $cfg_rules; do
        [ -f "$std_here/rules/$std_pack" ] || std_fail "the rule pack $std_pack is missing"
        set -- "$@" "$std_here/rules/$std_pack"
    done
    : > "$std_scan/rules"
    [ "$#" -gt 0 ] || return 0
    awk -v us="$std_us" '
        /^[ \t]*(#|$)/ { next }
        {
            n = 0; depth = 0; esc = 0
            for (i = 1; i <= length($0); i++) {
                c = substr($0, i, 1)
                if (esc) { esc = 0; continue }
                if (c == "\\") esc = 1
                else if (c == "(") depth++
                else if (c == ")") depth--
                else if (c == "|" && depth == 0) at[++n] = i
            }
            kind = (n >= 1) ? substr($0, 1, at[1] - 1) : ""
            ere = (n >= 2) ? substr($0, at[1] + 1, at[2] - at[1] - 1) : ""
            if (n != 2 || depth != 0 || kind !~ /^[a-z0-9-]+$/ || ere == "") {
                k = split(FILENAME, part, "/")
                printf "rule pack %s, line %d: not kind|ERE|exempt-ERE\n", part[k], FNR > "/dev/stderr"
                bad = 1
                next
            }
            print kind us ere us substr($0, at[2] + 1)
        }
        END { exit bad }' "$@" > "$std_scan/rules" || std_fail "a rule pack does not parse"
}

# Build the stream, run the rules and the denylist over it and report. Returns 0 when nothing is
# found, and 1 after printing the findings. Call after std_load_config and deny_load.
scan_run() {
    scan_load_rules
    # A random marker of letters that no hex rule matches, so content cannot fake one.
    std_mark=PRIVACYSCAN$(od -An -N9 -tx1 /dev/urandom 2> /dev/null | tr ' 0-9a-f' 'xg-v')
    [ "${#std_mark}" -gt 30 ] || std_fail "cannot read /dev/urandom"
    {
        printf '%s names\n' "$std_mark"
        cat "$std_scan/names"
        std_i=0
        while [ "$std_i" -lt "$std_pseudo" ]; do
            std_i=$((std_i + 1))
            printf '\n%s pseudo.%s\n' "$std_mark" "$std_i"
            cat "$std_scan/pseudo.$std_i"
        done
        printf '\n'
    } > "$std_scan/stream.raw" || std_fail "cannot build the scan"
    if [ -s "$std_scan/objs" ]; then
        # A missing object (a partial clone that cannot fetch it) must not pass as clean.
        git cat-file --batch-check="%(objectname) %(objecttype)" < "$std_scan/objs" \
            > "$std_scan/objcheck" || std_fail "cannot read the objects to scan"
        if grep -q " missing$" "$std_scan/objcheck"; then
            std_fail "an object to scan is missing"
        fi
        git cat-file --batch="$std_mark %(objectname)" < "$std_scan/objs" \
            >> "$std_scan/stream.raw" || std_fail "cannot read the objects to scan"
    fi
    LC_ALL=C tr -d '\000' < "$std_scan/stream.raw" > "$std_scan/stream.nul" ||
        std_fail "cannot build the scan"
    # Some chat and copy tools turn an at sign into the fullwidth U+FF20: fold it back, so
    # that the e-mail rule and denylist terms that hold an at sign still match.
    std_fw=$(printf '\357\274\240')
    LC_ALL=C sed "s/$std_fw/@/g" "$std_scan/stream.nul" > "$std_scan/stream" ||
        std_fail "cannot build the scan"
    rm -f -- "$std_scan/stream.raw"
    grep -n -a "^$std_mark " "$std_scan/stream" > "$std_scan/marks" ||
        std_fail "cannot index the scan"
    # One grep per rule and one for the denylist, each over the whole stream.
    std_i=0
    : > "$std_scan/rulekinds"
    while IFS=$std_us read -r std_kind std_ere _; do
        std_i=$((std_i + 1))
        printf '%s%s%s\n' "$std_i" "$std_us" "$std_kind" >> "$std_scan/rulekinds"
        LC_ALL=C.UTF-8 grep -n -o -a -E -e "$std_ere" "$std_scan/stream" > "$std_scan/hit.$std_i" ||
            [ $? -eq 1 ] || std_fail "grep failed on the $std_kind rule"
    done < "$std_scan/rules"
    : > "$std_scan/hit.deny"
    if [ "$deny_terms" -gt 0 ]; then
        LC_ALL=C.UTF-8 grep -n -o -a -i -F -f "$std_tmp/terms" "$std_scan/stream" \
            > "$std_scan/hit.deny" || [ $? -eq 1 ] || std_fail "grep failed on the denylist"
    fi
    set -- "$std_scan"/hit.*
    # A PRIVACY_ALLOW line of .github/standard.conf names the synthetic value it exempts, which
    # conf has checked against realistic samples, so the rule packs skip that line of the config;
    # the denylist does not. Without this, no exemption could ever be committed. Only the lines
    # that conf accepts count, only in a config blob that parses, and only at that path.
    : > "$std_scan/cfgallow"
    awk -F "$std_us" '$2 == ".github/standard.conf" && !(($1) in seen) { seen[$1] = 1; print $1 }' \
        "$std_scan/blobs" > "$std_scan/cfgoids" || std_fail "cannot list the config blobs"
    while read -r std_oid; do
        git cat-file blob "$std_oid" > "$std_scan/cfgblob" ||
            std_fail "cannot read .github/standard.conf"
        std_status=0
        sh "$std_conf" --allow-lines "$std_scan/cfgblob" > "$std_scan/cfglines" 2> /dev/null ||
            std_status=$?
        case $std_status in
            0)
                awk -v us="$std_us" -v oid="$std_oid" '{ print oid us $1 us $2 }' "$std_scan/cfglines" \
                    >> "$std_scan/cfgallow" || std_fail "cannot read .github/standard.conf"
                ;;
            1) ;;
            *) std_fail ".github/standard/bin/conf failed" ;;
        esac
    done < "$std_scan/cfgoids"
    # Map each hit to its unit, in two passes over the hit files: the path names first, so that
    # every path whose name holds a hit is known before a content hit could show it. Writes
    # rec.KIND (kind, path, where, value, and the blob for a hit in file content), vals.KIND and
    # paths.KIND for each kind found.
    LC_ALL=C awk -v us="$std_us" -v dir="$std_scan" -v mark="$std_mark" '
        function unit(n,    lo, hi, mid, at) {
            lo = 1; hi = nm; at = 0
            while (lo <= hi) {
                mid = int((lo + hi) / 2)
                if (ml[mid] <= n) { at = mid; lo = mid + 1 } else hi = mid - 1
            }
            return at
        }
        function emit(kind, id, path, where, value, blob) {
            if (id != "" && (kind us id) in accu) return
            if (id == "" && path != "" && (kind us path) in accp) return
            print kind us path us where us value us blob > (dir "/rec." kind)
            print value > (dir "/vals." kind)
            print path > (dir "/paths." kind)
            if (!(kind in found)) { found[kind] = 1; print kind > (dir "/kinds") }
        }
        function shown(path) {
            if (!(path in idxof)) return "an unlisted path"
            return (idxof[path] in hidden) ? "path name " idxof[path] " of the scanned paths" : path
        }
        FILENAME ~ /\/cfgallow$/ { cfga[$0] = 1; next }
        FILENAME ~ /\/accept\.units$/ { accu[$0] = 1; next }
        FILENAME ~ /\/accept\.paths$/ { accp[$0] = 1; next }
        FILENAME ~ /\/rulekinds$/ { i = index($0, us); kindof[substr($0, 1, i - 1)] = substr($0, i + 1); next }
        FILENAME ~ /\/units$/ { split($0, u, us); utype[u[1]] = u[2]; ulabel[u[1]] = u[3]; next }
        FILENAME ~ /\/names$/ { nameof[FNR] = $0; if (!($0 in idxof)) idxof[$0] = FNR; next }
        FILENAME ~ /\/blobs$/ { i = index($0, us); o = substr($0, 1, i - 1); bp[o] = bp[o] us substr($0, i + 1); next }
        FILENAME ~ /\/marks$/ {
            i = index($0, ":"); nm++; ml[nm] = substr($0, 1, i - 1) + 0
            mid_[nm] = substr($0, i + length(mark) + 2)
            next
        }
        FILENAME ~ /\/pass2$/ { pass = 2; next }
        FILENAME ~ /\/hit\.[^\/]*$/ {
            k = FILENAME; sub(/.*\/hit\./, "", k)
            kind = (k == "deny") ? "denylist" : kindof[k]
            i = index($0, ":"); n = substr($0, 1, i - 1) + 0; value = substr($0, i + 1)
            at = unit(n)
            isname = (at > 0 && mid_[at] == "names")
            if ((pass == 2) == isname) next
            if (at == 0) { emit(kind, "", "", "stream line " n, value); next }
            if (ml[at] == n) next
            line = n - ml[at]; id = mid_[at]
            if (isname) {
                hidden[line] = 1
                emit(kind, "", nameof[line], "path name " line " of the scanned paths", value)
            } else if (id in bp) {
                # A blob that a ref names directly has no path, and git lists each object once,
                # so a committed file with the same blob loses its path too. Such a hit names the
                # blob, under a path no git tree can hold: a PRIVACY_ALLOW path of ^$, meant for
                # messages, never exempts file content.
                c = split(substr(bp[id], 2), paths, us)
                if (c == 0) { c = 1; paths[1] = "" }
                for (j = 1; j <= c; j++) {
                    if (paths[j] == "") {
                        emit(kind, id, "/no-path", "blob " substr(id, 1, 12) ", line " line, value, id)
                        continue
                    }
                    if (kind != "denylist" && paths[j] == ".github/standard.conf" &&
                        ((id us line us kind) in cfga)) continue
                    emit(kind, id, paths[j], shown(paths[j]) ":" line, value, id)
                }
            } else if (utype[id] == "commit") {
                emit(kind, id, "", "commit " ulabel[id] ", line " line " of git cat-file -p", value)
            } else if (utype[id] == "tag") {
                emit(kind, id, "", "tag object " ulabel[id] ", line " line " of git cat-file -p", value)
            } else if (utype[id] == "identity") {
                emit(kind, id, "", (line == 1) ? "the author identity" : "the committer identity", value)
            } else if (utype[id] == "message") {
                emit(kind, id, "", "message line " line, value)
            } else {
                emit(kind, id, "", "stream line " n, value)
            }
            next
        }
        FILENAME ~ /\/eol$/ {
            # i/crlf or i/mixed in the index, unless the path is -text or eol=crlf.
            t = index($0, "\t"); meta = substr($0, 1, t - 1); path = substr($0, t + 1)
            if (meta !~ /^i\/(crlf|mixed) / || meta ~ /-text/ || meta ~ /eol=crlf/) next
            if (path in idxof) emit("crlf", "", path, shown(path), "")
            next
        }' "$std_scan/cfgallow" "$std_scan/accept.units" "$std_scan/accept.paths" "$std_scan/rulekinds" \
        "$std_scan/units" "$std_scan/names" "$std_scan/blobs" "$std_scan/marks" "$@" \
        "$std_scan/pass2" "$@" "$std_scan/eol" || std_fail "cannot map the findings"
    [ -f "$std_scan/kinds" ] || return 0
    scan_exempt
    # Drop the exempt hits, then print each finding once.
    set --
    for std_f in "$std_scan"/drop.*; do
        [ -e "$std_f" ] && set -- "$@" "$std_f"
    done
    : > "$std_scan/fblobs"
    LC_ALL=C awk -v us="$std_us" -v dir="$std_scan" '
        FILENAME ~ /\/drop\.[^\/]*$/ {
            key = FILENAME; sub(/.*\/drop\./, "", key)
            i = index($0, ":"); drop[key us substr($0, 1, i - 1)] = 1
            next
        }
        {
            kind = FILENAME; sub(/.*\/rec\./, "", kind)
            if ((kind us FNR) in drop) next
            n = split($0, r, us)
            print "privacy-scan: " r[3] ": " kind
            # No record accepts a denylist term, so scan_commits names no commit for one.
            if (r[n] != "" && kind != "denylist") print "privacy-scan: " r[3] ": " kind us r[n] > (dir "/fblobs")
        }' "$@" "$std_scan"/rec.* > "$std_scan/findings.raw" ||
        std_fail "cannot report the findings"
    # Each finding once, then by path and line number.
    LC_ALL=C sort -u "$std_scan/findings.raw" > "$std_scan/findings.u" ||
        std_fail "cannot report the findings"
    LC_ALL=C sort -t : -k 2,2 -k 3,3n "$std_scan/findings.u" > "$std_scan/findings" ||
        std_fail "cannot report the findings"
    [ -s "$std_scan/findings" ] || return 0
    if [ -n "$std_history" ] && [ -s "$std_scan/fblobs" ]; then
        scan_commits
        cat "$std_scan/findings.out" >&2
    else
        cat "$std_scan/findings" >&2
    fi
    std_count=$(awk 'END { print NR }' "$std_scan/findings")
    std_say "refused: $std_count finding(s). Use synthetic values (CONTRIBUTING.md, Test data);" \
        "a denylist term is never shown."
    if [ -n "$std_history" ] && [ -s "$std_scan/fblobs" ] &&
        grep -q '^privacy-scan:   in version [0-9a-f]*, brought in \(by commit\|outside\)' \
            "$std_scan/findings.out"; then
        std_say "a PRIVACY_HISTORY_ACCEPT record of any one commit that brought in a version" \
            "accepts that version for the kind of its record. First check that the value is" \
            "synthetic, and pick a commit that the base branch holds: the secret scan refuses a" \
            "record whose commit is not an ancestor of the base."
    fi
    if [ -n "$std_listing" ] && grep -q 'path name ' "$std_scan/findings"; then
        std_say "path name N is line N of: $std_listing"
    fi
    return 1
}

# For --history: write $std_scan/findings.out, the findings plus, under each finding in file
# content, one line for each version (blob) behind it. That line names, by full SHA in git log
# order, the commits whose PRIVACY_HISTORY_ACCEPT record would accept the version: those that bring
# it in as scan_accept reads a commit, with diff-tree. Commits that HEAD holds come first. The
# others are marked as outside HEAD, since the secret scan accepts only a record whose commit the
# base holds. A merge brings in nothing for diff-tree, and neither does a ref that names a blob
# directly, so a version only they bring in gets a line that says so. It makes one git log pass
# over every ref, and only when a history scan has findings in file content.
scan_commits() {
    LC_ALL=C sort -u "$std_scan/fblobs" > "$std_scan/fblobs.u" ||
        std_fail "cannot list the versions behind the findings"
    git log --all --root --raw --no-abbrev --no-renames --diff-filter=ACMRT --diff-merges=off \
        --no-show-signature --format='%x01%H' > "$std_scan/lograw" 2> /dev/null ||
        std_fail "cannot list the commits of the history"
    git rev-list HEAD > "$std_scan/headrevs" 2> /dev/null || : > "$std_scan/headrevs"
    LC_ALL=C awk -v us="$std_us" '
        FILENAME ~ /\/fblobs\.u$/ { want[substr($0, index($0, us) + 1)] = 1; next }
        FILENAME ~ /\/headrevs$/ { head[$0] = 1; next }
        /^\001/ { sha = substr($0, 2); next }
        /^:/ {
            split($0, m, " ")
            if (!(m[4] in want) || (m[4] us sha) in seen) next
            seen[m[4] us sha] = 1
            if (sha in head) inh[m[4]] = inh[m[4]] " " sha
            else out[m[4]] = out[m[4]] " " sha
        }
        END { for (b in want) print b us inh[b] us out[b] }' "$std_scan/fblobs.u" \
        "$std_scan/headrevs" "$std_scan/lograw" > "$std_scan/bycommit" ||
        std_fail "cannot list the commits of the history"
    LC_ALL=C awk -v us="$std_us" '
        FILENAME ~ /\/bycommit$/ { split($0, f, us); inh[f[1]] = f[2]; out[f[1]] = f[3]; next }
        FILENAME ~ /\/fblobs\.u$/ { i = index($0, us); vers[substr($0, 1, i - 1)] = vers[substr($0, 1, i - 1)] us substr($0, i + 1); next }
        {
            print
            if (!($0 in vers)) next
            n = split(substr(vers[$0], 2), b, us)
            for (j = 1; j <= n; j++) {
                v = "privacy-scan:   in version " substr(b[j], 1, 12) ", brought in "
                if (inh[b[j]] != "" && out[b[j]] != "")
                    print v "by commit" inh[b[j]] ", and outside HEAD by commit" out[b[j]]
                else if (inh[b[j]] != "")
                    print v "by commit" inh[b[j]]
                else if (out[b[j]] != "")
                    print v "outside HEAD only, by commit" out[b[j]]
                else
                    print v "by no commit that a PRIVACY_HISTORY_ACCEPT record can name" \
                        " (only a merge or a ref brings it in)"
            }
        }' "$std_scan/bycommit" "$std_scan/fblobs.u" "$std_scan/findings" > "$std_scan/findings.out" ||
        std_fail "cannot report the findings"
}

# Find the exempt hits of each kind found: those the kind's exempt ERE matches, and those a
# PRIVACY_ALLOW line of that kind covers by path and value. Writes their record numbers, in
# grep -n form, to drop.KIND. Denylist and crlf hits are never exempt.
scan_exempt() {
    while read -r std_kind; do
        case $std_kind in denylist | crlf) continue ;; esac
        : > "$std_scan/drop.$std_kind"
        while IFS=$std_us read -r std_rkind _ std_exempt; do
            [ "$std_rkind" = "$std_kind" ] && [ -n "$std_exempt" ] || continue
            LC_ALL=C.UTF-8 grep -n -i -E -e "$std_exempt" "$std_scan/vals.$std_kind" \
                >> "$std_scan/drop.$std_kind" || [ $? -eq 1 ] ||
                std_fail "grep failed on the exemption of the $std_kind rule"
        done < "$std_scan/rules"
        while read -r std_akind std_apath std_avalue; do
            [ "$std_akind" = "$std_kind" ] || continue
            LC_ALL=C.UTF-8 grep -n -i -E -e "$std_avalue" "$std_scan/vals.$std_kind" \
                > "$std_scan/allow.v" || [ $? -eq 1 ] || std_fail "grep failed on a PRIVACY_ALLOW value"
            LC_ALL=C.UTF-8 grep -n -E -e "$std_apath" "$std_scan/paths.$std_kind" \
                > "$std_scan/allow.p" || [ $? -eq 1 ] || std_fail "grep failed on a PRIVACY_ALLOW path"
            awk '
                { n = substr($0, 1, index($0, ":") - 1) }
                FILENAME ~ /\/allow\.v$/ { v[n] = 1; next }
                n in v { print n ":" }' "$std_scan/allow.v" "$std_scan/allow.p" \
                >> "$std_scan/drop.$std_kind" || std_fail "cannot apply PRIVACY_ALLOW"
        done < "$std_tmp/allow"
    done < "$std_scan/kinds"
}
