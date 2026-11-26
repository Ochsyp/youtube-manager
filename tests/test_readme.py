# -------------------------------------------------------
# README tests
#
# The README's install command names the newest release in CHANGELOG.md, so a new user starts on
# it, and a release pull request that adds its heading fails here until it moves that line too.
# -------------------------------------------------------

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# The README's install command, which writes the release a new user deploys into .env.
README_INSTALL_LINE = re.compile(r"echo 'YTM_VERSION=(?P<version>[^']*)' > \.env")

# A release heading as Keep a Changelog writes it, such as "## [1.2.3] - 2026-11-02".
RELEASE_HEADING = re.compile(r'## \[(?P<version>[^\]]+)\] - [0-9]{4}-[0-9]{2}-[0-9]{2}')


def test_readme_install_command_names_the_latest_release():
    readme = (REPO_ROOT / 'README.md').read_text(encoding='utf-8')
    changelog = (REPO_ROOT / 'CHANGELOG.md').read_text(encoding='utf-8')
    installs = [m['version'] for m in map(README_INSTALL_LINE.fullmatch, readme.splitlines()) if m]
    releases = [m['version'] for m in map(RELEASE_HEADING.fullmatch, changelog.splitlines()) if m]
    assert releases, 'CHANGELOG.md has no release heading'
    assert installs == [releases[0]], (
        f"README.md's install command should read: echo 'YTM_VERSION={releases[0]}' > .env"
    )
