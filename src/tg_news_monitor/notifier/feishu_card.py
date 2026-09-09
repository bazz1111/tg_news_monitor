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

from tg_news_monitor.core.models import NewsEvaluation, TelegramPost


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
        icon_token = get_header_icon(score)

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

        note_element = {
            "tag": "note",
            "elements": [
                {
                    "tag": "plain_text",
                    "content": "  |  ".join(note_parts),
                }
            ],
        }

        # 5. Action element: primary button linking directly to original post
        action_url = direct_url if direct_url else f"https://t.me/{channel}/{message_id}"
        action_element = {
            "tag": "action",
            "actions": [
                {
                    "tag": "button",
                    "text": {
                        "tag": "plain_text",
                        "content": "🔗 查看 Telegram 原文",
                    },
                    "type": "primary",
                    "url": action_url,
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
                "ud_icon": {
                    "tag": "standard_icon",
                    "token": icon_token,
                },
            },
            "body": {
                "elements": elements,
            },
        }

        return {
            "msg_type": "interactive",
            "card": card_schema_2,
        }

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
