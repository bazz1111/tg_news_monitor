"""Unit tests for NewsMonitorRunner in tg_news_monitor.core.runner."""

from __future__ import annotations

import shutil
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock

import pytest

from tg_news_monitor.config import Settings
from tg_news_monitor.core.models import NewsEvaluation, TelegramPost
from tg_news_monitor.core.runner import NewsMonitorRunner
from tg_news_monitor.storage.repository import PostRepository


# ==============================================================================
# Helpers & Mock Fixtures
# ==============================================================================

@contextmanager
def local_temp_db():
    tmp_path = Path(__file__).resolve().parent / f".tmp_runner_{uuid.uuid4().hex[:8]}"
    tmp_path.mkdir(parents=True, exist_ok=True)
    db_file = tmp_path / "test_runner.db"
    try:
        yield str(db_file)
    finally:
        shutil.rmtree(tmp_path, ignore_errors=True)


def make_sample_html(channel: str, posts_data: List[Dict[str, Any]]) -> str:
    """Generates standard Telegram web preview HTML fragment."""
    elements = []
    for p in posts_data:
        msg_id = p["message_id"]
        text = p.get("text", "")
        dt = p.get("published_at", "2026-09-08T21:00:00+00:00")
        is_service = p.get("is_service", False)
        service_class = "tgme_widget_message_service" if is_service else ""

        html = f"""
        <div class="tgme_widget_message {service_class}" data-post="{channel}/{msg_id}">
            <div class="tgme_widget_message_text">{text}</div>
            <time datetime="{dt}">Sep 8, 2026</time>
        </div>
        """
        elements.append(html)

    return f"<html><body>{''.join(elements)}</body></html>"


class MockScraper:
    def __init__(self, html_map: Optional[Dict[str, Optional[str]]] = None):
        self.html_map = html_map or {}
        self.fetch_calls: List[str] = []

    def fetch_channel_html(self, channel: str, before: Optional[int] = None) -> Optional[str]:
        self.fetch_calls.append(channel)
        return self.html_map.get(channel)

    def calculate_jittered_delay(self, interval: Optional[float] = None) -> float:
        return 0.01

    def calculate_inter_channel_delay(self, min_delay: float = 0.01, max_delay: float = 0.02) -> float:
        return 0.01


class MockEvaluator:
    def __init__(self, eval_map: Optional[Dict[int, NewsEvaluation]] = None, default_eval: Optional[NewsEvaluation] = None):
        self.eval_map = eval_map or {}
        self.default_eval = default_eval or NewsEvaluation(
            score=5,
            is_news=True,
            is_spam=False,
            title="Default Title",
            summary_bullets=["Summary item 1"],
            key_takeaways=["Takeaway 1"],
            category="行业快讯",
        )
        self.evaluated_posts: List[TelegramPost] = []

    def evaluate_post(self, post: TelegramPost) -> NewsEvaluation:
        self.evaluated_posts.append(post)
        return self.eval_map.get(post.message_id, self.default_eval)


class MockWebhookSender:
    def __init__(self, should_succeed: bool = True):
        self.should_succeed = should_succeed
        self.sent_payloads: List[Dict[str, Any]] = []

    def send(self, payload: Dict[str, Any]) -> bool:
        self.sent_payloads.append(payload)
        return self.should_succeed


# ==============================================================================
# Unit Tests for NewsMonitorRunner
# ==============================================================================

