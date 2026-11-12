# -------------------------------------------------------
# Image build tests
#
# Checks what the image build relies on, read from the files rather than a build: exact pins that
# yt-dlp accepts, base images pinned by digest, a wheel-only --no-deps install pip check verifies,
# tini with the web and worker commands, every call's yt-dlp arguments, the component versions the
# worker logs and the example's provider release. CI builds and smoke-tests the image itself.
# -------------------------------------------------------

import ast
import importlib.metadata
import json
import re
import shutil
import sqlite3
from pathlib import Path

# packaging comes with pytest.
from packaging.requirements import Requirement
from packaging.version import Version

from youtube_manager import worker

REPO_ROOT = Path(__file__).resolve().parent.parent

URL = 'https://www.youtube.com/watch?v=dQw4w9WgXcQ'

# The bgutil provider under its service name in compose.example.yaml.
POT_PROVIDER_URL = 'http://bgutil-provider:4416'

# Where the image puts deno.
DENO_PATH = '/usr/local/bin/deno'

# The processes entrypoint.sh runs for the image's `web` and `worker` commands.
WEB_COMMAND = 'uvicorn youtube_manager.app:app --host 0.0.0.0 --port 8080'
WORKER_COMMAND = 'python -m youtube_manager.worker'

# A requirements.txt line as uv writes it: a name, optional extras, one exact version and an
# optional environment marker. A range, a wildcard, a URL or a bare name does not match.
PINNED_REQUIREMENT = re.compile(
    r'[A-Za-z0-9][A-Za-z0-9._-]*(?:\[[A-Za-z0-9._,-]+\])?==[A-Za-z0-9.!+]+(?:\s*;\s*\S.*)?'
)

# The project name a requirement line starts with, before any extras or version.
REQUIREMENT_NAME = re.compile(r'[A-Za-z0-9][A-Za-z0-9._-]*')

# The marker environment of the image: CPython on Linux. The marker variables left out,
# python_version among them, take the values of the interpreter running the suite, 3.13 as in the
# image.
IMAGE_ENVIRONMENT = {
    'implementation_name': 'cpython',
    'sys_platform': 'linux',
    'platform_system': 'Linux',
}

# An image reference pinned to its content digest, with or without a tag.
DIGEST_PINNED_IMAGE = re.compile(r'[^@\s]+@sha256:[0-9a-f]{64}')

# A pipe into a shell, as in `curl ... | sh`; `||` is not a pipe.
SHELL_PIPE = re.compile(r'(?<!\|)\|(?!\|)\s*(?:\S*/)?(?:ba|da|z)?sh\b')

# The arguments of one pip install, up to the next &&, ; or |.
PIP_INSTALL = re.compile(r'\bpip3?\s+install\b([^&;|]*)')

# A pip install followed by pip check later in the same RUN.
INSTALL_THEN_CHECK = re.compile(r'\bpip3?\s+install\b.*&&\s*pip3?\s+check\b')

# One arm of a shell case statement: the pattern on its own line, then the body up to its ';;'.
CASE_ARM = re.compile(r'^\s*(\w+)\)\n(.*?)^\s*;;', re.MULTILINE | re.DOTALL)

# The tag of the bgutil provider server's image in compose.example.yaml.
PROVIDER_IMAGE = re.compile(
    r'^\s*image:\s*brainicism/bgutil-ytdlp-pot-provider:(\S+)\s*$', re.MULTILINE
)


# The text of a file at the repo root.
def repo_text(name: str) -> str:
    return (REPO_ROOT / name).read_text(encoding='utf-8')


# A project name in its PEP 503 normal form, so 'PyYAML' and 'pyyaml' compare equal.
def canonical(name: str) -> str:
    return re.sub(r'[-_.]+', '-', name).lower()


# The requirement lines of a requirements file, with comments, blank lines and uv's `# via`
# annotations dropped.
def requirement_lines(name: str) -> list[str]:
    lines = (line.split('#', 1)[0].strip() for line in repo_text(name).splitlines())
    return [line for line in lines if line]


# The normalized names requirements.txt pins.
def pinned_names() -> set[str]:
    lines = requirement_lines('requirements.txt')
    return {canonical(REQUIREMENT_NAME.match(line).group()) for line in lines}


# The version requirements.txt pins for each normalized name, from the lines whose marker holds in
# the image.
def pinned_versions() -> dict[str, str]:
    pins = {}
    for requirement in map(Requirement, requirement_lines('requirements.txt')):
        if requirement.marker is None or requirement.marker.evaluate(IMAGE_ENVIRONMENT):
            pins[canonical(requirement.name)] = next(iter(requirement.specifier)).version
    return pins


