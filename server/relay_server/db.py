"""Database schema and connection management for relay index."""

import os
import sqlite3
from pathlib import Path

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA busy_timeout=5000;

CREATE TABLE IF NOT EXISTS indexed_files (
    path TEXT PRIMARY KEY,
    size INTEGER NOT NULL,
    byte_offset INTEGER NOT NULL,
    indexed_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sessions (
    session_id TEXT PRIMARY KEY,
    project_dir TEXT,
    slug TEXT,
    first_timestamp TEXT,
    last_timestamp TEXT,
    message_count INTEGER DEFAULT 0,
    git_branch TEXT,
    cwd TEXT
);

CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    timestamp TEXT NOT NULL,
    model TEXT,
    FOREIGN KEY (session_id) REFERENCES sessions(session_id)
);

CREATE INDEX IF NOT EXISTS idx_messages_session_id ON messages(session_id);
CREATE INDEX IF NOT EXISTS idx_messages_timestamp ON messages(timestamp);
CREATE INDEX IF NOT EXISTS idx_sessions_project_dir ON sessions(project_dir);

CREATE TABLE IF NOT EXISTS message_tags (
    message_id INTEGER NOT NULL,
    tag TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT 'auto',
    PRIMARY KEY (message_id, tag),
    FOREIGN KEY (message_id) REFERENCES messages(id)
);

CREATE TABLE IF NOT EXISTS session_tags (
    session_id TEXT NOT NULL,
    tag TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT 'auto',
    PRIMARY KEY (session_id, tag),
    FOREIGN KEY (session_id) REFERENCES sessions(session_id)
);

CREATE INDEX IF NOT EXISTS idx_message_tags_tag ON message_tags(tag);
CREATE INDEX IF NOT EXISTS idx_session_tags_tag ON session_tags(tag);

CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(
    content,
    content=messages,
    content_rowid=id
);

CREATE TABLE IF NOT EXISTS session_hints (
    id INTEGER PRIMARY KEY,
    session_id TEXT NOT NULL,
    timestamp TEXT NOT NULL,
    source_file TEXT NOT NULL UNIQUE,
    workstream TEXT NOT NULL,
    summary TEXT NOT NULL,
    decisions TEXT,
    FOREIGN KEY (session_id) REFERENCES sessions(session_id)
);
CREATE INDEX IF NOT EXISTS idx_session_hints_session ON session_hints(session_id);
CREATE INDEX IF NOT EXISTS idx_session_hints_workstream ON session_hints(workstream);
"""

# Triggers must be created separately — they fail inside executescript
# if the FTS table already exists from a prior run with different triggers.
TRIGGERS = [
    """
    CREATE TRIGGER IF NOT EXISTS messages_ai AFTER INSERT ON messages BEGIN
        INSERT INTO messages_fts(rowid, content) VALUES (new.id, new.content);
    END;
    """,
    """
    CREATE TRIGGER IF NOT EXISTS messages_ad AFTER DELETE ON messages BEGIN
        INSERT INTO messages_fts(messages_fts, rowid, content)
            VALUES('delete', old.id, old.content);
    END;
    """,
]


def get_db_path() -> Path:
    """Return the database path, respecting XDG_DATA_HOME."""
    data_home = os.environ.get("XDG_DATA_HOME", os.path.expanduser("~/.local/share"))
    db_dir = Path(data_home) / "relay"
    db_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    return db_dir / "index.db"


def get_connection(db_path: Path | str) -> sqlite3.Connection:
    """Open a connection with WAL mode and busy timeout."""
    conn = sqlite3.connect(str(db_path), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def ensure_schema(db_path: Path | str) -> None:
    """Create tables and indexes if they don't exist."""
    conn = get_connection(db_path)
    try:
        conn.executescript(SCHEMA)
        for trigger_sql in TRIGGERS:
            conn.execute(trigger_sql)
        conn.commit()
    finally:
        conn.close()


def decode_project_dir(dirname: str) -> str:
    """Decode a Claude Code project directory name to a filesystem path.

    Claude encodes project paths by replacing '/' with '-' and prepending '-'.
    Example: '-home-matt-src-personal-squadkeeper' -> '/home/matt/src/personal/squadkeeper'
    """
    if not dirname.startswith("-"):
        return dirname
    # Strip leading dash, replace remaining dashes with slashes
    # But we need to be smarter: dashes within directory names are ambiguous.
    # Claude uses the full absolute path, so it always starts with /home or /Users etc.
    return "/" + dirname[1:].replace("-", "/")
