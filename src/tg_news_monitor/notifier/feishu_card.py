"""Feishu (Lark) Interactive Message Card Schema 2.0 Builder.

Constructs rich interactive alert cards compliant with Feishu Card Schema 2.0:
- Dynamic 4-tier color templates (red, orange, blue, grey) and standard icon badges based on Grok urgency score.
- Structured Chinese headline with category tag and score indicator.
- Markdown summary block displaying urgency score, source channel, and bulleted news points.
- Divider element (hr) providing clean visual hierarchy.
- Key takeaways block highlighting market/industry impact.
- Note element with UTC publication timestamp, message ID, and forward attribution.
- Primary action button linking directly to the original Telegram post (https://t.me/{channel}/{message_id}).
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Union
from datetime import timedelta

from tg_news_monitor.core.models import DigestBrief, DigestItem, NewsEvaluation, TelegramPost
from tg_news_monitor.core.wechat_photo import (
    normalize_wechat_caption,
    photo_link_urls,
    strip_tg_traces,
)
from tg_news_monitor.notifier.card_format import (
    format_detail_lines,
    is_long_summary,
    polish_overview_bullet,
    shorten_summary,
)


# ==============================================================================
# 4-Tier Severity Color Template & Icon Mappings
# ==============================================================================

COLOR_TEMPLATE_MAP: Dict[str, str] = {
    "breaking": "red",      # 9-10: Extreme urgency / breaking disaster
    "major": "orange",      # 7-8: High importance / breaking alert
    "standard": "blue",     # 5-6: Normal industry news
    "low": "grey",          # 1-4: Low urgency / chatter / noise
}

HEADER_ICON_MAP: Dict[str, str] = {
    "red": "alarm_outlined",
    "orange": "bell_outlined",
    "blue": "info_outlined",
    "grey": "cross_outlined",
}


def get_color_template(score: int) -> str:
    """Maps urgency score (1-10) to Feishu card header color template token.

    - Score 9-10: 'red' (Breaking / Extreme urgency)
    - Score 7-8: 'orange' (High importance)
    - Score 5-6: 'blue' (Standard news)
    - Score 1-4: 'grey' (Low urgency)
    """
    try:
        normalized_score = int(score)
    except (TypeError, ValueError):
        normalized_score = 1

    if normalized_score >= 9:
        return "red"
    elif normalized_score >= 7:
        return "orange"
    elif normalized_score >= 5:
        return "blue"
    else:
        return "grey"


def get_header_icon(score: int) -> str:
    """Maps urgency score to Feishu standard header icon token."""
    template = get_color_template(score)
    return HEADER_ICON_MAP.get(template, "info_outlined")


# ==============================================================================
# Feishu Card Builder Class & Helper Functions
# ==============================================================================

class FeishuCardBuilder:
    """Builder for Feishu Interactive Card Schema 2.0 payloads."""

    @classmethod
    def get_color_template(cls, score: int) -> str:
        return get_color_template(score)

    @classmethod
    def get_header_icon(cls, score: int) -> str:
        return get_header_icon(score)

    @classmethod
    def build_card(
        cls,
        post: Union[TelegramPost, Dict[str, Any]],
        evaluation: Union[NewsEvaluation, Dict[str, Any]],
        subtitle: str = "Telegram 实时新闻监控中心",
    ) -> Dict[str, Any]:
        """Constructs a complete Feishu Interactive Card Schema 2.0 payload.

        Args:
            post: Scraped TelegramPost domain model or equivalent dictionary.
            evaluation: NewsEvaluation result or equivalent dictionary.
            subtitle: Card header subtitle string.

        Returns:
            Dict conforming to Feishu Interactive Card Schema 2.0 structure:
            {"msg_type": "interactive", "card": {"schema": "2.0", ...}}
        """
        # Extract post attributes safely
        if isinstance(post, dict):
            channel = str(post.get("channel", "telegram")).lstrip("@")
            message_id = post.get("message_id", 0)
            published_at = post.get("published_at")
            forward_from = post.get("forward_from")
            direct_url = post.get("direct_url") or f"https://t.me/{channel}/{message_id}"
            post_text = post.get("text", "")
        else:
            channel = str(post.channel).lstrip("@")
            message_id = post.message_id
            published_at = post.published_at
            forward_from = post.forward_from
            direct_url = post.direct_url or f"https://t.me/{channel}/{message_id}"
            post_text = getattr(post, "text", "")

        # Extract evaluation attributes safely
        if isinstance(evaluation, dict):
            raw_score = evaluation.get("score") or evaluation.get("urgency_score", 1)
            title = evaluation.get("title", "实时资讯快讯")
            category = evaluation.get("category", "行业快讯")
            summary_bullets = evaluation.get("summary_bullets") or evaluation.get("summary_points") or []
            key_takeaways = evaluation.get("key_takeaways") or []
            actionable_insight = evaluation.get("actionable_insight")
        else:
            raw_score = evaluation.score
            title = evaluation.title or "实时资讯快讯"
            category = evaluation.category or "行业快讯"
            summary_bullets = evaluation.summary_bullets or []
            key_takeaways = evaluation.key_takeaways or []
            actionable_insight = evaluation.actionable_insight

        # Clamp and normalize score
        try:
            score = max(1, min(10, int(raw_score)))
        except (TypeError, ValueError):
            score = 1

        color_template = get_color_template(score)
        # Header Title with tier emoji & category tag
        if score >= 9:
            tier_prefix = "🚨"
        elif score >= 7:
            tier_prefix = "⚡"
        elif score >= 5:
            tier_prefix = "📢"
        else:
            tier_prefix = "ℹ️"

        header_title = f"{tier_prefix} [{score}/10] {category} | {title}"

        # 1. Summary Bullets formatting
        if isinstance(summary_bullets, str):
            bullets_list = [summary_bullets]
        else:
            bullets_list = list(summary_bullets)

        if bullets_list:
            formatted_bullets = "\n".join(
                f"• {b.lstrip('•-* ')}" for b in bullets_list if str(b).strip()
            )
        elif post_text.strip():
            snippet = post_text.strip()[:200]
            if len(post_text.strip()) > 200:
                snippet += "..."
            formatted_bullets = f"• {snippet}"
        else:
            formatted_bullets = "• 暂无详细摘要要点"

        score_stars = "🔥" * min(5, max(1, (score + 1) // 2))
        summary_markdown = (
            f"**🏷️ 资讯分类**：{category}    |    **📢 来源频道**：@{channel}    |    **🔥 紧迫度评分**：{score} / 10 {score_stars}\n\n"
            f"**📌 核心速览**\n{formatted_bullets}"
        )

        markdown_element = {
            "tag": "div",
            "text": {
                "tag": "lark_md",
                "content": summary_markdown,
            },
        }

        # 2. Divider element (hr)
        divider_element = {
            "tag": "hr",
        }

        # 3. Key Takeaways Block
        if isinstance(key_takeaways, list):
            if key_takeaways:
                takeaways_text = "\n".join(
                    f"• {t.lstrip('•-* ')}" for t in key_takeaways if str(t).strip()
                )
            else:
                takeaways_text = "紧密跟踪后续进展。"
        else:
            takeaways_text = str(key_takeaways).strip() or "紧密跟踪后续进展。"

        takeaways_markdown = f"**💡 关键影响**\n{takeaways_text}"
        if actionable_insight and str(actionable_insight).strip():
            takeaways_markdown += f"\n\n**🎯 关注建议**\n{str(actionable_insight).strip()}"

        takeaways_element = {
            "tag": "div",
            "text": {
                "tag": "lark_md",
                "content": takeaways_markdown,
            },
        }

        # 4. Note block with original publication time (UTC) and forward attribution
        if isinstance(published_at, datetime):
            # Ensure UTC representation
            if published_at.tzinfo is not None:
                utc_dt = published_at.astimezone(timezone.utc)
            else:
                utc_dt = published_at.replace(tzinfo=timezone.utc)
            pub_time_str = utc_dt.strftime("%Y-%m-%d %H:%M:%S UTC")
        elif published_at:
            pub_time_str = str(published_at)
        else:
            pub_time_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

        note_parts = [
            f"📢 来源频道: @{channel}",
            f"🕒 发布时间: {pub_time_str}",
            f"🆔 消息ID: #{message_id}",
        ]
        if forward_from and str(forward_from).strip():
            note_parts.append(f"🔁 转发自: {str(forward_from).strip()}")

        # Schema 2.0 webhook cards reject tag "note" (ErrCode 200861).
        # Use a markdown div for the same metadata.
        note_element = {
            "tag": "div",
            "text": {
                "tag": "lark_md",
                "content": "📝 " + "  |  ".join(note_parts),
            },
        }

        # 5. Action element: primary button linking directly to original post
        action_url = direct_url if direct_url else f"https://t.me/{channel}/{message_id}"
        # Schema 2.0 rejects legacy "action" wrapper (ErrCode 200861).
        # Use a button with open_url behaviors, plus a markdown fallback link.
        action_element = {
            "tag": "button",
            "text": {
                "tag": "plain_text",
                "content": "🔗 查看 Telegram 原文",
            },
            "type": "primary",
            "behaviors": [
                {
                    "type": "open_url",
                    "default_url": action_url,
                    "pc_url": action_url,
                    "ios_url": action_url,
                    "android_url": action_url,
                }
            ],
        }

        # Assemble elements in clean visual hierarchy
        elements = [
            markdown_element,
            divider_element,
            takeaways_element,
            note_element,
            action_element,
        ]

        card_schema_2 = {
            "schema": "2.0",
            "header": {
                "title": {
                    "tag": "plain_text",
                    "content": header_title,
                },
                "subtitle": {
                    "tag": "plain_text",
                    "content": subtitle,
                },
                "template": color_template,
                # webhook 自定义机器人不支持 header.ud_icon（会报 200621）
            },
            "body": {
                "elements": elements,
            },
        }

        return {
            "msg_type": "interactive",
            "card": card_schema_2,
        }


    @classmethod
    def build_digest_card(
        cls,
        digest: DigestBrief,
        subtitle: str = "Telegram 批量快讯汇总",
    ) -> Dict[str, Any]:
        """Build a Schema 2.0 interactive digest card (NO Telegram links/buttons)."""
        headline = (digest.headline or "本轮快讯汇总").strip()
        item_count = len(digest.items or [])
        # Prefer orange when material news exists, else blue
        color_template = "orange" if digest.has_material_news and item_count else "blue"

        elements: List[Dict[str, Any]] = []

        overview = (digest.overview or "").strip() or "本轮暂无概述。"
        elements.append(
            {
                "tag": "div",
                "text": {
                    "tag": "lark_md",
                    "content": f"**📰 本轮总览**\n{overview}",
                },
            }
        )
        elements.append({"tag": "hr"})

        for item in digest.items or []:
            rank = getattr(item, "rank", 0)
            title = getattr(item, "title", "") or "未命名"
            summary = getattr(item, "summary", "") or ""
            category = getattr(item, "category", "") or "行业快讯"
            channel = str(getattr(item, "channel", "") or "").lstrip("@")
            mid = getattr(item, "message_id", 0)
            impact_block = (
                f"**① 总体**：{getattr(item, 'impact_overall', '') or '影响有限'}\n"
                f"**② 美股**：{getattr(item, 'impact_us', '') or '影响有限'}\n"
                f"**③ 上证**：{getattr(item, 'impact_cn', '') or '影响有限'}\n"
                f"**④ 大宗（黄金/原油等）**：{getattr(item, 'impact_commodities', '') or '影响有限'}"
            )
            content = (
                f"**#{rank} [{category}] {title}**\n"
                f"来源：@{channel} / #{mid}\n"
                f"{summary}\n\n"
                f"**四维影响**\n{impact_block}"
            )
            elements.append(
                {
                    "tag": "div",
                    "text": {
                        "tag": "lark_md",
                        "content": content,
                    },
                }
            )
            elements.append({"tag": "hr"})

        # Drop trailing hr if present
        if elements and elements[-1].get("tag") == "hr":
            elements.pop()

        note = (digest.filtered_note or "").strip()
        if note:
            elements.append(
                {
                    "tag": "div",
                    "text": {
                        "tag": "lark_md",
                        "content": f"**🧹 过滤说明**：{note}",
                    },
                }
            )

        card_schema_2 = {
            "schema": "2.0",
            "header": {
                "title": {
                    "tag": "plain_text",
                    "content": headline,
                },
                "subtitle": {
                    "tag": "plain_text",
                    "content": subtitle,
                },
                "template": color_template,
            },
            "body": {
                "elements": elements,
            },
        }
        return {
            "msg_type": "interactive",
            "card": card_schema_2,
        }

    @classmethod
    def build_digest_item_card(
        cls,
        item: DigestItem,
        published_at: Optional[datetime] = None,
        subtitle: str = "投资情报快报",
        include_investment_impact: bool = True,
    ) -> Dict[str, Any]:
        """Build Schema 2.0 single-item card. No Telegram/links/buttons/italics.

        Urgency via header color. Investment impact uses column_set for alignment
        (label | bias | detail). Falls back to stacked rows if needed at send time
        is not handled here — column_set is the primary layout.
        """
        rank = int(getattr(item, "rank", 1) or 1)
        title = (getattr(item, "title", None) or "未命名").strip()
        category = (getattr(item, "category", None) or "行业快讯").strip()

        raw_score = getattr(item, "score", None)
        if raw_score is None:
            score = max(1, min(10, 11 - rank))
        else:
            try:
                score = max(1, min(10, int(raw_score)))
            except (TypeError, ValueError):
                score = max(1, min(10, 11 - rank))

        color_template = get_color_template(score)
        if score >= 9:
            tier_emoji, urgency_label = "🚨", "特急"
        elif score >= 7:
            tier_emoji, urgency_label = "⚡", "重要"
        elif score >= 5:
            tier_emoji, urgency_label = "📢", "一般"
        else:
            tier_emoji, urgency_label = "ℹ️", "低优"

        header_title = f"{tier_emoji} {category}｜{title}"

        dt = published_at if published_at is not None else getattr(item, "published_at", None)
        if isinstance(dt, datetime):
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            bj = dt.astimezone(timezone(timedelta(hours=8)))
            time_str = bj.strftime("%Y-%m-%d %H:%M")
        else:
            time_str = "未知"

        flames = "🔥" * min(5, max(1, (score + 1) // 2))
        time_md = (
            f"🕒 **发布时间** {time_str}（北京时间）\n"
            f"🎚️ **紧急** **{urgency_label}** {flames}"
        )

        summary = (getattr(item, "summary", None) or "").strip()
        bullets = getattr(item, "summary_bullets", None) or []
        if isinstance(bullets, str):
            bullets_list = [bullets]
        else:
            bullets_list = [str(b).strip() for b in bullets if str(b).strip()]
        bullets_list = [
            polish_overview_bullet(b.lstrip("•-* "))
            for b in bullets_list
            if b
        ]
        bullets_list = [b for b in bullets_list if b]

        # Prefer complete bullets as 核心速览. Long summaries may add 事件详情
        # only for extra points beyond the first three bullets.
        if not bullets_list:
            core = polish_overview_bullet(summary) or "暂无详细摘要要点"
            overview_md = f"**📌 核心速览**\n{core}"
        else:
            show = bullets_list[:3]
            bullet_icons = ["1️⃣", "2️⃣", "3️⃣"]
            bullets_md = "\n".join(
                f"{bullet_icons[i]} {b}" for i, b in enumerate(show)
            )
            overview_md = f"**📌 核心速览**\n{bullets_md}"
            extra = bullets_list[3:6]
            if is_long_summary(summary) and extra:
                detail_block = format_detail_lines(extra)
                if detail_block:
                    overview_md += f"\n📌 事件详情\n{detail_block}"

        allowed_bias = {"利多", "利空", "中性", "不确定"}
        bias_emoji = {
            "利多": "🟢 利多",
            "利空": "🔴 利空",
            "中性": "⚪ 中性",
            "不确定": "🟡 不确定",
        }

        def _bias(name: str) -> str:
            val = str(getattr(item, name, None) or "不确定").strip()
            if val not in allowed_bias:
                val = "不确定"
            return bias_emoji[val]

        def _impact(name: str, fallback: str = "影响有限") -> str:
            val = str(getattr(item, name, None) or "").strip()
            return val or fallback

        def _md_div(content: str) -> Dict[str, Any]:
            return {"tag": "div", "text": {"tag": "lark_md", "content": content}}

        def _impact_row(label: str, bias_key: str, impact_key: str) -> Dict[str, Any]:
            # Compact: fixed-narrow label/bias, detail takes remaining width (less wrap)
            return {
                "tag": "column_set",
                "flex_mode": "none",
                "background_style": "default",
                "horizontal_spacing": "4px",
                "columns": [
                    {
                        "tag": "column",
                        "width": "90px",
                        "vertical_align": "center",
                        "elements": [_md_div(f"**{label}**")],
                    },
                    {
                        "tag": "column",
                        "width": "72px",
                        "vertical_align": "center",
                        "elements": [_md_div(f"**{_bias(bias_key)}**")],
                    },
                    {
                        "tag": "column",
                        "width": "weighted",
                        "weight": 1,
                        "vertical_align": "center",
                        "elements": [_md_div(_impact(impact_key))],
                    },
                ],
            }

        insight = getattr(item, "actionable_insight", None)
        if insight and str(insight).strip():
            insight_md = f"**🎯 关注建议**\n💡 {str(insight).strip()}"
        else:
            insight_md = "**🎯 关注建议**\n💡 紧密跟踪后续进展与官方确认信息。"

        elements: List[Dict[str, Any]] = [
            _md_div(time_md),
            {"tag": "hr"},
            _md_div(overview_md),
            {"tag": "hr"},
        ]
        if include_investment_impact:
            elements.extend(
                [
                    _md_div("**💹 投资影响**"),
                    # Match reference card: emoji + 两字标签 | 圆点方向 | 说明（上证替换 A股）
                    _impact_row("🌐 整体", "bias_overall", "impact_overall"),
                    _impact_row("📈 美股", "bias_us", "impact_us"),
                    _impact_row("📊 上证", "bias_cn", "impact_cn"),
                    _impact_row("🛢️ 大宗", "bias_commodities", "impact_commodities"),
                    {"tag": "hr"},
                ]
            )
        elements.append(_md_div(insight_md))

        card_schema_2 = {
            "schema": "2.0",
            "header": {
                "title": {"tag": "plain_text", "content": header_title},
                "subtitle": {"tag": "plain_text", "content": subtitle or "投资情报快报"},
                "template": color_template,
            },
            "body": {"elements": elements},
        }
        return {"msg_type": "interactive", "card": card_schema_2}

    @classmethod
    def build_wechat_photo_card(
        cls,
        item: DigestItem,
        media_urls: Optional[List[str]] = None,
        subtitle: str = "公众号图片素材",
    ) -> Dict[str, Any]:
        """Title + ≤100字说明 + clickable photo links. No TG traces, no investment block."""
        title = strip_tg_traces((getattr(item, "title", None) or "").strip()) or "历史影像"
        title = title[:50]
        caption = normalize_wechat_caption(getattr(item, "summary", None) or "")
        if not caption:
            bullets = getattr(item, "summary_bullets", None) or []
            first = bullets[0] if bullets else ""
            caption = normalize_wechat_caption(str(first) or title)

        urls = photo_link_urls(media_urls)
        if not urls:
            urls = photo_link_urls(getattr(item, "media_urls", None))

        def _md_div(content: str) -> Dict[str, Any]:
            return {"tag": "div", "text": {"tag": "lark_md", "content": content}}

        elements: List[Dict[str, Any]] = [_md_div(caption or "（无说明）")]
        if urls:
            elements.append({"tag": "hr"})
            link_lines = "\n".join(f"[{idx}]({url})" for idx, url in enumerate(urls, 1))
            elements.append(_md_div(f"**图片**\n{link_lines}"))

        card_schema_2 = {
            "schema": "2.0",
            "header": {
                "title": {"tag": "plain_text", "content": title},
                "subtitle": {"tag": "plain_text", "content": subtitle or "公众号图片素材"},
                "template": "blue",
            },
            "body": {"elements": elements},
        }
        return {"msg_type": "interactive", "card": card_schema_2}

    def build(
        self,
        post: Union[TelegramPost, Dict[str, Any]],
        evaluation: Union[NewsEvaluation, Dict[str, Any]],
        subtitle: str = "Telegram 实时新闻监控中心",
    ) -> Dict[str, Any]:
        """Instance method alias for build_card."""
        return self.build_card(post, evaluation, subtitle)


def build_card(
    post: Union[TelegramPost, Dict[str, Any]],
    evaluation: Union[NewsEvaluation, Dict[str, Any]],
    subtitle: str = "Telegram 实时新闻监控中心",
) -> Dict[str, Any]:
    """Module-level helper to build Feishu Interactive Card Schema 2.0."""
    return FeishuCardBuilder.build_card(post, evaluation, subtitle)


def build_feishu_card(
    post: Union[TelegramPost, Dict[str, Any]],
    evaluation: Union[NewsEvaluation, Dict[str, Any]],
    subtitle: str = "Telegram 实时新闻监控中心",
) -> Dict[str, Any]:
    """Alias for build_card matching specification naming."""
    return FeishuCardBuilder.build_card(post, evaluation, subtitle)
