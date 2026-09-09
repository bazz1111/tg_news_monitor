"""Runner behavior for shoulder / quiet / morning-flush alert windows."""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from tg_news_monitor.config import Settings
from tg_news_monitor.core.models import DigestBrief, DigestItem, TelegramPost
from tg_news_monitor.core.policy import DeliveryPolicy
from tg_news_monitor.core.runner import NewsMonitorRunner
from tg_news_monitor.core.schedule import load_zone
from tg_news_monitor.storage.database import db_session
from tg_news_monitor.storage.repository import PostRepository
from tests.test_runner import MockEvaluator, MockScraper, MockWebhookSender

pytestmark = pytest.mark.real_schedule

SH = load_zone("Asia/Shanghai")


def sh_time(hour: int, minute: int = 0, day: int = 9) -> datetime:
    return datetime(2026, 9, day, hour, minute, tzinfo=SH)


def make_post(mid: int, text: str, published_at: datetime, channel: str = "wire") -> TelegramPost:
    return TelegramPost(
        channel=channel,
        message_id=mid,
        text=text,
        direct_url=f"https://t.me/{channel}/{mid}",
        published_at=published_at,
    )


def item_for(post: TelegramPost, score: int, title: str, rank: int = 1, escalation: bool = False) -> DigestItem:
    return DigestItem(
        rank=rank,
        night_alert=True,
        confirmed_source="reuters",
        is_update=escalation,
        update_reason="New confirmed escalation" if escalation else "",
        channel=post.channel,
        message_id=post.message_id,
        title=title,
        summary="fact",
        event_at=post.published_at,
        category="全球新闻",
        score=score,
        impact_overall="",
        impact_us="",
        impact_cn="",
        impact_commodities="",
    )


def make_runner(tmp_path, **cfg_kw):
    db = str(tmp_path / "quiet.db")
    defaults = dict(
        db_path=db,
        telegram_channels=["wire"],
        digest_min_candidates=1,
        digest_max_wait_seconds=0,
        digest_card_interval_seconds=0,
        quiet_digest_card_interval_seconds=0,
        shoulder_digest_card_interval_seconds=0,
        digest_min_interval_seconds=0,
        news_max_age_seconds=1800,
        hotness_threshold=7,
        quiet_digest_min_candidates=16,
        shoulder_digest_min_candidates=16,
    )
    defaults.update(cfg_kw)
    config = Settings(**defaults)
    repository = PostRepository(db)
    evaluator = MockEvaluator()
    sender = MockWebhookSender()
    runner = NewsMonitorRunner(config, repository, MockScraper(), evaluator, sender)
    return runner, repository, evaluator, sender


