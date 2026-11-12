# -------------------------------------------------------
# Schema version and backup tests
#
# The PRAGMA user_version ladder initialize_database() runs, the backup it takes before migrating,
# how the app and the worker treat a newer schema, backup_database() itself, and the worker's
# daily snapshot and the setting that times it.
# -------------------------------------------------------

import ast
import logging
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import event, text

from youtube_manager import database, worker

REPO_ROOT = Path(__file__).resolve().parent.parent

# Each frozen schema, v<version>.sql: what a release at that version creates.
SCHEMAS_DIR = REPO_ROOT / 'tests' / 'schemas'

VIDEO_ID = 'dQw4w9WgXcQ'

# A schema from before versions: two of today's tables with fewer columns, downloads.youtube_id
# declared UNIQUE on the column, and no playlists or subscriptions table.
LEGACY_SCHEMA = (
    'CREATE TABLE channels ('
    ' id INTEGER PRIMARY KEY, youtube_id VARCHAR UNIQUE, name VARCHAR NOT NULL,'
    ' url VARCHAR UNIQUE NOT NULL)',
    'CREATE TABLE downloads ('
    ' id INTEGER PRIMARY KEY, url VARCHAR NOT NULL, request_type VARCHAR NOT NULL,'
    ' youtube_id VARCHAR UNIQUE, parent_id INTEGER, title VARCHAR, status VARCHAR NOT NULL)',
)
LEGACY_CHANNELS = [
    (1, 'UC0000000000000000000001', 'First channel', 'https://www.youtube.com/@first'),
    (2, 'UC0000000000000000000002', 'Second channel', 'https://www.youtube.com/@second'),
]
LEGACY_DOWNLOADS = [
    (1, f'https://youtu.be/{VIDEO_ID}', 'video', VIDEO_ID, None, 'First', 'completed'),
    (2, 'https://www.youtube.com/playlist?list=PL1', 'playlist', None, None, 'Playlist', 'queued'),
    (3, 'https://youtu.be/aaaaaaaaaaa', 'video', 'aaaaaaaaaaa', 2, 'Child', 'failed'),
]


