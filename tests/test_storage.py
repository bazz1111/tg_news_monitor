"""Unit tests for SQLite persistent deduplication storage and repository."""

import os
import shutil
import uuid
from datetime import datetime, timezone
import pytest

from tg_news_monitor.core.models import TelegramPost
from tg_news_monitor.storage.database import db_session, get_connection, init_db
from tg_news_monitor.storage.repository import PostRepository, Storage


@pytest.fixture
def temp_db():
    """Generates an isolated SQLite file path in a test-local temporary folder and cleans up afterward."""
    test_dir = os.path.abspath(f"tests/.tmp_{uuid.uuid4().hex}")
    os.makedirs(test_dir, exist_ok=True)
    db_file = os.path.join(test_dir, "test.db")
    try:
        yield db_file
    finally:
        import gc
        gc.collect()
        if os.path.exists(test_dir):
            shutil.rmtree(test_dir, ignore_errors=True)


def create_sample_post(channel: str = "test_channel", message_id: int = 100, text: str = "Sample post") -> TelegramPost:
    """Helper factory generating a valid TelegramPost instance."""
    return TelegramPost(
        channel=channel,
        message_id=message_id,
        published_at=datetime.now(timezone.utc),
        text=text,
        has_media=False,
        media_type=None,
        direct_url=f"https://t.me/{channel}/{message_id}",
        forward_from=None,
        views="1.2K",
    )


class TestDatabasePragmasAndInit:
    """Test suite verifying SQLite connection pragmas, WAL mode, and table initialization."""

    def test_wal_mode_and_pragmas(self, temp_db):
        init_db(temp_db)

        with get_connection(temp_db) as conn:
            # Check journal_mode = wal
            cursor = conn.execute("PRAGMA journal_mode;")
            journal_mode = cursor.fetchone()[0].lower()
            assert journal_mode == "wal"

            # Check synchronous = 1 (NORMAL)
            cursor = conn.execute("PRAGMA synchronous;")
            synchronous = cursor.fetchone()[0]
            assert synchronous == 1

            # Check busy_timeout = 5000
            cursor = conn.execute("PRAGMA busy_timeout;")
            busy_timeout = cursor.fetchone()[0]
            assert busy_timeout == 5000

    def test_table_initialization_idempotence(self, temp_db):
        # Calling init_db multiple times must not raise errors or corrupt schema
        init_db(temp_db)
        init_db(temp_db)

        with db_session(temp_db) as conn:
            cursor = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name IN ('posts', 'channel_state')"
            )
            tables = {row["name"] for row in cursor.fetchall()}
            assert "posts" in tables
            assert "channel_state" in tables


class TestPostRepositoryDeduplication:
    """Test suite verifying composite key deduplication, filtering, and transactions."""

    @pytest.fixture
    def repo(self, temp_db):
        return PostRepository(temp_db)

    def test_save_post_and_duplicate_prevention(self, repo):
        post = create_sample_post("news_channel", 1001, "First occurrence")

        # First save should succeed
        assert repo.save_post(post) is True
        assert repo.is_processed("news_channel", 1001) is True
        assert repo.count_posts() == 1

        # Second save of identical (channel, message_id) should be prevented
        assert repo.save_post(post) is False
        assert repo.count_posts() == 1

        # Different channel with same message_id is allowed (composite key test)
        post_diff_channel = create_sample_post("other_channel", 1001, "Different channel")
        assert repo.save_post(post_diff_channel) is True
        assert repo.count_posts() == 2

    def test_batch_save_posts(self, repo):
        posts = [
            create_sample_post("feed", 1, "Msg 1"),
            create_sample_post("feed", 2, "Msg 2"),
            create_sample_post("feed", 3, "Msg 3"),
        ]

        # First batch insert: all 3 inserted
        inserted = repo.save_posts(posts)
        assert len(inserted) == 3
        assert repo.count_posts() == 3

        # Re-feed with identical 3 posts + 1 new post (ID 4)
        new_post = create_sample_post("feed", 4, "Msg 4")
        refeed_posts = posts + [new_post]
        refeed_inserted = repo.save_posts(refeed_posts)

        # Only the new post should be inserted
        assert len(refeed_inserted) == 1
        assert refeed_inserted[0].message_id == 4
        assert repo.count_posts() == 4

    def test_filter_unprocessed_signature(self, repo):
        p1 = create_sample_post("channel_a", 10, "A10")
        p2 = create_sample_post("channel_a", 20, "A20")
        p3 = create_sample_post("channel_b", 30, "B30")

        # Initial check: all 3 unprocessed
        unprocessed = repo.filter_unprocessed([p1, p2, p3])
        assert len(unprocessed) == 3

        # Ingest p1 and p3
        repo.save_post(p1)
        repo.save_post(p3)

        # Re-check with list of all 3: only p2 should remain
        filtered = repo.filter_unprocessed([p1, p2, p3])
        assert len(filtered) == 1
        assert filtered[0].message_id == 20

        # Channel-scoped check: filter_unprocessed(channel, posts)
        filtered_channel_a = repo.filter_unprocessed("channel_a", [p1, p2])
        assert len(filtered_channel_a) == 1
        assert filtered_channel_a[0].message_id == 20


