# -------------------------------------------------------
# Database connection tests
#
# The settings database.py gives every SQLite connection, the sqlite3 driver's transaction
# control, the refusal of a URL for any other database, and the SQLite version each process names
# in its startup log.
# -------------------------------------------------------

import logging
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from youtube_manager import database, worker
# Imported here, not in the test: importing app installs the shared log handler, which would
# replace the one caplog adds for the test.
from youtube_manager.app import app

REPO_ROOT = Path(__file__).resolve().parent.parent

# A password that must never appear in an error message or a log line.
PASSWORD = 'not-a-real-password'

# Imported in the child; argv[1] is the repo root, put first on sys.path.
IMPORT_SCRIPT = 'import sys; sys.path.insert(0, sys.argv[1]); import youtube_manager.database'


# Every connection the engine opens runs in WAL with synchronous=NORMAL, an 8 MiB WAL size limit
# and a 15 s busy timeout. The settings are per connection, so a second one open at the same time
# has them too.
def test_connection_pragmas(db):
    with database.engine.connect() as first, database.engine.connect() as second:
        for conn in (first, second):
            assert conn.execute(text('PRAGMA journal_mode')).scalar() == 'wal'
            # 1 is NORMAL; SQLite's default is FULL (2).
            assert conn.execute(text('PRAGMA synchronous')).scalar() == 1
            assert conn.execute(text('PRAGMA journal_size_limit')).scalar() == 8 * 1024 * 1024
            assert conn.execute(text('PRAGMA busy_timeout')).scalar() == 15000


# A connection that cannot use WAL keeps SQLite's default synchronous setting, since NORMAL is
# only safe under WAL, and says so at WARN. Neither an in-memory database nor a file opened with
# nolock can use WAL; the file stays in the rollback journal mode, where NORMAL would be unsafe.
@pytest.mark.parametrize('url, journal_mode', [
    ('sqlite://', 'memory'),
    ('sqlite:///file:{tmp}/nolock.db?nolock=1&uri=true', 'delete'),
])
def test_synchronous_unchanged_without_wal(caplog, tmp_path, url, journal_mode):
    caplog.set_level(logging.WARNING)
    default = sqlite3.connect(':memory:')
    try:
        default_synchronous = default.execute('PRAGMA synchronous').fetchone()[0]
    finally:
        default.close()

    engine = database._create_engine(url.format(tmp=tmp_path.as_posix()))
    try:
        with engine.connect() as conn:
            assert conn.execute(text('PRAGMA journal_mode')).scalar() == journal_mode
            assert conn.execute(text('PRAGMA synchronous')).scalar() == default_synchronous
    finally:
        engine.dispose()

    assert default_synchronous != 1
    assert any(
        record.levelno == logging.WARNING and f'journal_mode={journal_mode}' in record.getMessage()
        for record in caplog.records
    )


# The engine hands the sqlite3 driver its legacy transaction control and its busy timeout
# explicitly, the timeout as a connect argument so that it already covers the WAL switch, and
# sets no pool pre-ping or recycling. On a connection, a read begins no transaction and a write
# begins one.
def test_legacy_transaction_control_explicit(monkeypatch, db):
    captured = {}
    real_create_engine = database.create_engine

    # create_engine as _create_engine calls it, recording the keyword arguments it was given.
    def recording_create_engine(url, **kwargs):
        captured.update(kwargs)
        return real_create_engine(url, **kwargs)

    monkeypatch.setattr(database, 'create_engine', recording_create_engine)
    database._create_engine('sqlite://').dispose()

    assert captured['connect_args']['autocommit'] == sqlite3.LEGACY_TRANSACTION_CONTROL
    assert captured['connect_args']['timeout'] == database.BUSY_TIMEOUT_SECONDS == 15
    assert 'pool_pre_ping' not in captured
    assert 'pool_recycle' not in captured

    with database.engine.connect() as conn:
        raw = conn.connection.dbapi_connection
        assert raw.autocommit == sqlite3.LEGACY_TRANSACTION_CONTROL
        conn.execute(text('SELECT count(*) FROM downloads'))
        assert not raw.in_transaction
        conn.execute(text('UPDATE downloads SET title = title'))
        assert raw.in_transaction
        conn.rollback()


# A URL for another database, for SQLite through another driver, or with a scheme SQLAlchemy does
# not know, is refused with a message that ends with its scheme and holds nothing else of the URL.
@pytest.mark.parametrize('url', [
    f'postgresql://ytm:{PASSWORD}@db.example/ytm',
    f'mysql+pymysql://ytm:{PASSWORD}@db.example/ytm',
    'sqlite+aiosqlite:///youtube.db',
    'sqlite3:///youtube.db',
    f'postgress://ytm:{PASSWORD}@db.example/ytm',
])
def test_non_sqlite_url_refused(url):
    with pytest.raises(ValueError) as refused:
        database.require_sqlite_url(url)

    message = str(refused.value)
    assert message.endswith(f"sqlite3 driver, not {url.split('://')[0]}")
    assert PASSWORD not in message
    assert 'db.example' not in message


# A string that does not parse as a URL is refused without being repeated in the message, and the
# parse error is left out of the traceback. A port that is not a number fails to parse, and in a
# mistyped URL the text read as the port can be the password.
@pytest.mark.parametrize('url', [
    f'mistyped {PASSWORD}',
    f'postgresql://ytm:{PASSWORD}/ytm',
    f'postgresql://ytm:p@ss:{PASSWORD}@db.example/ytm',
])
def test_unparsable_url_refused(url):
    with pytest.raises(ValueError) as refused:
        database.require_sqlite_url(url)

    assert str(refused.value).startswith('YOUTUBE_DB_URI is not a database URL')
    assert PASSWORD not in str(refused.value)
    assert refused.value.__suppress_context__


# SQLite file URLs, with or without the driver named, are accepted.
@pytest.mark.parametrize('url', [
    'sqlite:///youtube.db',
    'sqlite:////app/data/youtube.db',
    'sqlite+pysqlite:///youtube.db',
])
def test_sqlite_url_accepted(url):
    database.require_sqlite_url(url)


# Importing database.py with YOUTUBE_DB_URI set to another database's URL, or to one that does
# not parse, stops the process with the refusal, and neither its output nor its traceback carries
# the password.
@pytest.mark.parametrize('url', [
    f'postgresql://ytm:{PASSWORD}@db.example/ytm',
    f'postgresql://ytm:{PASSWORD}/ytm',
])
def test_non_sqlite_uri_rejected_at_import(tmp_path, url):
    env = dict(os.environ)
    env.update({
        'YOUTUBE_DB_URI': url,
        'PYTHONDONTWRITEBYTECODE': '1',
    })

    result = subprocess.run(
        [sys.executable, '-c', IMPORT_SCRIPT, str(REPO_ROOT)],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )

    assert result.returncode != 0
    assert 'ValueError: YOUTUBE_DB_URI' in result.stderr
    assert PASSWORD not in result.stdout + result.stderr


# The web UI's starting line names the SQLite library, and so does the worker's Components line,
# the first line it logs.
def test_sqlite_version_logged_at_startup(caplog, db):
    caplog.set_level(logging.INFO)
    with TestClient(app):
        pass

    assert f'web UI starting, SQLite {sqlite3.sqlite_version}' in caplog.text
    assert worker.components_line().endswith(f', sqlite {sqlite3.sqlite_version}')