# Open the database file at `path` on its own sqlite3 connection, outside the app's engine.
def open_file(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(str(path))


# PRAGMA user_version of the file at `path`.
def stored_version(path: Path) -> int:
    connection = open_file(path)
    try:
        return connection.execute('PRAGMA user_version').fetchone()[0]
    finally:
        connection.close()


# Set PRAGMA user_version of the file at `path` to `version`.
def set_stored_version(path: Path, version: int) -> None:
    connection = open_file(path)
    try:
        connection.execute(f'PRAGMA user_version = {version}')
        connection.commit()
    finally:
        connection.close()


# Write LEGACY_SCHEMA and its rows into the empty file at `path`, at user_version 0.
def build_legacy_database(path: Path) -> None:
    connection = open_file(path)
    try:
        for statement in LEGACY_SCHEMA:
            connection.execute(statement)
        connection.executemany('INSERT INTO channels VALUES (?, ?, ?, ?)', LEGACY_CHANNELS)
        connection.executemany(
            'INSERT INTO downloads VALUES (?, ?, ?, ?, ?, ?, ?)', LEGACY_DOWNLOADS,
        )
        connection.commit()
    finally:
        connection.close()


# The table names in the database, without SQLite's own.
def table_names() -> set[str]:
    with database.engine.connect() as conn:
        rows = conn.execute(text(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        ))
        return {row[0] for row in rows}


# The column names of `table` in the database.
def column_names(table: str) -> set[str]:
    with database.engine.connect() as conn:
        return {row[1] for row in conn.execute(text(f'PRAGMA table_info({table})'))}


# The index names of `table` in the database, without SQLite's own.
def index_names(table: str) -> set[str]:
    with database.engine.connect() as conn:
        rows = conn.execute(text(f"PRAGMA index_list('{table}')"))
        return {row[1] for row in rows if not row[1].startswith('sqlite_')}


# The ORM's column names for `table`.
def orm_columns(table: str) -> set[str]:
    return {column.name for column in database.Base.metadata.tables[table].columns}


# The files in the database's backups/ directory, by name.
def backup_files() -> list[str]:
    directory = database.backup_directory()
    return sorted(path.name for path in directory.iterdir()) if directory.is_dir() else []


# The copies with `tag` in the database's backups/ directory, oldest first.
def copies(tag: str) -> list[Path]:
    return database.list_backups(database.backup_directory(), tag)


# Messages logged at `level`.
def messages_at(caplog: pytest.LogCaptureFixture, level: int) -> list[str]:
    return [record.getMessage() for record in caplog.records if record.levelno == level]


# An aware UTC time on `day` days after 2026-03-01, at `hour`:`minute`.
def utc(day: int, hour: int, minute: int) -> datetime:
    return datetime(2026, 3, 1, hour, minute, tzinfo=timezone.utc) + timedelta(days=day)


# A migration step that creates the table `name`.
def create_table_step(name: str):
    # The step, run inside the ladder's transaction on `conn`.
    def step(conn) -> None:
        conn.exec_driver_sql(f'CREATE TABLE {name} (id INTEGER PRIMARY KEY)')
    return step


# A migration step that fails.
def failing_step(conn) -> None:
    raise RuntimeError('step failed')


# A file with no tables gets every ORM table and SCHEMA_VERSION in one go, and no backup, since
# there is nothing to copy.
def test_fresh_database_gets_current_user_version(empty_db):
    database.initialize_database()

    assert stored_version(empty_db) == database.SCHEMA_VERSION == 1
    assert set(database.Base.metadata.tables) <= table_names()
    assert database.is_schema_initialized()
    assert backup_files() == []


# A database from before versions comes out at SCHEMA_VERSION with every ORM table and column,
# every row and value it had, the defaults on the added NOT NULL columns and downloads.youtube_id
# no longer unique. The log names the copy taken first, with each table's row count, and the row
# counts after the migration.
def test_legacy_v0_database_migrates_and_preserves_rows(empty_db, caplog):
    caplog.set_level(logging.INFO)
    build_legacy_database(empty_db)

    database.initialize_database()

    assert stored_version(empty_db) == 1
    assert 'downloads_legacy' not in table_names()
    for table in database.Base.metadata.tables:
        assert column_names(table) == orm_columns(table), table
    with database.engine.connect() as conn:
        channels = conn.execute(text('SELECT id, youtube_id, name, url FROM channels ORDER BY id'))
        assert [tuple(row) for row in channels] == LEGACY_CHANNELS
        downloads = conn.execute(text(
            'SELECT id, url, request_type, youtube_id, parent_id, title, status,'
            ' cancel_requested, group_by_channel FROM downloads ORDER BY id'
        )).all()
    assert [tuple(row[:7]) for row in downloads] == LEGACY_DOWNLOADS
    assert [tuple(row[7:]) for row in downloads] == [(0, 0)] * len(LEGACY_DOWNLOADS)

    with database.Session() as session:
        session.add(database.Download(url=f'https://youtu.be/{VIDEO_ID}', youtube_id=VIDEO_ID))
        session.commit()

    [copy] = copies('pre-v1-from-v0')
    infos = messages_at(caplog, logging.INFO)
    assert (
        f'Backed up the database to {copy.name} before migrating it from schema version 0 to 1'
        ' (channels 2, downloads 3)'
    ) in infos
    assert (
        'Migrated the database to schema version 1'
        ' (channels 2, downloads 3, playlists 0, subscriptions 0)'
    ) in infos


# The copy is written before the first step runs, holds the database as it was, at version 0
# with its rows, and is a complete file under its final name.
def test_pre_migration_backup_written_before_first_step(empty_db, monkeypatch):
    build_legacy_database(empty_db)
    seen = []

    # Migration 1 as the ladder runs it, first recording what backups/ holds at that moment.
    def recording_step(conn):
        found = copies('pre-v1-from-v0')
        seen.append([path.name for path in found])
        copy = open_file(found[0])
        try:
            seen.append(copy.execute('PRAGMA user_version').fetchone()[0])
            seen.append(copy.execute('SELECT count(*) FROM downloads').fetchone()[0])
        finally:
            copy.close()
        database.migrate_to_1(conn)

    monkeypatch.setattr(database, 'MIGRATIONS', {1: recording_step})

    database.initialize_database()

    names, version, rows = seen
    assert len(names) == 1 and names[0].startswith('youtube-pre-v1-from-v0-')
    assert (version, rows) == (0, len(LEGACY_DOWNLOADS))
    assert stored_version(empty_db) == 1
    assert not [name for name in backup_files() if '.tmp' in name]


# A step that fails takes its DDL, its rows and its version with it: the file stays at the last
# version it completed, an ERROR says so, and the next start runs the step again.
def test_failed_step_rolls_back_ddl_and_version(db, monkeypatch, caplog):
    path = database.database_file()

    # A step that adds a column, a table and a row, then fails.
    def adds_then_fails(conn):
        conn.exec_driver_sql('ALTER TABLE downloads ADD COLUMN probe TEXT')
        conn.exec_driver_sql('CREATE TABLE probe_table (id INTEGER PRIMARY KEY)')
        conn.exec_driver_sql(
            'INSERT INTO downloads (url, request_type, status, cancel_requested, group_by_channel)'
            " VALUES ('https://youtu.be/ccccccccccc', 'video', 'queued', 0, 0)"
        )
        raise RuntimeError('step failed')

    monkeypatch.setattr(database, 'SCHEMA_VERSION', 2)
    monkeypatch.setattr(database, 'MIGRATIONS', {1: database.migrate_to_1, 2: adds_then_fails})

    with pytest.raises(RuntimeError, match='step failed'):
        database.initialize_database()

    assert stored_version(path) == 1
    assert 'probe' not in column_names('downloads')
    assert 'probe_table' not in table_names()
    with database.engine.connect() as conn:
        assert conn.execute(text('SELECT count(*) FROM downloads')).scalar() == 0
    assert any(
        'to schema version 2 failed and was rolled back; it stays at version 1' in message
        for message in messages_at(caplog, logging.ERROR)
    )
    assert len(copies('pre-v2-from-v1')) == 1

    monkeypatch.setitem(database.MIGRATIONS, 2, create_table_step('step_2'))
    database.initialize_database()

    assert stored_version(path) == 2
    assert 'step_2' in table_names()


# The version is written inside the step's own transaction: when writing it fails, the step's
# DDL goes too, and the next start runs the step again.
def test_version_written_in_the_steps_transaction(db, monkeypatch):
    path = database.database_file()
    monkeypatch.setattr(database, 'SCHEMA_VERSION', 2)
    monkeypatch.setattr(
        database, 'MIGRATIONS', {1: database.migrate_to_1, 2: create_table_step('step_2')},
    )

    # A cursor hook that fails every write of the version, as a crash at that moment would.
    def refuse_version_write(conn, cursor, statement, parameters, context, executemany):
        if statement.startswith('PRAGMA user_version ='):
            raise RuntimeError('version write failed')

    event.listen(database.engine, 'before_cursor_execute', refuse_version_write)
    try:
        with pytest.raises(RuntimeError, match='version write failed'):
            database.initialize_database()
    finally:
        event.remove(database.engine, 'before_cursor_execute', refuse_version_write)

    assert stored_version(path) == 1
    assert 'step_2' not in table_names()

    database.initialize_database()

    assert stored_version(path) == 2
    assert 'step_2' in table_names()


# Each step commits on its own: when a later step fails, the steps before it stay, and the file
# is left at the version the last of them reached.
def test_each_step_commits_on_its_own(db, monkeypatch):
    path = database.database_file()
    monkeypatch.setattr(database, 'SCHEMA_VERSION', 3)
    monkeypatch.setattr(database, 'MIGRATIONS', {
        1: database.migrate_to_1, 2: create_table_step('step_2'), 3: failing_step,
    })

    with pytest.raises(RuntimeError, match='step failed'):
        database.initialize_database()

    assert stored_version(path) == 2
    assert 'step_2' in table_names()


# Steps run in order from the stored version, each committed with the version it reaches, and a
# step at or below the stored version never runs.
def test_steps_run_in_order_from_stored_version(db, monkeypatch):
    ran = []

    # A step that records which version it was run for and what the file said at that moment.
    def step(target):
        # The step for `target`.
        def run(conn):
            ran.append((target, database.read_schema_version(conn)))
            conn.exec_driver_sql(f'CREATE TABLE step_{target} (id INTEGER PRIMARY KEY)')
        return run

    monkeypatch.setattr(database, 'SCHEMA_VERSION', 3)
    monkeypatch.setattr(database, 'MIGRATIONS', {1: step(1), 2: step(2), 3: step(3)})

    database.initialize_database()

    assert ran == [(2, 1), (3, 2)]
    assert {'step_2', 'step_3'} <= table_names()
    assert 'step_1' not in table_names()
    assert stored_version(database.database_file()) == 3


# The last step in MIGRATIONS is SCHEMA_VERSION's, and SCHEMA_VERSION has a frozen schema. A ladder
# that stops short of SCHEMA_VERSION is an ERROR and stops the start, rather than a success with
# the worker left waiting.
def test_ladder_short_of_schema_version_refused(db, monkeypatch, caplog):
    assert max(database.MIGRATIONS) == database.SCHEMA_VERSION
    assert (SCHEMAS_DIR / f'v{database.SCHEMA_VERSION}.sql').is_file()

    monkeypatch.setattr(database, 'SCHEMA_VERSION', 2)
    monkeypatch.setattr(database, 'MIGRATIONS', {1: database.migrate_to_1})

    with pytest.raises(RuntimeError, match='MIGRATIONS has no step'):
        database.initialize_database()

    assert stored_version(database.database_file()) == 1
    assert any(
        'stop at schema version 1, short of' in message
        for message in messages_at(caplog, logging.ERROR)
    )


# Each table of the SQLite file at `path`, by name, with its columns (name, type, NOT NULL,
# default, primary key), its indexes (unique, columns, and the name of one made by CREATE INDEX)
# and its foreign keys: everything a schema change can alter.
def schema_signature(path: Path) -> dict:
    connection = open_file(path)
    try:
        tables = [row[0] for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        )]
        signature = {}
        for table in tables:
            columns = sorted(
                tuple(row[1:]) for row in connection.execute(f'PRAGMA table_info({table})')
            )
            indexes = sorted(
                (
                    bool(row[2]),
                    tuple(info[2] for info in connection.execute(f'PRAGMA index_info("{row[1]}")')),
                    row[1] if row[3] == 'c' else '',
                )
                for row in connection.execute(f"PRAGMA index_list('{table}')")
            )
            keys = sorted(
                tuple(row[2:5]) for row in connection.execute(f'PRAGMA foreign_key_list({table})')
            )
            signature[table] = (columns, indexes, keys)
        return signature
    finally:
        connection.close()


