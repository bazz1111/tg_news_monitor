"""Multi-stage fallback engine for Grok news evaluator.

Provides:
- Stage 1: Exponential backoff & jitter calculation.
- Stage 2: Regex-based JSON fence extraction, repair, and score clamping.
- Stage 3: Deterministic keyword-based heuristic triage on API failure/exhaustion.
"""

from __future__ import annotations

import json
import logging
import random
import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from tg_news_monitor.core.models import DigestBrief, NewsEvaluation, TelegramPost

logger = logging.getLogger(__name__)

# Level-3 deterministic emergency/breaking keywords
BREAKING_KEYWORDS = [
    "hack",
    "exploit",
    "stolen",
    "vulnerability",
    "breach",
    "drain",
    "halt",
    "pause",
    "suspend",
    "sec",
    "fed",
    "doj",
    "arrest",
    "insolvent",
    "emergency",
    "breaking",
    "突发",
    "快讯",
    "黑客",
    "漏洞",
    "暂停提现",
    "被盗",
    "暴跌",
    "清算",
    "紧急",
    "央行",
    "爆炸",
    "战争",
    "重组",
]

VALID_CATEGORIES = {
    "科技/AI",
    "宏观财经",
    "加密货币",
    "军事",
    "地缘政治",
    "行业快讯",
    "突发安全",
    "宏观监管",
    "市场异动",
    "重大商业",
    "日常闲聊",
    "推广广告",
}


def calculate_backoff_delay(
    attempt: int,
    base_delay: float = 2.0,
    max_delay: float = 60.0,
    jitter_range: Tuple[float, float] = (0.1, 0.5),
) -> float:
    """Calculates exponential backoff delay with randomized jitter.
    
    Formula: delay = min(base_delay * (2 ** attempt) + uniform(jitter), max_delay)
    """
    delay = base_delay * (2**attempt) + random.uniform(*jitter_range)
    return min(delay, max_delay)


def strip_markdown_code_fences(text: str) -> str:
    """Strips markdown code fences (```json ... ``` or ``` ... ```) from LLM output."""
    if not text:
        return ""
    stripped = text.strip()
    # Strip markdown block ```json ... ``` or ``` ... ```
    fence_pattern = r"^```(?:json)?\s*([\s\S]*?)\s*```$"
    match = re.search(fence_pattern, stripped, flags=re.IGNORECASE)
    if match:
        return match.group(1).strip()
    return stripped


def extract_first_json_object(text: str) -> Optional[str]:
    """Finds and extracts the outermost JSON object {...} within text."""
    if not text:
        return None
    # Match outermost curly braces
    start_idx = text.find("{")
    end_idx = text.rfind("}")
    if start_idx != -1 and end_idx != -1 and end_idx > start_idx:
        return text[start_idx : end_idx + 1]
    return None


def repair_json_string(json_str: str) -> str:
    """Performs regex-based repair for common LLM JSON syntax errors."""
    s = json_str.strip()
    # Remove trailing commas before closing braces or brackets: , } -> } and , ] -> ]
    s = re.sub(r",\s*([\}\]])", r"\1", s)
    # Replace unquoted python True/False/None with true/false/null
    s = re.sub(r"\bTrue\b", "true", s)
    s = re.sub(r"\bFalse\b", "false", s)
    s = re.sub(r"\bNone\b", "null", s)
    return s


