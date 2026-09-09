"""Persistent repository for Telegram post ingestion and deduplication."""

import json
from datetime import datetime, timezone
from typing import List, Optional, Sequence, Union, overload

from tg_news_monitor.core.models import TelegramPost
from tg_news_monitor.storage.database import db_session, get_connection, init_db


class PostRepository:
    """Thread-safe SQLite repository managing deduplication and post lifecycle."""

    def __init__(self, db_path: str = ":memory:"):
        self.db_path = db_path
        init_db(self.db_path)

    @staticmethod
    def _normalize_channel(channel: str) -> str:
        return channel.lower().lstrip("@")

    def is_processed(self, channel: str, message_id: int) -> bool:
        """Returns True if the (channel, message_id) post has already been recorded."""
        channel_norm = self._normalize_channel(channel)
        with db_session(self.db_path) as conn:
            cursor = conn.execute(
                "SELECT 1 FROM posts WHERE channel = ? AND message_id = ? LIMIT 1",
                (channel_norm, message_id),
            )
            return cursor.fetchone() is not None

    def save_post(self, post: TelegramPost) -> bool:
        """Saves a post to the database. Returns True if inserted, False if duplicate."""
        channel_norm = self._normalize_channel(post.channel)
        scraped_at = datetime.now(timezone.utc).isoformat()
        media_urls_json = json.dumps(post.media_urls)

        with db_session(self.db_path) as conn:
            try:
                conn.execute(
                    """
                    INSERT INTO posts (
                        channel, message_id, published_at, scraped_at, text,
                        has_media, media_type, media_urls, direct_url, forward_from,
                        views, alert_sent
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0)
                    """,
                    (
                        channel_norm,
                        post.message_id,
                        post.published_at.isoformat(),
                        scraped_at,
                        post.text,
                        1 if post.has_media else 0,
                        post.media_type,
                        media_urls_json,
                        post.direct_url,
                        post.forward_from,
                        post.views,
                    ),
                )
                return True
            except Exception as e:
                # Catch sqlite3.IntegrityError for UNIQUE constraint violations
                if "UNIQUE constraint failed" in str(e):
                    return False
                raise

    def save_posts(self, posts: Sequence[TelegramPost]) -> List[TelegramPost]:
        """Batch-saves posts in a single transaction, returning the list of newly inserted posts."""
        if not posts:
            return []

        inserted: List[TelegramPost] = []
        scraped_at = datetime.now(timezone.utc).isoformat()

        with db_session(self.db_path) as conn:
            for post in posts:
                channel_norm = self._normalize_channel(post.channel)
                media_urls_json = json.dumps(post.media_urls)
                try:
                    conn.execute(
                        """
                        INSERT INTO posts (
                            channel, message_id, published_at, scraped_at, text,
                            has_media, media_type, media_urls, direct_url, forward_from,
                            views, alert_sent
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0)
                        """,
                        (
                            channel_norm,
                            post.message_id,
                            post.published_at.isoformat(),
                            scraped_at,
                            post.text,
                            1 if post.has_media else 0,
                            post.media_type,
                            media_urls_json,
                            post.direct_url,
                            post.forward_from,
                            post.views,
                        ),
                    )
                    inserted.append(post)
                except Exception as e:
                    if "UNIQUE constraint failed" in str(e):
                        continue
                    raise

        return inserted

    @overload
    def filter_unprocessed(self, posts: Sequence[TelegramPost]) -> List[TelegramPost]:
        ...

    @overload
    def filter_unprocessed(self, channel: str, posts: Sequence[TelegramPost]) -> List[TelegramPost]:
        ...

    def filter_unprocessed(
        self,
        channel_or_posts: Union[str, Sequence[TelegramPost]],
        posts: Optional[Sequence[TelegramPost]] = None,
    ) -> List[TelegramPost]:
        """Filters a sequence of posts, returning only those not yet present in SQLite."""
        if isinstance(channel_or_posts, str):
            target_posts = posts or []
        else:
            target_posts = channel_or_posts

        if not target_posts:
            return []

        # Group by channel for efficient queries
        by_channel: dict[str, list[TelegramPost]] = {}
        for p in target_posts:
            c = self._normalize_channel(p.channel)
            by_channel.setdefault(c, []).append(p)

        unprocessed: List[TelegramPost] = []
        with db_session(self.db_path) as conn:
            for ch, p_list in by_channel.items():
                ids = [p.message_id for p in p_list]
                placeholders = ",".join("?" for _ in ids)
                query = f"SELECT message_id FROM posts WHERE channel = ? AND message_id IN ({placeholders})"
                cursor = conn.execute(query, [ch] + ids)
                existing_ids = {row["message_id"] for row in cursor.fetchall()}
                for p in p_list:
                    if p.message_id not in existing_ids:
                        unprocessed.append(p)

        return unprocessed

    def update_evaluation(
        self,
        channel: str,
        message_id: int,
        score: int,
        summary: Optional[str] = None,
        alert_sent: bool = False,
        is_filtered: bool = False,
        filter_reason: Optional[str] = None,
        key_takeaways: Optional[List[str]] = None,
    ) -> None:
        """Updates Grok evaluation scores and summary bullets."""
        channel_norm = self._normalize_channel(channel)
        now_iso = datetime.now(timezone.utc).isoformat()
        takeaways_json = json.dumps(key_takeaways or [])

        with db_session(self.db_path) as conn:
            conn.execute(
                """
                UPDATE posts SET
                    score = ?,
                    summary = ?,
                    alert_sent = CASE WHEN ? THEN 1 ELSE alert_sent END,
                    is_filtered = ?,
                    filter_reason = ?,
                    key_takeaways = ?,
                    evaluated_at = ?,
                    updated_at = ?
                WHERE channel = ? AND message_id = ?
                """,
                (
                    score,
                    summary,
                    alert_sent,
                    1 if is_filtered else 0,
                    filter_reason,
                    takeaways_json,
                    now_iso,
                    now_iso,
                    channel_norm,
                    message_id,
                ),
            )

    def mark_alert_sent(
        self,
        channel: str,
        message_id: int,
        score: Optional[int] = None,
        summary: Optional[str] = None,
    ) -> None:
        """Marks that an interactive card alert has been dispatched to Feishu."""
        channel_norm = self._normalize_channel(channel)
        now_iso = datetime.now(timezone.utc).isoformat()

        with db_session(self.db_path) as conn:
            if score is not None or summary is not None:
                conn.execute(
                    """
                    UPDATE posts SET
                        alert_sent = 1,
                        alert_sent_at = ?,
                        score = COALESCE(?, score),
                        summary = COALESCE(?, summary),
                        updated_at = ?
                    WHERE channel = ? AND message_id = ?
                    """,
                    (now_iso, score, summary, now_iso, channel_norm, message_id),
                )
            else:
                conn.execute(
                    """
                    UPDATE posts SET
                        alert_sent = 1,
                        alert_sent_at = ?,
                        updated_at = ?
                    WHERE channel = ? AND message_id = ?
                    """,
                    (now_iso, now_iso, channel_norm, message_id),
                )

    def mark_alert_failed(
        self,
        channel: str,
        message_id: int,
        error_msg: str,
        permanent: bool = False,
    ) -> None:
        """Marks alert dispatch failure."""
        channel_norm = self._normalize_channel(channel)
        status = -1 if permanent else 0
        now_iso = datetime.now(timezone.utc).isoformat()

        with db_session(self.db_path) as conn:
            conn.execute(
                """
                UPDATE posts SET
                    alert_sent = ?,
                    alert_error = ?,
                    retry_count = retry_count + 1,
                    updated_at = ?
                WHERE channel = ? AND message_id = ?
                """,
                (status, error_msg, now_iso, channel_norm, message_id),
            )

    def get_post(self, channel: str, message_id: int) -> Optional[dict]:
        """Retrieves a single post record as a dict, or None if not found."""
        channel_norm = self._normalize_channel(channel)
        with db_session(self.db_path) as conn:
            cursor = conn.execute(
                "SELECT * FROM posts WHERE channel = ? AND message_id = ? LIMIT 1",
                (channel_norm, message_id),
            )
            row = cursor.fetchone()
            return dict(row) if row else None

    def count_posts(self, channel: Optional[str] = None) -> int:
        """Returns the total number of posts stored."""
        with db_session(self.db_path) as conn:
            if channel:
                cursor = conn.execute(
                    "SELECT COUNT(*) AS c FROM posts WHERE channel = ?",
                    (self._normalize_channel(channel),),
                )
            else:
                cursor = conn.execute("SELECT COUNT(*) AS c FROM posts")
            return cursor.fetchone()["c"]

    @staticmethod
    def _parse_iso_dt(value: Optional[str]) -> datetime:
        if not value:
            return datetime.fromtimestamp(0, timezone.utc)
        try:
            dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except Exception:
            return datetime.fromtimestamp(0, timezone.utc)
        if dt.tzinfo is None:
            return datetime.fromtimestamp(0, timezone.utc)
        return dt

    def _row_to_telegram_post(self, row) -> TelegramPost:
        """Reconstruct TelegramPost from a SQLite row."""
        media_raw = row["media_urls"] if "media_urls" in row.keys() else None
        try:
            media_urls = json.loads(media_raw) if media_raw else []
            if not isinstance(media_urls, list):
                media_urls = []
        except Exception:
            media_urls = []
        published_at = self._parse_iso_dt(
            row["published_at"] if "published_at" in row.keys() else None
        )
        return TelegramPost(
            channel=str(row["channel"]),
            message_id=int(row["message_id"]),
            published_at=published_at,
            text=row["text"] or "",
            has_media=bool(row["has_media"]) if "has_media" in row.keys() else False,
            media_type=row["media_type"] if "media_type" in row.keys() else None,
            direct_url=row["direct_url"]
            or f"https://t.me/{row['channel']}/{row['message_id']}",
            forward_from=row["forward_from"] if "forward_from" in row.keys() else None,
            media_urls=media_urls,
            views=row["views"] if "views" in row.keys() else None,
        )

    def list_pending_with_scraped_at(self) -> List[tuple]:
        """Return unevaluated posts as (TelegramPost, scraped_at) ordered by scraped_at ASC."""
        with db_session(self.db_path) as conn:
            cursor = conn.execute(
                """
                SELECT * FROM posts
                WHERE evaluated_at IS NULL
                ORDER BY scraped_at ASC, message_id ASC
                """
            )
            rows = cursor.fetchall()

        result: List[tuple] = []
        for row in rows:
            post = self._row_to_telegram_post(row)
            scraped_at = self._parse_iso_dt(
                row["scraped_at"] if "scraped_at" in row.keys() else None
            )
            result.append((post, scraped_at))
        return result

    def list_pending_posts(self) -> List[TelegramPost]:
        """Return all posts with evaluated_at IS NULL as TelegramPost models."""
        return [post for post, _ in self.list_pending_with_scraped_at()]


    def update_channel_state(
        self,
        channel: str,
        last_message_id: int = 0,
        success: bool = True,
        error: Optional[str] = None,
        messages_seen: int = 0,
    ) -> None:
        """Updates channel polling status and metrics."""
        channel_norm = self._normalize_channel(channel)
        now_iso = datetime.now(timezone.utc).isoformat()

        with db_session(self.db_path) as conn:
            conn.execute(
                """
                INSERT INTO channel_state (
                    channel, last_message_id, last_polled_at, last_success_at,
                    consecutive_errors, last_error, total_messages_seen, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(channel) DO UPDATE SET
                    last_message_id = CASE WHEN ? > last_message_id THEN ? ELSE last_message_id END,
                    last_polled_at = ?,
                    last_success_at = CASE WHEN ? THEN ? ELSE last_success_at END,
                    consecutive_errors = CASE WHEN ? THEN 0 ELSE consecutive_errors + 1 END,
                    last_error = ?,
                    total_messages_seen = total_messages_seen + ?,
                    updated_at = ?
                """,
                (
                    channel_norm,
                    last_message_id,
                    now_iso,
                    now_iso if success else None,
                    0 if success else 1,
                    error,
                    messages_seen,
                    now_iso,
                    # ON CONFLICT params
                    last_message_id,
                    last_message_id,
                    now_iso,
                    success,
                    now_iso,
                    success,
                    error,
                    messages_seen,
                    now_iso,
                ),
            )


# Alias for interface contract compatibility
Storage = PostRepository
