"""Credibility, display_score, compact cards, and wechat near-dup (TDD)."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from tg_news_monitor.config import parse_yaml_file
from tg_news_monitor.core.models import DigestItem
from tg_news_monitor.core.near_dup import similar_event
from tg_news_monitor.core.runner import NewsMonitorRunner
from tg_news_monitor.evaluator.fallback import parse_digest_brief
from tg_news_monitor.evaluator.prompt import (
    DIGEST_SYSTEM_PROMPT,
    WECHAT_PHOTO_DIGEST_SYSTEM_PROMPT,
)
from tg_news_monitor.notifier.feishu_card import (
    FeishuCardBuilder,
    should_show_investment_impact,
)
from tg_news_monitor.storage.repository import PostRepository
from tests.test_runner import MockEvaluator, MockScraper, MockWebhookSender
from tests.test_wechat_photo import _post as _wechat_post

BJ = datetime(2026, 9, 12, 4, 0, tzinfo=timezone.utc)

IRAN_RUMOR_TITLE = "伊朗或将封锁霍尔木兹海峡冲击油价"


def _news_item(
    *,
    score: int = 10,
    category: str = "地缘政治",
    verification_status: str = "single_source",
    title: str = IRAN_RUMOR_TITLE,
    bias_overall: str = "利空",
    bias_us: str = "利空",
    bias_cn: str = "中性",
    bias_commodities: str = "利空",
    impact_overall: str = "风险资产承压",
    impact_us: str = "能源与航运波动加大",
    impact_cn: str = "无直接影响",
    impact_commodities: str = "原油偏多、风险偏好回落",
) -> DigestItem:
    return DigestItem(
        rank=1,
        channel="breakingnews",
        message_id=88,
        title=title,
        summary="社交媒体流传伊朗可能封锁霍尔木兹海峡，尚无官方证实。",
        category=category,
        score=score,
        verification_status=verification_status,
        summary_bullets=["社交媒体流传伊朗可能封锁海峡，尚未获官方证实。"],
        actionable_insight="等待官方与主流通讯社核实后再判断方向。",
        bias_overall=bias_overall,
        bias_us=bias_us,
        bias_cn=bias_cn,
        bias_commodities=bias_commodities,
        impact_overall=impact_overall,
        impact_us=impact_us,
        impact_cn=impact_cn,
        impact_commodities=impact_commodities,
        published_at=BJ,
    )


def _card_elements(item: DigestItem, include_flag: bool = True):
    payload = FeishuCardBuilder.build_digest_item_card(
        item, include_investment_impact=include_flag
    )
    return payload["card"], str(payload)


class TestVerificationStatusModelAndParse:
    def test_default_is_single_source(self) -> None:
        item = DigestItem(rank=1, channel="wire", message_id=1, title="标题")
        assert item.verification_status == "single_source"
        assert item.score is None

    @pytest.mark.parametrize("status", ["official", "multi_source", "single_source", "rumor"])
    def test_allowed_values_kept(self, status: str) -> None:
        item = DigestItem(
            rank=1, channel="wire", message_id=1, title="标题", verification_status=status
        )
        assert item.verification_status == status

    @pytest.mark.parametrize("raw", ["", "verified", "OFFICIAL", "官方", None, "leak"])
    def test_invalid_or_missing_falls_back_to_single_source(self, raw: object) -> None:
        payload = {
            "headline": "本轮快讯",
            "overview": "",
            "has_material_news": True,
            "items": [
                {
                    "rank": 1,
                    "channel": "reuters",
                    "message_id": 9,
                    "title": "标题",
                    "summary": "路透社频道发了一条未具名消息。",
                    "category": "地缘政治",
                    "score": 9,
                }
            ],
        }
        if raw is not None:
            payload["items"][0]["verification_status"] = raw
        brief = parse_digest_brief(json.dumps(payload, ensure_ascii=False))
        assert brief.items[0].verification_status == "single_source"
        assert brief.items[0].channel == "reuters"

    def test_parse_keeps_official_from_text_evidence_field(self) -> None:
        payload = {
            "headline": "本轮快讯",
            "overview": "",
            "has_material_news": True,
            "items": [
                {
                    "rank": 1,
                    "channel": "reuters",
                    "message_id": 10,
                    "title": "央行发布降息公告",
                    "summary": "正文写明央行官网发布降息公告。",
                    "category": "宏观财经",
                    "score": 9,
                    "verification_status": "official",
                }
            ],
        }
        brief = parse_digest_brief(json.dumps(payload, ensure_ascii=False))
        assert brief.items[0].verification_status == "official"
        assert brief.items[0].channel == "reuters"

    def test_schema_enum_and_default(self) -> None:
        from tg_news_monitor.evaluator.prompt import get_digest_item_json_schema

        schema = get_digest_item_json_schema()
        field = schema["properties"]["verification_status"]
        assert field["enum"] == ["official", "multi_source", "single_source", "rumor"]
        assert field.get("default") == "single_source"

    def test_prompt_judges_from_post_text_not_channel_name(self) -> None:
        assert "verification_status" in DIGEST_SYSTEM_PROMPT
        assert "official" in DIGEST_SYSTEM_PROMPT
        assert "multi_source" in DIGEST_SYSTEM_PROMPT
        assert "single_source" in DIGEST_SYSTEM_PROMPT
        assert "rumor" in DIGEST_SYSTEM_PROMPT
        assert "频道名" in DIGEST_SYSTEM_PROMPT or "频道名称" in DIGEST_SYSTEM_PROMPT
        assert "正文" in DIGEST_SYSTEM_PROMPT or "帖子" in DIGEST_SYSTEM_PROMPT


class TestDisplayScoreAndVisualGates:
    def test_display_score_does_not_mutate_raw_score(self) -> None:
        from tg_news_monitor.notifier.feishu_card import display_score

        item = _news_item(score=10, verification_status="rumor")
        shown = display_score(item)
        assert item.score == 10
        assert shown == 8
        assert shown < item.score

    def test_official_keeps_red_five_flames_and_investment(self) -> None:
        from tg_news_monitor.notifier.feishu_card import display_score

        item = _news_item(score=10, verification_status="official")
        assert display_score(item) == 10
        assert should_show_investment_impact(10, "地缘政治", True, "official") is True
        card, blob = _card_elements(item)
        assert card["header"]["template"] == "red"
        assert "🔥🔥🔥🔥🔥" in blob
        assert "特急" in blob
        assert "💹 投资影响" in blob

    @pytest.mark.parametrize("status,badge", [("single_source", "待核实"), ("rumor", "传闻")])
    def test_unverified_caps_orange_and_hides_investment(self, status: str, badge: str) -> None:
        from tg_news_monitor.notifier.feishu_card import display_score

        item = _news_item(score=10, verification_status=status)
        assert display_score(item) <= 8
        assert should_show_investment_impact(10, "地缘政治", True, status) is False
        card, blob = _card_elements(item)
        assert card["header"]["template"] == "orange"
        assert "🔥🔥🔥🔥🔥" not in blob
        assert "特急" not in blob
        assert badge in blob
        assert "💹 投资影响" not in blob
        assert "利空" not in blob or "投资影响" not in blob
        assert item.score == 10


class TestCompactHeaderAndInvestmentLines:
    def test_header_is_icon_category_level_title_is_first_body(self) -> None:
        item = _news_item(score=10, verification_status="official")
        card, blob = _card_elements(item)
        header = card["header"]["title"]["content"]
        assert IRAN_RUMOR_TITLE not in header
        assert "地缘政治" in header
        assert "｜" in header
        assert "特急" in header
        first = card["body"]["elements"][0]
        assert first.get("tag") == "div"
        assert IRAN_RUMOR_TITLE in first["text"]["content"]

    def test_rumor_header_uses_hearsay_level(self) -> None:
        item = _news_item(score=10, verification_status="rumor")
        card, _ = _card_elements(item)
        header = card["header"]["title"]["content"]
        assert IRAN_RUMOR_TITLE not in header
        assert header.endswith("传闻") or "｜传闻" in header
        assert IRAN_RUMOR_TITLE in card["body"]["elements"][0]["text"]["content"]

    def test_investment_only_non_neutral_max_three_lines(self) -> None:
        item = _news_item(verification_status="multi_source")
        _card, blob = _card_elements(item)
        assert blob.count("🌐 整体") == 1
        assert blob.count("📈 美股") == 1
        assert blob.count("🛢️ 大宗") == 1
        assert "📊 上证" not in blob
        assert blob.count("column_set") <= 3

    def test_all_neutral_omits_investment_block(self) -> None:
        item = _news_item(
            verification_status="official",
            bias_overall="中性",
            bias_us="不确定",
            bias_cn="中性",
            bias_commodities="不确定",
            impact_overall="无直接影响",
            impact_us="无直接影响",
            impact_cn="无直接影响",
            impact_commodities="传导尚不明确",
        )
        _card, blob = _card_elements(item)
        assert "💹 投资影响" not in blob
        assert "🌐 整体" not in blob


SIMAO_1930 = "照片为云南思茅马帮在茶马古道上负重前行，约1930年代。"
SIMAO_1980 = "照片记录1980年代思茅另一支马帮在江边歇脚，画面呈现商队生活。"
SIMAO_DUP = "画面呈现云南思茅马帮在茶马古道上负重前行，约1930年代。"


class TestWechatNearDupAndPrompt:
    def test_shared_templates_do_not_collapse_different_era_subjects(self) -> None:
        assert not similar_event(SIMAO_1930, SIMAO_1980)

    def test_same_subject_after_template_strip_is_near_dup(self) -> None:
        assert similar_event(SIMAO_1930, SIMAO_DUP)

    def test_wechat_prompt_forbids_template_and_fluff(self) -> None:
        prompt = WECHAT_PHOTO_DIGEST_SYSTEM_PROMPT
        assert "照片为" in prompt
        assert "照片记录" in prompt
        assert "画面呈现" in prompt
        assert "繁荣景象" in prompt
        assert "珍贵影像" in prompt
        assert "约" in prompt
        assert "据原帖" in prompt
        assert "尚待考证" in prompt

    def _photo_settings(self, db: str):
        from tg_news_monitor.config import Settings

        return Settings(
            db_path=db,
            digest_min_candidates=1,
            digest_max_wait_seconds=0,
            digest_card_interval_seconds=0,
            digest_min_interval_seconds=0,
            news_max_age_seconds=86400,
            morning_flush_enabled=False,
            quiet_hours="",
            shoulder_hours="",
            groups=[
                {
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
            ],
        )

    def _photo_item(self, post, title: str, summary: str) -> DigestItem:
        return DigestItem(
            rank=1,
            channel=post.channel,
            message_id=post.message_id,
            title=title,
            summary=summary,
            category="历史影像",
            score=8,
            event_at=post.published_at,
            impact_overall="无直接影响",
            impact_us="无直接影响",
            impact_cn="无直接影响",
            impact_commodities="无直接影响",
            media_urls=list(post.media_urls or []),
        )

    def test_within_digest_marks_near_duplicate(self, tmp_path) -> None:
        db = str(tmp_path / "simao_in.db")
        repo = PostRepository(db)
        posts = [
            _wechat_post(21, SIMAO_1930, published_at=BJ),
            _wechat_post(22, SIMAO_DUP, published_at=BJ),
        ]
        repo.save_posts(posts, group_id="old_photos")

        def builder(batch):
            from tg_news_monitor.core.models import DigestBrief

            items = [
                self._photo_item(batch[0], "思茅马帮古道", SIMAO_1930),
                self._photo_item(batch[1], "思茅马帮古道", SIMAO_DUP),
            ]
            items[1].rank = 2
            return DigestBrief(headline="影像", overview="", items=items, has_material_news=True)

        sender = MockWebhookSender()
        runner = NewsMonitorRunner(
            self._photo_settings(db),
            repo,
            MockScraper(),
            MockEvaluator(digest_builder=builder),
            sender,
        )
        summary = runner.process_pending(now=BJ)
        assert summary["alerts_sent"] == 1
        assert len(sender.sent_payloads) == 1
        row = repo.get_post("oldpix", 22, group_id="old_photos")
        assert row["filter_reason"] == "near_duplicate"

    def test_across_digests_marks_near_duplicate(self, tmp_path) -> None:
        db = str(tmp_path / "simao_across.db")
        repo = PostRepository(db)
        repo.save_posts([_wechat_post(31, SIMAO_1930, published_at=BJ)], group_id="old_photos")

        def first(batch):
            from tg_news_monitor.core.models import DigestBrief

            return DigestBrief(
                headline="影像",
                overview="",
                items=[self._photo_item(batch[0], "思茅马帮古道", SIMAO_1930)],
                has_material_news=True,
            )

        sender = MockWebhookSender()
        runner = NewsMonitorRunner(
            self._photo_settings(db),
            repo,
            MockScraper(),
            MockEvaluator(digest_builder=first),
            sender,
        )
        assert runner.process_pending(now=BJ)["alerts_sent"] == 1

        later = datetime(2026, 9, 12, 5, 0, tzinfo=timezone.utc)
        repo.save_posts([_wechat_post(32, SIMAO_DUP, published_at=later)], group_id="old_photos")

        def second(batch):
            from tg_news_monitor.core.models import DigestBrief

            return DigestBrief(
                headline="影像",
                overview="",
                items=[self._photo_item(batch[0], "思茅马帮古道", SIMAO_DUP)],
                has_material_news=True,
            )

        runner.evaluator.digest_builder = second
        assert runner.process_pending(now=later)["alerts_sent"] == 0
        assert len(sender.sent_payloads) == 1
        row = repo.get_post("oldpix", 32, group_id="old_photos")
        assert row["filter_reason"] == "near_duplicate"

    def test_different_era_simao_pair_still_sends(self, tmp_path) -> None:
        db = str(tmp_path / "simao_era.db")
        repo = PostRepository(db)
        repo.save_posts(
            [
                _wechat_post(41, SIMAO_1930, published_at=BJ),
                _wechat_post(42, SIMAO_1980, published_at=BJ),
            ],
            group_id="old_photos",
        )

        def builder(batch):
            from tg_news_monitor.core.models import DigestBrief

            items = [
                self._photo_item(batch[0], "一九三零思茅马帮", SIMAO_1930),
                self._photo_item(batch[1], "一九八零思茅歇脚", SIMAO_1980),
            ]
            items[1].rank = 2
            return DigestBrief(headline="影像", overview="", items=items, has_material_news=True)

        sender = MockWebhookSender()
        runner = NewsMonitorRunner(
            self._photo_settings(db),
            repo,
            MockScraper(),
            MockEvaluator(digest_builder=builder),
            sender,
        )
        summary = runner.process_pending(now=BJ)
        assert summary["alerts_sent"] == 2
        assert len(sender.sent_payloads) == 2


class TestExampleConfig:
    def test_old_photos_digest_max_batch_size_is_two(self) -> None:
        data = parse_yaml_file(Path("config.yaml.example"))
        old = next(g for g in data["groups"] if g["id"] == "old_photos")
        assert old["digest_max_batch_size"] == 2