def normalize_and_validate_evaluation(
    data: Dict[str, Any], fallback_title: str = ""
) -> NewsEvaluation:
    """Normalizes raw dictionary into a strictly validated NewsEvaluation model.
    
    Handles:
    - Score boundary clamping to [1, 10]
    - Field name aliasing ('urgency_score' -> 'score', 'summary_points' -> 'summary_bullets')
    - Spam post score clamping (score <= 2, is_news = False)
    - Key takeaways list normalization
    - Category fallback
    """
    # 1. Score extraction & clamping [1, 10]
    raw_score = data.get("score")
    if raw_score is None:
        raw_score = data.get("urgency_score", 5)
    try:
        score = int(raw_score)
    except (ValueError, TypeError):
        score = 5
    score = max(1, min(10, score))

    # 2. Category normalization
    category = str(data.get("category", "")).strip()
    if category not in VALID_CATEGORIES:
        if bool(data.get("is_spam", False)):
            category = "推广广告"
        elif score >= 7:
            category = "突发安全"
        else:
            category = "行业快讯"

    # 3. Spam & News detection
    is_spam = bool(data.get("is_spam", False))
    if "is_news" in data:
        is_news = bool(data.get("is_news"))
    else:
        # Infer is_news from spam, score, and category
        is_news = (not is_spam) and (score >= 5) and (category not in ("日常闲聊", "推广广告"))

    # If spam is true, force clamp score <= 2 and is_news = False
    if is_spam:
        score = min(score, 2)
        is_news = False
        category = "推广广告"

    # 3. Title normalization
    title = str(data.get("title", "")).strip()
    if not title:
        title = fallback_title.strip() if fallback_title else "实时快讯评估简报"
    # Bound title length
    if len(title) > 100:
        title = title[:97] + "..."

    # 4. Summary bullets normalization
    bullets_raw = data.get("summary_bullets")
    if bullets_raw is None:
        bullets_raw = data.get("summary_points", [])
    if isinstance(bullets_raw, str):
        summary_bullets = [
            b.strip().lstrip("•-*0123456789. ")
            for b in bullets_raw.splitlines()
            if b.strip()
        ]
    elif isinstance(bullets_raw, list):
        summary_bullets = [str(b).strip() for b in bullets_raw if str(b).strip()]
    else:
        summary_bullets = []
    if not summary_bullets:
        summary_bullets = [title]

    # 5. Key takeaways normalization
    takeaways_raw = data.get("key_takeaways", [])
    if isinstance(takeaways_raw, str):
        key_takeaways = [
            t.strip().lstrip("•-*0123456789. ")
            for t in takeaways_raw.splitlines()
            if t.strip()
        ]
    elif isinstance(takeaways_raw, list):
        key_takeaways = [str(t).strip() for t in takeaways_raw if str(t).strip()]
    else:
        key_takeaways = []
    if not key_takeaways:
        key_takeaways = ["密切跟踪事件后续官方动态及市场反应。"]

    # 6. Actionable insight
    actionable_insight = data.get("actionable_insight")
    if actionable_insight is not None:
        actionable_insight = str(actionable_insight).strip() or None

    return NewsEvaluation(
        score=score,
        is_news=is_news,
        is_spam=is_spam,
        title=title,
        summary_bullets=summary_bullets,
        key_takeaways=key_takeaways,
        category=category,
        actionable_insight=actionable_insight,
        evaluated_at=datetime.now(timezone.utc),
    )


def parse_and_repair_evaluation(
    raw_text: str, fallback_title: str = ""
) -> NewsEvaluation:
    """Stage 2 Fallback: Parses and repairs raw LLM text into NewsEvaluation.
    
    Attempts:
    1. Direct JSON loads after fence stripping.
    2. Regex extraction of outermost JSON block {...}.
    3. Regex JSON repair (trailing commas, python literals).
    """
    cleaned = strip_markdown_code_fences(raw_text)

    # Attempt 1: Direct JSON parsing
    try:
        parsed = json.loads(cleaned)
        if isinstance(parsed, dict):
            return normalize_and_validate_evaluation(parsed, fallback_title)
    except Exception:
        pass

    # Attempt 2: Extract {...} block
    json_block = extract_first_json_object(cleaned)
    if json_block:
        try:
            parsed = json.loads(json_block)
            if isinstance(parsed, dict):
                return normalize_and_validate_evaluation(parsed, fallback_title)
        except Exception:
            pass

        # Attempt 3: Repair syntax and retry
        repaired = repair_json_string(json_block)
        try:
            parsed = json.loads(repaired)
            if isinstance(parsed, dict):
                return normalize_and_validate_evaluation(parsed, fallback_title)
        except Exception as err:
            logger.warning("Stage 2 JSON repair failed: %s | Raw text: %s", err, raw_text[:200])

    raise ValueError(f"Unable to parse or repair JSON from LLM output: {raw_text[:200]}")


