# -------------------------------------------------------
# Database schema and session factory
#
# SQLAlchemy models, engine and session factory for the YouTube Manager.
# -------------------------------------------------------

import logging
import os
import time
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
    text,
)
from sqlalchemy.orm import declarative_base, relationship, sessionmaker

# =======================================================
#              ENGINE AND CONNECTION SETUP
# =======================================================
# Where the database lives, and the per-connection PRAGMAs that let two processes share it.

# data/ at the repo root, beside this package; /app/data in the image.
DATABASE_DIR = Path(__file__).resolve().parent.parent / 'data'

# YOUTUBE_DB_URI overrides the local SQLite file; the directory is only created when that default
# is actually in use, so an override never has to be writable here.
_configured_uri = os.getenv('YOUTUBE_DB_URI')
if not _configured_uri:
    DATABASE_DIR.mkdir(parents=True, exist_ok=True)
DATABASE_URL = _configured_uri or f"sqlite:///{DATABASE_DIR / 'youtube.db'}"


# Timezone-aware UTC. SQLite's DATETIME storage drops the offset, so a value read back from a
# query is naive while one still held in the session is aware -- normalize before comparing.
def utcnow() -> datetime:
    return datetime.now(timezone.utc)


# WAL lets the app read while the worker writes; busy_timeout makes the loser of a write race
# wait its turn instead of failing outright with "database is locked".
def _enable_sqlite_wal(dbapi_connection, connection_record):
    try:
        cursor = dbapi_connection.cursor()
        cursor.execute('PRAGMA journal_mode=WAL')
        cursor.execute('PRAGMA busy_timeout=5000')
        cursor.close()
    except Exception as exc:
        logging.warning('Failed to configure SQLite WAL or busy timeout: %s', exc)


# Build an engine for `url` with _enable_sqlite_wal attached to every connection it opens.
# Connects lazily, so nothing touches the database file until the first query.
def _create_engine(url: str) -> Engine:
    # Sessions are handed across threads, which SQLite only allows with check_same_thread=False.
    created = create_engine(
        url,
        connect_args={'check_same_thread': False},
        pool_pre_ping=True,
        pool_recycle=300,
    )
    event.listen(created, 'connect', _enable_sqlite_wal)
    return created


engine = _create_engine(DATABASE_URL)

# =======================================================
#                      ORM MODELS
# =======================================================
# The four tables and the relationships between them; a Download is the only row the worker writes.

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
# row on a schedule and queues whatever it has not recorded before; channel_id/playlist_id stay
# empty until the first scan resolves the URL to a Channel or Playlist row.
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
    # When False, the first scan after subscribing records the existing lineup without queuing
    # it (see worker.check_subscriptions), so only videos seen in a later scan get downloaded.
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
    # Episode number within the parent: a playlist's scan order, or a channel's upload-date order
    # (see worker.resequence_channel_index). None until a scan assigns one, and the organizer
    # waits for it rather than guessing, so a video without an index stays unfiled.
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
    # The app's cancel flag. Only the worker moves a row to 'cancelled', so this stays set until
    # it acts; meanwhile the UI shows 'cancelling' (see jobstate.compute_effective_status).
    cancel_requested = Column(Boolean, nullable=False, default=False)
    error = Column(Text)
    created_at = Column(DateTime, default=utcnow)
    updated_at = Column(DateTime, default=utcnow, onupdate=utcnow)
    completed_at = Column(DateTime)

    children = relationship('Download', back_populates='parent', cascade='all, delete-orphan', lazy='selectin')
    parent = relationship('Download', remote_side=[id], back_populates='children')
    playlist = relationship('Playlist', back_populates='downloads')
    channel = relationship('Channel', back_populates='downloads')


Index('idx_download_status', Download.status)
Index('idx_download_parent', Download.parent_id)
Index('idx_download_youtube_id', Download.youtube_id)
Index('idx_download_parent_status', Download.parent_id, Download.status)


# =======================================================
#                   SCHEMA MIGRATIONS
# =======================================================
# Raw SQL run against a live database, so every step here must be a no-op on an already-current one.

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


