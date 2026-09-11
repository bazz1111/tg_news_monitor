"""Peer-equal multi-group config, isolation, and schema migration."""

from __future__ import annotations

from datetime import datetime, timezone
from threading import Event, Thread
from zoneinfo import ZoneInfo

import pytest
from pydantic import ValidationError

from tg_news_monitor.config import (
    DEFAULT_LEGACY_GROUP_ID,
    Settings,
    parse_yaml_file,
)
from tg_news_monitor.core.models import DigestBrief, DigestItem, TelegramPost
from tg_news_monitor.core.policy import DeliveryPolicy
from tg_news_monitor.core.quiet_hours import QuietHours
from tg_news_monitor.core.runner import NewsMonitorRunner
from tg_news_monitor.core.schedule import MODE_DAY, MODE_SHOULDER, classify_alert_mode, knobs_for
from tg_news_monitor.notifier.feishu_card import FeishuCardBuilder
from tg_news_monitor.storage.database import db_session, get_connection, init_db
from tg_news_monitor.storage.repository import PostRepository
from tests.test_runner import MockEvaluator, MockScraper, MockWebhookSender


def _post(mid: int, text: str, channel: str = "wire", group_id: str | None = None) -> TelegramPost:
    return TelegramPost(
        channel=channel,
        message_id=mid,
        text=text,
        direct_url=f"https://t.me/{channel}/{mid}",
        published_at=datetime.now(timezone.utc),
        group_id=group_id,
    )