def parse_digest_brief(raw_response: str) -> DigestBrief:
    """Parse LLM JSON into DigestBrief with light fence stripping.

    Shared by the production CodeBuddy path and the leftover HTTP client.
    """
    cleaned = strip_markdown_code_fences(raw_response)
    data = json.loads(cleaned)
    if not isinstance(data, dict):
        raise ValueError("Digest response is not a JSON object")
    items = data.get("items") or []
    if isinstance(items, list) and len(items) > 5:
        data["items"] = items[:5]
    brief = DigestBrief.model_validate(data)
    if not brief.items:
        brief.has_material_news = False
    return brief


def heuristic_keyword_fallback(post: TelegramPost) -> NewsEvaluation:
    """Stage 3 Fallback: Deterministic keyword-based heuristic triage on API failure.
    
    When Grok API is completely unreachable or quota is exhausted:
    - Scans text for high-impact breaking keywords.
    - If keyword matches: assigns provisional score 7, extracts first sentence as headline,
      marks as emergency fallback, ensuring zero critical news is missed.
    - If no keyword matches: assigns score 3, marks is_news=False, preventing alert spam.
    """
    text = post.text.strip()
    text_lower = text.lower()

    # Check for keyword matches
    matched_keywords = [kw for kw in BREAKING_KEYWORDS if kw in text_lower]

    if matched_keywords:
        # Extract first non-empty sentence or first 40 chars as title
        first_line = text.splitlines()[0] if text.splitlines() else text
        # Split by common sentence delimiters
        sentences = re.split(r"[。！？!\?\n]", first_line)
        first_sentence = sentences[0].strip() if sentences else first_line[:40]
        if len(first_sentence) > 40:
            first_sentence = first_sentence[:37] + "..."
        if not first_sentence:
            first_sentence = f"来自 @{post.channel} 的突发动态"

        title = f"[降级预警] {first_sentence}"
        if len(title) > 100:
            title = title[:97] + "..."

        snippet = text[:200] + ("..." if len(text) > 200 else "")
        summary_bullets = [
            f"原文摘要：{snippet}",
            f"触发应急告警关键词：{', '.join(matched_keywords[:3])}",
            "Grok API 暂时不可用，系统触发第3级启发式规则应急预警。",
        ]
        key_takeaways = [
            "AI接口异常或配额耗尽，当前信息由规则引擎自动提取，请务必核验 Telegram 频道原文真实性。"
        ]

        return NewsEvaluation(
            score=7,
            is_news=True,
            is_spam=False,
            title=title,
            summary_bullets=summary_bullets,
            key_takeaways=key_takeaways,
            category="突发安全",
            actionable_insight="点击卡片直达链接查看 Telegram 原始公告核实详情。",
            evaluated_at=datetime.now(timezone.utc),
        )

    # No breaking keyword matched: low urgency fallback, no alert
    return NewsEvaluation(
        score=3,
        is_news=False,
        is_spam=False,
        title=f"未分级动态: @{post.channel}#{post.message_id}",
        summary_bullets=["未匹配突发高优先级关键词，降级跳过告警"],
        key_takeaways=["API服务不可用，经规则判定非紧急突发事件"],
        category="日常闲聊",
        actionable_insight=None,
        evaluated_at=datetime.now(timezone.utc),
    )


class MultiStageFallbackHandler:
    """Coordinates Level-1 retry, Level-2 JSON repair, and Level-3 heuristic fallback."""

    def __init__(self, max_retries: int = 3, base_delay: float = 2.0) -> None:
        self.max_retries = max_retries
        self.base_delay = base_delay

    def handle_json_response(
        self, raw_text: str, post: Optional[TelegramPost] = None
    ) -> NewsEvaluation:
        """Parses LLM response with Stage 2 recovery; falls back to Stage 3 if unparseable."""
        fallback_title = f"来自 @{post.channel} 的快讯" if post else ""
        try:
            return parse_and_repair_evaluation(raw_text, fallback_title=fallback_title)
        except Exception as e:
            logger.warning("Stage 2 recovery failed: %s. Engaging Stage 3 fallback.", e)
            if post:
                return heuristic_keyword_fallback(post)
            raise

    def handle_api_exhaustion(self, post: TelegramPost) -> NewsEvaluation:
        """Invoked when Grok API retries are completely exhausted or quota is reached."""
        logger.error(
            "Grok API exhaustion for @%s/#%s. Engaging Stage 3 heuristic fallback.",
            post.channel,
            post.message_id,
        )
        return heuristic_keyword_fallback(post)