# The Dockerfile's instructions as (KEYWORD, arguments) pairs in order, with continuation lines
# joined and comment lines dropped, as the builder reads them.
def dockerfile_instructions() -> list[tuple[str, str]]:
    instructions, pending = [], ''
    for line in repo_text('Dockerfile').splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith('#'):
            continue
        if stripped.endswith('\\'):
            pending += stripped[:-1] + ' '
            continue
        keyword, _, arguments = (pending + stripped).partition(' ')
        instructions.append((keyword.upper(), arguments.strip()))
        pending = ''
    return instructions


# The arguments of every `keyword` instruction in the Dockerfile, in order.
def dockerfile_arguments(keyword: str) -> list[str]:
    return [arguments for found, arguments in dockerfile_instructions() if found == keyword]


# Whether a list display in worker.py has a `*ytdlp_common_args()` element.
def includes_common_args(command: ast.List) -> bool:
    return any(
        isinstance(element, ast.Starred)
        and isinstance(element.value, ast.Call)
        and isinstance(element.value.func, ast.Name)
        and element.value.func.id == 'ytdlp_common_args'
        for element in command.elts
    )


# Every requirements.txt entry is an exact pin, so each build installs the same packages; a
# marker only decides which platforms a pin applies to.
def test_requirements_txt_pins_every_package_exactly():
    lines = requirement_lines('requirements.txt')
    assert lines
    assert [line for line in lines if not PINNED_REQUIREMENT.fullmatch(line)] == []


# Every direct dependency in requirements.in is pinned in requirements.txt. The image installs
# requirements.txt alone, so a dependency missing there would be missing from the image.
def test_requirements_in_names_are_pinned():
    lines = requirement_lines('requirements.in')
    direct = {canonical(REQUIREMENT_NAME.match(line).group()) for line in lines}
    assert direct
    assert direct - pinned_names() == set()


# Every requirement of yt-dlp's default extra that holds in the image is pinned to a version yt-dlp
# accepts. pip check skips extras, and yt-dlp refuses a yt-dlp-ejs solver built for another
# release, so a stale solver pin would otherwise pass the build.
def test_ytdlp_default_extra_is_pinned_to_accepted_versions():
    pins = pinned_versions()
    # The suite runs on requirements.txt, so the metadata read here is the pinned release's.
    assert Version(importlib.metadata.version('yt-dlp')) == Version(pins['yt-dlp'])

    environment = {**IMAGE_ENVIRONMENT, 'extra': 'default'}
    required = [
        requirement
        for requirement in map(Requirement, importlib.metadata.requires('yt-dlp'))
        if requirement.marker is None or requirement.marker.evaluate(environment)
    ]
    names = {canonical(requirement.name) for requirement in required}
    assert 'yt-dlp-ejs' in names
    assert names - pins.keys() == set()
    assert [
        str(requirement)
        for requirement in required
        if not requirement.specifier.contains(pins[canonical(requirement.name)], prereleases=True)
    ] == []


# compose.example.yaml runs the provider server at the release of the bgutil plugin the image pins,
# so a plugin update fails here until it moves the server too. The plugin refuses a server of
# another major release and warns about any other difference.
def test_example_provider_matches_the_plugin():
    tags = PROVIDER_IMAGE.findall(repo_text('compose.example.yaml'))
    assert tags == [pinned_versions()['bgutil-ytdlp-pot-provider']]


# Every FROM names its image by content digest, so a rebuild cannot pick up a different image
# pushed under the same tag.
def test_dockerfile_images_are_pinned_by_digest():
    images = [
        next(token for token in arguments.split() if not token.startswith('--'))
        for arguments in dockerfile_arguments('FROM')
    ]
    assert images
    assert [image for image in images if not DIGEST_PINNED_IMAGE.fullmatch(image)] == []


# Nothing is piped from the network into a shell, and the one pip install takes requirements.txt
# as wheels only, with --no-deps, followed by pip check: a pin set missing a dependency then fails
# the build instead of pulling in an unpinned package, and no sdist builds with unpinned tools.
def test_dockerfile_installs_only_the_pins():
    runs = dockerfile_arguments('RUN')
    assert [run for run in runs if SHELL_PIPE.search(run)] == []
    installs = [arguments.split() for run in runs for arguments in PIP_INSTALL.findall(run)]
    assert installs == [['--no-deps', '--only-binary=:all:', '-r', 'requirements.txt']]
    assert any(INSTALL_THEN_CHECK.search(run) for run in runs)


# tini is PID 1 for every command, reaping the orphaned yt-dlp and ffmpeg processes, and the web
# UI runs when the host names no command. Both are exec form, which json.loads requires; the last
# of each is the one the image keeps.
def test_image_runs_under_tini_and_defaults_to_web():
    assert json.loads(dockerfile_arguments('ENTRYPOINT')[-1]) == [
        '/usr/bin/tini', '-s', '--', './entrypoint.sh',
    ]
    assert json.loads(dockerfile_arguments('CMD')[-1]) == ['web']