class TestPostLifecycleAndStateTracking:
    """Test suite verifying post evaluation updates, alert dispatch states, and channel metrics."""

    @pytest.fixture
    def repo(self, temp_db):
        return PostRepository(temp_db)

    def test_evaluation_and_alert_sent_lifecycle(self, repo):
        post = create_sample_post("breaking", 501, "Breaking news story")
        repo.save_post(post)

        record = repo.get_post("breaking", 501)
        assert record is not None
        assert record["alert_sent"] == 0
        assert record["score"] is None

        # Update evaluation result
        repo.update_evaluation(
            channel="breaking",
            message_id=501,
            score=9,
            summary="Grok structured summary",
            is_filtered=False,
            filter_reason=None,
            key_takeaways=["Major impact on market liquidity"],
        )

        record_after_eval = repo.get_post("breaking", 501)
        assert record_after_eval["score"] == 9
        assert record_after_eval["summary"] == "Grok structured summary"
        assert record_after_eval["evaluated_at"] is not None
        assert record_after_eval["alert_sent"] == 0  # Alert not dispatched yet

        # Mark alert successfully sent to Feishu
        repo.mark_alert_sent("breaking", 501)
        record_after_alert = repo.get_post("breaking", 501)
        assert record_after_alert["alert_sent"] == 1
        assert record_after_alert["alert_sent_at"] is not None

    def test_alert_failure_and_retry_tracking(self, repo):
        post = create_sample_post("alerts", 601, "Alert to fail")
        repo.save_post(post)

        # Transient error (retryable)
        repo.mark_alert_failed("alerts", 601, error_msg="HTTP 429 rate limited", permanent=False)
        rec1 = repo.get_post("alerts", 601)
        assert rec1["alert_sent"] == 0
        assert rec1["retry_count"] == 1
        assert "429" in rec1["alert_error"]

        # Permanent failure
        repo.mark_alert_failed("alerts", 601, error_msg="Invalid webhook token", permanent=True)
        rec2 = repo.get_post("alerts", 601)
        assert rec2["alert_sent"] == -1
        assert rec2["retry_count"] == 2

    def test_persistence_across_process_restart(self, temp_db):
        # Session 1: Create repository and ingest posts
        repo1 = PostRepository(temp_db)
        p1 = create_sample_post("crypto", 101, "Post 101")
        p2 = create_sample_post("crypto", 102, "Post 102")
        repo1.save_post(p1)
        repo1.save_post(p2)
        repo1.mark_alert_sent("crypto", 101, score=8, summary="Summary 101")
        del repo1

        # Session 2: Instantiate fresh repository pointing to the same SQLite file
        repo2 = PostRepository(temp_db)
        assert repo2.count_posts() == 2
        assert repo2.is_processed("crypto", 101) is True
        assert repo2.is_processed("crypto", 102) is True
        assert repo2.is_processed("crypto", 103) is False

        # Re-feeding posts 101 and 102 must yield 0 unprocessed posts
        unprocessed = repo2.filter_unprocessed([p1, p2])
        assert len(unprocessed) == 0

        # Verification of preserved lifecycle state
        rec = repo2.get_post("crypto", 101)
        assert rec["alert_sent"] == 1
        assert rec["score"] == 8
        assert rec["summary"] == "Summary 101"

    def test_channel_state_tracking(self, repo):
        repo.update_channel_state(
            channel="whale_alert",
            last_message_id=4500,
            success=True,
            messages_seen=20,
        )

        with db_session(repo.db_path) as conn:
            cursor = conn.execute("SELECT * FROM channel_state WHERE channel = 'whale_alert'")
            row = cursor.fetchone()
            assert row is not None
            assert row["last_message_id"] == 4500
            assert row["consecutive_errors"] == 0
            assert row["total_messages_seen"] == 20

        # Simulate error update
        repo.update_channel_state(
            channel="whale_alert",
            success=False,
            error="Connection timed out",
        )

        with db_session(repo.db_path) as conn:
            cursor = conn.execute("SELECT * FROM channel_state WHERE channel = 'whale_alert'")
            row = cursor.fetchone()
            assert row["consecutive_errors"] == 1
            assert row["last_error"] == "Connection timed out"
