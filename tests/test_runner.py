"""Unit tests for NewsMonitorRunner in tg_news_monitor.core.runner."""

from __future__ import annotations

import shutil
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock

import pytest

from tg_news_monitor.config import Settings
from tg_news_monitor.core.models import DigestBrief, DigestItem, NewsEvaluation, TelegramPost
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
        dt = p.get("published_at", datetime.now(timezone.utc).isoformat())
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
    def __init__(
        self,
        html_map: Optional[Dict[str, Optional[str]]] = None,
        page_map: Optional[Dict[tuple, Optional[str]]] = None,
    ):
        self.html_map = html_map or {}
        self.page_map = page_map or {}
        self.fetch_calls: List[str] = []
        self.fetch_args: List[tuple] = []

    def fetch_channel_html(self, channel: str, before: Optional[int] = None) -> Optional[str]:
        self.fetch_calls.append(channel)
        self.fetch_args.append((channel, before))
        key = (channel.lower().lstrip("@"), before)
        if key in self.page_map:
            return self.page_map[key]
        if before is not None:
            return None
        return self.html_map.get(channel)

    def calculate_jittered_delay(self, interval: Optional[float] = None) -> float:
        return 0.01

    def calculate_inter_channel_delay(self, min_delay: float = 0.01, max_delay: float = 0.02) -> float:
        return 0.01