class TestNewsMonitorRunner:
    """Verifies end-to-end orchestration logic in NewsMonitorRunner."""

    def test_single_pass_threshold_gating_and_alert_dispatch(self):
        with local_temp_db() as db_path:
            channel = "whale_wire"
            posts_data = [
                {"message_id": 101, "text": "🚨 Breaking: Major regulatory approval announced!"},
                {"message_id": 102, "text": "Routine daily price chatter and community discussion."},
            ]
            html = make_sample_html(channel, posts_data)

            # Post 101: Score 9 >= 7 (Alert triggered)
            # Post 102: Score 4 < 7 (Filtered out)
            eval_map = {
                101: NewsEvaluation(
                    score=9,
                    is_news=True,
                    is_spam=False,
                    title="重磅监管获批公告",
                    summary_bullets=["监管机构正式批准首个指数ETF基金", "预计下月生效"],
                    key_takeaways=["对行业具有里程碑意义"],
                    category="突发快讯",
                ),
                102: NewsEvaluation(
                    score=4,
                    is_news=True,
                    is_spam=False,
                    title="每日闲聊日常",
                    summary_bullets=["社群日常行情讨论"],
                    key_takeaways=[],
                    category="日常聊天",
                ),
            }

            config = Settings(
                telegram_channels=[channel],
                hotness_threshold=7,
                db_path=db_path,
            )
            storage = PostRepository(db_path=db_path)
            scraper = MockScraper(html_map={channel: html})
            evaluator = MockEvaluator(eval_map=eval_map)
            webhook = MockWebhookSender(should_succeed=True)

            runner = NewsMonitorRunner(
                config=config,
                storage=storage,
                scraper_client=scraper,
                evaluator=evaluator,
                webhook_sender=webhook,
            )

            # Run single pass
            summary = runner.run_once()

            assert summary["channels_polled"] == 1
            assert summary["posts_discovered"] == 2
            assert summary["posts_evaluated"] == 2
            assert summary["alerts_sent"] == 1

            # Assert only 1 Feishu card was sent
            assert len(webhook.sent_payloads) == 1
            card_payload = webhook.sent_payloads[0]
            assert card_payload["msg_type"] == "interactive"
            assert "重磅监管获批公告" in str(card_payload)

            # Verify SQLite database state
            post_101 = storage.get_post(channel, 101)
            assert post_101 is not None
            assert post_101["score"] == 9
            assert post_101["alert_sent"] == 1

            post_102 = storage.get_post(channel, 102)
            assert post_102 is not None
            assert post_102["score"] == 4
            assert post_102["alert_sent"] == 0
            assert post_102["is_filtered"] == 1

    def test_spam_filtering_suppresses_alert(self):
        with local_temp_db() as db_path:
            channel = "crypto_alpha"
            posts_data = [
                {"message_id": 201, "text": "1000x GEM PRESALE IS LIVE! Free airdrop for first 50!"},
            ]
            html = make_sample_html(channel, posts_data)

            # High score 8, but is_spam = True
            eval_map = {
                201: NewsEvaluation(
                    score=8,
                    is_news=False,
                    is_spam=True,
                    title="广告代币推广",
                    summary_bullets=["推广垃圾代币项目预售"],
                    key_takeaways=[],
                    category="垃圾推广",
                )
            }

            config = Settings(
                telegram_channels=[channel],
                hotness_threshold=7,
                db_path=db_path,
            )
            storage = PostRepository(db_path=db_path)
            scraper = MockScraper(html_map={channel: html})
            evaluator = MockEvaluator(eval_map=eval_map)
            webhook = MockWebhookSender(should_succeed=True)

            runner = NewsMonitorRunner(
                config=config,
                storage=storage,
                scraper_client=scraper,
                evaluator=evaluator,
                webhook_sender=webhook,
            )

            summary = runner.run_once()
            assert summary["posts_discovered"] == 1
            assert summary["posts_evaluated"] == 1
            assert summary["alerts_sent"] == 0
            assert len(webhook.sent_payloads) == 0

            # Verify post recorded with is_filtered=1 and filter_reason='spam'
            record = storage.get_post(channel, 201)
            assert record is not None
            assert record["is_filtered"] == 1
            assert record["filter_reason"] == "spam"
            assert record["alert_sent"] == 0

    def test_deduplication_across_subsequent_runs(self):
        with local_temp_db() as db_path:
            channel = "news_chan"
            posts_data = [
                {"message_id": 301, "text": "Critical zero-day exploit discovered in library."},
            ]
            html = make_sample_html(channel, posts_data)

            eval_map = {
                301: NewsEvaluation(
                    score=10,
                    is_news=True,
                    is_spam=False,
                    title="严重漏洞披露",
                    summary_bullets=["发现远程代码执行0day漏洞"],
                    key_takeaways=["需立即升级安全补丁"],
                    category="安全预警",
                )
            }

            config = Settings(
                telegram_channels=[channel],
                hotness_threshold=7,
                db_path=db_path,
            )
            storage = PostRepository(db_path=db_path)
            scraper = MockScraper(html_map={channel: html})
            evaluator = MockEvaluator(eval_map=eval_map)
            webhook = MockWebhookSender(should_succeed=True)

            runner = NewsMonitorRunner(
                config=config,
                storage=storage,
                scraper_client=scraper,
                evaluator=evaluator,
                webhook_sender=webhook,
            )

            # Pass 1: Discovers and alerts
            res1 = runner.run_once()
            assert res1["posts_discovered"] == 1
            assert res1["alerts_sent"] == 1
            assert len(webhook.sent_payloads) == 1

            # Pass 2: Same HTML fed; should discover 0 new posts and trigger 0 alerts
            res2 = runner.run_once()
            assert res2["posts_discovered"] == 0
            assert res2["posts_evaluated"] == 0
            assert res2["alerts_sent"] == 0
            assert len(webhook.sent_payloads) == 1  # unchanged!

    def test_scraper_failure_resilience(self):
        with local_temp_db() as db_path:
            channel = "failing_channel"
            # Scraper returns None (network error or HTTP 429 backoff exhausted)
            scraper = MockScraper(html_map={channel: None})
            config = Settings(telegram_channels=[channel], db_path=db_path)
            storage = PostRepository(db_path=db_path)
            evaluator = MockEvaluator()
            webhook = MockWebhookSender()

            runner = NewsMonitorRunner(
                config=config,
                storage=storage,
                scraper_client=scraper,
                evaluator=evaluator,
                webhook_sender=webhook,
            )

            summary = runner.run_once()
            assert summary["channels_polled"] == 1
            assert summary["posts_discovered"] == 0
            assert summary["alerts_sent"] == 0
            assert summary["details"][0]["success"] is False

    def test_webhook_failure_handling(self):
        with local_temp_db() as db_path:
            channel = "alerts_channel"
            posts_data = [{"message_id": 401, "text": "High priority breaking news."}]
            html = make_sample_html(channel, posts_data)

            eval_map = {
                401: NewsEvaluation(
                    score=8,
                    is_news=True,
                    is_spam=False,
                    title="重要突发新闻",
                    summary_bullets=["要点1"],
                    key_takeaways=[],
                    category="突发快讯",
                )
            }

            config = Settings(telegram_channels=[channel], db_path=db_path)
            storage = PostRepository(db_path=db_path)
            scraper = MockScraper(html_map={channel: html})
            evaluator = MockEvaluator(eval_map=eval_map)
            # Webhook returns False (delivery failed)
            webhook = MockWebhookSender(should_succeed=False)

            runner = NewsMonitorRunner(
                config=config,
                storage=storage,
                scraper_client=scraper,
                evaluator=evaluator,
                webhook_sender=webhook,
            )

            summary = runner.run_once()
            assert summary["posts_discovered"] == 1
            assert summary["alerts_sent"] == 0  # Not marked as sent
            assert len(webhook.sent_payloads) == 1

            record = storage.get_post(channel, 401)
            assert record is not None
            assert record["alert_sent"] == 0

    def test_empty_channel_list(self):
        config = Settings(telegram_channels=[])
        runner = NewsMonitorRunner(config=config)
        summary = runner.run_once()
        assert summary["channels_polled"] == 0
        assert summary["posts_discovered"] == 0

    def test_stop_graceful_signal(self):
        config = Settings(telegram_channels=["chan_a"])
        runner = NewsMonitorRunner(config=config)
        assert runner._stop_requested is False
        runner.stop()
        assert runner._stop_requested is True

    def test_runner_initializes_with_deepseek_config(self):
        config = Settings(
            deepseek_api_key="sk-deepseek-unit-test",
            deepseek_model="deepseek-chat",
        )
        runner = NewsMonitorRunner(config=config)
        assert runner.evaluator.provider == "deepseek"
        assert runner.evaluator.api_key == "sk-deepseek-unit-test"
        assert runner.evaluator.api_base == "https://api.deepseek.com"
        assert runner.evaluator.model == "deepseek-chat"

