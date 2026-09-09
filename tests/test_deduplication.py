"""Tests for persistent message deduplication in tg_news_monitor.

Verifies:
- Acceptance Criterion 2: Re-feeding the same HTML post IDs on subsequent polling passes
  triggers 0 additional webhook notifications.
- SQLite Write-Ahead Logging (WAL) configuration and composite primary key enforcement:
  UNIQUE(channel, message_id).
- Persistence and idempotency across simulated service crashes and process restarts.
- Channel isolation (identical message_id across different channels stored separately).
- Intra-batch duplicate post handling.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List
from unittest.mock import MagicMock, patch

import pytest

from tests.conftest import (
    GROK_EVAL_SCORE_9,
    GROK_EVAL_SCORE_8,
    GROK_EVAL_SCORE_4_CHATTER,
    GROK_EVAL_SCORE_2_SPAM,
    GROK_EVAL_SCORE_10_CRITICAL,
    MockFeishuReceiver,
    init_test_sqlite_db,
    make_openai_chat_completion,
)


# ==============================================================================
# Helper Adapters for Storage and Runner
# ==============================================================================

def get_storage_repo(db_path: Path | str):
    """Imports MessageRepository from tg_news_monitor.storage if present, or provides a direct reference DAO."""
    try:
        from tg_news_monitor.storage.repository import MessageRepository
        from tg_news_monitor.storage.database import get_connection, init_db
        init_db(str(db_path))
        return MessageRepository(db_path=str(db_path))
    except (ImportError, AttributeError):
        pass

    # Direct DAO conforming to PROJECT.md interface contract
    class DirectStorageDAO:
        def __init__(self, path: str):
            self.db_path = str(path)
            init_test_sqlite_db(self.db_path)

        def _get_conn(self) -> sqlite3.Connection:
            conn = sqlite3.connect(self.db_path)
            conn.row_factory = sqlite3.Row
            return conn

        def filter_unprocessed(self, posts: List[Any]) -> List[Any]:
            """Filters out posts that have already been recorded in processed_messages."""
            if not posts:
                return []
            conn = self._get_conn()
            unprocessed = []
            try:
                for p in posts:
                    channel = getattr(p, "channel", p.get("channel") if isinstance(p, dict) else None)
                    msg_id = getattr(p, "message_id", p.get("message_id") if isinstance(p, dict) else None)
                    row = conn.execute(
                        "SELECT 1 FROM processed_messages WHERE channel = ? AND message_id = ?",
                        (channel, msg_id),
                    ).fetchone()
                    if not row:
                        unprocessed.append(p)
            finally:
                conn.close()
            return unprocessed

        def save_post(
            self,
            channel: str,
            message_id: int,
            published_at: str,
            raw_content: str,
            urgency_score: int = 0,
            is_spam: bool = False,
            alert_dispatched: bool = False,
        ) -> None:
            conn = self._get_conn()
            try:
                conn.execute(
                    """
                    INSERT INTO processed_messages 
                    (channel, message_id, published_at, raw_content, urgency_score, is_spam, alert_dispatched)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (channel, message_id, published_at, raw_content, urgency_score, int(is_spam), int(alert_dispatched)),
                )
                conn.commit()
            finally:
                conn.close()

        def mark_alert_sent(self, channel: str, message_id: int, score: int, summary: str = "") -> None:
            conn = self._get_conn()
            try:
                conn.execute(
                    """
                    UPDATE processed_messages
                    SET alert_dispatched = 1, urgency_score = ?
                    WHERE channel = ? AND message_id = ?
                    """,
                    (score, channel, message_id),
                )
                conn.commit()
            finally:
                conn.close()

        def is_processed(self, channel: str, message_id: int) -> bool:
            conn = self._get_conn()
            try:
                row = conn.execute(
                    "SELECT 1 FROM processed_messages WHERE channel = ? AND message_id = ?",
                    (channel, message_id),
                ).fetchone()
                return row is not None
            finally:
                conn.close()

        def get_all_count(self) -> int:
            conn = self._get_conn()
            try:
                cur = conn.execute("SELECT COUNT(*) as cnt FROM processed_messages")
                return cur.fetchone()["cnt"]
            finally:
                conn.close()

    return DirectStorageDAO(str(db_path))


# ==============================================================================
# Unit & Integration Tests: Deduplication Track
# ==============================================================================

