"""WeChat photo material pipeline: filters, caption, card, intake, isolation from news24."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import httpx
import pytest
from pydantic import ValidationError

from tg_news_monitor.config import CardProfile, Settings
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
from tg_news_monitor.scraper.client import TelegramScraperClient
from tg_news_monitor.evaluator.prompt import (
    DIGEST_SYSTEM_PROMPT,
    WECHAT_PHOTO_DIGEST_SYSTEM_PROMPT,
    build_digest_system_prompt,
    build_digest_user_prompt,
)
from tg_news_monitor.notifier.feishu_card import FeishuCardBuilder
from tg_news_monitor.storage.repository import PostRepository
from tests.test_runner import MockEvaluator, MockScraper, MockWebhookSender, make_sample_html


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
    category: str = "历史影像",
    event_at: datetime | None = None,
    media_urls: list[str] | None = None,
) -> DigestItem:
    return DigestItem(
        rank=1,
        channel=channel,
        message_id=message_id,
        title=title,
        summary=summary,
        category=category,
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


class FakeUploader:
    """Test double for FeishuImageUploader.embed_keys."""

    def __init__(self, mapping: dict[str, str] | None = None, *, fail: bool = False, configured: bool = True):
        self.mapping = mapping
        self.fail = fail
        self.configured = configured
        self.calls: list[list[str]] = []

    def embed_keys(self, urls):
        self.calls.append(list(urls or []))
        if self.fail:
            return []
        kept = photo_link_urls(urls)
        if self.mapping is None:
            return [f"img_v2_{idx}" for idx, _ in enumerate(kept, 1)]
        return [self.mapping[u] for u in kept if u in self.mapping]


def _img_keys(payload) -> list[str]:
    elements = payload.get("card", {}).get("body", {}).get("elements", [])
    return [str(el.get("img_key") or "") for el in elements if el.get("tag") == "img"]


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

    def test_card_embeds_img_keys_when_upload_ok(self):
        item = _item()
        urls = [
            "https://cdn.example.com/a.jpg",
            "https://cdn.example.com/b.jpg",
            "https://t.me/oldpix/1",
        ]
        card = FeishuCardBuilder.build_wechat_photo_card(
            item,
            media_urls=urls,
            image_keys=["img_v2_aaa", "img_v2_bbb"],
        )
        blob = str(card)
        keys = _img_keys(card)
        assert keys == ["img_v2_aaa", "img_v2_bbb"]
        imgs = [el for el in card["card"]["body"]["elements"] if el.get("tag") == "img"]
        assert imgs
        for el in imgs:
            assert "mode" not in el
            assert el.get("scale_type") in {None, "crop_center", "crop_top", "fit_horizontal"}
            assert el.get("img_key")
            assert el.get("alt", {}).get("tag") == "plain_text"
        assert "img_v2_aaa" in blob
        assert "https://cdn.example.com/a.jpg" not in blob
        assert "t.me" not in blob
        assert "投资影响" not in blob

    def test_embedded_img_omits_unsupported_mode(self):
        card = FeishuCardBuilder.build_wechat_photo_card(
            _item(),
            media_urls=["https://cdn.example.com/a.jpg"],
            image_keys=["img_v2_aaa"],
        )
        imgs = [el for el in card["card"]["body"]["elements"] if el.get("tag") == "img"]
        assert len(imgs) == 1
        el = imgs[0]
        assert set(el) <= {"tag", "img_key", "alt", "scale_type", "preview", "title", "corner_radius", "size"}
        assert "mode" not in el
        if "scale_type" in el:
            assert el["scale_type"] in {"crop_center", "crop_top", "fit_horizontal"}

    def test_card_falls_back_to_links_without_keys(self):
        item = _item()
        card = FeishuCardBuilder.build_wechat_photo_card(
            item,
            media_urls=["https://cdn.example.com/a.jpg"],
            image_keys=[],
        )
        assert _img_keys(card) == []
        assert "https://cdn.example.com/a.jpg" in str(card)

    def test_news_card_still_has_investment_and_time(self):
        item = _item(
            channel="wire",
            title="央行紧急降息",
            summary="央行宣布紧急降息以稳定经济。",
            score=9,
            category="宏观财经",
        )
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

    def test_embed_images_defaults_true_only_for_wechat_photo(self):
        wechat = CardProfile(prompt_variant="wechat_photo")
        news = CardProfile(prompt_variant="news")
        assert wechat.embed_images_enabled() is True
        assert news.embed_images_enabled() is False
        assert CardProfile(prompt_variant="wechat_photo", embed_images=False).embed_images_enabled() is False
        assert CardProfile(prompt_variant="news", embed_images=True).embed_images_enabled() is True

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
            feishu_app_id="",
            feishu_app_secret="",
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
                is_news = str(p.channel).lower().lstrip("@") == "wire"
                items.append(
                    _item(
                        channel=p.channel,
                        message_id=p.message_id,
                        title=p.text[:16],
                        summary=p.text if p.text.endswith("。") else p.text + "。",
                        score=9 if is_news else score,
                        category="宏观财经" if is_news else "历史影像",
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

    def test_send_embeds_img_when_upload_ok(self, tmp_path):
        db = str(tmp_path / "embed_ok.db")
        config = self._settings(db)
        sender = MockWebhookSender()
        uploader = FakeUploader(mapping={"https://cdn.example.com/p.jpg": "img_v2_ok"})
        runner = NewsMonitorRunner(
            config, PostRepository(db), MockScraper(), MockEvaluator(), sender, uploader
        )
        photo = next(g for g in config.enabled_groups() if g.id == "old_photos")
        item = _item(media_urls=["https://cdn.example.com/p.jpg", "https://t.me/oldpix/1"])
        with runner._bind_group(photo):
            assert runner._send_digest_item_card(item) is True
        assert len(uploader.calls) == 1
        payload = sender.sent_payloads[0]
        assert _img_keys(payload) == ["img_v2_ok"]
        blob = str(payload)
        assert "img_key" in blob
        assert "cdn.example.com/p.jpg" not in blob
        assert "t.me" not in blob
        assert "投资影响" not in blob

    def test_send_embeds_partial_upload_success(self, tmp_path):
        db = str(tmp_path / "embed_partial.db")
        config = self._settings(db)
        sender = MockWebhookSender()
        uploader = FakeUploader(mapping={"https://cdn.example.com/a.jpg": "img_v2_a"})
        runner = NewsMonitorRunner(
            config, PostRepository(db), MockScraper(), MockEvaluator(), sender, uploader
        )
        photo = next(g for g in config.enabled_groups() if g.id == "old_photos")
        item = _item(media_urls=["https://cdn.example.com/a.jpg", "https://cdn.example.com/b.jpg"])
        with runner._bind_group(photo):
            assert runner._send_digest_item_card(item) is True
        payload = sender.sent_payloads[0]
        assert _img_keys(payload) == ["img_v2_a"]
        blob = str(payload)
        assert "cdn.example.com/b.jpg" not in blob
        assert "t.me" not in blob

    def test_send_falls_back_to_links_when_upload_fails(self, tmp_path):
        db = str(tmp_path / "embed_fail.db")
        config = self._settings(db)
        sender = MockWebhookSender()
        uploader = FakeUploader(fail=True)
        runner = NewsMonitorRunner(
            config, PostRepository(db), MockScraper(), MockEvaluator(), sender, uploader
        )
        photo = next(g for g in config.enabled_groups() if g.id == "old_photos")
        item = _item(media_urls=["https://cdn.example.com/p.jpg"])
        with runner._bind_group(photo):
            assert runner._send_digest_item_card(item) is True
        blob = str(sender.sent_payloads[0])
        assert _img_keys(sender.sent_payloads[0]) == []
        assert "https://cdn.example.com/p.jpg" in blob
        assert "img_key" not in blob

    def test_send_falls_back_to_links_when_missing_creds(self, tmp_path):
        db = str(tmp_path / "embed_nocred.db")
        config = self._settings(db)
        sender = MockWebhookSender()
        runner = NewsMonitorRunner(config, PostRepository(db), MockScraper(), MockEvaluator(), sender)
        assert runner._image_uploader.configured is False
        photo = next(g for g in config.enabled_groups() if g.id == "old_photos")
        item = _item(media_urls=["https://cdn.example.com/p.jpg"])
        with runner._bind_group(photo):
            assert runner._send_digest_item_card(item) is True
        blob = str(sender.sent_payloads[0])
        assert _img_keys(sender.sent_payloads[0]) == []
        assert "https://cdn.example.com/p.jpg" in blob

    def test_embed_images_false_keeps_markdown_links(self, tmp_path):
        db = str(tmp_path / "embed_off.db")
        config = self._settings(
            db,
            card_profile={
                "subtitle": "公众号图片素材",
                "include_investment_impact": False,
                "prompt_variant": "wechat_photo",
                "embed_images": False,
            },
        )
        sender = MockWebhookSender()
        uploader = FakeUploader()
        runner = NewsMonitorRunner(
            config, PostRepository(db), MockScraper(), MockEvaluator(), sender, uploader
        )
        photo = next(g for g in config.enabled_groups() if g.id == "old_photos")
        item = _item(media_urls=["https://cdn.example.com/p.jpg"])
        with runner._bind_group(photo):
            assert runner._send_digest_item_card(item) is True
        assert uploader.calls == []
        assert "https://cdn.example.com/p.jpg" in str(sender.sent_payloads[0])
        assert _img_keys(sender.sent_payloads[0]) == []

    def test_news24_send_does_not_call_uploader(self, tmp_path):
        db = str(tmp_path / "news_no_upload.db")
        config = self._settings(db)
        sender = MockWebhookSender()
        uploader = FakeUploader()
        runner = NewsMonitorRunner(
            config, PostRepository(db), MockScraper(), MockEvaluator(), sender, uploader
        )
        news = next(g for g in config.enabled_groups() if g.id == "news24")
        item = _item(
            channel="wire",
            title="央行紧急降息",
            summary="央行宣布紧急降息以稳定经济。",
            score=9,
            category="宏观财经",
        )
        with runner._bind_group(news):
            assert runner._send_digest_item_card(item) is True
        assert uploader.calls == []
        blob = str(sender.sent_payloads[0])
        assert "img_key" not in blob
        assert "投资情报快报" in blob

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

    def test_old_published_at_blocks_news_not_photos(self, tmp_path):
        db = str(tmp_path / "pub_age.db")
        repo = PostRepository(db)
        now = _now()
        stale = now - timedelta(days=30)
        repo.save_posts(
            [
                TelegramPost(
                    channel="wire",
                    message_id=11,
                    text="Central bank announces emergency rate cut for the economy.",
                    direct_url="https://t.me/wire/11",
                    published_at=stale,
                )
            ],
            group_id="news24",
        )
        repo.save_posts(
            [_post(21, "民国时期的码头工人在卸货。", published_at=stale)],
            group_id="old_photos",
        )
        evaluator = MockEvaluator(digest_builder=self._builder())
        sender = MockWebhookSender()
        runner = NewsMonitorRunner(self._settings(db), repo, MockScraper(), evaluator, sender)
        summary = runner.process_pending(now=now)
        assert summary["alerts_sent"] == 1
        assert "公众号图片素材" in str(sender.sent_payloads)
        assert "投资情报快报" not in str(sender.sent_payloads)
        news_row = repo.get_post("wire", 11, group_id="news24")
        assert news_row["filter_reason"] == "expired_or_invalid_time"
        photo_row = repo.get_post("oldpix", 21, group_id="old_photos")
        assert photo_row["alert_sent"] == 1
        assert photo_row.get("filter_reason") in {None, ""}


class TestHistoryPagination:
    def _settings(self, db: str, **photo_over: object) -> Settings:
        photo = {
            "id": "old_photos",
            "channels": ["oldpix"],
            "webhook_url": "https://example.com/photos",
            "scrape_history_pages": 10,
            "card_profile": {
                "prompt_variant": "wechat_photo",
                "include_investment_impact": False,
            },
        }
        photo.update(photo_over)
        return Settings(
            db_path=db,
            inter_channel_delay_seconds=0,
            groups=[
                {
                    "id": "news24",
                    "channels": ["wire"],
                    "webhook_url": "https://example.com/news24",
                    "card_profile": {"prompt_variant": "news"},
                },
                photo,
            ],
        )

    def test_pages_with_before_and_stops_on_budget(self, tmp_path):
        db = str(tmp_path / "hist_budget.db")
        channel = "oldpix"
        page1 = make_sample_html(
            channel,
            [
                {"message_id": 30, "text": "thirty"},
                {"message_id": 29, "text": "twenty-nine"},
                {"message_id": 28, "text": "twenty-eight"},
            ],
        )
        page2 = make_sample_html(
            channel,
            [
                {"message_id": 27, "text": "twenty-seven"},
                {"message_id": 26, "text": "twenty-six"},
                {"message_id": 25, "text": "twenty-five"},
            ],
        )
        page3 = make_sample_html(
            channel,
            [{"message_id": 24, "text": "twenty-four"}],
        )
        scraper = MockScraper(
            page_map={
                (channel, None): page1,
                (channel, 28): page2,
                (channel, 25): page3,
            }
        )
        runner = NewsMonitorRunner(
            self._settings(db, scrape_history_pages=2),
            PostRepository(db),
            scraper,
            MockEvaluator(),
            MockWebhookSender(),
        )
        posts, result = runner._ingest_channel(channel, group_id="old_photos")
        assert result["success"] is True
        assert result["posts_discovered"] == 6
        assert scraper.fetch_args == [(channel, None), (channel, 28)]
        assert {p.message_id for p in posts} == {30, 29, 28, 27, 26, 25}

    def test_stops_when_history_page_already_ingested(self, tmp_path):
        db = str(tmp_path / "hist_caught.db")
        channel = "oldpix"
        repo = PostRepository(db)
        now = _now()
        repo.save_posts(
            [
                TelegramPost(
                    channel=channel,
                    message_id=mid,
                    text=f"old {mid}",
                    direct_url=f"https://t.me/{channel}/{mid}",
                    published_at=now,
                )
                for mid in (80, 85, 89)
            ],
            group_id="old_photos",
        )
        page1 = make_sample_html(
            channel,
            [
                {"message_id": 100, "text": "hundred"},
                {"message_id": 95, "text": "ninety-five"},
                {"message_id": 90, "text": "ninety"},
            ],
        )
        page2 = make_sample_html(
            channel,
            [
                {"message_id": 89, "text": "eighty-nine"},
                {"message_id": 85, "text": "eighty-five"},
                {"message_id": 80, "text": "eighty"},
            ],
        )
        page3 = make_sample_html(channel, [{"message_id": 70, "text": "seventy"}])
        scraper = MockScraper(
            page_map={
                (channel, None): page1,
                (channel, 90): page2,
                (channel, 80): page3,
            }
        )
        runner = NewsMonitorRunner(
            self._settings(db, scrape_history_pages=8),
            repo,
            scraper,
            MockEvaluator(),
            MockWebhookSender(),
        )
        posts, result = runner._ingest_channel(channel, group_id="old_photos")
        assert result["posts_discovered"] == 3
        assert {p.message_id for p in posts} == {100, 95, 90}
        assert scraper.fetch_args == [(channel, None), (channel, 90)]

    def test_latest_ingested_still_walks_older_new_history(self, tmp_path):
        db = str(tmp_path / "hist_backfill.db")
        channel = "oldpix"
        repo = PostRepository(db)
        now = _now()
        repo.save_posts(
            [
                TelegramPost(
                    channel=channel,
                    message_id=mid,
                    text=f"latest {mid}",
                    direct_url=f"https://t.me/{channel}/{mid}",
                    published_at=now,
                )
                for mid in (100, 95, 90)
            ],
            group_id="old_photos",
        )
        page1 = make_sample_html(
            channel,
            [
                {"message_id": 100, "text": "hundred"},
                {"message_id": 95, "text": "ninety-five"},
                {"message_id": 90, "text": "ninety"},
            ],
        )
        page2 = make_sample_html(
            channel,
            [
                {"message_id": 80, "text": "eighty"},
                {"message_id": 70, "text": "seventy"},
            ],
        )
        scraper = MockScraper(
            page_map={
                (channel, None): page1,
                (channel, 90): page2,
            }
        )
        runner = NewsMonitorRunner(
            self._settings(db, scrape_history_pages=10),
            repo,
            scraper,
            MockEvaluator(),
            MockWebhookSender(),
        )
        posts, result = runner._ingest_channel(channel, group_id="old_photos")
        assert result["posts_discovered"] == 2
        assert {p.message_id for p in posts} == {80, 70}
        assert scraper.fetch_args[:2] == [(channel, None), (channel, 90)]
        assert scraper.fetch_args[2] == (channel, 70)

    def test_empty_history_page_stops(self, tmp_path):
        db = str(tmp_path / "hist_empty.db")
        channel = "oldpix"
        page1 = make_sample_html(channel, [{"message_id": 12, "text": "twelve"}])
        scraper = MockScraper(
            page_map={
                (channel, None): page1,
                (channel, 12): "<html><body></body></html>",
            }
        )
        runner = NewsMonitorRunner(
            self._settings(db, scrape_history_pages=6),
            PostRepository(db),
            scraper,
            MockEvaluator(),
            MockWebhookSender(),
        )
        posts, result = runner._ingest_channel(channel, group_id="old_photos")
        assert result["posts_discovered"] == 1
        assert posts[0].message_id == 12
        assert scraper.fetch_args == [(channel, None), (channel, 12)]

    def test_soft_cap_stops_before_next_page(self, tmp_path):
        db = str(tmp_path / "hist_cap.db")
        channel = "oldpix"
        page1 = make_sample_html(
            channel,
            [
                {"message_id": 5, "text": "five"},
                {"message_id": 4, "text": "four"},
                {"message_id": 3, "text": "three"},
            ],
        )
        page2 = make_sample_html(channel, [{"message_id": 2, "text": "two"}])
        scraper = MockScraper(
            page_map={
                (channel, None): page1,
                (channel, 3): page2,
            }
        )
        runner = NewsMonitorRunner(
            self._settings(db, scrape_history_pages=5, scrape_history_max_new_posts=2),
            PostRepository(db),
            scraper,
            MockEvaluator(),
            MockWebhookSender(),
        )
        posts, result = runner._ingest_channel(channel, group_id="old_photos")
        assert result["posts_discovered"] == 2
        assert {p.message_id for p in posts} == {5, 4}
        assert scraper.fetch_args == [(channel, None)]

    def test_news_default_is_single_page(self, tmp_path):
        db = str(tmp_path / "hist_news.db")
        channel = "wire"
        page1 = make_sample_html(channel, [{"message_id": 9, "text": "nine"}])
        page2 = make_sample_html(channel, [{"message_id": 8, "text": "eight"}])
        scraper = MockScraper(
            page_map={
                (channel, None): page1,
                (channel, 9): page2,
            }
        )
        runner = NewsMonitorRunner(
            self._settings(db),
            PostRepository(db),
            scraper,
            MockEvaluator(),
            MockWebhookSender(),
        )
        posts, result = runner._ingest_channel(channel, group_id="news24")
        assert result["posts_discovered"] == 1
        assert posts[0].message_id == 9
        assert scraper.fetch_args == [(channel, None)]

    def test_http_before_query_and_caught_up(self, tmp_path):
        urls: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            urls.append(str(request.url))
            if "before=90" in str(request.url):
                return httpx.Response(
                    200,
                    text=make_sample_html(
                        "oldpix",
                        [
                            {"message_id": 80, "text": "eighty"},
                            {"message_id": 85, "text": "eighty-five"},
                            {"message_id": 89, "text": "eighty-nine"},
                        ],
                    ),
                )
            if "before=" in str(request.url):
                return httpx.Response(200, text="<html><body></body></html>")
            return httpx.Response(
                200,
                text=make_sample_html(
                    "oldpix",
                    [
                        {"message_id": 90, "text": "ninety"},
                        {"message_id": 95, "text": "ninety-five"},
                        {"message_id": 100, "text": "hundred"},
                    ],
                ),
            )

        db = str(tmp_path / "hist_http.db")
        repo = PostRepository(db)
        now = _now()
        repo.save_posts(
            [
                TelegramPost(
                    channel="oldpix",
                    message_id=mid,
                    text=f"old {mid}",
                    direct_url=f"https://t.me/oldpix/{mid}",
                    published_at=now,
                )
                for mid in (80, 85, 89)
            ],
            group_id="old_photos",
        )
        client = TelegramScraperClient(
            http_client=httpx.Client(transport=httpx.MockTransport(handler))
        )
        runner = NewsMonitorRunner(
            self._settings(db, scrape_history_pages=8),
            repo,
            client,
            MockEvaluator(),
            MockWebhookSender(),
        )
        posts, result = runner._ingest_channel("oldpix", group_id="old_photos")
        assert result["success"] is True
        assert {p.message_id for p in posts} == {100, 95, 90}
        assert urls[0] == "https://t.me/s/oldpix"
        assert urls[1] == "https://t.me/s/oldpix?before=90"
        assert len(urls) == 2


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