# The frozen schema of SCHEMA_VERSION is exactly what this release creates in a new file: column
# types, NOT NULL, defaults, keys, indexes and their uniqueness included. Changing any of them
# therefore needs a new version, its step and its frozen file.
def test_current_frozen_schema_matches_a_fresh_file(empty_db, tmp_path):
    frozen = tmp_path / 'frozen.db'
    connection = open_file(frozen)
    try:
        connection.executescript(
            (SCHEMAS_DIR / f'v{database.SCHEMA_VERSION}.sql').read_text(encoding='utf-8')
        )
    finally:
        connection.close()

    database.initialize_database()

    assert schema_signature(empty_db) == schema_signature(frozen)


# A file at each frozen schema version comes out with every table, column and index the ORM
# declares, so an ORM table, column or index shipped without its migration step fails here.
@pytest.mark.parametrize('schema', sorted(SCHEMAS_DIR.glob('v*.sql')), ids=lambda path: path.stem)
def test_frozen_schemas_migrate_to_the_orm(empty_db, schema):
    connection = open_file(empty_db)
    try:
        connection.executescript(schema.read_text(encoding='utf-8'))
        connection.execute(f'PRAGMA user_version = {int(schema.stem[1:])}')
        connection.commit()
    finally:
        connection.close()

    database.initialize_database()

    assert stored_version(empty_db) == database.SCHEMA_VERSION
    for name, table in database.Base.metadata.tables.items():
        assert column_names(name) == orm_columns(name), name
        assert {index.name for index in table.indexes} <= index_names(name), name


