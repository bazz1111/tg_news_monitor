"""WeChat photo material pipeline: filters, caption, card, intake, isolation from news24."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from tg_news_monitor.config import Settings
from tg_news_monitor.core.models import DigestBrief, DigestItem, TelegramPost
from tg_news_monitor.core.runner import NewsMonitorRunner
from tg_news_monitor.core.wechat_photo import (
    WECHAT_CAPTION_MAX,
    is_photo_album_post,
    normalize_wechat_caption,
    photo_link_urls,
    wechat_card_block_reason,
    wechat_looks_like_news,
    wechat_photo_prefilter,
    wechat_photo_unsafe,
)
from tg_news_monitor.evaluator.prompt import (
    DIGEST_SYSTEM_PROMPT,
    WECHAT_PHOTO_DIGEST_SYSTEM_PROMPT,
    build_digest_system_prompt,
    build_digest_user_prompt,
)
from tg_news_monitor.notifier.feishu_card import FeishuCardBuilder
from tg_news_monitor.storage.repository import PostRepository
from tests.test_runner import MockEvaluator, MockScraper, MockWebhookSender


def _now() -> datetime:
    # Beijing 10:00 — daytime, outside news24 quiet/morning windows.
    return datetime(2026, 9, 10, 2, 0, 0, tzinfo=timezone.utc)


def _post(
    mid: int,
    text: str,
    *,
    channel: str = "oldpix",
    has_media: bool = True,
    media_type: str | None = "photo",
    media_urls: list[str] | None = None,
    published_at: datetime | None = None,
) -> TelegramPost:
    return TelegramPost(
        channel=channel,
        message_id=mid,
        text=text,
        has_media=has_media,
        media_type=media_type,
        media_urls=media_urls if media_urls is not None else ["https://cdn.example.com/p.jpg"],
        direct_url=f"https://t.me/{channel}/{mid}",
        published_at=published_at or _now(),
    )


def _item(
    *,
    channel: str = "oldpix",
    message_id: int = 1,
    title: str = "弄堂石库门",
    summary: str = "上海石库门弄堂里晾着衣裳，行人从砖墙下走过。",
    score: int = 8,
    event_at: datetime | None = None,
    media_urls: list[str] | None = None,
) -> DigestItem:
    return DigestItem(
        rank=1,
        channel=channel,
        message_id=message_id,
        title=title,
        summary=summary,
        category="历史影像",
        score=score,
        event_at=event_at,
        impact_overall="无直接影响",
        impact_us="无直接影响",
        impact_cn="无直接影响",
        impact_commodities="无直接影响",
        media_urls=media_urls or [],
    )


class TestWechatLocalFilters:
    def test_rejects_vice_and_politics(self):
        for text in (
            "色情图集请私信",
            "赌场开业盛况",
            "冰毒缴获现场",
            "斩首示众",
            "习近平视察农村",
            "毛泽东在天安门",
            "台海开战最新",
            "俄乌前线影像",
        ):
            assert wechat_photo_unsafe(text), text

    def test_keeps_cultural_caption(self):
        assert not wechat_photo_unsafe("苏州河边的石库门与晾衣竿，约二十世纪三十年代。")
        assert not wechat_photo_unsafe("旗袍店橱窗与有轨电车。")

    def test_rejects_live_news_energy_and_conflict(self):
        assert wechat_photo_unsafe("布伦特原油单日上涨7美元，美伊冲突升级")
        assert wechat_looks_like_news(category="能源", text="油市波动")
        news_item = _item(
            title="布伦特原油单日上涨7美元，美伊冲突升级",
            summary="美伊冲突升级推高油价。",
        )
        news_item.category = "能源"
        news_item.media_urls = []
        assert wechat_card_block_reason(news_item) in {"wechat_unsafe", "wechat_news_like", "no_photo_urls"}
        cultural = _item()
        cultural.media_urls = ["https://cdn.example.com/p.jpg"]
        assert wechat_card_block_reason(cultural) is None
        no_media = _item()
        no_media.media_urls = []
        assert wechat_card_block_reason(no_media) == "no_photo_urls"

    def test_photo_only_intake(self):
        photo = _post(1, "老街一角", media_type="photo")
        album = _post(
            2,
            "一组院落",
            media_type="album",
            media_urls=["https://cdn.example.com/a.jpg", "https://cdn.example.com/b.jpg"],
        )
        text_only = _post(3, "这是一段没有配图的文字说明而已。", has_media=False, media_type=None, media_urls=[])
        video = _post(4, "纪录短片", media_type="video", media_urls=["https://cdn.example.com/v.mp4"])
        photo_no_url = _post(5, "有图无链", media_type="photo", media_urls=[])
        tme_only = _post(6, "只有原文链", media_type="photo", media_urls=["https://t.me/oldpix/6"])

        assert is_photo_album_post(photo)
        assert is_photo_album_post(album)
        assert not is_photo_album_post(text_only)
        assert not is_photo_album_post(video)
        assert not is_photo_album_post(photo_no_url)
        assert not is_photo_album_post(tme_only)

        kept, dropped = wechat_photo_prefilter(
            [photo, album, text_only, video, photo_no_url, tme_only]
        )
        assert {p.message_id for p in kept} == {1, 2}
        reasons = {p.message_id: reason for p, reason in dropped}
        assert reasons[3] == "not_photo_media"
        assert reasons[4] == "not_photo_media"
        assert reasons[5] == "not_photo_media"
        assert reasons[6] == "not_photo_media"

    def test_short_photo_caption_is_not_empty_spam(self):
        kept, dropped = wechat_photo_prefilter([_post(1, "[PHOTO]")])
        assert len(kept) == 1
        assert dropped == []

    def test_unsafe_caption_dropped_before_llm(self):
        kept, dropped = wechat_photo_prefilter([_post(1, "习近平旧照")])
        assert kept == []
        assert dropped[0][1] == "wechat_unsafe"


class TestWechatCaption:
    def test_complete_sentence_under_limit(self):
        out = normalize_wechat_caption("上海外滩的石库门街景")
        assert out == "上海外滩的石库门街景。"
        assert len(out) <= WECHAT_CAPTION_MAX
        assert not out.endswith("…")
        assert not out.endswith("...")

    def test_strips_ellipsis_and_tg_link(self):
        out = normalize_wechat_caption("见 https://t.me/oldpix/9 老桥还在……")
        assert "t.me" not in out
        assert not out.endswith("…")
        assert out.endswith("。")

    def test_truncates_to_100_complete(self):
        long = "这是一条用于测试的超长历史影像说明，" * 8
        out = normalize_wechat_caption(long)
        assert len(out) <= WECHAT_CAPTION_MAX
        assert out.endswith(("。", "！", "？", "；"))
        assert "…" not in out[-3:]


class TestWechatCard:
    def test_card_is_minimal_and_has_image_links(self):
        item = _item()
        urls = [
            "https://cdn.example.com/a.jpg",
            "https://cdn.example.com/b.jpg",
            "https://t.me/oldpix/1",
        ]
        card = FeishuCardBuilder.build_wechat_photo_card(item, media_urls=urls)
        blob = str(card)
        assert "弄堂石库门" in blob
        assert "上海石库门弄堂" in blob
        assert "https://cdn.example.com/a.jpg" in blob
        assert "https://cdn.example.com/b.jpg" in blob
        assert "t.me" not in blob
        assert "投资影响" not in blob
        assert "发布时间" not in blob
        assert "原文" not in blob
        assert "@oldpix" not in blob
        assert "Telegram" not in blob

    def test_news_card_still_has_investment_and_time(self):
        item = _item()
        news = FeishuCardBuilder.build_digest_item_card(item, include_investment_impact=True)
        blob = str(news)
        assert "💹 投资影响" in blob
        assert "发布时间" in blob
        assert "t.me" not in blob


class TestWechatPromptAndConfig:
    def test_variant_accepted(self):
        s = Settings(
            groups=[
                {
                    "id": "old_photos",
                    "channels": ["oldpix"],
                    "webhook_url": "https://example.com/photos",
                    "card_profile": {"prompt_variant": "wechat_photo", "include_investment_impact": False},
                }
            ]
        )
        assert s.groups[0].resolved_card_profile().prompt_variant == "wechat_photo"

    def test_unknown_variant_rejected(self):
        with pytest.raises(ValidationError, match="wechat_photo"):
            Settings(
                groups=[
                    {
                        "id": "old_photos",
                        "channels": ["oldpix"],
                        "webhook_url": "https://example.com/photos",
                        "card_profile": {"prompt_variant": "gossip"},
                    }
                ]
            )

    def test_prompt_is_stricter_and_news_prompt_unchanged(self):
        wechat = build_digest_system_prompt("wechat_photo")
        news = build_digest_system_prompt("news")
        assert wechat == WECHAT_PHOTO_DIGEST_SYSTEM_PROMPT
        assert news == DIGEST_SYSTEM_PROMPT
        assert "宁缺毋滥" in wechat
        assert "≤100" in wechat or "100字" in wechat
        assert "四维方向标签" in news
        assert "利多" in news

    def test_user_prompt_includes_photo_urls_only_for_wechat(self):
        post = _post(3, "老车站钟楼。", media_urls=["https://cdn.example.com/clock.jpg"])
        wechat = build_digest_user_prompt([post], variant="wechat_photo")
        news = build_digest_user_prompt([post], variant="news")
        assert "photo_urls:" in wechat
        assert "https://cdn.example.com/clock.jpg" in wechat
        assert "photo_urls:" not in news


class TestWechatRunnerIsolation:
    def _settings(self, db: str, **photo_over: object) -> Settings:
        photo = {
            "id": "old_photos",
            "channels": ["oldpix"],
            "webhook_url": "https://example.com/photos",
            "hotness_threshold": 7,
            "digest_min_candidates": 1,
            "digest_max_wait_seconds": 0,
            "digest_min_interval_seconds": 0,
            "digest_card_interval_seconds": 0,
            "news_max_age_seconds": 86400,
            "morning_flush_enabled": False,
            "quiet_hours": "",
            "shoulder_hours": "",
            "card_profile": {
                "subtitle": "公众号图片素材",
                "include_investment_impact": False,
                "prompt_variant": "wechat_photo",
            },
        }
        photo.update(photo_over)
        return Settings(
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
                photo,
            ],
        )

    def _builder(self, score: int = 8, event_at_offset: timedelta | None = None):
        def builder(posts):
            items = []
            for i, p in enumerate(posts, start=1):
                ev = p.published_at
                if event_at_offset is not None:
                    ev = p.published_at + event_at_offset
                items.append(
                    _item(
                        channel=p.channel,
                        message_id=p.message_id,
                        title=p.text[:16],
                        summary=p.text if p.text.endswith("。") else p.text + "。",
                        score=score,
                        event_at=ev,
                        media_urls=list(p.media_urls or []),
                    )
                )
            return DigestBrief(
                headline="iso",
                overview="",
                items=items,
                has_material_news=True,
            )

        return builder

    def test_text_and_video_never_reach_llm_on_old_photos(self, tmp_path):
        db = str(tmp_path / "intake.db")
        repo = PostRepository(db)
        now = _now()
        repo.save_posts(
            [
                _post(
                    1,
                    "一段没有配图的怀旧文字说明用来凑长度。",
                    has_media=False,
                    media_type=None,
                    media_urls=[],
                    published_at=now,
                ),
                _post(
                    2,
                    "纪录短片回顾老城厢。",
                    media_type="video",
                    media_urls=["https://cdn.example.com/v.mp4"],
                    published_at=now,
                ),
            ],
            group_id="old_photos",
        )
        evaluator = MockEvaluator(digest_builder=self._builder())
        runner = NewsMonitorRunner(self._settings(db), repo, MockScraper(), evaluator, MockWebhookSender())
        summary = runner.process_pending(now=now)
        assert evaluator.digest_batches == []
        assert summary["alerts_sent"] == 0
        assert repo.get_post("oldpix", 1, group_id="old_photos")["filter_reason"] == "not_photo_media"
        assert repo.get_post("oldpix", 2, group_id="old_photos")["filter_reason"] == "not_photo_media"

    def test_photo_sends_minimal_card_and_news24_keeps_investment(self, tmp_path):
        db = str(tmp_path / "iso.db")
        repo = PostRepository(db)
        now = _now()
        repo.save_posts(
            [
                TelegramPost(
                    channel="wire",
                    message_id=10,
                    text="Central bank announces emergency rate cut for the economy.",
                    direct_url="https://t.me/wire/10",
                    published_at=now,
                )
            ],
            group_id="news24",
        )
        repo.save_posts(
            [
                _post(
                    20,
                    "苏州河畔的石库门与有轨电车。",
                    media_urls=["https://cdn.example.com/lane.jpg", "https://cdn.example.com/tram.jpg"],
                    published_at=now,
                )
            ],
            group_id="old_photos",
        )
        evaluator = MockEvaluator(digest_builder=self._builder())
        sender = MockWebhookSender()
        runner = NewsMonitorRunner(self._settings(db), repo, MockScraper(), evaluator, sender)
        summary = runner.process_pending(now=now)
        assert summary["alerts_sent"] == 2
        assert len(evaluator.digest_batches) == 2
        assert {p.channel for p in evaluator.digest_batches[0]} == {"wire"}
        assert {p.channel for p in evaluator.digest_batches[1]} == {"oldpix"}
        payloads = str(sender.sent_payloads)
        assert payloads.count("💹 投资影响") == 1
        assert "投资情报快报" in payloads
        assert "公众号图片素材" in payloads
        assert "cdn.example.com/lane.jpg" in payloads
        assert "cdn.example.com/tram.jpg" in payloads
        assert "t.me" not in payloads
        assert "发布时间" in payloads  # news card only
        photo_card = next(
            p for p in sender.sent_payloads if "公众号图片素材" in str(p)
        )
        assert "发布时间" not in str(photo_card)
        assert "投资影响" not in str(photo_card)
        assert "t.me" not in str(photo_card)

    def test_old_event_at_blocks_news_not_photos(self, tmp_path):
        db = str(tmp_path / "age.db")
        repo = PostRepository(db)
        now = _now()
        old_event = timedelta(days=-20 * 365)
        repo.save_posts(
            [
                TelegramPost(
                    channel="wire",
                    message_id=11,
                    text="Central bank announces emergency rate cut for the economy.",
                    direct_url="https://t.me/wire/11",
                    published_at=now,
                )
            ],
            group_id="news24",
        )
        repo.save_posts(
            [_post(21, "民国时期的码头工人在卸货。", published_at=now)],
            group_id="old_photos",
        )
        evaluator = MockEvaluator(digest_builder=self._builder(event_at_offset=old_event))
        sender = MockWebhookSender()
        runner = NewsMonitorRunner(self._settings(db), repo, MockScraper(), evaluator, sender)
        summary = runner.process_pending(now=now)
        assert summary["alerts_sent"] == 1
        assert "公众号图片素材" in str(sender.sent_payloads)
        assert "投资情报快报" not in str(sender.sent_payloads)
        news_row = repo.get_post("wire", 11, group_id="news24")
        assert news_row["filter_reason"] == "event_time_unknown_or_expired"

    def test_post_digest_unsafe_caption_dropped(self, tmp_path):
        db = str(tmp_path / "unsafe.db")
        repo = PostRepository(db)
        now = _now()
        repo.save_posts([_post(30, "苏州河边的石库门。", published_at=now)], group_id="old_photos")

        def builder(posts):
            return DigestBrief(
                headline="bad",
                overview="",
                items=[_item(message_id=30, title="领袖旧照", summary="习近平年轻时的影像。")],
                has_material_news=True,
            )

        evaluator = MockEvaluator(digest_builder=builder)
        sender = MockWebhookSender()
        runner = NewsMonitorRunner(self._settings(db), repo, MockScraper(), evaluator, sender)
        summary = runner.process_pending(now=now)
        assert summary["alerts_sent"] == 0
        assert sender.sent_payloads == []
        assert repo.get_post("oldpix", 30, group_id="old_photos")["filter_reason"] == "wechat_unsafe"

    def test_news_like_item_without_media_not_sent_on_old_photos(self, tmp_path):
        db = str(tmp_path / "news_like.db")
        repo = PostRepository(db)
        now = _now()
        repo.save_posts([_post(40, "苏州河边的石库门。", published_at=now)], group_id="old_photos")

        def news_builder(posts):
            bad = _item(
                message_id=40,
                title="布伦特原油单日上涨7美元，美伊冲突升级",
                summary="地缘冲突推升能源价格。",
                media_urls=[],
            )
            bad.category = "能源"
            return DigestBrief(
                headline="bad-news",
                overview="",
                items=[bad],
                has_material_news=True,
            )

        evaluator = MockEvaluator(digest_builder=news_builder)
        sender = MockWebhookSender()
        runner = NewsMonitorRunner(self._settings(db), repo, MockScraper(), evaluator, sender)
        summary = runner.process_pending(now=now)
        assert summary["alerts_sent"] == 0
        assert sender.sent_payloads == []
        reason = repo.get_post("oldpix", 40, group_id="old_photos")["filter_reason"]
        assert reason in {"wechat_unsafe", "wechat_news_like", "no_photo_urls"}

    def test_wechat_send_requires_media_urls(self, tmp_path):
        db = str(tmp_path / "no_media.db")
        config = self._settings(db)
        sender = MockWebhookSender()
        runner = NewsMonitorRunner(config, PostRepository(db), MockScraper(), MockEvaluator(), sender)
        photo = next(g for g in config.enabled_groups() if g.id == "old_photos")
        item = _item(media_urls=[])
        with runner._bind_group(photo):
            assert runner._send_digest_item_card(item) is False
        assert sender.sent_payloads == []

    def test_news_like_with_media_still_rejected_on_old_photos(self, tmp_path):
        db = str(tmp_path / "news_media.db")
        repo = PostRepository(db)
        now = _now()
        repo.save_posts([_post(43, "苏州河边的石库门。", published_at=now)], group_id="old_photos")

        def news_builder(posts):
            bad = _item(
                message_id=43,
                title="布伦特原油单日上涨7美元，美伊冲突升级",
                summary="地缘冲突推升能源价格。",
                media_urls=["https://cdn.example.com/oil.jpg"],
            )
            bad.category = "能源"
            return DigestBrief(
                headline="bad-news",
                overview="",
                items=[bad],
                has_material_news=True,
            )

        sender = MockWebhookSender()
        runner = NewsMonitorRunner(
            self._settings(db), repo, MockScraper(), MockEvaluator(digest_builder=news_builder), sender
        )
        summary = runner.process_pending(now=now)
        assert summary["alerts_sent"] == 0
        assert sender.sent_payloads == []
        reason = repo.get_post("oldpix", 43, group_id="old_photos")["filter_reason"]
        assert reason in {"wechat_unsafe", "wechat_news_like"}


def test_photo_link_urls_dedupes_and_drops_tme():
    assert photo_link_urls(
        [
            "https://cdn.example.com/a.jpg",
            "https://cdn.example.com/a.jpg",
            "https://t.me/x/1",
            "ftp://cdn.example.com/a.jpg",
            "",
        ]
    ) == ["https://cdn.example.com/a.jpg"]
