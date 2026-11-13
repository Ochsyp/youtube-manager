-- Schema version 2, as a release at that version creates it, frozen for
-- tests/test_migrations.py::test_frozen_schemas_migrate_to_the_orm. Never edit this file: a schema
-- change adds a migration step and its own frozen file (CONTRIBUTING, Schema changes).
CREATE TABLE channels (
    id INTEGER NOT NULL,
    youtube_id VARCHAR,
    name VARCHAR NOT NULL,
    url VARCHAR NOT NULL,
    description TEXT,
    thumbnail VARCHAR,
    folder_name VARCHAR,
    created_at DATETIME,
    PRIMARY KEY (id),
    UNIQUE (youtube_id),
    UNIQUE (url)
);
CREATE TABLE playlists (
    id INTEGER NOT NULL,
    youtube_id VARCHAR,
    channel_id INTEGER,
    title VARCHAR NOT NULL,
    description TEXT,
    thumbnail VARCHAR,
    folder_name VARCHAR,
    created_at DATETIME,
    PRIMARY KEY (id),
    UNIQUE (youtube_id),
    FOREIGN KEY(channel_id) REFERENCES channels (id)
);
CREATE TABLE subscriptions (
    id INTEGER NOT NULL,
    type VARCHAR NOT NULL,
    url VARCHAR NOT NULL,
    youtube_id VARCHAR,
    channel_id INTEGER,
    playlist_id INTEGER,
    enabled BOOLEAN NOT NULL,
    backfill_existing BOOLEAN NOT NULL,
    last_checked DATETIME,
    created_at DATETIME,
    PRIMARY KEY (id),
    UNIQUE (url),
    UNIQUE (youtube_id),
    FOREIGN KEY(channel_id) REFERENCES channels (id),
    FOREIGN KEY(playlist_id) REFERENCES playlists (id)
);
CREATE TABLE downloads (
    id INTEGER NOT NULL,
    url VARCHAR NOT NULL,
    request_type VARCHAR NOT NULL,
    youtube_id VARCHAR,
    parent_id INTEGER,
    playlist_id INTEGER,
    channel_id INTEGER,
    group_by_channel BOOLEAN NOT NULL,
    title VARCHAR,
    filename VARCHAR,
    metadata_file VARCHAR,
    thumbnail VARCHAR,
    description TEXT,
    upload_date VARCHAR,
    playlist_index INTEGER,
    duration INTEGER,
    view_count INTEGER,
    like_count INTEGER,
    tags TEXT,
    categories TEXT,
    metadata_json TEXT,
    progress VARCHAR,
    speed VARCHAR,
    organized_at DATETIME,
    log TEXT,
    status VARCHAR NOT NULL,
    cancel_requested BOOLEAN NOT NULL,
    error TEXT,
    created_at DATETIME,
    updated_at DATETIME,
    completed_at DATETIME,
    auto_retry_attempts INTEGER DEFAULT 0 NOT NULL,
    next_auto_retry_at DATETIME,
    PRIMARY KEY (id),
    FOREIGN KEY(parent_id) REFERENCES downloads (id),
    FOREIGN KEY(playlist_id) REFERENCES playlists (id),
    FOREIGN KEY(channel_id) REFERENCES channels (id)
);
CREATE INDEX idx_download_youtube_id ON downloads (youtube_id);
CREATE INDEX idx_download_parent ON downloads (parent_id);
CREATE INDEX idx_download_status ON downloads (status);
CREATE INDEX idx_download_parent_status ON downloads (parent_id, status);
CREATE INDEX idx_download_updated_at ON downloads (updated_at);