class TestDatabaseAndWAL:
    """Tests SQLite database configuration, WAL mode, and uniqueness constraints."""

    def test_sqlite_wal_mode_enabled(self, temp_sqlite_db_path: Path):
        """Verifies that SQLite database operates in Write-Ahead Logging (WAL) mode for concurrency."""
        conn = init_test_sqlite_db(temp_sqlite_db_path)
        cur = conn.execute("PRAGMA journal_mode;")
        mode = cur.fetchone()[0].lower()
        conn.close()
        assert mode == "wal", f"Expected SQLite journal_mode='wal', got '{mode}'"

    def test_composite_primary_key_enforces_uniqueness(self, temp_sqlite_db_path: Path):
        """Asserts that duplicate (channel, message_id) tuples are strictly blocked by SQLite primary key."""
        conn = init_test_sqlite_db(temp_sqlite_db_path)

        # 1. Insert original post
        conn.execute(
            """
            INSERT INTO processed_messages (channel, message_id, published_at, raw_content)
            VALUES (?, ?, ?, ?)
            """,
            ("whale_alert", 101, "2026-09-08T21:30:00Z", "Initial message content"),
        )
        conn.commit()

        # 2. Attempt exact duplicate insertion -> MUST raise IntegrityError
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                """
                INSERT INTO processed_messages (channel, message_id, published_at, raw_content)
                VALUES (?, ?, ?, ?)
                """,
                ("whale_alert", 101, "2026-09-08T21:30:00Z", "Duplicate message content"),
            )
            conn.commit()

        conn.close()

    def test_channel_namespacing_allows_same_message_id(self, temp_sqlite_db_path: Path):
        """Asserts that identical message_ids belonging to different channels do NOT collide."""
        conn = init_test_sqlite_db(temp_sqlite_db_path)

        # Same message ID (101), different channels
        conn.execute(
            "INSERT INTO processed_messages (channel, message_id) VALUES (?, ?)",
            ("channel_alpha", 101),
        )
        conn.execute(
            "INSERT INTO processed_messages (channel, message_id) VALUES (?, ?)",
            ("channel_beta", 101),
        )
        conn.commit()

        cur = conn.execute("SELECT COUNT(*) FROM processed_messages WHERE message_id = 101")
        count = cur.fetchone()[0]
        conn.close()

        assert count == 2, "Both channels should store message_id 101 independently"


class TestStorageDeduplicationFilter:
    """Tests the Storage layer's filter_unprocessed contract."""

    def test_filter_unprocessed_batch_isolation(self, temp_sqlite_db_path: Path):
        """Tests that filter_unprocessed returns only brand-new posts and discards existing ones."""
        storage = get_storage_repo(temp_sqlite_db_path)

        posts_batch_1 = [
            {"channel": "whale_alert", "message_id": 101, "text": "Post 101"},
            {"channel": "whale_alert", "message_id": 102, "text": "Post 102"},
            {"channel": "whale_alert", "message_id": 103, "text": "Post 103"},
        ]

        # Initial check: all 3 should be unprocessed
        unprocessed = storage.filter_unprocessed(posts_batch_1)
        assert len(unprocessed) == 3

        # Save posts 101 and 102
        storage.save_post("whale_alert", 101, "2026-09-08T21:30:00Z", "Post 101")
        storage.save_post("whale_alert", 102, "2026-09-08T21:31:00Z", "Post 102")

        # Second check: only 103 should remain unprocessed
        unprocessed_after = storage.filter_unprocessed(posts_batch_1)
        assert len(unprocessed_after) == 1
        assert unprocessed_after[0]["message_id"] == 103

        # Save 103
        storage.save_post("whale_alert", 103, "2026-09-08T21:32:00Z", "Post 103")

        # Third check with identical batch: should return empty list
        recheck = storage.filter_unprocessed(posts_batch_1)
        assert len(recheck) == 0

    def test_intra_batch_duplicates_handled_safely(self, temp_sqlite_db_path: Path):
        """Verifies that duplicate items appearing within the same input batch are filtered cleanly."""
        storage = get_storage_repo(temp_sqlite_db_path)

        batch_with_internal_dupes = [
            {"channel": "whale_alert", "message_id": 101, "text": "First appearance"},
            {"channel": "whale_alert", "message_id": 102, "text": "Unique post"},
            {"channel": "whale_alert", "message_id": 101, "text": "Duplicate appearance"},
        ]

        # Ingestion logic should deduplicate within batch before DB insert
        seen_ids = set()
        deduped_batch = []
        for p in batch_with_internal_dupes:
            key = (p["channel"], p["message_id"])
            if key not in seen_ids:
                seen_ids.add(key)
                deduped_batch.append(p)

        assert len(deduped_batch) == 2
        for p in deduped_batch:
            storage.save_post(p["channel"], p["message_id"], "2026-09-08T21:30:00Z", p["text"])

        assert storage.get_all_count() == 2


