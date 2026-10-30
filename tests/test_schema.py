# -------------------------------------------------------
# Schema creation and migration tests
#
# Characterization tests for database.initialize_database(): the schema a fresh file gets, that a
# rerun changes nothing, and that a legacy downloads table with a column-level UNIQUE youtube_id
# is rebuilt without it and keeps every row.
# -------------------------------------------------------

from sqlalchemy import inspect, text

import database
from database import Base, Download

VIDEO_ID = 'dQw4w9WgXcQ'

# Columns of the legacy downloads table below, in its declared order.
LEGACY_COLUMNS = ('id', 'url', 'request_type', 'youtube_id', 'parent_id', 'title', 'status')


# Every schema object sqlite_master holds, with its SQL, for comparing a schema before and after.
def schema_snapshot() -> list[tuple]:
    with database.engine.connect() as conn:
        rows = conn.execute(text('SELECT type, name, sql FROM sqlite_master ORDER BY name'))
        return [tuple(row) for row in rows]


# Insert a row reusing VIDEO_ID and commit it. Raises IntegrityError on a second call while a
# UNIQUE constraint still covers downloads.youtube_id.
def insert_row_with_shared_youtube_id() -> None:
    with database.Session() as session:
        session.add(Download(url=f'https://www.youtube.com/watch?v={VIDEO_ID}', youtube_id=VIDEO_ID))
        session.commit()


# A fresh file gets every ORM table with exactly the ORM's columns, the named indexes and WAL
# journaling, lets two rows share a youtube_id, and passes the worker's readiness check.
def test_initialize_database_fresh(db):
    inspector = inspect(database.engine)
    assert set(Base.metadata.tables) <= set(inspector.get_table_names())
    for name, table in Base.metadata.tables.items():
        live = {column['name'] for column in inspector.get_columns(name)}
        assert live == {column.name for column in table.columns}, name

    indexes = {index['name'] for index in inspector.get_indexes('downloads')}
    assert {
        'idx_download_status',
        'idx_download_parent',
        'idx_download_youtube_id',
        'idx_download_parent_status',
    } <= indexes

    with database.engine.connect() as conn:
        assert conn.execute(text('PRAGMA journal_mode')).scalar() == 'wal'
    assert database.is_schema_initialized()

    insert_row_with_shared_youtube_id()
    insert_row_with_shared_youtube_id()


# Running initialize_database() again on a current schema leaves the schema and the rows in it
# exactly as they were.
def test_initialize_database_idempotent(db, make_job):
    job = make_job(youtube_id=VIDEO_ID, title='Kept')
    before = schema_snapshot()

    database.initialize_database()
    database.initialize_database()

    assert schema_snapshot() == before
    with db() as session:
        kept = session.get(Download, job.id)
        assert (kept.youtube_id, kept.title, kept.status) == (VIDEO_ID, 'Kept', 'queued')


# A downloads table from before the ORM gained its newer columns, with youtube_id declared UNIQUE
# on the column, comes out with the ORM's column set, every original row and value, the defaults
# on the added NOT NULL columns, no leftover legacy table, and duplicate ids allowed.
def test_relax_youtube_id_uniqueness_rebuild_preserves_rows(empty_db):
    legacy_rows = [
        (1, f'https://www.youtube.com/watch?v={VIDEO_ID}', 'video', VIDEO_ID, None, 'First', 'completed'),
        (2, 'https://www.youtube.com/playlist?list=PL1', 'playlist', None, None, 'Playlist', 'queued'),
        (3, 'https://www.youtube.com/watch?v=aaaaaaaaaaa', 'video', 'aaaaaaaaaaa', 2, 'Child', 'failed'),
    ]
    with database.engine.begin() as conn:
        conn.execute(text(
            'CREATE TABLE downloads ('
            ' id INTEGER PRIMARY KEY,'
            ' url VARCHAR NOT NULL,'
            ' request_type VARCHAR NOT NULL,'
            ' youtube_id VARCHAR UNIQUE,'
            ' parent_id INTEGER,'
            ' title VARCHAR,'
            ' status VARCHAR NOT NULL)'
        ))
        conn.execute(
            text(
                f'INSERT INTO downloads ({", ".join(LEGACY_COLUMNS)})'
                f' VALUES ({", ".join(":" + name for name in LEGACY_COLUMNS)})'
            ),
            [dict(zip(LEGACY_COLUMNS, row)) for row in legacy_rows],
        )

    database.initialize_database()

    with database.engine.connect() as conn:
        tables = {
            row[0] for row in conn.execute(text("SELECT name FROM sqlite_master WHERE type='table'"))
        }
        live_columns = {row[1] for row in conn.execute(text('PRAGMA table_info(downloads)'))}
        rows = conn.execute(text(
            f'SELECT {", ".join(LEGACY_COLUMNS)}, cancel_requested, group_by_channel'
            ' FROM downloads ORDER BY id'
        )).all()

    assert 'downloads_legacy' not in tables
    assert live_columns == {column.name for column in Download.__table__.columns}
    assert [tuple(row[:7]) for row in rows] == legacy_rows
    assert [tuple(row[7:]) for row in rows] == [(0, 0)] * len(legacy_rows)
    assert database.is_schema_initialized()

    # Row 1 already holds VIDEO_ID, so this insert needs the UNIQUE constraint gone.
    insert_row_with_shared_youtube_id()