class TestQuietAndShoulderSendRules:
    def test_quiet_holds_non_urgent_below_candidate_floor(self, tmp_path):
        now = sh_time(2, 0)
        runner, repository, evaluator, sender = make_runner(tmp_path)
        repository.save_posts([make_post(1, "Routine equity market commentary after the close.", now)])

        summary = runner.process_pending(now=now)
        assert summary["posts_evaluated"] == 0
        assert summary["alerts_sent"] == 0
        assert evaluator.digest_batches == []
        assert sender.sent_payloads == []
        assert len(repository.list_pending_posts()) == 1

    def test_quiet_keyword_does_not_bypass_score_or_source(self, tmp_path):
        now = sh_time(2, 0)
        runner, repository, evaluator, sender = make_runner(tmp_path)

        def select(posts):
            return DigestBrief(
                headline="break-glass",
                overview="",
                items=[item_for(posts[0], score=6, title="紧急降息")],
                has_material_news=True,
            )

        evaluator.digest_builder = select
        repository.save_posts([make_post(1, "Central bank announces emergency rate cut", now)])

        summary = runner.process_pending(now=now)
        assert summary["posts_evaluated"] == 1
        assert summary["alerts_sent"] == 0
        assert len(sender.sent_payloads) == 0

    def test_quiet_score_nine_sends_score_eight_does_not(self, tmp_path):
        now = sh_time(3, 0)
        runner, repository, evaluator, sender = make_runner(tmp_path, quiet_digest_min_candidates=1)

        def select(posts):
            by_id = {p.message_id: p for p in posts}
            return DigestBrief(
                headline="night",
                overview="",
                items=[
                    item_for(by_id[1], score=8, title="八分新闻", rank=1),
                    item_for(by_id[2], score=9, title="九分新闻", rank=2),
                ],
                has_material_news=True,
            )

        evaluator.digest_builder = select
        repository.save_posts([
            make_post(1, "Reuters reports emergency rate cut in Europe.", now),
            make_post(2, "Reuters reports emergency rate cut in America.", now),
        ])

        summary = runner.process_pending(now=now)
        assert summary["alerts_sent"] == 1
        assert "九分新闻" in str(sender.sent_payloads)
        assert "八分新闻" not in str(sender.sent_payloads)

    def test_quiet_card_cap_persists_and_resets_after_window(self, tmp_path):
        now = sh_time(4, 0)
        runner, repository, evaluator, sender = make_runner(
            tmp_path, quiet_digest_min_candidates=1, quiet_card_cap=2
        )

        def select(posts):
            return DigestBrief(
                headline="cap",
                overview="",
                items=[
                    item_for(p, score=9, title=f"夜报{p.message_id}", rank=min(i, 5), escalation=True)
                    for i, p in enumerate(posts, start=1)
                ],
                has_material_news=True,
            )

        evaluator.digest_builder = select
        repository.save_posts([
            make_post(i, f"Reuters emergency rate cut number {i} hits global markets.", now)
            for i in range(1, 4)
        ])

        first = runner.process_pending(now=now)
        assert first["alerts_sent"] == 2
        assert runner.policy.quiet_cards_sent("2026-09-09") == 2

        later = sh_time(4, 40)
        repository.save_posts([make_post(4, "Reuters reports another emergency rate cut overnight.", later)])
        runner.config.digest_min_interval_seconds = 0
        # Quiet interval is 1800s; force the next night eval by using a later clock + zeroed quiet interval.
        runner.config.quiet_digest_min_interval_seconds = 0
        second = runner.process_pending(now=later)
        assert second["alerts_sent"] == 0
        assert runner.policy.quiet_cards_sent("2026-09-09") == 2

        # Leaving quiet at 08:00 resets the persisted counter.
        runner.policy.sync_quiet_window("day", None)
        assert runner.policy.quiet_cards_sent("2026-09-09") == 0

    def test_shoulder_requires_score_eight(self, tmp_path):
        now = sh_time(23, 30, day=8)
        runner, repository, evaluator, sender = make_runner(
            tmp_path, shoulder_digest_min_candidates=1
        )

        def select(posts):
            by_id = {p.message_id: p for p in posts}
            return DigestBrief(
                headline="shoulder",
                overview="",
                items=[
                    item_for(by_id[1], score=7, title="七分肩时段", rank=1),
                    item_for(by_id[2], score=8, title="八分肩时段", rank=2),
                ],
                has_material_news=True,
            )

        evaluator.digest_builder = select
        repository.save_posts([
            make_post(1, "Tech giant unveils a new consumer laptop refresh today.", now),
            make_post(2, "Central government announces a new industrial policy package.", now),
        ])

        summary = runner.process_pending(now=now)
        assert summary["alerts_sent"] == 1
        assert "八分肩时段" in str(sender.sent_payloads)
        assert "七分肩时段" not in str(sender.sent_payloads)


class TestMorningFlush:
    def test_one_flush_relaxes_age_and_does_not_repeat(self, tmp_path):
        night = sh_time(7, 0)
        morning = sh_time(8, 5)
        runner, repository, evaluator, sender = make_runner(tmp_path, digest_min_candidates=12)

        # Establish last_mode=quiet with a still-fresh post that quiet will hold.
        held_at = night
        repository.save_posts([
            make_post(1, "Overnight industrial output surprise will be digested at dawn.", held_at)
        ])
        held = runner.process_pending(now=night)
        assert held["posts_evaluated"] == 0
        assert runner.policy.peek_morning_flush("day", morning, 8 * 60)

        def select(posts):
            return DigestBrief(
                headline="flush",
                overview="",
                items=[item_for(posts[0], score=7, title="晨间冲刷")],
                has_material_news=True,
            )

        evaluator.digest_builder = select
        # Age the held post to 90 minutes so daytime 30-minute max-age would drop it.
        aged_at = (morning - timedelta(minutes=90)).isoformat()
        with db_session(repository.db_path) as conn:
            conn.execute("UPDATE posts SET published_at=? WHERE message_id=1", (aged_at,))

        flushed = runner.process_pending(now=morning)
        assert flushed["posts_evaluated"] == 1
        assert flushed["alerts_sent"] == 1
        assert "晨间冲刷" in str(sender.sent_payloads)

        # A second morning pass must not take another flush call.
        repository.save_posts([
            make_post(2, "A second dawn backlog item about factory orders.", morning - timedelta(minutes=80))
        ])
        again = runner.process_pending(now=morning + timedelta(minutes=10))
        assert len(evaluator.digest_batches) == 1
        assert again["posts_evaluated"] == 0


class TestQuietCapTable:
    def test_policy_cap_resets_when_window_changes(self, tmp_path):
        db = str(tmp_path / "cap.db")
        policy = DeliveryPolicy(db)
        policy.sync_quiet_window("quiet", "2026-09-09")
        policy.record_quiet_card("2026-09-09")
        policy.record_quiet_card("2026-09-09")
        assert policy.quiet_cards_sent("2026-09-09") == 2
        policy.sync_quiet_window("day", None)
        assert policy.quiet_cards_sent("2026-09-09") == 0
        policy.sync_quiet_window("quiet", "2026-09-10")
        assert policy.quiet_cards_sent("2026-09-10") == 0
