# -------------------------------------------------------
# Import side-effect test
#
# Imports every project module in a fresh interpreter with all configured paths in a temp tree,
# and checks that the import writes nothing at all: not into the repo (data/, youtube.db,
# bytecode), not anywhere in the temp tree, and not at the /downloads, /media or /tmp defaults.
# -------------------------------------------------------

import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# The paths each module falls back to when its variable is unset; on Windows they land on the
# current drive, as C:\downloads and so on.
ABSOLUTE_DEFAULTS = (Path('/downloads'), Path('/media'), Path('/tmp/worker-heartbeat'))

# Imported in the child; argv[1] is the repo root, put first on sys.path.
IMPORT_SCRIPT = (
    'import sys; sys.path.insert(0, sys.argv[1]); '
    'from youtube_manager import app, worker, organizer, database'
)


# Every file and directory under `root` mapped to its (size, mtime_ns), skipping the subtrees
# named in `skip` relative to it. Directories map to None, so only their presence is compared.
def tree_snapshot(root: Path, skip: tuple[str, ...] = ()) -> dict[str, tuple[int, int] | None]:
    snapshot = {}
    for current, dirs, files in os.walk(root):
        relative_dir = Path(current).relative_to(root)
        dirs[:] = [name for name in dirs if (relative_dir / name).as_posix() not in skip]
        for name in dirs:
            snapshot[(relative_dir / name).as_posix()] = None
        for name in files:
            stat = (Path(current) / name).stat()
            snapshot[(relative_dir / name).as_posix()] = (stat.st_size, stat.st_mtime_ns)
    return snapshot


# A child interpreter imports app, worker, organizer and database with cwd, HOME and every
# configured path in tmp_path. Afterwards the repo, tmp_path and the absolute defaults are exactly
# as before: data/ and home/ stay empty and staging/ is never created.
def test_imports_write_nothing_outside_temp_roots(tmp_path):
    cwd = tmp_path / 'cwd'
    home = tmp_path / 'home'
    data = tmp_path / 'data'
    for directory in (cwd, home, data):
        directory.mkdir()

    env = dict(os.environ)
    env.update({
        # as_posix(), as in conftest.sqlite_url, so a Windows drive path parses.
        'YOUTUBE_DB_URI': f'sqlite:///{(data / "youtube.db").as_posix()}',
        'DOWNLOAD_PATH': str(tmp_path / 'staging'),
        'MEDIA_PATH': str(tmp_path / 'media'),
        'WORKER_HEARTBEAT_PATH': str(data / 'worker-heartbeat'),
        'HOME': str(home),
        'USERPROFILE': str(home),
        'PYTHONDONTWRITEBYTECODE': '1',
    })

    # Checked one by one after the run, so a failure names the directory that changed.
    allowed = ('data', 'staging', 'home')
    repo_before = tree_snapshot(REPO_ROOT, skip=('.git',))
    tmp_before = tree_snapshot(tmp_path, skip=allowed)
    defaults_before = {path: path.exists() for path in ABSOLUTE_DEFAULTS}

    result = subprocess.run(
        [sys.executable, '-c', IMPORT_SCRIPT, str(REPO_ROOT)],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stderr

    assert tree_snapshot(REPO_ROOT, skip=('.git',)) == repo_before
    assert tree_snapshot(tmp_path, skip=allowed) == tmp_before
    assert not (tmp_path / 'staging').exists()
    assert not any(data.iterdir())
    assert not any(home.iterdir())
    assert {path: path.exists() for path in ABSOLUTE_DEFAULTS} == defaults_before
