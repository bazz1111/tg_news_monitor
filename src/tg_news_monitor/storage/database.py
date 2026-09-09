"""SQLite database initialization and connection management."""

import os
import sqlite3
from contextlib import contextmanager
from typing import Generator


DDL_SCHEMA = """
PRAGMA journal_mode = WAL;
PRAGMA synchronous = NORMAL;
PRAGMA busy_timeout = 5000;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS posts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    channel TEXT NOT NULL,
    message_id INTEGER NOT NULL,
    published_at TEXT NOT NULL,
    scraped_at TEXT NOT NULL,
    text TEXT NOT NULL,
    has_media INTEGER NOT NULL DEFAULT 0,
    media_type TEXT,
    media_urls TEXT DEFAULT '[]',
    direct_url TEXT NOT NULL,
    forward_from TEXT,
    views TEXT,
    
    -- Evaluation outputs
    score INTEGER,
    is_filtered INTEGER NOT NULL DEFAULT 0,
    filter_reason TEXT,
    summary TEXT,
    key_takeaways TEXT DEFAULT '[]',
    evaluated_at TEXT,
    
    -- Alert status (0 = unalerted/pending, 1 = alert sent, -1 = permanent failure)
    alert_sent INTEGER NOT NULL DEFAULT 0,
    alert_sent_at TEXT,
    alert_error TEXT,
    retry_count INTEGER NOT NULL DEFAULT 0,
    
    created_at TEXT DEFAULT (datetime('now', 'utc')),
    updated_at TEXT DEFAULT (datetime('now', 'utc')),
    
    CONSTRAINT uq_posts_channel_msgid UNIQUE(channel, message_id)
);

CREATE TABLE IF NOT EXISTS channel_state (
    channel TEXT PRIMARY KEY,
    last_message_id INTEGER DEFAULT 0,
    last_polled_at TEXT,
    last_success_at TEXT,
    consecutive_errors INTEGER DEFAULT 0,
    last_error TEXT,
    total_messages_seen INTEGER DEFAULT 0,
    updated_at TEXT DEFAULT (datetime('now', 'utc'))
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_posts_channel_msgid ON posts(channel, message_id);
CREATE INDEX IF NOT EXISTS idx_posts_alert_sent ON posts(alert_sent, score);
CREATE INDEX IF NOT EXISTS idx_posts_published_at ON posts(published_at DESC);
"""


def get_connection(db_path: str, timeout: float = 5.0) -> sqlite3.Connection:
    """Creates a new SQLite connection configured with WAL mode and pragmas."""
    if db_path != ":memory:":
        parent_dir = os.path.dirname(os.path.abspath(db_path))
        if parent_dir and not os.path.exists(parent_dir):
            os.makedirs(parent_dir, exist_ok=True)

    conn = sqlite3.connect(db_path, timeout=timeout)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL;")
    conn.execute("PRAGMA synchronous = NORMAL;")
    conn.execute("PRAGMA busy_timeout = 5000;")
    conn.execute("PRAGMA foreign_keys = ON;")
    return conn


def init_db(db_path: str) -> None:
    """Initializes SQLite database tables and indices idempotently."""
    conn = get_connection(db_path)
    try:
        conn.executescript(DDL_SCHEMA)
        conn.commit()
    finally:
        conn.close()


@contextmanager
def db_session(db_path: str) -> Generator[sqlite3.Connection, None, None]:
    """Context manager yielding a thread-safe connection with auto-commit/rollback."""
    conn = get_connection(db_path)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