# Drop the legacy UNIQUE constraint on downloads.youtube_id, preserving existing rows, since
# one video may be a child of several playlists. A named index is dropped in place; an
# sqlite_autoindex can only go by rebuilding the table.
def relax_downloads_youtube_id_uniqueness() -> None:
    with engine.begin() as conn:
        # Nothing to migrate if the table has not been created yet.
        if not conn.execute(
            text("SELECT name FROM sqlite_master WHERE type='table' AND name='downloads'")
        ).first():
            return

        # Absent on a database already migrated, and on one created fresh from today's ORM, so
        # every startup after the first returns here.
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

        # The renamed table keeps owning its index names, so they must be dropped before the new
        # table can claim them. Copying only the shared columns is safe because
        # ensure_table_columns() has already brought the legacy table up to the ORM's columns.
        conn.execute(text('ALTER TABLE downloads RENAME TO downloads_legacy'))
        for index in table.indexes:
            conn.execute(text(f'DROP INDEX IF EXISTS "{index.name}"'))
        table.create(conn)
        conn.execute(
            text(f'INSERT INTO downloads ({column_list}) SELECT {column_list} FROM downloads_legacy')
        )
        conn.execute(text('DROP TABLE downloads_legacy'))
        logging.info('Rebuilt downloads table without the UNIQUE constraint on youtube_id')


# Stand-in for migrations: add any ORM column the live table is missing.
def ensure_table_columns() -> None:
    # Open a transactional connection: ALTER TABLE statements issued on a plain connection are
    # rolled back when it closes, which would silently discard every column added here.
    with engine.begin() as conn:
        for table_name, table in Base.metadata.tables.items():
            existing_columns = {
                row[1] for row in conn.execute(text(f"PRAGMA table_info({table_name})"))
            }

            # A table that does not exist yet has nothing to alter; create_all() owns creating it.
            if not existing_columns:
                continue

            # Additions only: a column the ORM has dropped or retyped is left exactly as it is on
            # the live table, so nothing here can lose data.
            for column in table.columns:
                if column.name in existing_columns:
                    continue

                # ADD COLUMN is the one alteration SQLite performs in place, and every existing row
                # takes the default clause -- which is why a NOT NULL column has to carry one.
                column_type = get_sqlite_column_type(column)
                default_clause = get_sqlite_column_default(column)
                conn.execute(
                    text(
                        f'ALTER TABLE {table_name} ADD COLUMN {column.name} {column_type}{default_clause}'
                    )
                )


# =======================================================
#              STARTUP AND SESSION FACTORY
# =======================================================
# What the app runs once at boot, what the worker waits on, and the sessionmaker both then use.

# Create the schema and bring an existing one up to date: tables, then any column the ORM has
# gained, then the youtube_id uniqueness migration. Only the app process calls this at startup;
# the worker blocks in wait_for_schema() instead, so two writers never race through the DDL.
def initialize_database() -> None:
    with engine.begin() as conn:
        conn.execute(text('PRAGMA journal_mode=WAL'))
        Base.metadata.create_all(conn)
    # Backfilling columns has to come first: the rebuild below copies only the columns both
    # schemas share, and an ORM column declared NOT NULL carries a Python-side default that
    # never reaches the DDL, so copying without it aborts on "NOT NULL constraint failed".
    ensure_table_columns()
    relax_downloads_youtube_id_uniqueness()


# "Initialized" means the downloads table exists *and* has every column the ORM expects, not
# merely that create_all() has run: the worker queries these columns as soon as this is true.
def is_schema_initialized() -> bool:
    expected_columns = {column.name for column in Base.metadata.tables['downloads'].columns}

    with engine.connect() as conn:
        live_columns = {row[1] for row in conn.execute(text('PRAGMA table_info(downloads)'))}

    return bool(live_columns) and expected_columns.issubset(live_columns)


# Block until the schema exists. Only the app calls initialize_database(), so the worker can
# start against an empty file and polls here until the app has created the tables.
def wait_for_schema(timeout_seconds: int | None = None) -> bool:
    deadline = None if timeout_seconds is None else time.time() + timeout_seconds
    reported_error = False

    while True:
        try:
            if is_schema_initialized():
                return True
        except Exception as exc:
            # Reported once: an unreachable database otherwise looks exactly like a schema that
            # is merely slow to appear, and the caller only ever sees the timeout.
            if not reported_error:
                logging.warning('Still waiting for the database schema: %s', exc)
                reported_error = True

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