# entrypoint.sh maps the image's two commands, web and worker, to their processes with any extra
# arguments passed on, then execs the result as PUID/PGID, so any other command runs as given.
def test_entrypoint_maps_web_and_worker():
    script = repo_text('entrypoint.sh')
    arms = dict(CASE_ARM.findall(script))
    assert f'set -- {WEB_COMMAND} "$@"' in arms.get('web', '')
    assert f'set -- {WORKER_COMMAND} "$@"' in arms.get('worker', '')
    assert script.rstrip().endswith('exec gosu "$PUID:$PGID" "$@"')


# ytdlp_common_args() starts with --ignore-config, so no config file in the image, staging or a
# home directory changes a call; the PO-token extractor args follow only when a provider is set.
def test_ytdlp_common_args_start_with_ignore_config(monkeypatch):
    monkeypatch.setattr(worker, 'POT_PROVIDER_URL', '')
    assert worker.ytdlp_common_args() == ['--ignore-config']

    monkeypatch.setattr(worker, 'POT_PROVIDER_URL', POT_PROVIDER_URL)
    assert worker.ytdlp_common_args() == [
        '--ignore-config',
        '--extractor-args',
        f'youtubepot-bgutilhttp:base_url={POT_PROVIDER_URL}',
    ]


# Every yt-dlp argv in worker.py, a call added later included, has *ytdlp_common_args() in it, so
# --ignore-config and the PO-token args reach every call.
def test_every_ytdlp_command_includes_the_common_args():
    tree = ast.parse(repo_text('youtube_manager/worker.py'))
    commands = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.List)
        and node.elts
        and isinstance(node.elts[0], ast.Constant)
        and node.elts[0].value == 'yt-dlp'
    ]
    assert commands
    assert [command.lineno for command in commands if not includes_common_args(command)] == []


# The download fetches no challenge solver at run time, with or without a provider: deno runs
# the yt-dlp-ejs that requirements.txt pins next to yt-dlp.
def test_download_command_fetches_no_remote_components(monkeypatch):
    assert 'yt-dlp-ejs' in pinned_names()
    for provider in ('', POT_PROVIDER_URL):
        monkeypatch.setattr(worker, 'POT_PROVIDER_URL', provider)
        command = worker.build_download_command(URL, worker.OUTPUT_TEMPLATE_FLAT, 1)
        assert command[:2] == ['yt-dlp', '--ignore-config']
        assert [arg for arg in command if arg.startswith('--remote-components')] == []


# component_versions() gives each package's installed version, then deno's path, then the SQLite
# library's version, and the startup line lists them after the release, in the form the image
# smoke test looks for.
def test_component_versions_reports_each_component(monkeypatch):
    installed = {
        'yt-dlp': '2000.01.01',
        'yt-dlp-ejs': '0.1.0',
        'bgutil-ytdlp-pot-provider': '1.0.0',
    }
    monkeypatch.setattr(importlib.metadata, 'version', lambda package: installed[package])
    monkeypatch.setattr(shutil, 'which', lambda name: DENO_PATH if name == 'deno' else None)
    monkeypatch.setattr(sqlite3, 'sqlite_version', '3.99.0')
    monkeypatch.setattr(worker, 'VERSION', '1.2.3')

    assert worker.component_versions() == {**installed, 'deno': DENO_PATH, 'sqlite': '3.99.0'}
    assert worker.components_line() == (
        'Components: youtube-manager 1.2.3, yt-dlp 2000.01.01, yt-dlp-ejs 0.1.0, '
        f'bgutil-ytdlp-pot-provider 1.0.0, deno {DENO_PATH}, sqlite 3.99.0'
    )


# A package with no metadata reads 'not installed', one whose version cannot be read 'unknown',
# and a deno missing from PATH 'not found'; none of them raises.
def test_component_versions_marks_missing_components(monkeypatch):
    # importlib.metadata.version() with yt-dlp-ejs absent and no Version in the plugin's metadata.
    def version(package: str) -> str:
        if package == 'yt-dlp-ejs':
            raise importlib.metadata.PackageNotFoundError(package)
        if package == 'bgutil-ytdlp-pot-provider':
            raise KeyError('Version')
        return '2000.01.01'

    monkeypatch.setattr(importlib.metadata, 'version', version)
    monkeypatch.setattr(shutil, 'which', lambda name: None)

    assert worker.component_versions() == {
        'yt-dlp': '2000.01.01',
        'yt-dlp-ejs': 'not installed',
        'bgutil-ytdlp-pot-provider': 'unknown',
        'deno': 'not found',
        'sqlite': sqlite3.sqlite_version,
    }
