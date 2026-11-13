# -------------------------------------------------------
# Database schema and session factory
#
# SQLAlchemy models, engine and session factory for the YouTube Manager.
# -------------------------------------------------------

import logging
import os
import re
import sqlite3
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Engine,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    create_engine,
    event,
    make_url,
    text,
)
from sqlalchemy.exc import ArgumentError
from sqlalchemy.orm import declarative_base, relationship, sessionmaker

# =======================================================
#              ENGINE AND CONNECTION SETUP
# =======================================================
# Where the database lives, and the per-connection settings that let two processes share it.

# data/ at the repo root, beside this package; /app/data in the image.
DATABASE_DIR = Path(__file__).resolve().parent.parent / 'data'

# YOUTUBE_DB_URI overrides the local SQLite file; the directory is only created when that default
# is actually in use, so an override never has to be writable here.
_configured_uri = os.getenv('YOUTUBE_DB_URI')
if not _configured_uri:
    DATABASE_DIR.mkdir(parents=True, exist_ok=True)
DATABASE_URL = _configured_uri or f"sqlite:///{DATABASE_DIR / 'youtube.db'}"

# How long a connection waits for another connection's write lock before failing with "database
# is locked". A channel expansion that inserts hundreds of rows can hold the lock for seconds on
# an SD card.
BUSY_TIMEOUT_SECONDS = 15

# The size the WAL file is cut back to by the first commit after a checkpoint has emptied it, so
# one burst of writes cannot leave a large file on disk for good.
JOURNAL_SIZE_LIMIT_BYTES = 8 * 1024 * 1024


# Timezone-aware UTC. SQLite's DATETIME storage drops the offset, so a value read back from a
# query is naive while one still held in the session is aware -- normalize before comparing.
def utcnow() -> datetime:
    return datetime.now(timezone.utc)