# A second start on a migrated file changes nothing and takes no second copy.
def test_rerun_is_noop_without_new_backup(empty_db):
    build_legacy_database(empty_db)
    database.initialize_database()
    taken = backup_files()
    with database.engine.connect() as conn:
        before = conn.execute(text('SELECT type, name, sql FROM sqlite_master ORDER BY name')).all()

    database.initialize_database()

    assert backup_files() == taken and len(taken) == 1
    with database.engine.connect() as conn:
        after = conn.execute(text('SELECT type, name, sql FROM sqlite_master ORDER BY name')).all()
    assert after == before
    assert stored_version(empty_db) == 1


# A file at version 0 with any table at all is a database from before versions, not a new file:
# it is copied first, then migration 1 gives it every ORM table and column.
def test_file_with_any_table_is_migrated_not_created(empty_db):
    connection = open_file(empty_db)
    try:
        connection.execute(LEGACY_SCHEMA[0])
        connection.executemany('INSERT INTO channels VALUES (?, ?, ?, ?)', LEGACY_CHANNELS)
        connection.commit()
    finally:
        connection.close()

    database.initialize_database()

    assert stored_version(empty_db) == 1
    assert len(copies('pre-v1-from-v0')) == 1
    assert column_names('channels') == orm_columns('channels')