class TestGroupLoading:
    def test_legacy_synthesis_from_channels_and_webhook(self):
        s = Settings(
            telegram_channels=["zaobaosg", "@cnalatest"],
            feishu_webhook_url="https://open.feishu.cn/open-apis/bot/v2/hook/legacy",
            feishu_webhook_secret="sign",
        )
        assert len(s.groups) == 1
        g = s.groups[0]
        assert g.id == DEFAULT_LEGACY_GROUP_ID
        assert g.channels == ["zaobaosg", "cnalatest"]
        assert g.resolve_webhook_url() == "https://open.feishu.cn/open-apis/bot/v2/hook/legacy"
        assert g.resolve_webhook_secret() == "sign"
        assert [x.id for x in s.enabled_groups()] == [DEFAULT_LEGACY_GROUP_ID]

    def test_explicit_groups_ignore_telegram_channels_as_primary(self):
        s = Settings(
            telegram_channels=["old_chan"],
            feishu_webhook_url="https://open.feishu.cn/open-apis/bot/v2/hook/old",
            groups=[
                {
                    "id": "news24",
                    "name": "7x24 新闻",
                    "channels": ["zaobaosg"],
                    "webhook_url": "https://open.feishu.cn/open-apis/bot/v2/hook/news24",
                }
            ],
        )
        assert [g.id for g in s.groups] == ["news24"]
        assert s.enabled_groups()[0].channels == ["zaobaosg"]
        assert s.telegram_channels == ["old_chan"]

    def test_empty_or_disabled_groups_are_skipped(self):
        s = Settings(
            groups=[
                {
                    "id": "news24",
                    "channels": ["zaobaosg"],
                    "webhook_url": "https://example.com/news24",
                },
                {
                    "id": "xhs_hot",
                    "enabled": False,
                    "channels": ["xhs_demo"],
                    "webhook_url": "https://example.com/xhs",
                },
                {
                    "id": "old_stories",
                    "channels": [],
                    "webhook_url": "https://example.com/stories",
                },
            ]
        )
        assert [g.id for g in s.enabled_groups()] == ["news24"]

    def test_duplicate_group_id_rejected(self):
        with pytest.raises(ValidationError, match="duplicate group id"):
            Settings(
                groups=[
                    {"id": "news24", "channels": ["a"], "webhook_url": "https://example.com/a"},
                    {"id": "news24", "channels": ["b"], "webhook_url": "https://example.com/b"},
                ]
            )

    def test_channel_conflict_rejected_even_if_disabled(self):
        with pytest.raises(ValidationError, match="mutually exclusive"):
            Settings(
                groups=[
                    {"id": "news24", "channels": ["shared"], "webhook_url": "https://example.com/a"},
                    {
                        "id": "xhs_hot",
                        "enabled": False,
                        "channels": ["shared"],
                        "webhook_url": "https://example.com/b",
                    },
                ]
            )

    def test_webhook_url_env_resolution(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("FEISHU_WEBHOOK_NEWS24", "https://open.feishu.cn/open-apis/bot/v2/hook/from-env")
        s = Settings(
            groups=[
                {
                    "id": "news24",
                    "channels": ["zaobaosg"],
                    "webhook_url_env": "FEISHU_WEBHOOK_NEWS24",
                }
            ]
        )
        assert s.groups[0].resolve_webhook_url(s.env_lookup) == (
            "https://open.feishu.cn/open-apis/bot/v2/hook/from-env"
        )
        assert s.runtime_validation_errors(require_webhook=True) == []

    def test_runtime_webhook_required_for_enabled_group(self):
        s = Settings(
            groups=[{"id": "news24", "channels": ["zaobaosg"]}]
        )
        errs = s.runtime_validation_errors(require_webhook=True)
        assert any("news24" in e and "webhook" in e for e in errs)

    def test_yaml_groups_and_overrides(self, tmp_path):
        yaml_path = tmp_path / "config.yaml"
        yaml_path.write_text(
            """
groups:
  - id: news24
    name: 7x24 新闻
    channels:
      - zaobaosg
      - cnalatest
    webhook_url: https://example.com/news24
    hotness_threshold: 8
    quiet_hours: "01:00-07:00"
    digest_max_calls_per_day: 40
    card_profile:
      subtitle: 投资情报快报
      include_investment_impact: true
      prompt_variant: news
  - id: xhs_hot
    name: 小红书热点（示例）
    enabled: false
    channels: []
    webhook_url_env: FEISHU_WEBHOOK_XHS
    card_profile:
      subtitle: 故事速览
      include_investment_impact: false
      prompt_variant: story
""",
            encoding="utf-8",
        )
        parsed = parse_yaml_file(yaml_path)
        assert isinstance(parsed["groups"], list)
        s = Settings.load(config_path=yaml_path)
        assert [g.id for g in s.groups] == ["news24", "xhs_hot"]
        view = s.group_settings("news24")
        assert view.hotness_threshold == 8
        assert view.quiet_hours == "01:00-07:00"
        assert s.hotness_threshold == 7
        assert knobs_for(view, "day").hotness_threshold == 8
        assert s.groups[1].resolved_card_profile().include_investment_impact is False
        assert s.groups[1].resolved_card_profile().prompt_variant == "story"

    @pytest.mark.real_schedule
    def test_empty_group_windows_do_not_inherit_global(self, tmp_path):
        """quiet_hours/shoulder_hours: '' or explicit null disable; omit inherits."""
        yaml_path = tmp_path / "config.yaml"
        yaml_path.write_text(
            """
quiet_hours: "00:00-08:00"
shoulder_hours: "22:00-00:00"
shoulder_digest_min_candidates: 12
shoulder_digest_min_interval_seconds: 420
digest_min_candidates: 3
digest_min_interval_seconds: 180
groups:
  - id: old_photos
    channels: [ussrpictures]
    webhook_url: https://example.com/photos
    quiet_hours: ""
    shoulder_hours: ""
    digest_min_candidates: 2
    digest_min_interval_seconds: 1800
  - id: news24
    channels: [zaobaosg]
    webhook_url: https://example.com/news24
  - id: extra
    channels: [solidot]
    webhook_url: https://example.com/extra
    quiet_hours: null
    shoulder_hours: null
    digest_min_candidates: 4
""",
            encoding="utf-8",
        )
        s = Settings.load(config_path=yaml_path)
        photos = s.group_settings("old_photos")
        news = s.group_settings("news24")
        extra = s.group_settings("extra")

        assert photos.quiet_hours == ""
        assert photos.shoulder_hours == ""
        assert extra.quiet_hours == ""
        assert extra.shoulder_hours == ""
        assert news.quiet_hours == "00:00-08:00"
        assert news.shoulder_hours == "22:00-00:00"

        bj_shoulder = datetime(2026, 9, 11, 23, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
        bj_quiet = datetime(2026, 9, 12, 1, 0, tzinfo=ZoneInfo("Asia/Shanghai"))

        assert classify_alert_mode(bj_shoulder, photos.quiet_hours, photos.shoulder_hours) == MODE_DAY
        photo_knobs = knobs_for(
            photos,
            classify_alert_mode(bj_shoulder, photos.quiet_hours, photos.shoulder_hours),
        )
        assert photo_knobs.mode == MODE_DAY
        assert photo_knobs.min_candidates == 2
        assert photo_knobs.min_interval_seconds == 1800

        assert classify_alert_mode(bj_quiet, photos.quiet_hours, photos.shoulder_hours) == MODE_DAY

        assert classify_alert_mode(bj_shoulder, news.quiet_hours, news.shoulder_hours) == MODE_SHOULDER
        news_knobs = knobs_for(
            news,
            classify_alert_mode(bj_shoulder, news.quiet_hours, news.shoulder_hours),
        )
        assert news_knobs.mode == MODE_SHOULDER
        assert news_knobs.min_candidates == 12
        assert news_knobs.min_interval_seconds == 420

        extra_knobs = knobs_for(
            extra,
            classify_alert_mode(bj_shoulder, extra.quiet_hours, extra.shoulder_hours),
        )
        assert extra_knobs.mode == MODE_DAY
        assert extra_knobs.min_candidates == 4

    def test_constructor_empty_windows_do_not_inherit(self):
        s = Settings(
            quiet_hours="00:00-08:00",
            shoulder_hours="22:00-00:00",
            groups=[
                {
                    "id": "old_photos",
                    "channels": ["ussrpictures"],
                    "webhook_url": "https://example.com/photos",
                    "quiet_hours": "",
                    "shoulder_hours": "",
                    "digest_min_candidates": 2,
                },
                {
                    "id": "news24",
                    "channels": ["zaobaosg"],
                    "webhook_url": "https://example.com/news24",
                },
            ],
        )
        photos = s.group_settings("old_photos")
        news = s.group_settings("news24")
        assert photos.quiet_hours == ""
        assert photos.shoulder_hours == ""
        assert s.groups[0].quiet_hours == ""
        assert news.quiet_hours == "00:00-08:00"
        assert news.shoulder_hours == "22:00-00:00"


class TestGroupIsolation:
    def test_delivery_claims_and_pending_are_per_group(self, tmp_path):
        db = str(tmp_path / "iso.db")
        repo = PostRepository(db)
        a = DeliveryPolicy(db, group_id="news24")
        b = DeliveryPolicy(db, group_id="old_stories")
        text = "Federal Reserve raises interest rates 25 basis points."
        assert a.claim(text, "news")
        assert b.claim(text, "story")
        assert a.seen(text)
        assert b.seen(text)
        assert "news" in a.history()
        assert "story" in b.history()
        assert "story" not in a.history()

        repo.save_post(_post(1, "Markets rally after policy decision.", channel="wire", group_id="news24"), group_id="news24")
        repo.save_post(_post(1, "An old village story about harvest.", channel="tales", group_id="old_stories"), group_id="old_stories")
        news_pending = repo.list_pending_posts(group_id="news24")
        story_pending = repo.list_pending_posts(group_id="old_stories")
        assert [p.channel for p in news_pending] == ["wire"]
        assert [p.channel for p in story_pending] == ["tales"]
        assert repo.is_processed("wire", 1, group_id="news24")
        assert not repo.is_processed("wire", 1, group_id="old_stories")

    def test_runner_does_not_mix_groups_in_one_llm_call(self, tmp_path):
        db = str(tmp_path / "runner.db")
        repo = PostRepository(db)
        config = Settings(
            db_path=db,
            digest_min_candidates=1,
            digest_max_wait_seconds=0,
            digest_card_interval_seconds=0,
            digest_min_interval_seconds=0,
            groups=[
                {
                    "id": "news24",
                    "channels": ["wire"],
                    "webhook_url": "https://example.com/news24",
                    "hotness_threshold": 7,
                },
                {
                    "id": "old_stories",
                    "channels": ["tales"],
                    "webhook_url": "https://example.com/stories",
                    "hotness_threshold": 5,
                    "card_profile": {
                        "subtitle": "故事速览",
                        "include_investment_impact": False,
                    },
                },
            ],
        )
        now = datetime.now(timezone.utc)
        repo.save_posts(
            [
                TelegramPost(
                    channel="wire",
                    message_id=1,
                    text="Central bank announces emergency rate cut for the economy.",
                    direct_url="https://t.me/wire/1",
                    published_at=now,
                )
            ],
            group_id="news24",
        )
        repo.save_posts(
            [
                TelegramPost(
                    channel="tales",
                    message_id=1,
                    text="A restored temple reopened after a decade of quiet repairs.",
                    direct_url="https://t.me/tales/1",
                    published_at=now,
                )
            ],
            group_id="old_stories",
        )

        def builder(posts):
            items = []
            for i, p in enumerate(posts, start=1):
                items.append(
                    DigestItem(
                        rank=i,
                        channel=p.channel,
                        message_id=p.message_id,
                        title=p.text[:20],
                        summary=p.text,
                        event_at=p.published_at,
                        score=9,
                        category="宏观财经",
                        impact_overall="总体关注",
                        impact_us="无直接影响",
                        impact_cn="无直接影响",
                        impact_commodities="无直接影响",
                    )
                )
            return DigestBrief(
                headline="iso",
                overview="",
                items=items,
                has_material_news=True,
            )

        evaluator = MockEvaluator(digest_builder=builder)
        sender = MockWebhookSender()
        runner = NewsMonitorRunner(config, repo, MockScraper(), evaluator, sender)
        summary = runner.process_pending()
        assert summary["alerts_sent"] == 2
        assert len(evaluator.digest_batches) == 2
        assert {p.channel for p in evaluator.digest_batches[0]} == {"wire"}
        assert {p.channel for p in evaluator.digest_batches[1]} == {"tales"}
        payloads = str(sender.sent_payloads)
        assert "投资情报快报" in payloads
        assert "故事速览" in payloads
        assert payloads.count("💹 投资影响") == 1

    def test_quiet_morning_claims_are_per_group(self, tmp_path):
        db = str(tmp_path / "quiet.db")
        cfg = Settings()
        q1 = QuietHours(db, cfg, group_id="news24")
        q2 = QuietHours(db, cfg, group_id="old_stories")
        now = datetime(2026, 9, 9, 16, tzinfo=timezone.utc)  # Beijing 00:00
        morning = now.replace(hour=0) + __import__("datetime").timedelta(days=1)
        # 08:00 Beijing = 00:00 UTC next day... midnight+8h = 08:00 BJ
        morning = now + __import__("datetime").timedelta(hours=8)
        assert q1.morning_due(morning)
        assert q1.claim_morning(morning)
        assert not q1.morning_due(morning)
        assert q2.morning_due(morning)

    def test_ingest_only_does_not_activate_group(self, tmp_path):
        db = str(tmp_path / "ingest_activate.db")
        config = Settings(
            db_path=db,
            inter_channel_delay_seconds=0,
            groups=[
                {
                    "id": "news24",
                    "channels": ["wire"],
                    "webhook_url": "https://example.com/news24",
                    "card_profile": {
                        "subtitle": "投资情报快报",
                        "include_investment_impact": True,
                        "prompt_variant": "news",
                    },
                },
                {
                    "id": "old_photos",
                    "channels": ["oldpix"],
                    "webhook_url": "https://example.com/photos",
                    "card_profile": {
                        "subtitle": "历史影像",
                        "include_investment_impact": False,
                        "prompt_variant": "wechat_photo",
                    },
                },
            ],
        )
        runner = NewsMonitorRunner(config, PostRepository(db), MockScraper(), MockEvaluator(), MockWebhookSender())
        news = next(g for g in config.enabled_groups() if g.id == "news24")
        runner._activate_group(news)
        activated: list[str | None] = []
        orig = runner._activate_group

        def spy(group):
            activated.append(getattr(group, "id", None))
            return orig(group)

        runner._activate_group = spy  # type: ignore[method-assign]
        runner.run_once(ingest_only=True)
        assert activated == []
        assert runner._group_id == "news24"
        assert runner._card_profile.subtitle == "投资情报快报"

    def test_news24_send_keeps_snapshot_if_ingest_activates_old_photos(self, tmp_path):
        """Ingest-thread _activate_group(old_photos) must not steal a news24 send."""
        db = str(tmp_path / "race.db")
        now = datetime(2026, 9, 10, 2, 0, 0, tzinfo=timezone.utc)
        config = Settings(
            db_path=db,
            digest_min_candidates=1,
            digest_max_wait_seconds=0,
            digest_card_interval_seconds=0,
            digest_min_interval_seconds=0,
            inter_channel_delay_seconds=0,
            groups=[
                {
                    "id": "news24",
                    "channels": ["walterbloomberg"],
                    "webhook_url": "https://example.com/news24",
                    "hotness_threshold": 7,
                    "digest_min_candidates": 1,
                    "digest_max_wait_seconds": 0,
                    "digest_min_interval_seconds": 0,
                    "digest_card_interval_seconds": 0,
                    "quiet_hours": "",
                    "shoulder_hours": "",
                    "morning_flush_enabled": False,
                    "card_profile": {
                        "subtitle": "投资情报快报",
                        "include_investment_impact": True,
                        "prompt_variant": "news",
                    },
                },
                {
                    "id": "old_photos",
                    "channels": ["oldpix"],
                    "webhook_url": "https://example.com/photos",
                    "hotness_threshold": 7,
                    "digest_min_candidates": 1,
                    "digest_max_wait_seconds": 0,
                    "digest_min_interval_seconds": 0,
                    "digest_card_interval_seconds": 0,
                    "quiet_hours": "",
                    "shoulder_hours": "",
                    "morning_flush_enabled": False,
                    "card_profile": {
                        "subtitle": "历史影像",
                        "include_investment_impact": False,
                        "prompt_variant": "wechat_photo",
                    },
                },
            ],
        )
        repo = PostRepository(db)
        repo.save_posts(
            [
                TelegramPost(
                    channel="walterbloomberg",
                    message_id=1,
                    text="Brent crude jumps seven dollars as US-Iran tensions escalate overnight.",
                    direct_url="https://t.me/walterbloomberg/1",
                    published_at=now,
                )
            ],
            group_id="news24",
        )

        def builder(posts):
            items = [
                DigestItem(
                    rank=1,
                    channel=p.channel,
                    message_id=p.message_id,
                    title="布伦特原油单日上涨7美元，美伊冲突升级",
                    summary=p.text,
                    event_at=p.published_at,
                    score=9,
                    category="地缘政治",
                    impact_overall="油价冲击风险资产",
                    impact_us="能源股波动",
                    impact_cn="输入性通胀关注",
                    impact_commodities="原油大涨",
                )
                for p in posts
            ]
            return DigestBrief(
                headline="oil",
                overview="",
                items=items,
                has_material_news=True,
            )

        news_sender = MockWebhookSender()
        photo_sender = MockWebhookSender()
        entered, release = Event(), Event()

        class GateSender:
            def __init__(self, inner: MockWebhookSender) -> None:
                self.inner = inner
                self.sent_payloads = inner.sent_payloads

            def send(self, payload):
                entered.set()
                assert release.wait(3), "timed out waiting to resume news24 send"
                return self.inner.send(payload)

        runner = NewsMonitorRunner(
            config, repo, MockScraper(), MockEvaluator(digest_builder=builder), MockWebhookSender()
        )
        runner._senders["news24"] = GateSender(news_sender)
        runner._senders["old_photos"] = photo_sender
        photo = next(g for g in config.enabled_groups() if g.id == "old_photos")

        result: dict = {}

        def worker():
            result["summary"] = runner.process_pending(now=now)

        thread = Thread(target=worker)
        thread.start()
        assert entered.wait(3), "news24 send never started"
        runner._activate_group(photo)
        release.set()
        thread.join(5)
        assert not thread.is_alive()
        assert result["summary"]["alerts_sent"] == 1
        assert len(news_sender.sent_payloads) == 1
        assert photo_sender.sent_payloads == []
        blob = str(news_sender.sent_payloads[0])
        assert "投资情报快报" in blob
        assert "💹 投资影响" in blob
        assert "历史影像" not in blob
        assert "布伦特原油单日上涨7美元" in blob


class TestSchemaMigration:
    def test_old_posts_unique_migrates_to_legacy_group(self, tmp_path):
        db = str(tmp_path / "old.db")
        conn = get_connection(db)
        conn.executescript(
            """
            CREATE TABLE posts (
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
                created_at TEXT,
                updated_at TEXT,
                CONSTRAINT uq_posts_channel_msgid UNIQUE(channel, message_id)
            );
            INSERT INTO posts (
                channel, message_id, published_at, scraped_at, text, direct_url
            ) VALUES (
                'wire', 9, '2026-09-09T00:00:00+00:00', '2026-09-09T00:00:01+00:00',
                'legacy row', 'https://t.me/wire/9'
            );
            """
        )
        conn.commit()
        conn.close()

        init_db(db, default_group_id="legacy")
        with db_session(db) as conn:
            row = conn.execute("SELECT group_id, channel, message_id FROM posts").fetchone()
            assert row["group_id"] == "legacy"
            sql = conn.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name='posts'"
            ).fetchone()[0]
            assert "group_id" in sql
        repo = PostRepository(db)
        assert repo.is_processed("wire", 9, group_id="legacy")
        # same channel/msgid allowed in another group after migration
        assert repo.save_post(_post(9, "other group copy", group_id="news24"), group_id="news24")


def test_story_card_omits_investment_block():
    item = DigestItem(
        rank=1,
        channel="tales",
        message_id=1,
        title="古镇灯会",
        summary="灯会重新开放。",
        category="宏观财经",
        score=9,
        impact_overall="无直接影响",
        impact_us="无直接影响",
        impact_cn="无直接影响",
        impact_commodities="无直接影响",
    )
    news = FeishuCardBuilder.build_digest_item_card(item, include_investment_impact=True)
    story = FeishuCardBuilder.build_digest_item_card(
        item, subtitle="故事速览", include_investment_impact=False
    )
    assert "💹 投资影响" in str(news)
    assert "💹 投资影响" not in str(story)
    assert "故事速览" in str(story)
    assert "发布时间" in str(story)
    assert "t.me" not in str(story)
