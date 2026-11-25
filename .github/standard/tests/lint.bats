#!/usr/bin/env bats
# bats file_tags=lint
# lint.bats of the repository standard: the same file in every repository; never edit it here.
# Syntax checks (sh -n, dash -n where present, bash -n), ShellCheck, the modes git stores, LF
# line ends, and image pins, for the hooks, the standard's tools and tests, and the shell scripts
# in ci/. CI runs it on Linux in every repository; on Windows the other suites run without it.

# The sh -c scripts, the actionlint template and the fixtures are single-quoted on purpose.
# shellcheck disable=SC2016
load lib

setup() {
    isolate_home
}

# Print the shell files to lint, relative to REPO_ROOT, as "sh PATH" or "bash PATH": the hooks
# and lib.sh, the standard's bin/ and tests/, and every script in ci/ with a shell shebang.
shell_files() {
    local path first
    while IFS= read -r path; do
        case $path in
            .githooks/rules/*) continue ;;
            .githooks/lib.sh) printf 'sh %s\n' "$path"; continue ;;
            *.bash | *.bats) printf 'bash %s\n' "$path"; continue ;;
        esac
        first=
        IFS= read -r first < "$REPO_ROOT/$path" || true
        case $first in
            '#!/bin/sh'*) printf 'sh %s\n' "$path" ;;
            '#!/bin/bash'* | '#!/usr/bin/env bash'*) printf 'bash %s\n' "$path" ;;
        esac
    done < <(git -C "$REPO_ROOT" ls-files -- .githooks .github/standard/bin .github/standard/tests ci)
}

@test "the hooks, lib.sh and the sh scripts pass sh -n, and dash -n where dash exists" {
    local shell path checked=0
    while read -r shell path; do
        [[ $shell == sh ]] || continue
        sh -n "$REPO_ROOT/$path"
        if command -v dash > /dev/null 2>&1; then
            dash -n "$REPO_ROOT/$path"
        fi
        checked=$((checked + 1))
    done < <(shell_files)
    (( checked >= 8 ))
}

@test "the bash scripts and helpers pass bash -n" {
    local shell path
    while read -r shell path; do
        [[ $shell == bash && $path != *.bats ]] || continue
        bash -n "$REPO_ROOT/$path"
    done < <(shell_files)
}

@test "ShellCheck reports nothing in the hooks, the standard's tools and tests, and ci/" {
    local -a paths=()
    local shell path
    need command shellcheck
    while read -r shell path; do
        paths+=("$path")
    done < <(shell_files)
    run_shellcheck "${paths[@]}"
    [ "$status" -eq 0 ] || { shellcheck_code_counts >&2; printf '%s\n' "$output" >&2; return 1; }
}

@test "git stores files with a shebang and .bats files as 100755, the others as 100644" {
    local mode path want first
    while read -r mode _ _ path; do
        first=
        IFS= read -r first < "$REPO_ROOT/$path" || true
        if [[ $first == '#!'* || $path == *.bats ]]; then want=100755; else want=100644; fi
        if [[ $mode != "$want" ]]; then
            printf '%s is stored with mode %s, not %s\n' "$path" "$mode" "$want" >&2
            return 1
        fi
    done < <(git -C "$REPO_ROOT" ls-files -s -- .githooks .github/standard ci)
}

@test "every hook and every tool of bin/ is a shell script that the lint checks" {
    local path
    local listed
    listed=$(shell_files)
    while IFS= read -r path; do
        [[ $path == .githooks/rules/* ]] && continue
        if [[ $listed != *" $path"* ]]; then
            printf '%s is not linted: give it a #!/bin/sh or #!/bin/bash line\n' "$path" >&2
            return 1
        fi
    done < <(git -C "$REPO_ROOT" ls-files -- .githooks .github/standard/bin)
}

@test "the standard's files and the ci/ shell scripts hold no CR byte" {
    local -a files=()
    local path
    while IFS= read -r path; do
        files+=("$REPO_ROOT/$path")
    done < <(git -C "$REPO_ROOT" ls-files -- .githooks .github/standard
        shell_files | awk '$2 ~ /^ci\// { print $2 }')
    (( ${#files[@]} > 0 ))
    # -U keeps a grep that reads files in text mode from dropping each CR before it matches.
    run grep -U -l $'\r' -- "${files[@]}"
    [ "$status" -eq 1 ]
}

# Print "FILE: REFERENCE" for each image reference without @sha256: in the files given: a FROM
# line, an image: key, a docker:// reference, and an image= option such as buildx's driver-opts.
# References built from expressions or variables are left to the rules that build them.
unpinned_images() {
    local path line
    for path in "$@"; do
        while IFS= read -r line; do
            [[ $line == *@sha256:* ]] || printf '%s: %s\n' "${path##*/}" "$line"
        done < <(grep -E -o '(^[[:space:]]*FROM[[:space:]]+[^[:space:]]+|image:[[:space:]]+[^[:space:]]+|image=[A-Za-z0-9][A-Za-z0-9._/-]*[:/][^[:space:],$]+|docker://[^[:space:]]+)' \
            "$path" | grep -v -E '\$\{\{|^[[:space:]]*FROM[[:space:]]+scratch$' || true)
    done
}

@test "every image reference in ci/ and .github/ carries @sha256:" {
    local -a files=()
    local path
    while IFS= read -r path; do
        files+=("$REPO_ROOT/$path")
    done < <(git -C "$REPO_ROOT" ls-files -- ci .github ':!.github/standard/tests')
    (( ${#files[@]} > 0 ))
    run unpinned_images "${files[@]}"
    [ -z "$output" ] || { printf 'an image without @sha256: %s\n' "$output" >&2; return 1; }
}

@test "an image reference without @sha256: is found in each form, an image= option included" {
    printf '%s\n' 'FROM debian:13' '    container:' '      image: debian:13' 'uses: docker://alpine:3.22' \
        '          driver-opts: image=moby/buildkit:v0.34.1' 'image=$1' 'type=image,name=x' \
        'image=ghcr.io/$(printf x)' \
        '          driver-opts: image=moby/buildkit:v0.34.1@sha256:0123' > "$BATS_TEST_TMPDIR/pins.yml"
    run unpinned_images "$BATS_TEST_TMPDIR/pins.yml"
    [ "${#lines[@]}" -eq 4 ] || { printf '%s\n' "$output" >&2; return 1; }
    [[ ${lines[3]} == *"image=moby/buildkit:v0.34.1" ]]
}

@test "actionlint finds nothing in the workflows but the ubuntu-26.04 label it does not know yet" {
    local message
    need command actionlint
    # actionlint 1.7.12 does not know that runner label; ci.yml ignores exactly that error.
    # actionlint turns each \n of a -format string into a line feed before it parses the template,
    # so the line feed stands outside the template's actions.
    run --separate-stderr sh -c 'cd "$1" && exec actionlint -no-color -format "$2"' sh "$REPO_ROOT" \
        '{{range $err := .}}{{$err.Message}}\n{{end}}'
    [ "$status" -le 1 ] || { printf '%s\n' "$stderr" >&2; return 1; }
    while IFS= read -r message; do
        [[ -z $message || $message == 'label "ubuntu-26.04" is unknown'* ]] ||
            { printf 'actionlint: %s\n' "$message" >&2; return 1; }
    done <<< "$output"
}