# A step waits for a write lock another connection holds, as BEGIN IMMEDIATE does, instead of
# failing halfway on it.
def test_migration_waits_for_another_writer(empty_db, monkeypatch):
    build_legacy_database(empty_db)
    writer = sqlite3.connect(str(empty_db), check_same_thread=False, isolation_level=None)
    release = threading.Timer(0.5, lambda: writer.execute('COMMIT'))
    real_backup = database.backup_database

    # The pre-migration copy, after which another connection holds the write lock for 0.5 s.
    def backup_then_lock(tag, keep, now=None):
        path = real_backup(tag, keep, now)
        writer.execute('BEGIN IMMEDIATE')
        writer.execute(
            "INSERT INTO channels (id, name, url)"
            " VALUES (3, 'Third channel', 'https://www.youtube.com/@third')"
        )
        release.start()
        return path

    monkeypatch.setattr(database, 'backup_database', backup_then_lock)
    try:
        database.initialize_database()
    finally:
        release.join()
        writer.close()

    assert stored_version(empty_db) == 1
    with database.engine.connect() as conn:
        assert conn.execute(text('SELECT count(*) FROM channels')).scalar() == 3


# When SQLite has already rolled a failed step back by itself, as it does after a full disk, the
# step's own error is what the start fails with, and the version stays.
def test_step_error_kept_when_sqlite_rolled_back_first(db, monkeypatch):
    # A step that SQLite rolls back before it reports its error.
    def rolled_back_then_fails(conn):
        conn.exec_driver_sql('ROLLBACK')
        raise RuntimeError('disk full')

    monkeypatch.setattr(database, 'SCHEMA_VERSION', 2)
    monkeypatch.setattr(
        database, 'MIGRATIONS', {1: database.migrate_to_1, 2: rolled_back_then_fails},
    )

    with pytest.raises(RuntimeError, match='disk full'):
        database.initialize_database()

    assert stored_version(database.database_file()) == 1


# A version another process raised while this one was copying the file is never written back
# down: the step is skipped, and the newer version is reported like any newer schema.
def test_version_raised_meanwhile_is_not_lowered(empty_db, monkeypatch, caplog):
    build_legacy_database(empty_db)
    real_backup = database.backup_database
    ran = []

    # The pre-migration copy, after which another process migrates the file further.
    def backup_then_raise_version(tag, keep, now=None):
        path = real_backup(tag, keep, now)
        set_stored_version(empty_db, database.SCHEMA_VERSION + 1)
        return path

    monkeypatch.setattr(database, 'backup_database', backup_then_raise_version)
    monkeypatch.setattr(database, 'MIGRATIONS', {1: lambda conn: ran.append(conn)})

    database.initialize_database()

    assert ran == []
    assert stored_version(empty_db) == database.SCHEMA_VERSION + 1
    assert any('only adds to it' in message for message in messages_at(caplog, logging.WARNING))


# Copies are kept per pair of versions: a start that keeps failing partway through a ladder copies
# the partly migrated file each time and keeps the newest PRE_MIGRATION_BACKUPS_KEPT of those, but
# never pushes out the copy of the file as it was before the upgrade.
def test_pre_migration_copies_kept_per_source_version(empty_db, monkeypatch):
    build_legacy_database(empty_db)
    clock = [utc(0, 0, 0)]

    # The current time, a minute later at each call, so that every copy gets its own name.
    def ticking_utcnow():
        clock[0] += timedelta(minutes=1)
        return clock[0]

    monkeypatch.setattr(database, 'utcnow', ticking_utcnow)
    monkeypatch.setattr(database, 'SCHEMA_VERSION', 3)
    monkeypatch.setattr(database, 'MIGRATIONS', {
        1: database.migrate_to_1, 2: create_table_step('step_2'), 3: failing_step,
    })

    for _ in range(5):
        with pytest.raises(RuntimeError, match='step failed'):
            database.initialize_database()

    [original] = copies('pre-v3-from-v0')
    partial = copies('pre-v3-from-v2')
    assert stored_version(original) == 0
    assert len(partial) == database.PRE_MIGRATION_BACKUPS_KEPT == 3
    assert [stored_version(path) for path in partial] == [2, 2, 2]
    assert stored_version(empty_db) == 2


