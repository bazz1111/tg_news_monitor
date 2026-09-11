"""SQLite database initialization and connection management."""

import os
import re
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Generator, List, Optional

from tg_news_monitor.config import DEFAULT_LEGACY_GROUP_ID

POSTS_RETENTION_DAYS = 90
DIGEST_CALLS_RETENTION_DAYS = 90
NIGHT_RETENTION_DAYS = 30

_SAFE_GROUP_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")


def safe_group_id(value: Optional[str]) -> str:
    text = str(value or "").strip() or DEFAULT_LEGACY_GROUP_ID
    if not _SAFE_GROUP_ID.match(text):
        return DEFAULT_LEGACY_GROUP_ID
    return text


DDL_SCHEMA = """
PRAGMA journal_mode = WAL;
PRAGMA synchronous = NORMAL;
PRAGMA busy_timeout = 5000;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS posts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    group_id TEXT NOT NULL DEFAULT 'legacy',
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

    CONSTRAINT uq_posts_group_channel_msgid UNIQUE(group_id, channel, message_id)
);

CREATE TABLE IF NOT EXISTS channel_state (
    group_id TEXT NOT NULL DEFAULT 'legacy',
    channel TEXT NOT NULL,
    last_message_id INTEGER DEFAULT 0,
    last_polled_at TEXT,
    last_success_at TEXT,
    consecutive_errors INTEGER DEFAULT 0,
    last_error TEXT,
    total_messages_seen INTEGER DEFAULT 0,
    updated_at TEXT DEFAULT (datetime('now', 'utc')),
    PRIMARY KEY (group_id, channel)
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_posts_group_channel_msgid ON posts(group_id, channel, message_id);
CREATE INDEX IF NOT EXISTS idx_posts_alert_sent ON posts(alert_sent, score);
CREATE INDEX IF NOT EXISTS idx_posts_published_at ON posts(published_at DESC);
CREATE INDEX IF NOT EXISTS idx_posts_group_pending ON posts(group_id, evaluated_at);
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


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (name,),
    ).fetchone()
    return row is not None


def _columns(conn: sqlite3.Connection, table: str) -> List[str]:
    return [row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()]


def _table_sql(conn: sqlite3.Connection, table: str) -> str:
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name=?",
        (table,),
    ).fetchone()
    return (row[0] or "") if row else ""


def _begin_immediate(conn: sqlite3.Connection) -> None:
    """Close any implicit transaction, then take an exclusive write lock."""
    conn.commit()
    conn.execute("BEGIN IMMEDIATE")


def _rebuild_posts(conn: sqlite3.Connection, default_gid: str) -> None:
    if not _table_exists(conn, "posts"):
        return
    cols = _columns(conn, "posts")
    sql = _table_sql(conn, "posts")
    has_new_unique = "uq_posts_group_channel_msgid" in sql or (
        "UNIQUE(group_id, channel, message_id)" in sql.replace(" ", "")
        or "UNIQUE (group_id, channel, message_id)" in sql
    )
    if "group_id" in cols and has_new_unique:
        return

    _begin_immediate(conn)
    try:
        conn.execute("DROP TABLE IF EXISTS posts_migrate")
        conn.execute(
            """
            CREATE TABLE posts_migrate (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                group_id TEXT NOT NULL DEFAULT 'legacy',
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
                score INTEGER,
                is_filtered INTEGER NOT NULL DEFAULT 0,
                filter_reason TEXT,
                summary TEXT,
                key_takeaways TEXT DEFAULT '[]',
                evaluated_at TEXT,
                alert_sent INTEGER NOT NULL DEFAULT 0,
                alert_sent_at TEXT,
                alert_error TEXT,
                retry_count INTEGER NOT NULL DEFAULT 0,
                created_at TEXT DEFAULT (datetime('now', 'utc')),
                updated_at TEXT DEFAULT (datetime('now', 'utc')),
                CONSTRAINT uq_posts_group_channel_msgid UNIQUE(group_id, channel, message_id)
            )
            """
        )
        gid_expr = "group_id" if "group_id" in cols else "?"
        params = () if "group_id" in cols else (default_gid,)
        conn.execute(
            f"""
            INSERT INTO posts_migrate (
                id, group_id, channel, message_id, published_at, scraped_at, text,
                has_media, media_type, media_urls, direct_url, forward_from, views,
                score, is_filtered, filter_reason, summary, key_takeaways, evaluated_at,
                alert_sent, alert_sent_at, alert_error, retry_count, created_at, updated_at
            )
            SELECT
                id, {gid_expr}, channel, message_id, published_at, scraped_at, text,
                has_media, media_type, media_urls, direct_url, forward_from, views,
                score, is_filtered, filter_reason, summary, key_takeaways, evaluated_at,
                alert_sent, alert_sent_at, alert_error, retry_count, created_at, updated_at
            FROM posts
            """,
            params,
        )
        conn.execute("DROP TABLE posts")
        conn.execute("ALTER TABLE posts_migrate RENAME TO posts")
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_posts_group_channel_msgid "
            "ON posts(group_id, channel, message_id)"
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_posts_alert_sent ON posts(alert_sent, score)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_posts_published_at ON posts(published_at DESC)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_posts_group_pending ON posts(group_id, evaluated_at)")
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def _rebuild_channel_state(conn: sqlite3.Connection, default_gid: str) -> None:
    if not _table_exists(conn, "channel_state"):
        return
    cols = _columns(conn, "channel_state")
    sql = _table_sql(conn, "channel_state")
    if "group_id" in cols and "PRIMARY KEY (group_id, channel)" in sql.replace("  ", " "):
        return
    # Also treat PRIMARY KEY(group_id, channel) without space as done
    compact = sql.replace(" ", "")
    if "group_id" in cols and "PRIMARYKEY(group_id,channel)" in compact:
        return

    _begin_immediate(conn)
    try:
        conn.execute("DROP TABLE IF EXISTS channel_state_migrate")
        conn.execute(
            """
            CREATE TABLE channel_state_migrate (
                group_id TEXT NOT NULL DEFAULT 'legacy',
                channel TEXT NOT NULL,
                last_message_id INTEGER DEFAULT 0,
                last_polled_at TEXT,
                last_success_at TEXT,
                consecutive_errors INTEGER DEFAULT 0,
                last_error TEXT,
                total_messages_seen INTEGER DEFAULT 0,
                updated_at TEXT DEFAULT (datetime('now', 'utc')),
                PRIMARY KEY (group_id, channel)
            )
            """
        )
        gid_expr = "group_id" if "group_id" in cols else "?"
        params = () if "group_id" in cols else (default_gid,)
        conn.execute(
            f"""
            INSERT OR IGNORE INTO channel_state_migrate (
                group_id, channel, last_message_id, last_polled_at, last_success_at,
                consecutive_errors, last_error, total_messages_seen, updated_at
            )
            SELECT {gid_expr}, channel, last_message_id, last_polled_at, last_success_at,
                   consecutive_errors, last_error, total_messages_seen, updated_at
            FROM channel_state
            """,
            params,
        )
        conn.execute("DROP TABLE channel_state")
        conn.execute("ALTER TABLE channel_state_migrate RENAME TO channel_state")
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def ensure_runtime_tables(conn: sqlite3.Connection, default_group_id: str = DEFAULT_LEGACY_GROUP_ID) -> None:
    """Create/migrate delivery, digest-budget, and quiet-hour tables (scoped by group_id)."""
    gid = safe_group_id(default_group_id)

    # digest_calls: add group_id if missing
    conn.execute(
        "CREATE TABLE IF NOT EXISTS digest_calls ("
        "started REAL NOT NULL, tokens INTEGER, group_id TEXT NOT NULL DEFAULT 'legacy')"
    )
    if _table_exists(conn, "digest_calls") and "group_id" not in _columns(conn, "digest_calls"):
        conn.execute(
            f"ALTER TABLE digest_calls ADD COLUMN group_id TEXT NOT NULL DEFAULT '{gid}'"
        )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_digest_calls_group_started "
        "ON digest_calls(group_id, started)"
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_digest_calls_started ON digest_calls(started)")

    def _tx(work):
        _begin_immediate(conn)
        try:
            work()
            conn.commit()
        except Exception:
            conn.rollback()
            raise

    # delivery_claims: PK (group_id, fingerprint)
    if _table_exists(conn, "delivery_claims"):
        cols = _columns(conn, "delivery_claims")
        sql = _table_sql(conn, "delivery_claims")
        compact = sql.replace(" ", "")
        needs_rebuild = "group_id" not in cols or "PRIMARYKEY(fingerprint)" in compact
        if needs_rebuild:
            def _work_claims() -> None:
                conn.execute("DROP TABLE IF EXISTS delivery_claims_migrate")
                conn.execute(
                    "CREATE TABLE delivery_claims_migrate ("
                    "group_id TEXT NOT NULL DEFAULT 'legacy', "
                    "fingerprint TEXT NOT NULL, "
                    "claimed REAL NOT NULL, "
                    "summary TEXT NOT NULL, "
                    "status TEXT NOT NULL DEFAULT 'unknown', "
                    "PRIMARY KEY(group_id, fingerprint))"
                )
                gid_expr = "group_id" if "group_id" in cols else "?"
                params = () if "group_id" in cols else (gid,)
                conn.execute(
                    f"INSERT OR IGNORE INTO delivery_claims_migrate "
                    f"(group_id, fingerprint, claimed, summary, status) "
                    f"SELECT {gid_expr}, fingerprint, claimed, summary, status FROM delivery_claims",
                    params,
                )
                conn.execute("DROP TABLE delivery_claims")
                conn.execute("ALTER TABLE delivery_claims_migrate RENAME TO delivery_claims")

            _tx(_work_claims)
    else:
        conn.execute(
            "CREATE TABLE delivery_claims ("
            "group_id TEXT NOT NULL DEFAULT 'legacy', "
            "fingerprint TEXT NOT NULL, "
            "claimed REAL NOT NULL, "
            "summary TEXT NOT NULL, "
            "status TEXT NOT NULL DEFAULT 'unknown', "
            "PRIMARY KEY(group_id, fingerprint))"
        )

    # alert_schedule_state: one row per group
    if _table_exists(conn, "alert_schedule_state"):
        cols = _columns(conn, "alert_schedule_state")
        if "group_id" not in cols:
            def _work_sched() -> None:
                conn.execute("DROP TABLE IF EXISTS alert_schedule_state_migrate")
                conn.execute(
                    "CREATE TABLE alert_schedule_state_migrate ("
                    "group_id TEXT PRIMARY KEY, "
                    "last_mode TEXT, "
                    "last_mode_at REAL, "
                    "quiet_window_id TEXT, "
                    "quiet_cards_sent INTEGER NOT NULL DEFAULT 0, "
                    "flush_date TEXT)"
                )
                conn.execute(
                    "INSERT INTO alert_schedule_state_migrate "
                    "(group_id, last_mode, last_mode_at, quiet_window_id, quiet_cards_sent, flush_date) "
                    "SELECT ?, last_mode, last_mode_at, quiet_window_id, quiet_cards_sent, flush_date "
                    "FROM alert_schedule_state",
                    (gid,),
                )
                conn.execute("DROP TABLE alert_schedule_state")
                conn.execute("ALTER TABLE alert_schedule_state_migrate RENAME TO alert_schedule_state")

            _tx(_work_sched)
    else:
        conn.execute(
            "CREATE TABLE alert_schedule_state ("
            "group_id TEXT PRIMARY KEY, "
            "last_mode TEXT, "
            "last_mode_at REAL, "
            "quiet_window_id TEXT, "
            "quiet_cards_sent INTEGER NOT NULL DEFAULT 0, "
            "flush_date TEXT)"
        )
    conn.execute(
        "INSERT OR IGNORE INTO alert_schedule_state(group_id, quiet_cards_sent) VALUES (?, 0)",
        (gid,),
    )

    # night_candidates
    if _table_exists(conn, "night_candidates"):
        cols = _columns(conn, "night_candidates")
        compact = _table_sql(conn, "night_candidates").replace(" ", "")
        if "group_id" not in cols or "PRIMARYKEY(day,channel,message_id)" in compact:
            def _work_night() -> None:
                conn.execute("DROP TABLE IF EXISTS night_candidates_migrate")
                conn.execute(
                    "CREATE TABLE night_candidates_migrate ("
                    "group_id TEXT NOT NULL, day TEXT, channel TEXT, message_id INTEGER, "
                    "post TEXT NOT NULL, item TEXT NOT NULL, "
                    "PRIMARY KEY(group_id, day, channel, message_id))"
                )
                gid_expr = "group_id" if "group_id" in cols else "?"
                params = () if "group_id" in cols else (gid,)
                conn.execute(
                    f"INSERT OR IGNORE INTO night_candidates_migrate "
                    f"(group_id, day, channel, message_id, post, item) "
                    f"SELECT {gid_expr}, day, channel, message_id, post, item FROM night_candidates",
                    params,
                )
                conn.execute("DROP TABLE night_candidates")
                conn.execute("ALTER TABLE night_candidates_migrate RENAME TO night_candidates")

            _tx(_work_night)
    else:
        conn.execute(
            "CREATE TABLE night_candidates ("
            "group_id TEXT NOT NULL, day TEXT, channel TEXT, message_id INTEGER, "
            "post TEXT NOT NULL, item TEXT NOT NULL, "
            "PRIMARY KEY(group_id, day, channel, message_id))"
        )

    # morning_reports
    if _table_exists(conn, "morning_reports"):
        cols = _columns(conn, "morning_reports")
        compact = _table_sql(conn, "morning_reports").replace(" ", "")
        if "group_id" not in cols or "PRIMARYKEY(day)" in compact:
            def _work_morning() -> None:
                conn.execute("DROP TABLE IF EXISTS morning_reports_migrate")
                conn.execute(
                    "CREATE TABLE morning_reports_migrate ("
                    "group_id TEXT NOT NULL, day TEXT, status TEXT NOT NULL, "
                    "PRIMARY KEY(group_id, day))"
                )
                gid_expr = "group_id" if "group_id" in cols else "?"
                params = () if "group_id" in cols else (gid,)
                conn.execute(
                    f"INSERT OR IGNORE INTO morning_reports_migrate (group_id, day, status) "
                    f"SELECT {gid_expr}, day, status FROM morning_reports",
                    params,
                )
                conn.execute("DROP TABLE morning_reports")
                conn.execute("ALTER TABLE morning_reports_migrate RENAME TO morning_reports")

            _tx(_work_morning)
    else:
        conn.execute(
            "CREATE TABLE morning_reports ("
            "group_id TEXT NOT NULL, day TEXT, status TEXT NOT NULL, "
            "PRIMARY KEY(group_id, day))"
        )

    # night_alerts
    if _table_exists(conn, "night_alerts"):
        cols = _columns(conn, "night_alerts")
        if "group_id" not in cols:
            def _work_alerts() -> None:
                conn.execute("DROP TABLE IF EXISTS night_alerts_migrate")
                conn.execute(
                    "CREATE TABLE night_alerts_migrate (group_id TEXT NOT NULL, claimed REAL NOT NULL)"
                )
                conn.execute(
                    "INSERT INTO night_alerts_migrate (group_id, claimed) "
                    "SELECT ?, claimed FROM night_alerts",
                    (gid,),
                )
                conn.execute("DROP TABLE night_alerts")
                conn.execute("ALTER TABLE night_alerts_migrate RENAME TO night_alerts")

            _tx(_work_alerts)
    else:
        conn.execute(
            "CREATE TABLE night_alerts (group_id TEXT NOT NULL, claimed REAL NOT NULL)"
        )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_night_alerts_group ON night_alerts(group_id, claimed)"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS runtime_heartbeat ("
        "name TEXT PRIMARY KEY, updated REAL NOT NULL)"
    )


def init_db(db_path: str, default_group_id: str = DEFAULT_LEGACY_GROUP_ID) -> None:
    """Initializes SQLite database tables and indices idempotently."""
    gid = safe_group_id(default_group_id)
    conn = get_connection(db_path)
    try:
        _rebuild_posts(conn, gid)
        _rebuild_channel_state(conn, gid)
        conn.executescript(DDL_SCHEMA)
        ensure_runtime_tables(conn, gid)
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


def record_heartbeat(db_path: str, name: str, now_ts: Optional[float] = None) -> None:
    import time

    ts = time.time() if now_ts is None else float(now_ts)
    with db_session(db_path) as conn:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS runtime_heartbeat ("
            "name TEXT PRIMARY KEY, updated REAL NOT NULL)"
        )
        conn.execute(
            "INSERT INTO runtime_heartbeat(name, updated) VALUES (?, ?) "
            "ON CONFLICT(name) DO UPDATE SET updated=excluded.updated",
            (name, ts),
        )


def get_heartbeat(db_path: str, name: str) -> Optional[float]:
    if not db_path or db_path == ":memory:":
        return None
    if db_path != ":memory:" and not os.path.exists(db_path):
        return None
    with db_session(db_path) as conn:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS runtime_heartbeat ("
            "name TEXT PRIMARY KEY, updated REAL NOT NULL)"
        )
        row = conn.execute(
            "SELECT updated FROM runtime_heartbeat WHERE name=?",
            (name,),
        ).fetchone()
    return float(row[0]) if row is not None else None


def purge_retention(db_path: str, now: Optional[datetime] = None) -> dict:
    """Delete aged evaluated posts and schedule/budget rows. Returns deleted counts."""
    clock = now or datetime.now(timezone.utc)
    posts_cut = (clock - timedelta(days=POSTS_RETENTION_DAYS)).isoformat()
    digest_cut = clock.timestamp() - DIGEST_CALLS_RETENTION_DAYS * 86400
    night_cut = clock.timestamp() - NIGHT_RETENTION_DAYS * 86400
    day_cut = (clock.date() - timedelta(days=NIGHT_RETENTION_DAYS)).isoformat()
    counts = {"posts": 0, "digest_calls": 0, "night_alerts": 0, "morning_reports": 0, "night_candidates": 0}
    with db_session(db_path) as conn:
        counts["posts"] = conn.execute(
            "DELETE FROM posts WHERE evaluated_at IS NOT NULL AND evaluated_at < ?",
            (posts_cut,),
        ).rowcount
        if _table_exists(conn, "digest_calls"):
            counts["digest_calls"] = conn.execute(
                "DELETE FROM digest_calls WHERE started < ?",
                (digest_cut,),
            ).rowcount
        if _table_exists(conn, "night_alerts"):
            counts["night_alerts"] = conn.execute(
                "DELETE FROM night_alerts WHERE claimed < ?",
                (night_cut,),
            ).rowcount
        if _table_exists(conn, "morning_reports"):
            counts["morning_reports"] = conn.execute(
                "DELETE FROM morning_reports WHERE day < ?",
                (day_cut,),
            ).rowcount
        if _table_exists(conn, "night_candidates"):
            counts["night_candidates"] = conn.execute(
                "DELETE FROM night_candidates WHERE day < ?",
                (day_cut,),
            ).rowcount
        try:
            conn.execute("PRAGMA optimize")
        except sqlite3.Error:
            pass
    return counts


class InstanceLock:
    """Single-instance lock next to the SQLite file (fcntl flock, or exclusive create)."""

    def __init__(self, lock_path: str) -> None:
        self.lock_path = lock_path
        self._fh = None

    def acquire(self) -> None:
        parent = os.path.dirname(os.path.abspath(self.lock_path))
        if parent and not os.path.exists(parent):
            os.makedirs(parent, exist_ok=True)
        self._fh = open(self.lock_path, "a+")
        try:
            import fcntl

            fcntl.flock(self._fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except ImportError:
            try:
                os.lockf(self._fh.fileno(), os.F_TLOCK, 0)
            except (AttributeError, OSError) as exc:
                self.release()
                raise RuntimeError(
                    f"another tg_news_monitor instance holds {self.lock_path}"
                ) from exc
        except OSError as exc:
            self.release()
            raise RuntimeError(
                f"another tg_news_monitor instance is already running "
                f"(lock file {self.lock_path})"
            ) from exc
        self._fh.seek(0)
        self._fh.truncate()
        self._fh.write(str(os.getpid()))
        self._fh.flush()

    def release(self) -> None:
        fh = self._fh
        self._fh = None
        if fh is None:
            return
        try:
            import fcntl

            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        except Exception:
            pass
        try:
            fh.close()
        except Exception:
            pass

    def __enter__(self) -> "InstanceLock":
        self.acquire()
        return self

    def __exit__(self, *exc) -> None:
        self.release()


def instance_lock_path(db_path: str) -> str:
    if not db_path or db_path == ":memory:":
        return os.path.join(os.getcwd(), "tg_news_monitor.lock")
    return f"{os.path.abspath(db_path)}.lock"
