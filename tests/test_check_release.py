# -------------------------------------------------------
# Release check tests
#
# scripts/check_release.py, loaded by path because scripts/ is not a package: which tags name a
# release, which CHANGELOG headings match one, and what main prints and returns.
# -------------------------------------------------------

import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / 'scripts' / 'check_release.py'

# Two releases under [Unreleased], their compare links at the end, as the release PR leaves it.
CHANGELOG = '''# Changelog

## [Unreleased]

## [1.2.3] - 2026-11-02

### Fixed

- Something.

## [1.2.0] - 2026-10-01

### Added

- Something else.

[Unreleased]: https://github.com/Ochsyp/youtube-manager/compare/v1.2.3...HEAD
[1.2.3]: https://github.com/Ochsyp/youtube-manager/compare/v1.2.0...v1.2.3
[1.2.0]: https://github.com/Ochsyp/youtube-manager/releases/tag/v1.2.0
'''


# Import scripts/check_release.py as a module without putting scripts/ on sys.path.
def load_script():
    spec = importlib.util.spec_from_file_location('check_release', SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


check_release = load_script()


# The CHANGELOG above, written to a temp file for main to read.
@pytest.fixture
def changelog_file(tmp_path):
    path = tmp_path / 'CHANGELOG.md'
    path.write_text(CHANGELOG, encoding='utf-8')
    return path


# A vMAJOR.MINOR.PATCH tag gives its version without the v.
@pytest.mark.parametrize(
    ('tag', 'version'),
    [('v1.2.3', '1.2.3'), ('v0.1.0', '0.1.0'), ('v0.0.0', '0.0.0'), ('v10.20.30', '10.20.30')],
)
def test_release_tag_gives_its_version(tag, version):
    assert check_release.version_from_tag(tag) == version


# Anything else is refused: a missing part, no v, a leading zero, a prerelease or build suffix,
# a fourth part, another case, stray whitespace, and a non-ASCII digit.
@pytest.mark.parametrize(
    'tag',
    [
        'v1.2',
        '1.2.3',
        'v01.2.3',
        'v1.02.3',
        'v1.2.03',
        'v1.2.3-rc.1',
        'v1.2.3+build.5',
        'v1.2.3.4',
        'V1.2.3',
        ' v1.2.3',
        'v1.2.3\n',
        'v',
        '',
        'v\N{ARABIC-INDIC DIGIT ONE}.2.3',
    ],
)
def test_other_tags_are_refused(tag):
    assert check_release.version_from_tag(tag) is None


# Each released version has its heading; one that was never released has none.
def test_changelog_heading_found_for_released_versions():
    assert check_release.has_release_heading(CHANGELOG, '1.2.3')
    assert check_release.has_release_heading(CHANGELOG, '1.2.0')
    assert not check_release.has_release_heading(CHANGELOG, '1.2.4')
    assert not check_release.has_release_heading(CHANGELOG, '1.2')


# A heading counts only in the exact form with a real date: not without a date or brackets, at
# another level, indented, with an en dash, a short or impossible date, or anything after it, and
# not as a compare link or another version that merely starts the same.
@pytest.mark.parametrize(
    'line',
    [
        '## [1.2.3]',
        '## 1.2.3 - 2026-11-02',
        '### [1.2.3] - 2026-11-02',
        ' ## [1.2.3] - 2026-11-02',
        '## [1.2.3] \N{EN DASH} 2026-11-02',
        '## [1.2.3] - 2026-11-2',
        '## [1.2.3] - 2026-13-01',
        '## [1.2.3] - 2026-02-30',
        '## [1.2.3] - 2026-11-02 [YANKED]',
        '[1.2.3]: https://github.com/Ochsyp/youtube-manager/compare/v1.2.0...v1.2.3',
        '## [1.2.30] - 2026-11-02',
        '## [v1.2.3] - 2026-11-02',
    ],
)
def test_malformed_heading_does_not_count(line):
    changelog = f'# Changelog\n\n## [Unreleased]\n\n{line}\n\n- Something.\n'
    assert not check_release.has_release_heading(changelog, '1.2.3')


# A CHANGELOG saved with CRLF line endings still matches.
def test_changelog_heading_found_with_crlf():
    assert check_release.has_release_heading(CHANGELOG.replace('\n', '\r\n'), '1.2.3')


# A valid tag with its heading prints the bare version, and nothing else, on stdout.
def test_main_prints_version(changelog_file, capsys):
    assert check_release.main(['v1.2.3', '--changelog', str(changelog_file)]) == 0
    out, err = capsys.readouterr()
    assert out == '1.2.3\n'
    assert err == ''


# Without an argument the tag comes from $TAG, as the release workflow passes it.
def test_main_reads_tag_from_environment(changelog_file, capsys, monkeypatch):
    monkeypatch.setenv('TAG', 'v1.2.0')
    assert check_release.main(['--changelog', str(changelog_file)]) == 0
    assert capsys.readouterr().out == '1.2.0\n'


# An invalid tag fails before the CHANGELOG is read, naming the tag and printing no version.
def test_main_refuses_invalid_tag(tmp_path, capsys):
    missing = tmp_path / 'absent.md'
    assert check_release.main(['v1.2.3-rc.1', '--changelog', str(missing)]) == 1
    out, err = capsys.readouterr()
    assert out == ''
    assert "'v1.2.3-rc.1'" in err


# A valid tag without its CHANGELOG heading fails, naming the heading it looked for.
def test_main_refuses_version_without_heading(changelog_file, capsys):
    assert check_release.main(['v1.2.4', '--changelog', str(changelog_file)]) == 1
    out, err = capsys.readouterr()
    assert out == ''
    assert '## [1.2.4] - YYYY-MM-DD' in err


# A CHANGELOG that cannot be read fails rather than raising.
def test_main_refuses_missing_changelog(tmp_path, capsys):
    assert check_release.main(['v1.2.3', '--changelog', str(tmp_path / 'absent.md')]) == 1
    assert capsys.readouterr().out == ''


# No argument and no $TAG is a usage error.
def test_main_without_tag(changelog_file, capsys, monkeypatch):
    monkeypatch.delenv('TAG', raising=False)
    assert check_release.main(['--changelog', str(changelog_file)]) == 2
    assert capsys.readouterr().out == ''


# Run as the workflow runs it, a separate interpreter with the tag in $TAG: the exit code and
# stdout are the script's whole interface.
def test_script_runs_standalone(changelog_file):
    result = subprocess.run(
        [sys.executable, str(SCRIPT), '--changelog', str(changelog_file)],
        env={**os.environ, 'TAG': 'v1.2.3'},
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == '1.2.3\n'
