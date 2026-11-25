# shellcheck shell=bash
# lib.bash of the repository standard's tools: the same file in every repository; never edit it
# here. The bash tools of .github/standard/bin source it; it runs nothing by itself.
#   say TEXT...           print "TOOL: TEXT" on stderr
#   error TEXT...         the same, as an error annotation (::error::) in Actions
#   die TEXT...           error TEXT, then exit 1
#   on_windows            true in Git Bash and the other Windows shells
#   win_path PATH         PATH as Windows writes it in Git Bash, unchanged elsewhere
#   mixed_path PATH       PATH with a drive letter and forward slashes in Git Bash (C:/...), which
#                         bash and PowerShell both read, so a hook of either kind gets it; unchanged
#                         elsewhere
#   has_hook NAME         true when ci/NAME or ci/NAME.ps1 exists
#   run_hook NAME ARGS... run ci/NAME, then ci/NAME.ps1, with ARGS (the ci/ interface)
#   version_lt A B        true when version A is lower than version B, both X.Y.Z
#   check_sums DIR        true when DIR/SHA256SUMS lists exactly the other files of DIR and
#                         matches them; else false after an error that names the problem
#   extract ARCHIVE PATH  print the member PATH of a .zip or .tar.gz ARCHIVE
#   json_value FILE KEY   print the value of KEY in a release JSON file with one key per line,
#                         without its quotes; false unless KEY is there exactly once
# TOOL is the name of the script that sources this file. The tools run from the top of the
# repository, where ci/ lives.

std_tool=${0##*/}

say() {
    printf '%s: %s\n' "$std_tool" "$*" >&2
}

error() {
    if [[ ${GITHUB_ACTIONS:-} == true ]]; then
        printf '::error::%s: %s\n' "$std_tool" "$*" >&2
    else
        say "$@"
    fi
}

die() {
    error "$@"
    exit 1
}

on_windows() {
    case $(uname -s) in
        MINGW* | MSYS* | CYGWIN*) return 0 ;;
        *) return 1 ;;
    esac
}

win_path() {
    if command -v cygpath > /dev/null 2>&1; then
        cygpath -w "$1"
    else
        printf '%s\n' "$1"
    fi
}

mixed_path() {
    if command -v cygpath > /dev/null 2>&1; then
        cygpath -m "$1"
    else
        printf '%s\n' "$1"
    fi
}

has_hook() {
    [[ -f ci/$1 || -f ci/$1.ps1 ]]
}

version_lt() {
    local -a first second
    IFS=. read -r -a first <<< "$1"
    IFS=. read -r -a second <<< "$2"
    if (( first[0] != second[0] )); then (( first[0] < second[0] )); return; fi
    if (( first[1] != second[1] )); then (( first[1] < second[1] )); return; fi
    (( first[2] < second[2] ))
}

check_sums() {
    local listed present file
    if [[ ! -f $1/SHA256SUMS ]]; then
        error "the assets have no SHA256SUMS"
        return 1
    fi
    listed=$(sed 's/^[^ ]* [ *]//' "$1/SHA256SUMS" | LC_ALL=C sort)
    present=$(for file in "$1"/*; do
        if [[ -f $file && ${file##*/} != SHA256SUMS ]]; then printf '%s\n' "${file##*/}"; fi
    done | LC_ALL=C sort)
    if [[ $listed != "$present" ]]; then
        error "SHA256SUMS must list exactly the assets"
        return 1
    fi
    if ! (cd "$1" && sha256sum --check --strict --quiet SHA256SUMS) > /dev/null 2>&1; then
        error "SHA256SUMS does not match the assets"
        return 1
    fi
}

# tar reads the archive on stdin, so it never takes a path with a colon for a remote tape host.
extract() {
    case $1 in
        *.zip) unzip -p "$1" "$2" ;;
        *) tar -xzOf - "$2" < "$1" ;;
    esac
}

json_value() {
    local value
    value=$(sed -n 's/^[[:space:]]*"'"$2"'"[[:space:]]*:[[:space:]]*"\{0,1\}\([^",]*\)"\{0,1\}[[:space:]]*,\{0,1\}[[:space:]]*$/\1/p' \
        "$1" | tr -d '\r') || return 1
    [[ -n $value && $value != *$'\n'* ]] || return 1
    printf '%s\n' "$value"
}

# Run ci/NAME with ARGS when it exists, then ci/NAME.ps1 with ARGS under Windows PowerShell 5.1,
# as spec 2.6 sets it: -NoProfile -ExecutionPolicy Bypass, with TEMP and TMP set to RUNNER_TEMP.
# Return the first failing status, or 0. Off Windows, a ci/NAME.ps1 ends the tool with an error.
# POWERSHELL names the PowerShell to run (default powershell.exe).
run_hook() {
    local name=$1 temp
    shift
    if [[ -f ci/$name ]]; then
        "./ci/$name" "$@" || return
    fi
    if [[ -f ci/$name.ps1 ]]; then
        on_windows || die "ci/$name.ps1 needs Windows PowerShell 5.1, which runs on Windows only"
        temp=$(win_path "${RUNNER_TEMP:-${TMPDIR:-/tmp}}")
        TEMP=$temp TMP=$temp "${POWERSHELL:-powershell.exe}" -NoProfile -ExecutionPolicy Bypass \
            -File "$(win_path "ci/$name.ps1")" "$@" || return
    fi
}
