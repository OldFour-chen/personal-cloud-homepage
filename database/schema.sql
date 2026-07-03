CREATE TABLE likes (
    key TEXT PRIMARY KEY,
    label TEXT NOT NULL,
    count INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    nickname TEXT NOT NULL,
    content TEXT NOT NULL,
    reply TEXT DEFAULT '',
    created_at TEXT NOT NULL
);

CREATE TABLE media_assets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    filename TEXT NOT NULL,
    object_key TEXT NOT NULL,
    url TEXT NOT NULL,
    content_type TEXT,
    size INTEGER,
    category TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE content_blocks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    key TEXT NOT NULL UNIQUE,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE experiences (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    time_label TEXT NOT NULL,
    description TEXT NOT NULL,
    tags TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE experience_images (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    experience_id INTEGER NOT NULL,
    media_asset_id INTEGER,
    filename TEXT NOT NULL,
    object_key TEXT NOT NULL,
    url TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE skill_documents (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    skill_key TEXT NOT NULL,
    title TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    media_asset_id INTEGER,
    filename TEXT NOT NULL,
    object_key TEXT NOT NULL,
    url TEXT NOT NULL,
    created_at TEXT NOT NULL
);