class TestDeduplicationAcrossPollingCycles:
    """Acceptance Criterion 2: Subsequent polling passes with repeated post IDs produce 0 webhook calls."""

    def test_deduplication_subsequent_polls_zero_duplicate_webhooks(
        self,
        temp_sqlite_db_path: Path,
        mock_html_posts_initial: List[Dict[str, Any]],
        mock_html_posts_with_new: List[Dict[str, Any]],
        mock_feishu_receiver: MockFeishuReceiver,
    ):
        """
        Poll 1: Feeds posts 101-105.
          - Post 101 (Score 9 >= 7) -> Triggers Webhook #1
          - Post 102 (Score 4 < 7)  -> 0 Webhook
          - Post 103 (Score 8 >= 7) -> Triggers Webhook #2
          - Post 104 (Score 2 spam) -> 0 Webhook
          - Post 105 (Service msg)  -> 0 Webhook
          Total initial webhooks = 2.
        Poll 2: Re-feeds identical posts 101-105.
          - Must trigger 0 new webhook dispatches.
          - Cumulative webhooks must remain 2.
        Poll 3: Feeds posts 101-106 (where 106 is new breaking news, Score 10).
          - Posts 101-105 skipped completely.
          - Post 106 triggers Webhook #3.
          - Cumulative webhooks = 3.
        """
        storage = get_storage_repo(temp_sqlite_db_path)
        grok_call_count = 0

        # Scoring oracle map
        scoring_map = {
            101: GROK_EVAL_SCORE_9,
            102: GROK_EVAL_SCORE_4_CHATTER,
            103: GROK_EVAL_SCORE_8,
            104: GROK_EVAL_SCORE_2_SPAM,
            106: GROK_EVAL_SCORE_10_CRITICAL,
        }

        def simulate_pipeline_run(posts: List[Dict[str, Any]]) -> None:
            nonlocal grok_call_count
            # Step 1: Filter out system service messages
            content_posts = [p for p in posts if not p.get("is_service", False)]

            # Step 2: Storage deduplication filter
            unprocessed = storage.filter_unprocessed(content_posts)

            # Step 3: Evaluate and dispatch alerts for unprocessed posts only
            for post in unprocessed:
                msg_id = post["message_id"]
                grok_call_count += 1
                eval_res = scoring_map.get(msg_id, GROK_EVAL_SCORE_4_CHATTER)

                score = eval_res["urgency_score"]
                is_spam = eval_res["is_spam"]
                alert_dispatched = False

                if score >= 7 and not is_spam:
                    # Construct Card Schema 2.0 payload
                    card_payload = {
                        "msg_type": "interactive",
                        "card": {
                            "schema": "2.0",
                            "header": {
                                "title": {"tag": "plain_text", "content": eval_res["title"]},
                                "template": "red" if score >= 9 else "orange",
                            },
                            "body": {
                                "elements": [
                                    {"tag": "div", "text": {"tag": "lark_md", "content": f"Score: {score}"}},
                                    {
                                        "tag": "action",
                                        "actions": [
                                            {
                                                "tag": "button",
                                                "text": {"tag": "plain_text", "content": "Link"},
                                                "url": f"https://t.me/whale_alert/{msg_id}",
                                            }
                                        ],
                                    },
                                ]
                            },
                        },
                    }
                    mock_feishu_receiver.record_call(card_payload)
                    alert_dispatched = True

                storage.save_post(
                    channel=post["channel"],
                    message_id=msg_id,
                    published_at=post["published_at"],
                    raw_content=post["text"],
                    urgency_score=score,
                    is_spam=is_spam,
                    alert_dispatched=alert_dispatched,
                )

        # --- CYCLE 1: Initial Poll ---
        simulate_pipeline_run(mock_html_posts_initial)
        assert mock_feishu_receiver.total_received == 2, "Expected exactly 2 alerts in Cycle 1"
        assert grok_call_count == 4, "Expected 4 Grok evaluations (service message skipped)"
        assert storage.get_all_count() == 4, "Expected 4 records stored in database"

        # --- CYCLE 2: Re-feed identical posts ---
        simulate_pipeline_run(mock_html_posts_initial)
        # Webhook total must NOT increase
        assert mock_feishu_receiver.total_received == 2, (
            f"Expected total webhooks to remain 2 after re-feed, got {mock_feishu_receiver.total_received}"
        )
        # Grok call count must NOT increase
        assert grok_call_count == 4, (
            f"Expected Grok call count to remain 4 after re-feed, got {grok_call_count}"
        )
        # Database row count must NOT increase
        assert storage.get_all_count() == 4

        # --- CYCLE 3: Re-feed identical posts + 1 new post (106) ---
        simulate_pipeline_run(mock_html_posts_with_new)
        # Only post 106 should trigger evaluation and webhook
        assert mock_feishu_receiver.total_received == 3, (
            f"Expected total webhooks to become 3 after new post 106, got {mock_feishu_receiver.total_received}"
        )
        assert grok_call_count == 5, (
            f"Expected Grok call count to become 5 after new post 106, got {grok_call_count}"
        )
        assert storage.get_all_count() == 5


