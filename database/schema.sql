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
CREATE TABLE sqlite_sequence(name,seq);
