# -------------------------------------------------------
# Test harness
#
# Shared pytest setup: points every path the project reads from the environment at a session temp
# root, then provides per-test database, library, web-client and job-row fixtures. This file loads
# before pytest_configure runs, so project modules are imported inside fixtures, never at the top.
# -------------------------------------------------------

import os
import shutil
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest

# Temp directory the project's environment paths point into, assigned by pytest_configure.
SESSION_ROOT: Path | None = None

# The name the UI is served under, allowed through YTM_ALLOWED_HOSTS as in a deployment. The
# client fixture sends it as both Host and Origin, which is what a browser sends for a form on the
# UI's own pages.
APP_HOST = 'ytdl.home.arpa'
APP_ORIGIN = f'http://{APP_HOST}'


# SQLAlchemy URL for a SQLite file. as_posix(), so a Windows drive path parses as well.
def sqlite_url(path: Path) -> str:
    return f'sqlite:///{path.as_posix()}'


# Point YOUTUBE_DB_URI, DOWNLOAD_PATH, MEDIA_PATH, WORKER_HEARTBEAT_PATH and tempfile's directory
# into a session temp root, and allow APP_HOST through YTM_ALLOWED_HOSTS, before collection imports
# any project module: database.py builds its engine, media.py reads its paths and app.py reads its
# hosts at import, and organizer copies MEDIA_PATH, so none of them sees a later change.
@pytest.hookimpl(trylast=True)
def pytest_configure(config: pytest.Config) -> None:
    global SESSION_ROOT
    # trylast, so pytest's tmpdir plugin has already attached the factory tmp_path draws from.
    SESSION_ROOT = config._tmp_path_factory.mktemp('ytm-session')
    os.environ['YOUTUBE_DB_URI'] = sqlite_url(SESSION_ROOT / 'session.db')
    os.environ['DOWNLOAD_PATH'] = str(SESSION_ROOT / 'downloads')
    os.environ['MEDIA_PATH'] = str(SESSION_ROOT / 'media')
    os.environ['WORKER_HEARTBEAT_PATH'] = str(SESSION_ROOT / 'worker-heartbeat')
    os.environ['YTM_ALLOWED_HOSTS'] = APP_HOST
    # tempfile.gettempdir() places the worker's per-job final-path files; set after the factory
    # above has fixed its own base, so only later callers land here.
    tempdir = SESSION_ROOT / 'tmp'
    tempdir.mkdir()
    tempfile.tempdir = str(tempdir)


# Skip tests marked posix on Windows, which has neither os.killpg nor SIGKILL.
def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    if sys.platform != 'win32':
        return
    skip = pytest.mark.skip(reason='needs os.killpg and SIGKILL, which Windows lacks')
    for item in items:
        if item.get_closest_marker('posix'):
            item.add_marker(skip)


# Bind the shared engine and Session to a fresh SQLite file for this test, with no schema yet, and
# yield the file's path. Teardown rebinds to the session database, which also closes this file's
# pooled connections so Windows can delete it.
@pytest.fixture
def empty_db(tmp_path: Path):
    import database

    path = tmp_path / 'youtube.db'
    database.configure_database(sqlite_url(path))
    yield path
    database.configure_database(database.DATABASE_URL)


# The empty_db file after initialize_database(), as the app's startup leaves it. Returns the
# shared Session factory, now bound to that file.
@pytest.fixture
def db(empty_db: Path):
    import database

    database.initialize_database()
    return database.Session


# Recreate the staging and library directories empty for this test. Returns their paths as
# .staging and .media; both are the module-level paths media, organizer and worker read.
@pytest.fixture
def library() -> SimpleNamespace:
    import media

    for root in (media.DOWNLOAD_PATH, media.MEDIA_PATH):
        # Only ever empty a directory under the harness's own temp root.
        assert root.is_relative_to(SESSION_ROOT), f'{root} is outside the test session root'
        shutil.rmtree(root, ignore_errors=True)
        root.mkdir(parents=True)
    return SimpleNamespace(staging=media.DOWNLOAD_PATH, media=media.MEDIA_PATH)


# A TestClient for the app on the db fixture's database, entered with 'with' so the lifespan, and
# initialize_database() with it, runs. Every request carries APP_ORIGIN as its Host and Origin.
@pytest.fixture
def client(db):
    from fastapi.testclient import TestClient

    from app import app

    with TestClient(app, base_url=APP_ORIGIN, headers={'Origin': APP_ORIGIN}) as test_client:
        yield test_client


# Factory for Download rows on the db fixture's database: each call inserts and commits one row
# and returns it detached. Its columns stay readable; its relationships are not loaded.
@pytest.fixture
def make_job(db):
    from database import Download

    # Insert one Download built from a queued single-video request, overridden by `fields`.
    def make(**fields) -> Download:
        values = {
            'url': 'https://www.youtube.com/watch?v=dQw4w9WgXcQ',
            'request_type': 'video',
            'status': 'queued',
        }
        values.update(fields)
        with db() as session:
            job = Download(**values)
            session.add(job)
            session.commit()
        return job

    return make