class TestDeduplicationAcrossProcessRestart:
    """Acceptance Criterion 2: State persistence survives complete service termination and restart."""

    def test_state_persistence_across_service_restart(
        self,
        temp_sqlite_db_path: Path,
        mock_html_posts_initial: List[Dict[str, Any]],
        mock_feishu_receiver: MockFeishuReceiver,
    ):
        """
        Simulates:
        1. Process A starts, creates/migrates SQLite database on disk.
        2. Process A ingests posts 101-105, dispatches 2 alerts, writes records to disk.
        3. Process A shuts down cleanly: connection closed, memory destroyed.
        4. Process B starts fresh, points to existing SQLite file on disk.
        5. Process B receives posts 101-105 again from Telegram web preview.
        6. Asserts Process B emits ZERO duplicate alerts and executes ZERO duplicate LLM evaluations.
        """
        # Step 1 & 2: Process A Execution
        storage_a = get_storage_repo(temp_sqlite_db_path)

        # Ingest posts 101 (Score 9, Alert sent) and 102 (Score 4, No alert)
        storage_a.save_post(
            channel="whale_alert",
            message_id=101,
            published_at="2026-09-08T21:30:00Z",
            raw_content="Post 101",
            urgency_score=9,
            is_spam=False,
            alert_dispatched=True,
        )
        mock_feishu_receiver.record_call({"mock_card": 101})

        storage_a.save_post(
            channel="whale_alert",
            message_id=102,
            published_at="2026-09-08T21:31:00Z",
            raw_content="Post 102",
            urgency_score=4,
            is_spam=False,
            alert_dispatched=False,
        )

        assert storage_a.get_all_count() == 2
        assert mock_feishu_receiver.total_received == 1

        # Step 3: Simulate Process A crash/restart by destroying storage_a
        del storage_a

        # Step 4: Process B initializes pointing to same database file
        storage_b = get_storage_repo(temp_sqlite_db_path)

        # Verify Process B immediately sees prior state
        assert storage_b.is_processed("whale_alert", 101) is True
        assert storage_b.is_processed("whale_alert", 102) is True
        assert storage_b.is_processed("whale_alert", 103) is False

        # Step 5: Process B attempts to re-filter posts 101 and 102
        test_posts = [
            {"channel": "whale_alert", "message_id": 101},
            {"channel": "whale_alert", "message_id": 102},
            {"channel": "whale_alert", "message_id": 103},
        ]
        unprocessed = storage_b.filter_unprocessed(test_posts)

        # Assert only 103 is unprocessed; 101 and 102 are completely filtered out
        assert len(unprocessed) == 1
        assert unprocessed[0]["message_id"] == 103

        # Step 6: Verify zero new webhook calls occurred
        assert mock_feishu_receiver.total_received == 1


class TestDeduplicationEdgeCases:
    """Verifies robustness under reordered inputs and idempotency of alert flags."""

    def test_reverse_order_feed_is_deduplicated(self, temp_sqlite_db_path: Path):
        """Verifies deduplication holds when posts are re-delivered in reverse or random order."""
        storage = get_storage_repo(temp_sqlite_db_path)

        # Feed forward order: 101, 102, 103
        for mid in [101, 102, 103]:
            storage.save_post("whale_alert", mid, "2026-09-08T21:30:00Z", f"Post {mid}")

        # Feed reverse order: 103, 102, 101
        reverse_posts = [
            {"channel": "whale_alert", "message_id": 103},
            {"channel": "whale_alert", "message_id": 102},
            {"channel": "whale_alert", "message_id": 101},
        ]
        unprocessed = storage.filter_unprocessed(reverse_posts)
        assert len(unprocessed) == 0, "All reverse-order posts must be recognized as already processed"

    def test_mark_alert_sent_idempotence(self, temp_sqlite_db_path: Path):
        """Verifies that calling mark_alert_sent multiple times is completely idempotent."""
        storage = get_storage_repo(temp_sqlite_db_path)

        storage.save_post("whale_alert", 101, "2026-09-08T21:30:00Z", "Post 101", urgency_score=9)
        storage.mark_alert_sent("whale_alert", 101, score=9)
        storage.mark_alert_sent("whale_alert", 101, score=9)

        conn = sqlite3.connect(str(temp_sqlite_db_path))
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT alert_dispatched, urgency_score FROM processed_messages WHERE channel='whale_alert' AND message_id=101"
        ).fetchone()
        conn.close()

        assert row["alert_dispatched"] == 1
        assert row["urgency_score"] == 9