# The worker waits while the stored version is older than SCHEMA_VERSION, even on a file with
# every table and column that a release from before versions created, and goes on once the app
# has migrated it.
def test_worker_waits_for_user_version(db, monkeypatch):
    path = database.database_file()
    set_stored_version(path, 0)
    assert not database.is_schema_initialized()
    assert database.wait_for_schema(timeout_seconds=0) is False
    sleeps = []

    # The worker's one-second wait, during which the app migrates the file.
    def app_migrates_meanwhile(seconds):
        sleeps.append(seconds)
        database.initialize_database()

    monkeypatch.setattr(database.time, 'sleep', app_migrates_meanwhile)

    assert database.wait_for_schema(timeout_seconds=30) is True
    assert sleeps == [1]
    assert stored_version(path) == 1


# The worker gives the app five minutes, which a backup and a migration on a slow disk can take:
# SCHEMA_WAIT_SECONDS is 300, and the worker's entry point passes it to wait_for_schema.
def test_worker_schema_wait_is_300_seconds():
    assert worker.SCHEMA_WAIT_SECONDS == 300

    tree = ast.parse((REPO_ROOT / 'youtube_manager' / 'worker.py').read_text(encoding='utf-8'))
    main_blocks = [
        node for node in tree.body
        if isinstance(node, ast.If) and ast.unparse(node.test) == "__name__ == '__main__'"
    ]
    calls = [
        node for block in main_blocks for node in ast.walk(block)
        if isinstance(node, ast.Call) and ast.unparse(node.func) == 'wait_for_schema'
    ]
    assert [ast.unparse(call) for call in calls] == [
        'wait_for_schema(timeout_seconds=SCHEMA_WAIT_SECONDS)'
    ]


# A schema newer than this release's in the same hundred is used as it is: the app neither
# migrates nor copies it, the worker goes on, and both say so at WARN.
def test_newer_compatible_schema_used_as_is(db, caplog):
    path = database.database_file()
    set_stored_version(path, database.SCHEMA_VERSION + 1)

    database.initialize_database()

    assert stored_version(path) == database.SCHEMA_VERSION + 1
    assert backup_files() == []
    assert database.is_schema_initialized()
    assert database.wait_for_schema(timeout_seconds=0) is True
    warnings = messages_at(caplog, logging.WARNING)
    assert len([message for message in warnings if 'only adds to it' in message]) == 2
    assert messages_at(caplog, logging.ERROR) == []


