#!/usr/bin/env bats
# workflows.bats of the repository standard: the same file in every repository; never edit it
# here. The workflow rules: pins with their version comments, actions.allow,
# persist-credentials, triggers, expressions in scripts, top-level permissions, timeouts, GH_TOKEN,
# image digests, and the contracts of ci.yml, secret-scan.yml and release.yml. Each rule runs over
# this repository's workflows, and over a fixture that breaks it.
# A legacy release.yml or image.yml (RELEASE_KIND=legacy) meets the general rules except
# actions.allow.

# The awk programs and the YAML fixtures are single-quoted on purpose.
# shellcheck disable=SC2016
load lib

setup() {
    isolate_home
    WF=$REPO_ROOT/.github/workflows
    FIX=$BATS_TEST_TMPDIR/workflows
    mkdir -p "$FIX"
}

# Print the YAML file $1 as "PATH<TAB>VALUE" lines. PATH joins the keys with dots, and a list item
# with its index from 0; VALUE is the rest of the line with its comment, so a pin keeps its version
# comment. A block scalar (| or >) gives one line per line of its text, and a key without a value
# one line with an empty value. This is the subset of YAML that the workflows use.
flat() {
    awk '
        function path(n,   p, i) { p = ""; for (i = 1; i <= n; i++) p = p (i > 1 ? "." : "") key[i]; return p }
        function under(n, k) { return (n ? path(n) "." : "") k }
        {
            sub(/\r$/, "")
            if (block) {
                if ($0 ~ /^[ \t]*$/) next
                match($0, /^ */)
                if (RLENGTH > bind) { line = $0; sub(/^ */, "", line); print bpath "\t" line; next }
                block = 0
            }
            if ($0 ~ /^[ \t]*(#.*)?$/ || $0 ~ /^---/) next
            match($0, /^ */)
            ind = RLENGTH
            text = substr($0, ind + 1)
            while (depth > 0 && indent[depth] >= ind) depth--
            if (text ~ /^-( |$)/) {
                parent = path(depth)
                n = count[parent]++
                depth++; indent[depth] = ind; key[depth] = n
                text = substr(text, 3)
                ind += 2
                if (text !~ /^[A-Za-z0-9_.-]+:( |$)/) { print path(depth) "\t" text; next }
            }
            if (text ~ /^[A-Za-z0-9_.-]+:( |$)/) {
                k = substr(text, 1, index(text, ":") - 1)
                v = substr(text, index(text, ":") + 1)
                sub(/^ +/, "", v)
                bare = v
                sub(/^#.*$/, "", bare)
                sub(/[ \t]+#.*$/, "", bare)
                if (bare == "") {
                    print under(depth, k) "\t"
                    depth++; indent[depth] = ind; key[depth] = k
                    next
                }
                if (bare ~ /^[|>][-+]?$/) { block = 1; bind = ind; bpath = under(depth, k); next }
                print under(depth, k) "\t" v
                next
            }
            print path(depth) "\t" text
        }' "$1"
}

# The rules. Each takes workflow files, prints one line per problem on stderr and fails on any.

rule_pins() {
    local file path value bad=0
    for file in "$@"; do
        while IFS=$'\t' read -r path value; do
            [[ $path == uses || $path == *.uses ]] || continue
            [[ $value == ./* ]] && continue
            if [[ ! $value =~ ^[A-Za-z0-9_.-]+/[A-Za-z0-9_./-]+@[0-9a-f]{40}\ \#\ v[0-9]+(\.[0-9]+){0,2}$ ]]; then
                printf '%s: %s is not local, nor owner/repo@<40 hex> # vX[.Y[.Z]]\n' "${file##*/}" "$path" >&2
                bad=1
            fi
        done < <(flat "$file")
    done
    return "$bad"
}

# rule_allow ALLOW EXACT FILE...: every action the files use is in ALLOW; with EXACT=1 also every
# entry of ALLOW is used.
rule_allow() {
    local allow=$1 exact=$2 file line action bad=0
    local -A used=() listed=()
    shift 2
    while IFS= read -r line || [[ -n $line ]]; do
        [[ $line =~ ^[[:space:]]*(#|$) ]] && continue
        if [[ ! $line =~ ^([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)@\*$ ]]; then
            printf 'actions.allow: a line that is not owner/repo@*\n' >&2
            bad=1
            continue
        fi
        listed[${BASH_REMATCH[1]}]=1
    done < "$allow"
    for file in "$@"; do
        while IFS= read -r action; do
            used[$action]=1
            if [[ -z ${listed[$action]:-} ]]; then
                printf '%s uses %s, which actions.allow does not list\n' "${file##*/}" "$action" >&2
                bad=1
            fi
        done < <(flat "$file" | awk -F '\t' '$1 ~ /(^|\.)uses$/ && $2 !~ /^\.\// {
                v = $2; sub(/@.*/, "", v); split(v, p, "/"); print p[1] "/" p[2] }')
    done
    if (( exact )); then
        for action in "${!listed[@]}"; do
            if [[ -z ${used[$action]:-} ]]; then
                printf 'actions.allow lists %s, which no workflow uses\n' "$action" >&2
                bad=1
            fi
        done
    fi
    return "$bad"
}

rule_checkout() {
    local file bad=0
    for file in "$@"; do
        flat "$file" | awk -F '\t' -v f="${file##*/}" '
            $1 ~ /\.uses$/ && $2 ~ /^actions\/checkout@/ { s = $1; sub(/\.uses$/, "", s); co[s] = 1 }
            $1 ~ /\.with\.persist-credentials$/ {
                v = $2; sub(/[ \t]+#.*$/, "", v)
                if (v == "false") { s = $1; sub(/\.with\.persist-credentials$/, "", s); ok[s] = 1 }
            }
            END {
                for (s in co) if (!(s in ok)) { print f ": " s ": a checkout without persist-credentials: false" > "/dev/stderr"; bad = 1 }
                exit bad
            }' || bad=1
    done
    return "$bad"
}

rule_triggers() {
    local file bad=0
    for file in "$@"; do
        flat "$file" | awk -F '\t' -v f="${file##*/}" '
            $1 ~ /^on\.(pull_request_target|workflow_run)(\.|$)/ ||
            ($1 ~ /^on(\.[0-9]+)?$/ && $2 ~ /(pull_request_target|workflow_run)/) {
                print f ": the trigger pull_request_target or workflow_run" > "/dev/stderr"; bad = 1
            }
            END { exit bad }' || bad=1
    done
    return "$bad"
}

rule_expressions() {
    local file bad=0
    for file in "$@"; do
        flat "$file" | awk -F '\t' -v f="${file##*/}" '
            $1 ~ /(^|\.)run$/ && index($2, "${{") {
                print f ": " $1 ": an expression inside a run script; pass it through env" > "/dev/stderr"; bad = 1
            }
            END { exit bad }' || bad=1
    done
    return "$bad"
}

rule_permissions() {
    local file bad=0
    for file in "$@"; do
        flat "$file" | awk -F '\t' '
            $1 ~ /^on\.[^.]+$/ { trig[substr($1, 4)] = 1 }
            $1 == "on" && $2 != "" { v = $2; gsub(/[][ ]/, "", v); n = split(v, a, ","); for (i = 1; i <= n; i++) trig[a[i]] = 1 }
            $1 ~ /^on\.[0-9]+$/ { trig[$2] = 1 }
            $1 == "permissions" { perm = 1 }
            END {
                only = 1; any = 0
                for (t in trig) { any = 1; if (t != "workflow_call") only = 0 }
                if (any && only) exit 0
                exit !perm
            }' || { printf '%s: no top-level permissions\n' "${file##*/}" >&2; bad=1; }
    done
    return "$bad"
}

rule_timeouts() {
    local file bad=0
    for file in "$@"; do
        flat "$file" | awk -F '\t' -v f="${file##*/}" '
            $1 ~ /^jobs\.[^.]+\.steps(\.|$)/ { split($1, p, "."); steps[p[2]] = 1 }
            $1 ~ /^jobs\.[^.]+\.timeout-minutes$/ { split($1, p, "."); tm[p[2]] = 1 }
            END {
                for (j in steps) if (!(j in tm)) { print f ": job " j " runs steps without timeout-minutes" > "/dev/stderr"; bad = 1 }
                exit bad
            }' || bad=1
    done
    return "$bad"
}

rule_token() {
    local file bad=0
    for file in "$@"; do
        flat "$file" | awk -F '\t' -v f="${file##*/}" '
            $1 ~ /(^|\.)env\.GH_TOKEN$/ {
                if ($1 !~ /^jobs\.[^.]+\.steps\.[0-9]+\.env\.GH_TOKEN$/) {
                    print f ": " $1 ": GH_TOKEN outside the env of a step" > "/dev/stderr"; bad = 1; next
                }
                s = $1; sub(/\.env\.GH_TOKEN$/, "", s); tok[s] = 1
            }
            $1 ~ /^jobs\.[^.]+\.steps\.[0-9]+\.run$/ {
                s = $1; sub(/\.run$/, "", s)
                if ($2 ~ /(^|[^A-Za-z0-9_.\/-])gh[ \t]/ || $2 ~ /\.github\/standard\/bin\/(release-[a-z]+|pr-title)([ \t]|$)/) calls[s] = 1
            }
            END {
                for (s in tok) if (!(s in calls)) { print f ": " s ": GH_TOKEN in a step that runs neither gh nor a release tool" > "/dev/stderr"; bad = 1 }
                exit bad
            }' || bad=1
    done
    return "$bad"
}

rule_images() {
    local file bad=0
    for file in "$@"; do
        flat "$file" | awk -F '\t' -v f="${file##*/}" '
            ($1 ~ /^jobs\.[^.]+\.container$/ && $2 != "") || $1 ~ /^jobs\.[^.]+\.(container|services\.[^.]+)\.image$/ {
                if ($2 !~ /@sha256:[0-9a-f]{64}/) { print f ": " $1 ": an image without @sha256:" > "/dev/stderr"; bad = 1 }
            }
            END { exit bad }' || bad=1
    done
    return "$bad"
}

# The contracts. contract FILE NAME TRIGGERS JOBS TOP JOB=MAP... checks the name, the set of
# triggers, the set of job ids, the top-level permission map, that no job has a name, and the
# exact permission map of each job. A map is its sorted key=value words joined by commas, or {}.

# Print the permission map at the path $2 of the flattened file $1.
perm_map() {
    awk -F '\t' -v p="$2" '
        $1 == p && $2 != "" { v = $2; sub(/[ \t]+#.*$/, "", v); print v; next }
        index($1, p ".") == 1 { k = substr($1, length(p) + 2); v = $2; sub(/[ \t]+#.*$/, "", v); print k "=" v }' "$1" |
        LC_ALL=C sort | paste -s -d , -
}

contract() {
    local file=$1 name=$2 triggers=$3 jobs=$4 top=$5 flat=$BATS_TEST_TMPDIR/contract.flat got spec job want bad=0
    shift 5
    flat "$file" > "$flat"
    got=$(awk -F '\t' '$1 == "name" { print $2 }' "$flat")
    [[ $got == "$name" ]] || { printf '%s: name %s, not %s\n' "${file##*/}" "$got" "$name" >&2; bad=1; }
    got=$(awk -F '\t' '$1 ~ /^on\.[^.]+$/ { print substr($1, 4) }' "$flat" | LC_ALL=C sort | paste -s -d ' ' -)
    [[ $got == "$triggers" ]] || { printf '%s: triggers %s, not %s\n' "${file##*/}" "$got" "$triggers" >&2; bad=1; }
    got=$(awk -F '\t' '$1 ~ /^jobs\.[^.]+$/ { print substr($1, 6) }' "$flat" | LC_ALL=C sort | paste -s -d ' ' -)
    [[ $got == "$jobs" ]] || { printf '%s: jobs %s, not %s\n' "${file##*/}" "$got" "$jobs" >&2; bad=1; }
    got=$(perm_map "$flat" permissions)
    [[ $got == "$top" ]] || { printf '%s: top-level permissions %s, not %s\n' "${file##*/}" "$got" "$top" >&2; bad=1; }
    if grep -q -E $'^jobs\\.[^.]+\\.name\t' "$flat"; then
        printf '%s: a job has a name, so its check is not its job id\n' "${file##*/}" >&2
        bad=1
    fi
    for spec in "$@"; do
        job=${spec%%=*}
        want=${spec#*=}
        got=$(perm_map "$flat" "jobs.$job.permissions")
        [[ $got == "$want" ]] || { printf '%s: job %s has permissions %s, not %s\n' "${file##*/}" "$job" "${got:-none}" "$want" >&2; bad=1; }
    done
    return "$bad"
}

contract_ci() {
    local bad=0 needs
    contract "$1" ci 'pull_request push schedule workflow_call workflow_dispatch' \
        'all-passed hooks-windows image lint standard system test' contents=read \
        standard=contents=read,pull-requests=read lint=contents=read test=contents=read \
        system=contents=read image=contents=read hooks-windows=contents=read 'all-passed={}' || bad=1
    needs=$(flat "$1" | awk -F '\t' '$1 == "jobs.all-passed.needs" { print $2 }')
    if [[ $needs != '[standard, lint, test, system, image, hooks-windows]' ]]; then
        printf 'ci.yml: all-passed needs %s, not every other job\n' "$needs" >&2
        bad=1
    fi
    # The jobs that run the standard's suites see the release tags: release-meta.bats checks a
    # repository against its tags, and without them a published release looks as if it waits.
    if [[ $(flat "$1" | awk -F '\t' '$1 == "jobs.standard.steps.0.with.fetch-depth" { print $2 }') != 0 ]]; then
        printf 'ci.yml: the standard job checks out without the history and the tags\n' >&2
        bad=1
    fi
    if [[ $(flat "$1" | awk -F '\t' '$1 == "jobs.test.steps.0.with.fetch-tags" { print $2 }') != true ]]; then
        printf 'ci.yml: the test job checks out without the tags\n' >&2
        bad=1
    fi
    # A title or body edited in the browser becomes the squash commit's message, so CI checks the
    # pull request text again on every edit.
    if [[ $(flat "$1" | awk -F '\t' '$1 == "on.pull_request.types" { print $2 }') != \
        '[opened, synchronize, reopened, edited]' ]]; then
        printf 'ci.yml: pull_request does not run again when the title or body is edited\n' >&2
        bad=1
    fi
    return "$bad"
}

contract_scan() {
    contract "$1" secret-scan 'pull_request push schedule workflow_dispatch' scan contents=read \
        scan=contents=read
}

contract_release() {
    local flat=$BATS_TEST_TMPDIR/release.flat bad=0 job want got
    contract "$1" release 'pull_request push release workflow_dispatch' \
        'attest build build-image checks draft dry-run meta promote verify' '{}' \
        meta=contents=read dry-run=contents=read checks=contents=read,pull-requests=read \
        build=contents=read build-image=contents=read,packages=write \
        attest=attestations=write,id-token=write draft=contents=write \
        promote=contents=read,packages=write verify=attestations=read,contents=read,packages=read || bad=1
    flat "$1" > "$flat"
    for job in draft promote verify; do
        want=.github/standard/bin
        # release-verify reads the config through conf, which needs the rule packs in .githooks.
        [[ $job != verify ]] || want='.github/standard/bin .githooks ci'
        got=$(awk -F '\t' -v p="jobs.$job.steps.0.with.sparse-checkout" '$1 == p { print $2 }' "$flat" | paste -s -d ' ' -)
        [[ $got == "$want" ]] ||
            { printf 'release.yml: job %s checks out %s, not the directories %s\n' "$job" "${got:-everything}" "$want" >&2; bad=1; }
        if awk -F '\t' -v p="jobs.$job.steps.0.with.sparse-checkout-cone-mode" '$1 == p && $2 != "true" { f = 1 } END { exit !f }' "$flat"; then
            printf 'release.yml: job %s turns cone mode off\n' "$job" >&2
            bad=1
        fi
    done
    return "$bad"
}

# The workflows of this repository, and those of them that the rules hold to all of the standard:
# every one but a legacy release.yml or image.yml.
own_workflows() {
    local file kind
    kind=$(sh "$REPO_ROOT/.github/standard/bin/conf" --get RELEASE_KIND "$REPO_ROOT/.github/standard.conf")
    for file in "$WF"/*.yml "$WF"/*.yaml; do
        [[ -f $file ]] || continue
        if [[ ${1:-} == --canonical && $kind == legacy ]]; then
            case ${file##*/} in release.yml | image.yml) continue ;; esac
        fi
        printf '%s\n' "$file"
    done
}

# The workflows that the MANIFEST byte-checks here: its file entries under .github/workflows/,
# less the release pair under RELEASE_KIND=legacy.
checked_workflows() {
    local path kind
    kind=$(sh "$REPO_ROOT/.github/standard/bin/conf" --get RELEASE_KIND "$REPO_ROOT/.github/standard.conf")
    while IFS= read -r path; do
        if [[ $kind == legacy ]]; then
            case $path in */release.yml | */image.yml) continue ;; esac
        fi
        [[ -f $REPO_ROOT/$path ]] && printf '%s\n' "$REPO_ROOT/$path"
    done < <(awk '$1 == "file" && $5 ~ /^\.github\/workflows\// { print $5 }' "$REPO_ROOT/.github/standard/MANIFEST")
}

@test "the flattener reads keys, list items, block scalars and comments as the rules expect" {
    printf '%s\n' 'name: x # a comment' 'on:' '  push:' '    branches: [main]' 'jobs:' '  a:' \
        '    steps:' '      - uses: o/r@0123 # v1' '        with:' '          k: v' '      - run: |' \
        '          echo one' '' '          # two' '      - name: n' > "$FIX/x.yml"
    run flat "$FIX/x.yml"
    [ "$status" -eq 0 ]
    [ "${lines[0]}" = $'name\tx # a comment' ]
    [ "${lines[1]}" = $'on\t' ]
    [ "${lines[3]}" = $'on.push.branches\t[main]' ]
    [[ $output == *$'jobs.a.steps.0.uses\to/r@0123 # v1'* ]]
    [[ $output == *$'jobs.a.steps.0.with.k\tv'* ]]
    [[ $output == *$'jobs.a.steps.1.run\techo one\njobs.a.steps.1.run\t# two'* ]]
    [[ $output == *$'jobs.a.steps.2.name\tn'* ]]
}

@test "every uses: is local, or an action pinned to a commit with its version comment" {
    mapfile -t files < <(own_workflows)
    (( ${#files[@]} > 0 ))
    run rule_pins "${files[@]}"
    [ "$status" -eq 0 ] || { printf '%s\n' "$output" >&2; return 1; }
    printf '%s\n' 'on: push' 'jobs:' '  a:' '    steps:' '      - uses: actions/checkout@v7' > "$FIX/tag.yml"
    printf '%s\n' 'on: push' 'jobs:' '  a:' '    steps:' \
        '      - uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1' > "$FIX/bare.yml"
    for fixture in tag bare; do
        run rule_pins "$FIX/$fixture.yml"
        [ "$status" -eq 1 ]
    done
}

@test "the actions the checked workflows use are in actions.allow, and in the source both ways" {
    local exact=0 role
    mapfile -t files < <(checked_workflows)
    (( ${#files[@]} > 0 ))
    role=$(sh "$REPO_ROOT/.github/standard/bin/conf" --get ROLE "$REPO_ROOT/.github/standard.conf")
    [[ $role != source ]] || exact=1
    run rule_allow "$REPO_ROOT/.github/standard/actions.allow" "$exact" "${files[@]}"
    [ "$status" -eq 0 ] || { printf '%s\n' "$output" >&2; return 1; }
    printf '%s\n' 'on: push' 'jobs:' '  a:' '    steps:' \
        '      - uses: example-owner/action@0123456789012345678901234567890123456789 # v1' > "$FIX/other.yml"
    run rule_allow "$REPO_ROOT/.github/standard/actions.allow" 0 "$FIX/other.yml"
    [ "$status" -eq 1 ]
    [[ $output == *"example-owner/action, which actions.allow does not list"* ]]
    printf '%s\n' 'actions/checkout@*' 'example-owner/unused@*' > "$FIX/allow"
    printf '%s\n' 'on: push' 'jobs:' '  a:' '    steps:' \
        '      - uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1 # v7.0.1' > "$FIX/one.yml"
    run rule_allow "$FIX/allow" 1 "$FIX/one.yml"
    [ "$status" -eq 1 ]
    [[ $output == *"lists example-owner/unused, which no workflow uses"* ]]
    run rule_allow "$FIX/allow" 0 "$FIX/one.yml"
    [ "$status" -eq 0 ]
}

@test "every checkout sets persist-credentials: false" {
    mapfile -t files < <(own_workflows)
    run rule_checkout "${files[@]}"
    [ "$status" -eq 0 ] || { printf '%s\n' "$output" >&2; return 1; }
    sed 's/persist-credentials: false/persist-credentials: true/' "$WF/secret-scan.yml" > "$FIX/scan.yml"
    run rule_checkout "$FIX/scan.yml"
    [ "$status" -eq 1 ]
    printf '%s\n' 'on: push' 'jobs:' '  a:' '    steps:' \
        '      - uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1 # v7.0.1' > "$FIX/none.yml"
    run rule_checkout "$FIX/none.yml"
    [ "$status" -eq 1 ]
}

@test "no workflow runs on pull_request_target or workflow_run" {
    mapfile -t files < <(own_workflows)
    run rule_triggers "${files[@]}"
    [ "$status" -eq 0 ] || { printf '%s\n' "$output" >&2; return 1; }
    printf '%s\n' 'on:' '  pull_request_target:' > "$FIX/target.yml"
    printf '%s\n' 'on: [push, workflow_run]' > "$FIX/run.yml"
    for fixture in target run; do
        run rule_triggers "$FIX/$fixture.yml"
        [ "$status" -eq 1 ]
    done
}

@test "no run script holds an expression; values come through env" {
    mapfile -t files < <(own_workflows)
    run rule_expressions "${files[@]}"
    [ "$status" -eq 0 ] || { printf '%s\n' "$output" >&2; return 1; }
    printf '%s\n' 'on: push' 'jobs:' '  a:' '    steps:' '      - run: echo ${{ github.head_ref }}' > "$FIX/line.yml"
    printf '%s\n' 'on: push' 'jobs:' '  a:' '    steps:' '      - run: |' '          true' \
        '          echo "${{ github.event.pull_request.title }}"' > "$FIX/block.yml"
    for fixture in line block; do
        run rule_expressions "$FIX/$fixture.yml"
        [ "$status" -eq 1 ]
    done
}

@test "every workflow but one only called by others has top-level permissions" {
    mapfile -t files < <(own_workflows)
    run rule_permissions "${files[@]}"
    [ "$status" -eq 0 ] || { printf '%s\n' "$output" >&2; return 1; }
    grep -v -E '^permissions:|^  contents: read$' "$WF/secret-scan.yml" > "$FIX/scan.yml"
    run rule_permissions "$FIX/scan.yml"
    [ "$status" -eq 1 ]
    printf '%s\n' 'on:' '  workflow_call:' 'jobs: {}' > "$FIX/called.yml"
    run rule_permissions "$FIX/called.yml"
    [ "$status" -eq 0 ]
}

@test "every job that runs steps has timeout-minutes" {
    mapfile -t files < <(own_workflows)
    run rule_timeouts "${files[@]}"
    [ "$status" -eq 0 ] || { printf '%s\n' "$output" >&2; return 1; }
    grep -v 'timeout-minutes' "$WF/secret-scan.yml" > "$FIX/scan.yml"
    run rule_timeouts "$FIX/scan.yml"
    [ "$status" -eq 1 ]
}

@test "GH_TOKEN is only in the env of a step that runs gh or a release tool" {
    mapfile -t files < <(own_workflows --canonical)
    run rule_token "${files[@]}"
    [ "$status" -eq 0 ] || { printf '%s\n' "$output" >&2; return 1; }
    printf '%s\n' 'on: push' 'jobs:' '  a:' '    env:' '      GH_TOKEN: x' '    steps:' '      - run: gh api x' \
        > "$FIX/job.yml"
    printf '%s\n' 'on: push' 'jobs:' '  a:' '    steps:' '      - env:' '          GH_TOKEN: x' \
        '        run: ./ci/test' > "$FIX/other.yml"
    for fixture in job other; do
        run rule_token "$FIX/$fixture.yml"
        [ "$status" -eq 1 ]
    done
    printf '%s\n' 'on: push' 'jobs:' '  a:' '    steps:' '      - env:' '          GH_TOKEN: x' \
        '        run: .github/standard/bin/release-verify --tag "$TAG"' > "$FIX/tool.yml"
    run rule_token "$FIX/tool.yml"
    [ "$status" -eq 0 ]
}

@test "container and services images are pinned by digest" {
    mapfile -t files < <(own_workflows --canonical)
    run rule_images "${files[@]}"
    [ "$status" -eq 0 ] || { printf '%s\n' "$output" >&2; return 1; }
    printf '%s\n' 'on: push' 'jobs:' '  a:' '    container: debian:13' '    steps:' '      - run: true' \
        > "$FIX/container.yml"
    printf '%s\n' 'on: push' 'jobs:' '  a:' '    services:' '      db:' '        image: postgres:18' \
        > "$FIX/services.yml"
    for fixture in container services; do
        run rule_images "$FIX/$fixture.yml"
        [ "$status" -eq 1 ]
    done
}

@test "ci.yml keeps its contract: triggers, jobs, the gate's needs, no job names, each job's permissions" {
    run contract_ci "$WF/ci.yml"
    [ "$status" -eq 0 ] || { printf '%s\n' "$output" >&2; return 1; }
    sed 's/^  lint:$/  lint:\n    name: Lint/' "$WF/ci.yml" > "$FIX/named.yml"
    run contract_ci "$FIX/named.yml"
    [ "$status" -eq 1 ]
    [[ $output == *"a job has a name"* ]]
    sed '/^  standard:$/,/^  [a-z-]*:$/ s/pull-requests: read/pull-requests: write/' "$WF/ci.yml" > "$FIX/write.yml"
    run contract_ci "$FIX/write.yml"
    [ "$status" -eq 1 ]
    [[ $output == *"job standard has permissions contents=read,pull-requests=write"* ]]
    sed 's/^    needs: \[standard, lint, test, system, image, hooks-windows\]$/    needs: [standard, lint]/' \
        "$WF/ci.yml" > "$FIX/needs.yml"
    run contract_ci "$FIX/needs.yml"
    [ "$status" -eq 1 ]
    grep -v -E '^ +fetch-tags: true$' "$WF/ci.yml" > "$FIX/tagless.yml"
    run contract_ci "$FIX/tagless.yml"
    [ "$status" -eq 1 ]
    [[ $output == *"the test job checks out without the tags"* ]]
}

@test "secret-scan.yml keeps its contract" {
    run contract_scan "$WF/secret-scan.yml"
    [ "$status" -eq 0 ] || { printf '%s\n' "$output" >&2; return 1; }
    grep -v -E '^  schedule:$|cron:' "$WF/secret-scan.yml" > "$FIX/scan.yml"
    run contract_scan "$FIX/scan.yml"
    [ "$status" -eq 1 ]
    [[ $output == *"triggers pull_request push workflow_dispatch"* ]]
}

@test "release.yml keeps its contract, and its sparse checkouts list directories only" {
    local kind
    kind=$(sh "$REPO_ROOT/.github/standard/bin/conf" --get RELEASE_KIND "$REPO_ROOT/.github/standard.conf")
    # Under legacy, release.yml is the repository's own; the test below holds it to the general
    # rules, and there is no canonical file here to break.
    [[ $kind != legacy ]] || return 0
    run contract_release "$WF/release.yml"
    [ "$status" -eq 0 ] || { printf '%s\n' "$output" >&2; return 1; }
    awk '/^  verify:$/ { v = 1 } v && $1 == "ci" { sub(/ci$/, "ci/release-verify*") } { print }' \
        "$WF/release.yml" > "$FIX/glob.yml"
    run contract_release "$FIX/glob.yml"
    [ "$status" -eq 1 ]
    [[ $output == *"job verify checks out .github/standard/bin .githooks ci/release-verify*"* ]]
    sed '/^  build:$/,/^  build-image:$/ s/contents: read/contents: write/' "$WF/release.yml" > "$FIX/write.yml"
    run contract_release "$FIX/write.yml"
    [ "$status" -eq 1 ]
    [[ $output == *"job build has permissions contents=write"* ]]
}

@test "a legacy release.yml meets the general rules only, never actions.allow or the contract" {
    printf '%s\n' '# A legacy release workflow.' 'name: release' 'on:' '  push:' '    tags: ["v*"]' \
        'permissions:' '  contents: write' 'jobs:' '  publish:' '    runs-on: ubuntu-26.04' \
        '    timeout-minutes: 10' '    steps:' \
        '      - uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1 # v7.0.1' \
        '        with:' '          persist-credentials: false' \
        '      - uses: example-owner/publish@0123456789012345678901234567890123456789 # v2.1' \
        > "$FIX/release.yml"
    for rule in rule_pins rule_checkout rule_triggers rule_expressions rule_permissions rule_timeouts; do
        run "$rule" "$FIX/release.yml"
        [ "$status" -eq 0 ] || { printf '%s: %s\n' "$rule" "$output" >&2; return 1; }
    done
    run contract_release "$FIX/release.yml"
    [ "$status" -eq 1 ]
    run rule_allow "$REPO_ROOT/.github/standard/actions.allow" 0 "$FIX/release.yml"
    [ "$status" -eq 1 ]
}