# `dt` as an aware UTC time; a naive one, as SQLite hands back, is read as UTC. None stays None.
def normalize_datetime(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


# Raise ValueError unless `url` names a SQLite database through Python's sqlite3 driver, the only
# one the settings and schema checks in this module are written for. The message repeats no more
# of the URL than its scheme, so a password in a mistaken URL never reaches the log.
def require_sqlite_url(url: str) -> None:
    try:
        parsed = make_url(url)
    # ValueError too: a port that is not a number fails int() with that text quoted, and in a
    # mistyped URL the text read as the port can be the password.
    except (ArgumentError, ValueError):
        # from None, so the traceback leaves out the parse error and the URL text it may quote.
        raise ValueError(
            'YOUTUBE_DB_URI is not a database URL; use sqlite:///<path to the database file>'
        ) from None
    # The backend is checked first, so a driver name is only ever resolved for a SQLite URL.
    if parsed.get_backend_name() != 'sqlite' or parsed.get_driver_name() != 'pysqlite':
        raise ValueError(
            "YOUTUBE_DB_URI must be a sqlite:///<path to the database file> URL for Python's "
            f'sqlite3 driver, not {parsed.drivername}'
        )


# Apply the per-connection settings to a connection as it opens. WAL lets the app read while the
# worker writes. Under WAL, synchronous=NORMAL cannot corrupt the database and saves an fsync per
# commit: a power cut or an OS crash can roll back only the last few commits, as if both processes
# had stopped a moment earlier, and a crash of the app or its container loses none. A connection
# left out of WAL keeps SQLite's default, FULL, as NORMAL is only safe under WAL. A failure is
# logged at WARN and the connection is used as it is.
def _configure_sqlite_connection(dbapi_connection, connection_record):
    try:
        cursor = dbapi_connection.cursor()
        try:
            journal_mode = cursor.execute('PRAGMA journal_mode=WAL').fetchone()[0]
            if journal_mode == 'wal':
                cursor.execute('PRAGMA synchronous=NORMAL')
            else:
                logging.warning(
                    'SQLite kept journal_mode=%s instead of WAL, so synchronous stays FULL',
                    journal_mode,
                )
            cursor.execute(f'PRAGMA journal_size_limit={JOURNAL_SIZE_LIMIT_BYTES}')
        finally:
            cursor.close()
    except Exception as exc:
        logging.warning('Could not configure the SQLite connection: %s', exc)


# Build an engine for `url` that runs _configure_sqlite_connection on every connection it opens;
# `url` must pass require_sqlite_url. Connects lazily, so nothing touches the database file until
# the first query. The pool keeps its defaults: a SQLite file has no server connection that could
# go stale, so it needs no pre-ping or recycling.
def _create_engine(url: str) -> Engine:
    require_sqlite_url(url)
    created = create_engine(
        url,
        connect_args={
            # Sessions are handed across threads, which SQLite only allows with this off.
            'check_same_thread': False,
            # The driver sets the busy timeout as it connects, so the timeout already covers the
            # WAL switch in _configure_sqlite_connection.
            'timeout': BUSY_TIMEOUT_SECONDS,
            # The driver's legacy transaction control, stated rather than defaulted: it begins a
            # transaction only before an INSERT, UPDATE, DELETE or REPLACE, and runs DDL on its
            # own. A session that only reads holds no snapshot open, which would keep
            # checkpoints from emptying the WAL, and each of its queries sees the other process's
            # latest commit. Python plans to change the default; a Python without this constant
            # fails here, at import, rather than changing how the app's transactions behave.
            'autocommit': sqlite3.LEGACY_TRANSACTION_CONTROL,
        },
    )
    event.listen(created, 'connect', _configure_sqlite_connection)
    return created


engine = _create_engine(DATABASE_URL)

# =======================================================
#                      ORM MODELS
# =======================================================
# The four tables and the relationships between them. Both processes write a Download. The worker
# owns a video's in-flight states. The app sets request flags (cancel_requested), resets a row that
# has stopped (Retry, Fetch Metadata, Add), cancels a queued or expanding row outright, and
# settles or re-queues a playlist or channel row from its children (jobstate.settle_parent).
# Neither the worker's status writes (worker.update_job) nor a settle overwrite a 'deleted' row;
# only an explicit request queues one again.

Base = declarative_base()


# One YouTube channel, created on demand by a download or a subscription rather than declared up
# front. Owns the playlists and downloads attributed to it, and is the grouping a channel-wide
# request files its videos under.
class Channel(Base):

    __tablename__ = 'channels'

    id = Column(Integer, primary_key=True)
    youtube_id = Column(String, unique=True)
    name = Column(String, nullable=False)
    url = Column(String, unique=True, nullable=False)
    description = Column(Text)
    thumbnail = Column(String)
    # Stable, filesystem-safe folder name for this channel's grouped videos (see
    # organizer.plan_channel_video), assigned once and kept even if the channel's name changes
    # later. Mirrors Playlist.folder_name.
    folder_name = Column(String)
    created_at = Column(DateTime, default=utcnow)

    playlists = relationship('Playlist', back_populates='channel')
    downloads = relationship('Download', back_populates='channel')
    subscriptions = relationship('Subscription', back_populates='channel')


# One YouTube playlist, likewise created on demand. Belongs to the channel that published it,
# when one is known, and is what a Download becomes an episode of: a row carrying playlist_id is
# filed as a numbered episode of this playlist's show instead of as a movie.
class Playlist(Base):

    __tablename__ = 'playlists'

    id = Column(Integer, primary_key=True)
    youtube_id = Column(String, unique=True)
    channel_id = Column(Integer, ForeignKey('channels.id'))
    title = Column(String, nullable=False)
    description = Column(Text)
    thumbnail = Column(String)
    # Per-playlist show folder, assigned once; see Channel.folder_name.
    folder_name = Column(String)
    created_at = Column(DateTime, default=utcnow)

    channel = relationship('Channel', back_populates='playlists')
    downloads = relationship('Download', back_populates='playlist')
    subscriptions = relationship('Subscription', back_populates='playlist')


# A channel or playlist watched for new uploads. worker.check_subscriptions rescans every enabled
# row on a schedule and queues whatever it has not recorded before, and a first scan with
# backfill_existing set also the skipped rows it lists; channel_id/playlist_id stay empty until the
# first scan resolves the URL to a Channel or Playlist row.
class Subscription(Base):

    __tablename__ = 'subscriptions'

    id = Column(Integer, primary_key=True)
    # 'channel' or 'playlist'. Renamed because the underlying column is literally named `type`,
    # so a query has to filter on Subscription.subscription_type.
    subscription_type = Column('type', String, nullable=False)
    url = Column(String, nullable=False, unique=True)
    youtube_id = Column(String, unique=True)
    channel_id = Column(Integer, ForeignKey('channels.id'))
    playlist_id = Column(Integer, ForeignKey('playlists.id'))
    enabled = Column(Boolean, nullable=False, default=True)
    # When False, a first scan (last_checked NULL) records the listing as skipped without queuing
    # it, so only videos seen in a later scan get downloaded; when True it queues the listing, the
    # skipped rows it lists included (see worker.check_subscriptions).
    backfill_existing = Column(Boolean, nullable=False, default=True)
    last_checked = Column(DateTime)
    created_at = Column(DateTime, default=utcnow)

    channel = relationship('Channel', back_populates='subscriptions')
    playlist = relationship('Playlist', back_populates='subscriptions')


# One download job; a playlist/channel job also owns a child row per video.
class Download(Base):

    __tablename__ = 'downloads'

    id = Column(Integer, primary_key=True)
    url = Column(String, nullable=False)
    request_type = Column(String, nullable=False, default='video')
    # Deliberately not unique: one video can be a child of several playlists (and a standalone
    # download), each needing its own row and playlist_index.
    youtube_id = Column(String)
    parent_id = Column(Integer, ForeignKey('downloads.id'))
    playlist_id = Column(Integer, ForeignKey('playlists.id'))
    channel_id = Column(Integer, ForeignKey('channels.id'))
    # Set only for a channel-wide context -- a child of an expanded 'channel' request, or a video
    # queued by a channel subscription -- so the organizer files it into the shared per-channel
    # folder. Not inferred from channel_id: yt-dlp populates that for nearly every video.
    group_by_channel = Column(Boolean, nullable=False, default=False)
    title = Column(String)
    filename = Column(String)
    # Path to yt-dlp's .info.json sidecar while that file exists; the organizer clears it when the
    # sidecar is discarded. metadata_json below holds the same content and outlives it.
    metadata_file = Column(String)
    thumbnail = Column(String)
    description = Column(Text)
    upload_date = Column(String)
    # Episode number within the show. In a playlist: the number the video already holds there,
    # else its scan position, else the next past the highest held (see worker.PlaylistNumbering).
    # In a channel: the next free number in upload-date order, never changed once filed (see
    # worker.resequence_channel_index). None until one is assigned, and the organizer waits for it
    # rather than guessing, so a video without an index stays unfiled.
    playlist_index = Column(Integer)
    # Descriptive metadata captured from yt-dlp so the organizer can write a complete NFO
    # even after the .info.json sidecar has been cleaned up.
    duration = Column(Integer)
    view_count = Column(Integer)
    like_count = Column(Integer)
    tags = Column(Text)
    categories = Column(Text)
    # The full yt-dlp info dictionary (minus its bulky format/caption/thumbnail lists), stored
    # as JSON so nothing yt-dlp reported is lost once the sidecar file is removed.
    metadata_json = Column(Text)
    progress = Column(String)
    speed = Column(String)
    # When the organizer filed this video into the library; None means not yet organized. Clearing
    # it is how a re-file is requested -- the next organizer pass plans the move again.
    organized_at = Column(DateTime)
    log = Column(Text)
    status = Column(String, nullable=False, default='queued')
    # The app's request to cancel, set only on a row in progress. The app cancels a queued or
    # expanding row outright; a row in flight keeps it until the worker acts on it, the UI
    # meanwhile showing 'cancelling' (see jobstate.compute_effective_status), and a parent in
    # flight with it set settles as 'cancelled' once no child is (jobstate.settle_parent).
    cancel_requested = Column(Boolean, nullable=False, default=False)
    error = Column(Text)
    created_at = Column(DateTime, default=utcnow)
    updated_at = Column(DateTime, default=utcnow, onupdate=utcnow)
    completed_at = Column(DateTime)
    # Automatic retries this video has had since it last stopped failing or a person retried it,
    # and when the worker may try it next. Kept on the row, so a restart does not start the
    # schedule over. The DDL default lets the release before these columns insert rows.
    auto_retry_attempts = Column(Integer, nullable=False, default=0, server_default=text('0'))
    next_auto_retry_at = Column(DateTime)

    children = relationship('Download', back_populates='parent', cascade='all, delete-orphan', lazy='selectin')
    parent = relationship('Download', remote_side=[id], back_populates='children')
    playlist = relationship('Playlist', back_populates='downloads')
    channel = relationship('Channel', back_populates='downloads')


Index('idx_download_status', Download.status)
Index('idx_download_parent', Download.parent_id)
Index('idx_download_youtube_id', Download.youtube_id)
Index('idx_download_parent_status', Download.parent_id, Download.status)
# The dashboard's change check reads max(updated_at) on every poll.
Index('idx_download_updated_at', Download.updated_at)


# =======================================================
#                   SCHEMA MIGRATIONS
# =======================================================
# The steps that bring a database's schema up to SCHEMA_VERSION, kept in PRAGMA user_version. Each
# runs in one transaction with the version it reaches (initialize_database), and each is a no-op on
# a database it finds already done, since it runs against a live file.

# The schema version this release creates and migrates to. A database from before versions, or a
# new file, reads 0. Versions in the same hundred are compatible: a step inside a hundred keeps
# the release before it working on the migrated file, so it may add a nullable column, a column
# with a DDL default (server_default; an ORM default= alone never reaches the DDL), a non-unique
# index or a table, and fill in values that release reads the same way. Anything else -- a
# dropped, renamed or retyped column, a NOT NULL column without a DDL default, a UNIQUE or CHECK
# constraint -- takes the next hundred and ships in a major release (CONTRIBUTING, Schema changes).
SCHEMA_VERSION = 2

# Copies kept of the database at one version before one migration, tagged
# pre-v<new version>-from-v<old version>. A start that keeps failing takes one each time, and since
# the tag names the version copied, a partly migrated file never pushes out the copy of the file
# as it was before the upgrade.
PRE_MIGRATION_BACKUPS_KEPT = 3


# Raised when the database's schema is from a later hundred than SCHEMA_VERSION: a later major
# release migrated it, and this release would misread it.
class SchemaTooNewError(RuntimeError):
    pass


# The database's schema version, PRAGMA user_version, read through `conn`.
def read_schema_version(conn) -> int:
    return conn.exec_driver_sql('PRAGMA user_version').scalar()


# How this release stands to schema `version`: 'current'; 'older', which initialize_database
# migrates; 'newer', a later version in the same hundred, which only adds to this release's schema,
# so this release runs on it as it is; or 'incompatible', a version in a later hundred.
def schema_relation(version: int) -> str:
    if version == SCHEMA_VERSION:
        return 'current'
    if version < SCHEMA_VERSION:
        return 'older'
    if version // 100 == SCHEMA_VERSION // 100:
        return 'newer'
    return 'incompatible'


# Report schema `version`, which is newer than SCHEMA_VERSION: a WARN for one in the same hundred,
# which this release runs on as it is, and for a later hundred an ERROR, then SchemaTooNewError.
def check_newer_schema(version: int) -> None:
    if schema_relation(version) == 'newer':
        logging.warning(
            'The database schema is version %d, newer than this release\'s %d. It only adds to '
            'it, so this release runs on it as it is',
            version, SCHEMA_VERSION,
        )
        return
    message = (
        f'The database schema is version {version}, from a later major release than this one '
        f'(schema version {SCHEMA_VERSION}), so it is left untouched. Run that release, or '
        'restore a backup taken before it'
    )
    logging.error(message)
    raise SchemaTooNewError(message)


# The SQLite type name for an ORM column, for splicing into ALTER TABLE ADD COLUMN. A type the
# dialect cannot compile falls back to the nearest SQLite affinity and anything unrecognized to
# TEXT, so an exotic column type can never block a migration.
def get_sqlite_column_type(column) -> str:
    try:
        return column.type.compile(engine.dialect)
    except Exception:
        # Swallowed rather than raised: one uncompilable column would otherwise abort
        # ensure_table_columns() and leave every later table short its new columns as well.
        if isinstance(column.type, Integer):
            return 'INTEGER'
        if isinstance(column.type, Boolean):
            return 'BOOLEAN'
        if isinstance(column.type, DateTime):
            return 'DATETIME'
        # Text is a String subclass and both land on TEXT, so their order carries no meaning.
        if isinstance(column.type, Text):
            return 'TEXT'
        if isinstance(column.type, String):
            return 'TEXT'
        return 'TEXT'


# Render a column's default as a SQLite DEFAULT clause for ALTER TABLE ADD COLUMN.
def get_sqlite_column_default(column) -> str:
    server_default = getattr(column, 'server_default', None)
    if server_default is not None and getattr(server_default, 'arg', None) is not None:
        default_value = server_default.arg
        if isinstance(default_value, str):
            return f" DEFAULT '{default_value}'"
        return f' DEFAULT {default_value}'

    default = getattr(column, 'default', None)
    if default is not None:
        # A callable default (utcnow) has no SQLite equivalent, so the column gets none.
        if getattr(default, 'is_callable', False):
            default = None
        elif getattr(default, 'arg', None) is not None:
            default_value = default.arg

            if isinstance(default_value, str):
                return f" DEFAULT '{default_value}'"
            if isinstance(default_value, bool):
                return f' DEFAULT {int(default_value)}'
            return f' DEFAULT {default_value}'

    # SQLite rejects ADD COLUMN NOT NULL without a default, so synthesize one from the type.
    if not column.nullable:
        column_type = get_sqlite_column_type(column)
        fallback = {
            'BOOLEAN': '0',
            'INTEGER': '0',
            'DATETIME': "'1970-01-01 00:00:00'",
            'TEXT': "''",
        }.get(column_type, 'NULL')
        return f' DEFAULT {fallback}'

    return ''


# A DEFAULT clause, as get_sqlite_column_default renders it, that gives the column no value: none
# at all, or NULL however a server_default spells it, such as "null" or "(NULL)". The text 'NULL'
# in quotes is a value.
NULL_DEFAULT_CLAUSE = re.compile(r'\s*(?:DEFAULT\s*\(*\s*NULL\s*\)*\s*)?', re.IGNORECASE)


# Whether `default_clause` leaves an added column with no value for the rows already there.
def is_null_default(default_clause: str) -> bool:
    return NULL_DEFAULT_CLAUSE.fullmatch(default_clause) is not None


# Name of the UNIQUE index covering exactly downloads.youtube_id, if one exists. A column-level
# UNIQUE from an older schema shows up here as an sqlite_autoindex, which cannot be dropped.
def find_unique_youtube_id_index(conn) -> str | None:
    for row in conn.execute(text("PRAGMA index_list('downloads')")):
        index_name, is_unique = row[1], row[2]
        if not is_unique:
            continue
        # Only an index over youtube_id alone qualifies; composite ones are legitimate.
        columns = [info[2] for info in conn.execute(text(f'PRAGMA index_info("{index_name}")'))]
        if columns == ['youtube_id']:
            return index_name
    return None


# Drop the legacy UNIQUE constraint on downloads.youtube_id through `conn`, preserving existing
# rows, since one video may be a child of several playlists. A named index is dropped in place;
# an sqlite_autoindex can only go by rebuilding the table. Runs inside the caller's transaction,
# so a rebuild that fails partway leaves the original table as it was.
def relax_downloads_youtube_id_uniqueness(conn) -> None:
    # Nothing to migrate if the table has not been created yet.
    if not conn.execute(
        text("SELECT name FROM sqlite_master WHERE type='table' AND name='downloads'")
    ).first():
        return

    # Absent on a database already migrated, and on one created fresh from today's ORM, so every
    # startup after the first returns here.
    index_name = find_unique_youtube_id_index(conn)
    if not index_name:
        return

    # An explicit CREATE UNIQUE INDEX can simply be dropped; SQLite offers no way to drop the
    # sqlite_autoindex a column-level UNIQUE creates.
    if not index_name.startswith('sqlite_autoindex'):
        conn.execute(text(f'DROP INDEX "{index_name}"'))
        logging.info('Dropped legacy UNIQUE index on downloads.youtube_id')
        return

    # Rebuild path: the autoindex only disappears with the table that owns it, so the rows are
    # copied through a rename into a freshly created table and the original is dropped.
    table = Base.metadata.tables['downloads']
    legacy_columns = {row[1] for row in conn.execute(text('PRAGMA table_info(downloads)'))}
    shared_columns = [column.name for column in table.columns if column.name in legacy_columns]
    column_list = ', '.join(f'"{name}"' for name in shared_columns)

    # The renamed table keeps owning its index names, so they must be dropped before the new table
    # can claim them. Copying only the shared columns is safe because ensure_table_columns() has
    # already brought the legacy table up to the ORM's columns.
    conn.execute(text('ALTER TABLE downloads RENAME TO downloads_legacy'))
    for index in table.indexes:
        conn.execute(text(f'DROP INDEX IF EXISTS "{index.name}"'))
    table.create(conn)
    conn.execute(
        text(f'INSERT INTO downloads ({column_list}) SELECT {column_list} FROM downloads_legacy')
    )
    conn.execute(text('DROP TABLE downloads_legacy'))
    logging.info('Rebuilt downloads table without the UNIQUE constraint on youtube_id')


# Add through `conn` any ORM column the live tables are missing. Runs inside the caller's
# transaction, so a failure leaves no column of the batch behind.
def ensure_table_columns(conn) -> None:
    for table_name, table in Base.metadata.tables.items():
        existing_columns = {
            row[1] for row in conn.execute(text(f"PRAGMA table_info({table_name})"))
        }

        # A table that does not exist yet has nothing to alter; create_all() owns creating it.
        if not existing_columns:
            continue

        # Additions only: a column the ORM has dropped or retyped is left exactly as it is on the
        # live table, so nothing here can lose data.
        for column in table.columns:
            if column.name in existing_columns:
                continue

            # ADD COLUMN is the one alteration SQLite performs in place, and every existing row
            # takes the default clause -- which is why a NOT NULL column has to carry one.
            column_type = get_sqlite_column_type(column)
            default_clause = get_sqlite_column_default(column)
            # NOT NULL where the ORM declares it, so the column comes out as a new file has it.
            # SQLite refuses that beside a NULL default on a table with rows, so a column whose
            # type offers no default for its rows is still added without it, as it always was.
            keeps_not_null = not column.nullable and not is_null_default(default_clause)
            not_null = ' NOT NULL' if keeps_not_null else ''
            conn.execute(text(
                f'ALTER TABLE {table_name} ADD COLUMN {column.name} {column_type}'
                f'{default_clause}{not_null}'
            ))


# Migration 1: bring a database from before schema versions to version 1 through `conn`. It
# creates any table the ORM declares that the file lacks, adds the columns the ORM has gained, then
# drops the legacy UNIQUE constraint on downloads.youtube_id. The columns come first: the rebuild
# copies only the columns both tables share, and a NOT NULL column's Python-side default never
# reaches the DDL, so the copy would abort on "NOT NULL constraint failed". It reads today's ORM,
# so a later step has to allow for columns, tables and indexes this one already created.
def migrate_to_1(conn) -> None:
    Base.metadata.create_all(conn)
    ensure_table_columns(conn)
    relax_downloads_youtube_id_uniqueness(conn)


# Migration 2: the auto-retry columns and the index for the dashboard's change check, through
# `conn`. Each column is added only where it is missing, since migration 1 adds today's columns
# to a database from before versions. Then every row gets an updated_at, so the change check can
# read that one column: its completed_at, else its created_at, else 1 January 1970. The DDL is
# written out rather than read from today's model, so later model changes cannot alter the step.
def migrate_to_2(conn) -> None:
    columns = {row[1] for row in conn.execute(text('PRAGMA table_info(downloads)'))}
    if 'auto_retry_attempts' not in columns:
        conn.execute(text(
            'ALTER TABLE downloads ADD COLUMN auto_retry_attempts INTEGER DEFAULT 0 NOT NULL'
        ))
    if 'next_auto_retry_at' not in columns:
        conn.execute(text('ALTER TABLE downloads ADD COLUMN next_auto_retry_at DATETIME'))
    conn.execute(text(
        'CREATE INDEX IF NOT EXISTS idx_download_updated_at ON downloads (updated_at)'
    ))
    conn.execute(text(
        "UPDATE downloads SET updated_at = COALESCE(completed_at, created_at,"
        " '1970-01-01 00:00:00.000000') WHERE updated_at IS NULL"
    ))


# The step that brings a database to each schema version, keyed by that version.
MIGRATIONS = {
    1: migrate_to_1,
    2: migrate_to_2,
}


# =======================================================
#                       BACKUPS
# =======================================================
# Consistent copies of the live database, written with SQLite's backup API into backups/ beside it.

# The directory, beside the database file, that backup_database writes into.
BACKUP_DIR_NAME = 'backups'

# A backup tag: lower-case letters and digits in hyphen-separated runs, safe in a file name and in
# the patterns that find a tag's copies again.
BACKUP_TAG = re.compile(r'[a-z0-9]+(?:-[a-z0-9]+)*')


# The database's file, as SQLite itself reports it (PRAGMA database_list), so every URL form
# works; None for a database in memory.
def database_file() -> Path | None:
    with engine.connect() as conn:
        for row in conn.exec_driver_sql('PRAGMA database_list'):
            if row[1] == 'main':
                return Path(row[2]) if row[2] else None
    return None


# The directory backups go into: backups/ beside the database file. Raises ValueError for a
# database in memory, which has no file to put them beside.
def backup_directory() -> Path:
    path = database_file()
    if path is None:
        raise ValueError('The database is in memory, so it has nowhere to keep backups')
    return path.parent / BACKUP_DIR_NAME


# The copies with `tag` in `directory`, oldest first. Only names backup_database writes count, so no
# other file there is ever pruned.
def list_backups(directory: Path, tag: str) -> list[Path]:
    pattern = re.compile(rf'youtube-{re.escape(tag)}-[0-9]{{8}}T[0-9]{{6}}Z\.db')
    if not directory.is_dir():
        return []
    return sorted(path for path in directory.iterdir() if pattern.fullmatch(path.name))


# Whether backups/ holds a copy with `tag` whose name dates it at or after `start` and before
# `end`, both aware times. The fixed-width UTC times in the names sort in time order, so they are
# compared as text, and no name, however odd its time, can make the check fail.
def backup_taken_within(tag: str, start: datetime, end: datetime) -> bool:
    low, high = (f'{moment.astimezone(timezone.utc):%Y%m%dT%H%M%SZ}' for moment in (start, end))
    prefix = f'youtube-{tag}-'
    return any(
        low <= path.name.removeprefix(prefix).removesuffix('.db') < high
        for path in list_backups(backup_directory(), tag)
    )


# Copy the live database with SQLite's backup API to backups/youtube-<tag>-<UTC time>.db beside it,
# the time being `now` or the current one, then delete all but the `keep` newest copies with that
# tag, never the one just written. The copy is one consistent snapshot even while both processes
# write, and it is left in rollback-journal mode, so it is a single self-contained file; a
# restored copy goes back to WAL as the app opens it. It is written under a .tmp name and renamed
# into place, so a file under the final name is always complete, and what an interrupted copy
# left behind under that name, journal files included, is removed first. Returns the copy's path.
# Raises ValueError for a bad tag or count, or a database in memory; any other failure is
# re-raised once the partial copy is gone.
def backup_database(tag: str, keep: int, now: datetime | None = None) -> Path:
    if not BACKUP_TAG.fullmatch(tag) or keep < 1:
        raise ValueError(f'A backup needs a tag like "daily" and a count of at least 1, not '
                         f'{tag!r} and {keep}')
    directory = backup_directory()
    directory.mkdir(parents=True, exist_ok=True)
    for leftover in directory.glob(f'youtube-{tag}-*.db.tmp*'):
        leftover.unlink(missing_ok=True)

    stamp = (now or utcnow()).astimezone(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    final = directory / f'youtube-{tag}-{stamp}.db'
    partial = final.with_name(f'{final.name}.tmp')
    source = engine.raw_connection()
    try:
        copy = sqlite3.connect(partial)
        try:
            source.dbapi_connection.backup(copy)
            copy.execute('PRAGMA journal_mode=DELETE')
        finally:
            copy.close()
        os.replace(partial, final)
    except BaseException:
        partial.unlink(missing_ok=True)
        raise
    finally:
        source.close()

    # A clock that went back can date the new copy before older ones; it is kept all the same.
    others = [path for path in list_backups(directory, tag) if path != final]
    for old in others[:max(0, len(others) - (keep - 1))]:
        old.unlink(missing_ok=True)
    return final


# =======================================================
#              STARTUP AND SESSION FACTORY
# =======================================================
# What the app runs once at boot, what the worker waits on, and the sessionmaker both then use.

# A connection on which the driver opens no transaction by itself, for DDL the caller wraps in
# _immediate_transaction. The pool puts the usual transaction control back when it is returned.
@contextmanager
def _autocommit_connection():
    with engine.connect() as conn:
        yield conn.execution_options(isolation_level='AUTOCOMMIT')


# Run the block in one BEGIN IMMEDIATE transaction on `conn`, an _autocommit_connection: committed
# if the block returns, rolled back if it raises. IMMEDIATE takes the write lock before the first
# statement, waiting for it as long as the busy timeout allows, so the block never stops halfway
# on a lock another connection holds.
@contextmanager
def _immediate_transaction(conn):
    conn.exec_driver_sql('BEGIN IMMEDIATE')
    try:
        yield
    except BaseException:
        # SQLite has already rolled back after some failures, a full disk among them; a second
        # ROLLBACK would then raise and hide the error that caused it.
        if conn.connection.dbapi_connection.in_transaction:
            conn.exec_driver_sql('ROLLBACK')
        raise
    conn.exec_driver_sql('COMMIT')


# The ORM tables the database has, each with its row count, as "name count" pairs for a log line.
def _row_counts(conn) -> str:
    present = {
        row[0] for row in conn.exec_driver_sql("SELECT name FROM sqlite_master WHERE type='table'")
    }
    return ', '.join(
        f'{name} {conn.exec_driver_sql(f"SELECT count(*) FROM {name}").scalar()}'
        for name in sorted(Base.metadata.tables)
        if name in present
    )


# Create the schema, or bring the database's up to SCHEMA_VERSION. A file without tables gets the
# ORM's tables and SCHEMA_VERSION in one transaction. An older schema is first copied with
# backup_database, then moved up one MIGRATIONS step at a time, each step in one BEGIN IMMEDIATE
# transaction with the version it reaches, so a failed step leaves the file at the last version
# it completed. The copy, and each table's row count before and after, go to the log. A ladder
# that ends short of SCHEMA_VERSION, for want of a step, is an ERROR and raises RuntimeError. A
# newer schema is reported by check_newer_schema and left as it is, which for a later hundred
# means SchemaTooNewError. Only the app process calls this, at startup; the worker waits in
# wait_for_schema(), so two writers never race through the DDL.
def initialize_database() -> None:
    with _autocommit_connection() as conn:
        version = read_schema_version(conn)
        relation = schema_relation(version)
        if relation == 'current':
            return
        if relation != 'older':
            check_newer_schema(version)
            return

        if version == 0 and not conn.exec_driver_sql(
            "SELECT 1 FROM sqlite_master WHERE type='table'"
        ).first():
            with _immediate_transaction(conn):
                Base.metadata.create_all(conn)
                conn.exec_driver_sql(f'PRAGMA user_version = {SCHEMA_VERSION}')
            logging.info('Created the database schema at version %d', SCHEMA_VERSION)
            return

        counts = _row_counts(conn)
        tag = f'pre-v{SCHEMA_VERSION}-from-v{version}'
        try:
            backup = backup_database(tag, keep=PRE_MIGRATION_BACKUPS_KEPT)
        except Exception:
            logging.error('Could not back up the database before migrating it, so nothing was '
                          'migrated')
            raise
        logging.info(
            'Backed up the database to %s before migrating it from schema version %d to %d (%s)',
            backup.name, version, SCHEMA_VERSION, counts,
        )

        for target in sorted(step for step in MIGRATIONS if version < step <= SCHEMA_VERSION):
            try:
                with _immediate_transaction(conn):
                    # Another process may have taken this step since `version` was read.
                    if read_schema_version(conn) < target:
                        MIGRATIONS[target](conn)
                        conn.exec_driver_sql(f'PRAGMA user_version = {target}')
            except Exception:
                logging.error(
                    'Migrating the database to schema version %d failed and was rolled back; it '
                    'stays at version %d', target, read_schema_version(conn),
                )
                raise

        # A ladder without a step for SCHEMA_VERSION would leave the file short of it, and the
        # worker waiting; another process may also have gone further meanwhile.
        reached = read_schema_version(conn)
        relation = schema_relation(reached)
        if relation == 'older':
            message = (
                f'The migrations stop at schema version {reached}, short of this release\'s '
                f'{SCHEMA_VERSION}: MIGRATIONS has no step to reach it'
            )
            logging.error(message)
            raise RuntimeError(message)
        if relation != 'current':
            check_newer_schema(reached)
            return
        logging.info(
            'Migrated the database to schema version %d (%s)', reached, _row_counts(conn),
        )


# Whether this release can run on the database as it stands: its schema is SCHEMA_VERSION, or newer
# within the same hundred. The same rule wait_for_schema applies before the worker starts.
def is_schema_initialized() -> bool:
    with engine.connect() as conn:
        return schema_relation(read_schema_version(conn)) in ('current', 'newer')


# Block until this release can run on the database (is_schema_initialized) and return True. Only
# the app creates and migrates the schema, so the worker can start against an empty or older file
# and polls here until the app is done. Returns False once `timeout_seconds` pass first. A newer
# schema is reported by check_newer_schema, so one from a later hundred raises SchemaTooNewError at
# once instead of waiting out the timeout.
def wait_for_schema(timeout_seconds: int | None = None) -> bool:
    deadline = None if timeout_seconds is None else time.time() + timeout_seconds
    reported_error = False

    while True:
        try:
            with engine.connect() as conn:
                version = read_schema_version(conn)
        except Exception as exc:
            # Reported once: an unreachable database otherwise looks exactly like a schema that
            # is merely slow to appear, and the caller only ever sees the timeout.
            if not reported_error:
                logging.warning('Still waiting for the database schema: %s', exc)
                reported_error = True
        else:
            relation = schema_relation(version)
            if relation == 'current':
                return True
            if relation != 'older':
                check_newer_schema(version)
                return True

        if deadline is not None and time.time() > deadline:
            return False
        time.sleep(1)


# expire_on_commit=False keeps committed instances readable outside their session.
Session = sessionmaker(bind=engine, expire_on_commit=False, autoflush=True)


# Rebind the module engine and the shared Session factory to `url`, then dispose of the old
# engine's pooled connections. Tests only: the app and worker never call it. Every importer
# follows, since each uses Session or reads database.engine, and none imports engine by name.
def configure_database(url: str) -> None:
    global engine
    previous = engine
    engine = _create_engine(url)
    Session.configure(bind=engine)
    previous.dispose()
