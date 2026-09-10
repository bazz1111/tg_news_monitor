"""Feishu card copy helpers: no outbound links; long-summary detail block."""

from __future__ import annotations

from typing import Any, List, Sequence

LONG_SUMMARY_CHARS = 80
LONG_SUMMARY_LINES = 2  # more than 2 lines => long
SUMMARY_SOFT_CAP = 80
DETAIL_MAX_ITEMS = 3
DETAIL_ITEM_CHARS = 72


def _line_count(text: str) -> int:
    t = (text or "").strip()
    if not t:
        return 0
    return t.count("\n") + 1


def is_long_summary(text: str) -> bool:
    """True when summary is >80 chars or spans more than 2 lines."""
    t = (text or "").strip()
    if not t:
        return False
    return len(t) > LONG_SUMMARY_CHARS or _line_count(t) > LONG_SUMMARY_LINES


def _bullets_from_item(item: Any) -> List[str]:
    raw = getattr(item, "summary_bullets", None) or []
    if isinstance(raw, str):
        raw = [raw]
    out = [str(b).strip().lstrip("•-* ") for b in raw if str(b).strip()]
    return out


def _split_detail_from_summary(summary: str) -> List[str]:
    """Fallback details when model did not return bullets."""
    text = (summary or "").strip()
    # Prefer sentence-ish splits
    for sep in ["。", "；", ";", "\n"]:
        parts = [p.strip() for p in text.replace("\n", "。").split("。") if p.strip()]
        if len(parts) >= 2:
            return parts
    # chunk by soft cap
    chunks = []
    rest = text
    while rest and len(chunks) < DETAIL_MAX_ITEMS:
        chunks.append(rest[:DETAIL_ITEM_CHARS].strip())
        rest = rest[DETAIL_ITEM_CHARS:].strip()
    return [c for c in chunks if c]


def polish_overview_bullet(text: str) -> str:
    """Make overview lines complete: strip trailing ellipsis, close the sentence."""
    s = (text or "").strip().lstrip("•-* ")
    if not s:
        return ""
    # strip trailing truncation markers
    while s and s[-1] in ".…⋯．":
        # keep a normal Chinese period if we already closed the thought
        if s.endswith("。") or s.endswith("！") or s.endswith("？"):
            break
        s = s[:-1].rstrip()
    s = s.rstrip(".…⋯． ").strip()
    if not s:
        return ""
    if s[-1] not in "。！？；":
        s = s + "。"
    return s


def shorten_summary(summary: str, cap: int = SUMMARY_SOFT_CAP) -> str:
    """Prefer a complete sentence under cap; never end with bare ellipsis."""
    t = polish_overview_bullet(summary)
    if not t:
        return ""
    first = t.split("\n", 1)[0].strip()
    for sep in ["。", "！", "？"]:
        if sep in first:
            head = first.split(sep, 1)[0].strip()
            if head:
                first = head + sep
                break
    if len(first) <= cap:
        return polish_overview_bullet(first)
    # cut at last clause mark within cap
    window = first[:cap]
    for sep in ["，", "、", "；", " "]:
        idx = window.rfind(sep)
        if idx >= max(20, cap // 3):
            return polish_overview_bullet(window[:idx])
    return polish_overview_bullet(window)


def format_detail_lines(details: Sequence[str]) -> str:
    lines = []
    for d in details[:DETAIL_MAX_ITEMS]:
        s = polish_overview_bullet(str(d))
        if not s:
            continue
        if len(s) > DETAIL_ITEM_CHARS:
            s = shorten_summary(s, DETAIL_ITEM_CHARS)
        lines.append(f"- {s}")
    return "\n".join(lines)


def format_morning_item_md(
    index: int,
    item: Any,
    event_time_hm: str,
) -> str:
    """Morning-recap block without Telegram/原文 links."""
    title = (getattr(item, "title", None) or "未命名").strip()
    summary = (getattr(item, "summary", None) or "").strip()
    focus = (getattr(item, "impact_overall", None) or getattr(item, "actionable_insight", None) or "").strip()
    focus_line = f"关注：{focus}" if focus else "关注：跟踪后续进展"

    if is_long_summary(summary):
        short = shorten_summary(summary)
        short = polish_overview_bullet(short)
        bullets = _bullets_from_item(item)
        details = bullets if bullets else _split_detail_from_summary(summary)
        # Drop detail that duplicates the short summary
        details = [d for d in details if d and d not in short][:DETAIL_MAX_ITEMS]
        if not details:
            details = _split_detail_from_summary(summary)[:DETAIL_MAX_ITEMS]
        detail_block = format_detail_lines(details)
        body = f"**{index}. {title}**\n{short}"
        if detail_block:
            body += f"\n📌 事件详情\n{detail_block}"
        body += f"\n{focus_line}\n北京时间：{event_time_hm}"
        return body

    body = f"**{index}. {title}**\n{summary}\n{focus_line}\n北京时间：{event_time_hm}"
    return body


def format_model_disclaimer() -> str:
    """Optional single-card footer: no event time, no outbound links."""
    return "模型解读需核实"
