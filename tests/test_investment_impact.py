"""Conditional「💹 投资影响」on news24 single-item digest cards."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from tg_news_monitor.core.models import DigestItem
from tg_news_monitor.evaluator.fallback import VALID_CATEGORIES, normalize_and_validate_evaluation
from tg_news_monitor.evaluator.prompt import (
    DIGEST_SYSTEM_PROMPT,
    NEWS_EVALUATION_JSON_SCHEMA,
    SYSTEM_PROMPT,
)
from tg_news_monitor.notifier.feishu_card import (
    FeishuCardBuilder,
    should_show_investment_impact,
)


def _item(*, score: int, category: str) -> DigestItem:
    return DigestItem(
        rank=1,
        channel="wire",
        message_id=1,
        title="测试标题",
        summary="央行宣布紧急降息以稳定经济。",
        category=category,
        score=score,
        summary_bullets=["央行宣布紧急降息以稳定经济。"],
        actionable_insight="关注后续官方确认。",
        bias_overall="利多",
        bias_us="中性",
        bias_cn="利多",
        bias_commodities="不确定",
        impact_overall="总体偏多",
        impact_us="无直接影响",
        impact_cn="对上证情绪构成支撑",
        impact_commodities="传导尚不明确",
        published_at=datetime(2026, 9, 10, 2, 0, 0, tzinfo=timezone.utc),
    )


def _card_blob(score: int, category: str, include_flag: bool = True) -> str:
    return str(
        FeishuCardBuilder.build_digest_item_card(
            _item(score=score, category=category),
            include_investment_impact=include_flag,
        )
    )


def _has_investment_block(blob: str) -> bool:
    return "💹 投资影响" in blob or "**💹 投资影响**" in blob


class TestShouldShowInvestmentImpact:
    @pytest.mark.parametrize("category", ["军事", "地缘政治", "宏观财经", " 宏观财经 "])
    def test_score_9_whitelist_on(self, category: str) -> None:
        assert should_show_investment_impact(9, category, True) is True

    def test_score_8_macro_off(self) -> None:
        assert should_show_investment_impact(8, "宏观财经", True) is False

    def test_score_10_tech_off(self) -> None:
        assert should_show_investment_impact(10, "科技/AI", True) is False

    def test_flag_false_military_score_10_off(self) -> None:
        assert should_show_investment_impact(10, "军事", False) is False

    def test_invalid_score_off(self) -> None:
        assert should_show_investment_impact("x", "军事", True) is False


class TestDigestItemCardGate:
    @pytest.mark.parametrize("category", ["军事", "地缘政治", "宏观财经"])
    def test_score_9_whitelist_renders_block_and_four_rows(self, category: str) -> None:
        blob = _card_blob(9, category)
        assert _has_investment_block(blob)
        assert "🌐 整体" in blob
        assert "📈 美股" in blob
        assert "📊 上证" in blob
        assert "🛢️ 大宗" in blob
        assert "🕒 **发布时间**" in blob
        assert "**📌 核心速览**" in blob
        assert "**🎯 关注建议**" in blob

    def test_score_8_macro_omits_block(self) -> None:
        blob = _card_blob(8, "宏观财经")
        assert not _has_investment_block(blob)
        assert "🌐 整体" not in blob
        assert "📈 美股" not in blob
        assert "📊 上证" not in blob
        assert "🛢️ 大宗" not in blob
        assert "🕒 **发布时间**" in blob
        assert "**📌 核心速览**" in blob
        assert "**🎯 关注建议**" in blob

    def test_score_10_tech_omits_block(self) -> None:
        blob = _card_blob(10, "科技/AI")
        assert not _has_investment_block(blob)
        assert "🕒 **发布时间**" in blob
        assert "**🎯 关注建议**" in blob

    def test_flag_false_military_score_10_omits_block(self) -> None:
        blob = _card_blob(10, "军事", include_flag=False)
        assert not _has_investment_block(blob)
        assert "🕒 **发布时间**" in blob
        assert "**🎯 关注建议**" in blob

    def test_wechat_photo_card_never_has_block(self) -> None:
        item = _item(score=10, category="军事")
        blob = str(FeishuCardBuilder.build_wechat_photo_card(item))
        assert "投资影响" not in blob
        assert "🌐 整体" not in blob


class TestMilitaryCategoryTaxonomy:
    def test_digest_prompt_allows_military_and_documents_split(self) -> None:
        assert "军事" in DIGEST_SYSTEM_PROMPT
        assert "地缘政治" in DIGEST_SYSTEM_PROMPT
        assert "战争" in DIGEST_SYSTEM_PROMPT or "冲突" in DIGEST_SYSTEM_PROMPT
        assert "外交" in DIGEST_SYSTEM_PROMPT or "制裁" in DIGEST_SYSTEM_PROMPT

    def test_single_item_prompt_schema_and_valid_set(self) -> None:
        assert "军事" in SYSTEM_PROMPT
        assert "军事" in NEWS_EVALUATION_JSON_SCHEMA["properties"]["category"]["enum"]
        assert "军事" in VALID_CATEGORIES
        assert "地缘政治" in VALID_CATEGORIES

    def test_normalize_keeps_military(self) -> None:
        ev = normalize_and_validate_evaluation(
            {
                "score": 9,
                "is_news": True,
                "is_spam": False,
                "category": "军事",
                "title": "边境交火升级",
                "summary_bullets": ["边境交火升级。"],
                "key_takeaways": ["关注局势。"],
            }
        )
        assert ev.category == "军事"
