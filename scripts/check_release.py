# -------------------------------------------------------
# Release check
#
# Run by the release workflow before anything is built: checks that the release tag is
# vMAJOR.MINOR.PATCH and that CHANGELOG.md has a dated heading for that version, then prints the
# version without its "v". Standard library only, so the runner's own python3 can run it.
# -------------------------------------------------------

import argparse
import os
import re
import sys
from datetime import date
from pathlib import Path

# The CHANGELOG at the repo root, beside scripts/.
CHANGELOG = Path(__file__).resolve().parent.parent / 'CHANGELOG.md'

# A SemVer core with no leading zeros and no prerelease or build part. [0-9] rather than \d, which
# also matches non-ASCII digits; used with fullmatch, since $ alone also matches before a "\n".
TAG_PATTERN = re.compile(r'^v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$')

# A release heading as Keep a Changelog writes it, such as "## [1.2.3] - 2026-11-02".
HEADING_PATTERN = re.compile(r'## \[(?P<version>[^\]]+)\] - (?P<date>[0-9]{4}-[0-9]{2}-[0-9]{2})')


# The version a release tag names, '1.2.3' for 'v1.2.3'; None unless the tag is exactly
# vMAJOR.MINOR.PATCH.
def version_from_tag(tag: str) -> str | None:
    if TAG_PATTERN.fullmatch(tag) is None:
        return None
    return tag[1:]


# True when a whole line of `changelog` is "## [<version>] - YYYY-MM-DD" with a real date.
# splitlines(), so a file with CRLF line endings matches as well.
def has_release_heading(changelog: str, version: str) -> bool:
    for line in changelog.splitlines():
        match = HEADING_PATTERN.fullmatch(line)
        if match is None or match['version'] != version:
            continue
        try:
            date.fromisoformat(match['date'])
        except ValueError:
            continue
        return True
    return False


# Command line: check_release.py [--changelog PATH] [TAG], where TAG defaults to $TAG as the
# release workflow sets it. Prints the version and returns 0; otherwise explains on stderr and
# returns 1, or 2 when no tag was given at all. An unknown option exits 2 through argparse.
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description='Check a release tag against CHANGELOG.md and print its version.'
    )
    parser.add_argument(
        'tag', nargs='?', default=os.environ.get('TAG', ''), help='such as v1.2.3 (default: $TAG)'
    )
    parser.add_argument(
        '--changelog', type=Path, default=CHANGELOG, help='default: CHANGELOG.md at the repo root'
    )
    args = parser.parse_args(argv)

    if not args.tag:
        print('check_release: no tag; pass one or set TAG', file=sys.stderr)
        return 2

    version = version_from_tag(args.tag)
    if version is None:
        print(
            f'check_release: tag {args.tag!r} is not vMAJOR.MINOR.PATCH, such as v1.2.3',
            file=sys.stderr,
        )
        return 1

    try:
        changelog = args.changelog.read_text(encoding='utf-8')
    except OSError as exc:
        print(f'check_release: cannot read {args.changelog}: {exc}', file=sys.stderr)
        return 1

    if not has_release_heading(changelog, version):
        print(
            f'check_release: {args.changelog.name} has no "## [{version}] - YYYY-MM-DD" heading',
            file=sys.stderr,
        )
        return 1

    print(version)
    return 0


if __name__ == '__main__':
    sys.exit(main())