class MockEvaluator:
    """Supports batch digest mode via evaluate_digest; keeps evaluate_post for compat."""

    def __init__(
        self,
        eval_map: Optional[Dict[int, NewsEvaluation]] = None,
        default_eval: Optional[NewsEvaluation] = None,
        digest: Optional[DigestBrief] = None,
        digest_builder: Optional[Any] = None,
    ):
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
        self.digest = digest
        self.digest_builder = digest_builder
        self.evaluated_posts: List[TelegramPost] = []
        self.digest_batches: List[List[TelegramPost]] = []

    def evaluate_post(self, post: TelegramPost) -> NewsEvaluation:
        self.evaluated_posts.append(post)
        return self.eval_map.get(post.message_id, self.default_eval)

    def evaluate_digest(self, posts: List[TelegramPost]) -> DigestBrief:
        self.digest_batches.append(list(posts))
        self.evaluated_posts.extend(posts)
        if self.digest_builder is not None:
            return self.digest_builder(posts)
        if self.digest is not None:
            return self.digest
        # Default: convert high-score non-spam eval_map entries into digest items
        items: List[DigestItem] = []
        for post in posts:
            ev = self.eval_map.get(post.message_id, self.default_eval)
            if getattr(ev, "is_spam", False) or not getattr(ev, "is_news", True):
                continue
            if getattr(ev, "score", 0) < 7:
                continue
            items.append(
                DigestItem(
                    rank=len(items) + 1,
                    channel=post.channel,
                    message_id=post.message_id,
                    event_at=post.published_at,
                    title=ev.title,
                    summary="; ".join(ev.summary_bullets or [ev.title]),
                    category=ev.category or "行业快讯",
                    impact_overall="总体影响中等",
                    impact_us="美股影响有限",
                    impact_cn="A股影响有限",
                    impact_commodities="大宗商品影响有限",
                )
            )
            if len(items) >= 5:
                break
        return DigestBrief(
            headline="本轮测试汇总" if items else "本轮无实质新闻",
            overview="单元测试自动生成的 digest",
            items=items,
            has_material_news=bool(items),
            filtered_note=None if items else "digest_empty",
        )


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
                digest_min_candidates=1,
                digest_max_wait_seconds=0,
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

            # Assert 1 Feishu single-item card was sent (multi-card mode, 1 material item)
            assert len(evaluator.digest_batches) == 1
            assert len(webhook.sent_payloads) == 1
            card_payload = webhook.sent_payloads[0]
            assert card_payload["msg_type"] == "interactive"
            assert "重磅监管获批公告" in str(card_payload)
            # Item cards must not include Telegram original-link buttons or t.me traces
            assert "查看 Telegram" not in str(card_payload)
            assert "https://t.me/whale_wire/101" not in str(card_payload)

            # Verify SQLite database state (batch digest: selected item score from rank)
            post_101 = storage.get_post(channel, 101)
            assert post_101 is not None
            # rank=1 -> score 10 in batch digest mode (was per-post score 9)
            assert post_101["score"] >= 7
            assert post_101["alert_sent"] == 1

            post_102 = storage.get_post(channel, 102)
            assert post_102 is not None
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
                digest_min_candidates=1,
                digest_max_wait_seconds=0,
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
            # Coarse filter drops clear airdrop/presale spam before LLM
            assert summary["posts_evaluated"] == 0
            assert summary["alerts_sent"] == 0
            assert len(webhook.sent_payloads) == 0
            assert len(evaluator.digest_batches) == 0

            # Verify post recorded with is_filtered=1 via coarse_filter
            record = storage.get_post(channel, 201)
            assert record is not None
            assert record["is_filtered"] == 1
            assert record["filter_reason"] in {"spam", "coarse_filter", "crypto_filter", "digest_empty", "digest_filtered", "digest_not_selected"}
            assert record["alert_sent"] == 0

    def test_topic_filter_marks_reason_and_skips_llm(self, tmp_path):
        policy = tmp_path / "topic_filters.yaml"
        policy.write_text(
            "default:\n  enabled: true\n  categories:\n    demo:\n      - RESTRICTED_WIDGET_TOKEN\n",
            encoding="utf-8",
        )
        from tg_news_monitor.core.filters import reset_topic_filter_cache

        reset_topic_filter_cache()
        with local_temp_db() as db_path:
            channel = "asia_wire"
            posts_data = [
                {
                    "message_id": 401,
                    "text": "desk note mentions RESTRICTED_WIDGET_TOKEN inside a longer wire",
                },
            ]
            html = make_sample_html(channel, posts_data)
            config = Settings(
                telegram_channels=[channel],
                hotness_threshold=7,
                db_path=db_path,
                digest_min_candidates=1,
                digest_max_wait_seconds=0,
                topic_filters_path=str(policy),
            )
            storage = PostRepository(db_path=db_path)
            scraper = MockScraper(html_map={channel: html})
            evaluator = MockEvaluator()
            webhook = MockWebhookSender(should_succeed=True)
            runner = NewsMonitorRunner(
                config=config,
                storage=storage,
                scraper_client=scraper,
                evaluator=evaluator,
                webhook_sender=webhook,
            )
            summary = runner.run_once()
            assert summary["posts_evaluated"] == 0
            assert summary["alerts_sent"] == 0
            assert evaluator.digest_batches == []
            record = storage.get_post(channel, 401)
            assert record is not None
            assert record["is_filtered"] == 1
            assert record["filter_reason"] == "topic_filter"
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
                digest_min_candidates=1,
                digest_max_wait_seconds=0,
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
            config = Settings(telegram_channels=[channel], db_path=db_path,
                digest_min_candidates=1,
                digest_max_wait_seconds=0,
            )
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

            config = Settings(telegram_channels=[channel], db_path=db_path,
                digest_min_candidates=1,
                digest_max_wait_seconds=0,
            )
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

    def test_runner_uses_codebuddy_quality_defaults(self):
        runner = NewsMonitorRunner(config=Settings(codebuddy_api_key="cb-unit-test"))
        assert runner.evaluator.model == "deepseek-v4.1-flash"
        assert runner.evaluator.fallback_model == "hy3"
        assert runner.evaluator.timeout == 300.0
        assert runner.evaluator.effort == "max"
        assert runner.evaluator.autocompact == "auto"

    def test_runner_initializes_with_codebuddy_config(self):
        config = Settings(
            codebuddy_api_key="cb-unit-test",
            codebuddy_model="fast-model",
            codebuddy_fallback_model="hy3",
            codebuddy_timeout=420.0,
            codebuddy_effort="high",
            codebuddy_autocompact="aggressive",
        )
        runner = NewsMonitorRunner(config=config)
        assert runner.evaluator.provider == "codebuddy"
        assert runner.evaluator.api_key == "cb-unit-test"
        assert runner.evaluator.model == "fast-model"
        assert runner.evaluator.fallback_model == "hy3"
        assert runner.evaluator.timeout == 420.0
        assert runner.evaluator.effort == "high"
        assert runner.evaluator.autocompact == "aggressive"

    def test_buffering_skips_llm_when_below_min_candidates(self):
        """With default min_candidates=3, a single pending post stays unevaluated."""
        with local_temp_db() as db_path:
            channel = "buffer_chan"
            posts_data = [
                {"message_id": 501, "text": "Federal Reserve hints at unexpected policy shift this quarter."},
            ]
            html = make_sample_html(channel, posts_data)
            config = Settings(
                telegram_channels=[channel],
                db_path=db_path,
                digest_min_candidates=3,
                digest_max_wait_seconds=900,
            )
            storage = PostRepository(db_path=db_path)
            scraper = MockScraper(html_map={channel: html})
            evaluator = MockEvaluator()
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
            assert summary["posts_evaluated"] == 0
            assert summary["alerts_sent"] == 0
            assert len(evaluator.digest_batches) == 0
            assert len(webhook.sent_payloads) == 0
            # Still pending
            pending = storage.list_pending_posts()
            assert len(pending) == 1
            assert pending[0].message_id == 501

    def test_multi_item_sends_one_card_per_digest_item(self):
        """Material digest with 2 items should dispatch 2 single cards."""
        with local_temp_db() as db_path:
            channel = "multi_chan"
            posts_data = [
                {"message_id": 601, "text": "Major bank announces unexpected rate cut affecting global markets today."},
                {"message_id": 602, "text": "Tech giant unveils breakthrough chip architecture for AI training workloads."},
                {"message_id": 603, "text": "Oil prices surge after supply disruption in key producing region overnight."},
            ]
            html = make_sample_html(channel, posts_data)

            def builder(posts):
                items = []
                for i, post in enumerate(posts[:2], start=1):
                    items.append(
                        DigestItem(
                            rank=i,
                            channel=post.channel,
                            message_id=post.message_id,
                    event_at=post.published_at,
                            title=f"单卡测试#{i}",
                            summary=f"摘要{i}",
                            category="宏观快讯",
                            impact_overall="总体影响显著",
                            impact_us="美股短线波动",
                            impact_cn="A股情绪升温",
                            impact_commodities="大宗跟随",
                        )
                    )
                return DigestBrief(
                    headline="双卡测试",
                    overview="两则实质新闻",
                    items=items,
                    has_material_news=True,
                )

            config = Settings(
                telegram_channels=[channel],
                db_path=db_path,
                digest_min_candidates=3,
                digest_max_wait_seconds=0,
                digest_card_interval_seconds=0,
            )
            storage = PostRepository(db_path=db_path)
            scraper = MockScraper(html_map={channel: html})
            evaluator = MockEvaluator(digest_builder=builder)
            webhook = MockWebhookSender(should_succeed=True)
            runner = NewsMonitorRunner(
                config=config,
                storage=storage,
                scraper_client=scraper,
                evaluator=evaluator,
                webhook_sender=webhook,
            )
            summary = runner.run_once()
            assert summary["posts_discovered"] == 3
            assert summary["posts_evaluated"] == 3
            assert summary["alerts_sent"] == 2
            assert len(webhook.sent_payloads) == 2
            joined = str(webhook.sent_payloads)
            assert "单卡测试#1" in joined
            assert "单卡测试#2" in joined
            assert "查看 Telegram" not in joined

    def test_news_max_age_zero_sends_stale_published_at(self):
        with local_temp_db() as db_path:
            now = datetime.now(timezone.utc)
            stale = now - timedelta(days=10)
            channel = "wire"
            repo = PostRepository(db_path=db_path)
            repo.save_posts(
                [
                    TelegramPost(
                        channel=channel,
                        message_id=77,
                        text="Federal Reserve hints at unexpected policy shift this quarter.",
                        direct_url="https://t.me/wire/77",
                        published_at=stale,
                    )
                ]
            )
            config = Settings(
                telegram_channels=[channel],
                db_path=db_path,
                news_max_age_seconds=0,
                digest_min_candidates=1,
                digest_max_wait_seconds=0,
                digest_min_interval_seconds=0,
                digest_card_interval_seconds=0,
            )
            evaluator = MockEvaluator(
                eval_map={
                    77: NewsEvaluation(
                        score=9,
                        is_news=True,
                        is_spam=False,
                        title="政策转向",
                        summary_bullets=["联储释放政策转向信号"],
                        key_takeaways=["关注后续声明"],
                        category="宏观快讯",
                    )
                }
            )
            webhook = MockWebhookSender()
            runner = NewsMonitorRunner(
                config=config,
                storage=repo,
                scraper_client=MockScraper(),
                evaluator=evaluator,
                webhook_sender=webhook,
            )
            summary = runner.process_pending(now=now)
            assert summary["alerts_sent"] == 1
            assert len(webhook.sent_payloads) == 1
            record = repo.get_post(channel, 77)
            assert record["alert_sent"] == 1
            assert record.get("filter_reason") in {None, ""}