# A schema from a later hundred is refused: the app raises SchemaTooNewError without touching the
# file, the worker raises it at once instead of waiting, and each says so at ERROR.
def test_newer_incompatible_schema_refused(db, monkeypatch, caplog):
    path = database.database_file()
    later = (database.SCHEMA_VERSION // 100 + 1) * 100 + 1
    set_stored_version(path, later)
    monkeypatch.setattr(database.time, 'sleep', lambda seconds: pytest.fail('the worker waited'))

    with pytest.raises(database.SchemaTooNewError):
        database.initialize_database()
    with pytest.raises(database.SchemaTooNewError):
        database.wait_for_schema(timeout_seconds=300)

    assert stored_version(path) == later
    assert backup_files() == []
    assert not database.is_schema_initialized()
    errors = messages_at(caplog, logging.ERROR)
    assert len([message for message in errors if 'from a later major release' in message]) == 2


# A copy that cannot be finished leaves no partial file, and the migration does not run: the file
# stays at version 0 and an ERROR says nothing was migrated.
def test_failed_backup_blocks_migration(empty_db, monkeypatch, caplog):
    build_legacy_database(empty_db)

    # os.replace as on a full disk.
    def replace_fails(source, target):
        raise OSError(28, 'No space left on device')

    monkeypatch.setattr(database.os, 'replace', replace_fails)

    with pytest.raises(OSError):
        database.initialize_database()

    assert stored_version(empty_db) == 0
    assert backup_files() == []
    assert any(
        'Could not back up the database before migrating it' in message
        for message in messages_at(caplog, logging.ERROR)
    )


# A copy is one consistent snapshot of the live file: it opens on its own, passes SQLite's
# integrity check, is a single file in rollback-journal mode, and holds what was committed when it
# was taken and nothing another connection had not yet committed.
def test_snapshot_is_openable_and_consistent(db, make_job):
    for index in range(3):
        make_job(youtube_id=f'aaaaaaaaaa{index}', title=f'Committed {index}')
    writer = open_file(database.database_file())
    try:
        writer.execute('BEGIN IMMEDIATE')
        writer.execute(
            "INSERT INTO downloads (url, request_type, status, cancel_requested, group_by_channel)"
            " VALUES ('https://youtu.be/bbbbbbbbbbb', 'video', 'queued', 0, 0)"
        )
        snapshot = database.backup_database('daily', keep=7)
        writer.rollback()
    finally:
        writer.close()

    copy = open_file(snapshot)
    try:
        assert copy.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
        assert copy.execute('PRAGMA journal_mode').fetchone()[0] == 'delete'
        assert copy.execute('PRAGMA user_version').fetchone()[0] == database.SCHEMA_VERSION
        titles = [row[0] for row in copy.execute('SELECT title FROM downloads ORDER BY id')]
    finally:
        copy.close()
    assert titles == ['Committed 0', 'Committed 1', 'Committed 2']
    assert snapshot.parent == database.database_file().parent / 'backups'


# Pruning keeps the newest copies of the tag that was backed up and touches nothing else in
# backups/: other tags, and files of other names, stay.
def test_backup_prunes_only_its_own_tag(db):
    directory = database.backup_directory()
    directory.mkdir()
    others = ['notes.txt', 'youtube-daily-latest.db', 'youtube-pre-v1-20260101T000000Z.db']
    for name in others:
        (directory / name).write_text('kept')
    for day in range(1, 4):
        (directory / f'youtube-daily-2026010{day}T003000Z.db').write_text('old')

    newest = database.backup_database('daily', keep=2, now=utc(0, 0, 30))

    daily = [path.name for path in database.list_backups(directory, 'daily')]
    assert daily == ['youtube-daily-20260103T003000Z.db', newest.name]
    assert all((directory / name).read_text() == 'kept' for name in others)


# A copy whose time is earlier than the copies already kept, as after the clock went back, is
# kept all the same: the oldest of the others goes instead.
def test_backup_keeps_a_copy_dated_before_older_ones(db):
    directory = database.backup_directory()
    directory.mkdir()
    for day in range(1, 4):
        (directory / f'youtube-daily-2030010{day}T003000Z.db').write_text('later')

    copy = database.backup_database('daily', keep=3, now=utc(0, 0, 30))

    assert copy.is_file()
    assert [path.name for path in copies('daily')] == [
        copy.name, 'youtube-daily-20300102T003000Z.db', 'youtube-daily-20300103T003000Z.db',
    ]


# What an interrupted copy left behind under the tag, its journal included, is removed before the
# next copy; another tag's leftovers stay.
def test_backup_removes_leftovers_of_an_interrupted_copy(db):
    directory = database.backup_directory()
    directory.mkdir()
    leftovers = [
        'youtube-daily-20260101T003000Z.db.tmp', 'youtube-daily-20260101T003000Z.db.tmp-journal',
    ]
    others = ['youtube-weekly-20260101T003000Z.db.tmp']
    for name in leftovers + others:
        (directory / name).write_text('partial')

    database.backup_database('daily', keep=7)

    names = backup_files()
    assert not set(leftovers) & set(names)
    assert set(others) <= set(names)


# A tag that is not lower-case letters and digits in hyphen-separated runs, or a count below 1, is
# refused before anything is written.
@pytest.mark.parametrize('tag, keep', [('../daily', 7), ('Daily', 7), ('', 7), ('daily', 0)])
def test_backup_refuses_bad_tag_or_count(db, tag, keep):
    with pytest.raises(ValueError):
        database.backup_database(tag, keep=keep)
    assert backup_files() == []


# The daily snapshot is written on the first check at or after the snapshot time and never twice
# in the span until the next day's, not even by a restarted worker, and the newest
# DAILY_SNAPSHOTS_KEPT stay.
def test_daily_snapshot_written_once_per_span_and_pruned(db):
    snapshot = worker.DailySnapshot(30)

    first = snapshot.run_if_due(utc(0, 0, 30))
    assert first is not None and first.name == 'youtube-daily-20260301T003000Z.db'
    assert snapshot.run_if_due(utc(0, 12, 0)) is None
    assert worker.DailySnapshot(30).run_if_due(utc(1, 0, 29)) is None

    for day in range(1, 9):
        assert snapshot.run_if_due(utc(day, 0, 29)) is None
        assert snapshot.run_if_due(utc(day, 6, 0)) is not None

    kept = [path.name for path in copies('daily')]
    assert worker.DAILY_SNAPSHOTS_KEPT == 7
    assert kept == [f'youtube-daily-202603{day:02d}T060000Z.db' for day in range(3, 10)]


# A day's snapshot stays due until the next day's snapshot time, so a worker busy past midnight
# with a late snapshot time writes it when it gets back to its loop.
def test_daily_snapshot_due_until_the_next_one(db):
    snapshot = worker.DailySnapshot(23 * 60 + 30)

    assert snapshot.run_if_due(utc(0, 23, 30)) is not None
    assert snapshot.run_if_due(utc(2, 0, 10)) is not None
    assert snapshot.run_if_due(utc(2, 0, 20)) is None

    assert [path.name for path in copies('daily')] == [
        'youtube-daily-20260301T233000Z.db', 'youtube-daily-20260303T001000Z.db',
    ]


# A worker that starts before the snapshot time, with no copy since the day before, writes one
# at once.
def test_daily_snapshot_written_at_once_after_a_gap(db):
    copy = worker.DailySnapshot(30).run_if_due(utc(0, 0, 10))

    assert copy is not None and copy.name == 'youtube-daily-20260301T001000Z.db'


# Only a copy dated within the span counts: one dated later, as by a clock that ran ahead, or
# with an impossible time in its name, neither ends the span nor stops the check.
def test_daily_snapshot_ignores_copies_dated_outside_the_span(db):
    directory = database.backup_directory()
    directory.mkdir()
    odd = ['youtube-daily-20300101T003000Z.db', 'youtube-daily-20261399T999999Z.db']
    for name in odd:
        (directory / name).write_text('odd')

    copy = worker.DailySnapshot(30).run_if_due(utc(0, 6, 0))

    assert copy is not None and copy.name == 'youtube-daily-20260301T060000Z.db'


# A failed snapshot is tried again an hour later, not on every pass. The first failure of a day is
# an ERROR, a retry that fails the same day is a WARN, and a later success writes the copy.
def test_daily_snapshot_failure_retried_hourly(db, monkeypatch, caplog):
    attempts = []

    # backup_database as on a full disk.
    def failing_backup(tag, keep, now=None):
        attempts.append(now)
        raise OSError(28, 'No space left on device')

    monkeypatch.setattr(worker, 'backup_database', failing_backup)
    snapshot = worker.DailySnapshot(30)

    assert snapshot.run_if_due(utc(0, 0, 31)) is None
    assert snapshot.run_if_due(utc(0, 1, 0)) is None
    assert snapshot.run_if_due(utc(0, 1, 31)) is None
    assert attempts == [utc(0, 0, 31), utc(0, 1, 31)]
    assert len([m for m in messages_at(caplog, logging.ERROR) if 'snapshot failed' in m]) == 1
    assert len([m for m in messages_at(caplog, logging.WARNING) if 'failed again' in m]) == 1

    monkeypatch.setattr(worker, 'backup_database', database.backup_database)
    assert snapshot.run_if_due(utc(0, 2, 31)) is not None


# The loop's snapshot check asks for the current time in UTC, whatever the host's time zone, and
# hands that time to run_if_due.
def test_take_daily_snapshot_uses_utc(monkeypatch):
    zones = []
    seen = []
    fixed = utc(0, 6, 0)

    # datetime, with now() recording the zone it is asked for.
    class RecordingDatetime(datetime):
        # A fixed time, aware in the zone `tz` names, or naive without one.
        @classmethod
        def now(cls, tz=None):
            zones.append(tz)
            return fixed if tz is not None else fixed.replace(tzinfo=None)

    # A DailySnapshot stand-in that records the time it is given.
    class Recorder:
        # Record `now`.
        def run_if_due(self, now):
            seen.append(now)

    monkeypatch.setattr(worker, 'datetime', RecordingDatetime)

    worker.take_daily_snapshot(Recorder())

    assert zones == [timezone.utc]
    assert seen == [fixed]


# YTM_DB_SNAPSHOT_TIME is read as HH:MM in UTC, minutes after midnight; unset or empty means
# 00:30, and anything else is an ERROR that leaves 00:30 in force.
@pytest.mark.parametrize('value, minutes, error', [
    (None, 30, False),
    ('', 30, False),
    ('06:15', 375, False),
    ('23:59', 1439, False),
    ('6:15', 30, True),
    ('24:00', 30, True),
    ('noon', 30, True),
])
def test_snapshot_time_setting(monkeypatch, caplog, value, minutes, error):
    if value is None:
        monkeypatch.delenv('YTM_DB_SNAPSHOT_TIME', raising=False)
    else:
        monkeypatch.setenv('YTM_DB_SNAPSHOT_TIME', value)

    assert worker.snapshot_minute_of_day() == minutes
    errors = [m for m in messages_at(caplog, logging.ERROR) if 'YTM_DB_SNAPSHOT_TIME' in m]
    assert bool(errors) == error
