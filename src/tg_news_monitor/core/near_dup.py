"""Deterministic same-event near-duplicate check (no LLM).

Used at Feishu send time for news digest cards. Compares candidate
``title: summary`` against recent ``delivery_claims.summary`` rows.

Thresholds (documented so they can be retuned):

- Strip boilerplate phrases (创历史新高 / 零售均价 / 每加仑 / 美元 / 加息…).
- Tokens = CJK character bigrams + latin words ≥2 chars.
- Number buckets = integers truncated toward zero so ``$6.06`` ≈ ``$6``;
  a jump to ``$7`` or ``50`` vs ``25`` is a different bucket.
- Generic tokens (美国 / 升至 / 央行 / 利率…) may overlap without meaning
  the same event; they do not count toward the specific-overlap minimum.
- Same-event if number buckets are compatible (overlap, or either side
  has none) AND either:
  - Jaccard ≥ 0.45 with ≥1 specific overlapping token, or
  - Jaccard ≥ 0.30 with ≥4 specific overlapping tokens (paraphrase).

Tuned to block the 2026-09-11 diesel pair (零售均价 $6 vs 创历史新高 $6.06)
while still sending distinct macro stories (Fed vs diesel, gasoline vs diesel).

ponytail: 2-gram overlap is not NLU. Low-overlap paraphrases can miss;
``is_update`` + a new number/entity in ``update_reason`` is the escape hatch.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Iterable, Optional, Set

NEAR_DUP_JACCARD = 0.45
NEAR_DUP_SPECIFIC_MIN = 1
NEAR_DUP_LOOSE_JACCARD = 0.30
NEAR_DUP_LOOSE_SPECIFIC_MIN = 4
NEAR_DUP_WINDOW_SECONDS = 86400
EVENT_KEY_MIN_PARTS = 3

# Longest first. Framing / units / rate-hike boilerplate, not entities.
_TEMPLATE_PHRASES = tuple(
    sorted(
        (
            "创下历史新高",
            "刷新历史新高",
            "创历史新高",
            "创近年来新高",
            "近年来新高",
            "历史新高",
            "再创新高",
            "创纪录",
            "零售均价",
            "零售价",
            "批发价",
            "均价",
            "每加仑",
            "每桶",
            "万亿美元",
            "亿美元",
            "数据显示",
            "根据数据",
            "报道称",
            "消息称",
            "部分地区",
            "个基点",
            "人民币",
            "美元",
            "基点",
            "加息",
            "降息",
            "上调",
            "下调",
            "突破",
            "价格",
            "宣布",
            "表示",
            "指出",
        ),
        key=len,
        reverse=True,
    )
)

_GENERIC_TOKENS = frozenset(
    {
        "美国",
        "中国",
        "日本",
        "英国",
        "欧盟",
        "全球",
        "欧洲",
        "全美",
        "升至",
        "达到",
        "报于",
        "位于",
        "维持",
        "不变",
        "宣布",
        "表示",
        "利率",
        "加息",
        "降息",
        "基点",
        "上调",
        "下调",
        "个基",
        "将利",
        "率上",
        "央行",
        "行加",
        "行将",
        "储将",
        "储加",
        "突破",
        "价格",
        "均价",
        "美元",
        "以色",
        "色列",
        "公布",
        "劳工",
        "工部",
        "国劳",
        "部公",
        "同比",
        "the",
        "and",
        "for",
        "from",
        "with",
        "that",
        "this",
        "达",
        "至",
        "将",
        "已",
        "对",
    }
)

_NUM_RE = re.compile(
    r"(?:\$|usd|us\$|€|£|¥|￥)?"
    r"(\d{1,3}(?:,\d{3})+|\d+)(?:\.(\d+))?",
    re.I,
)
_LATIN_RE = re.compile(r"[a-z]{2,}")
_CJK_RE = re.compile(r"[\u4e00-\u9fff]+")


def normalize_news_text(text: str) -> str:
    return re.sub(r"\s+", " ", unicodedata.normalize("NFKC", text or "").casefold()).strip()


def number_buckets(text: str) -> Set[str]:
    """Integer-truncated magnitudes so 6.06 and 6 share a bucket; 6 vs 7 do not."""
    buckets: Set[str] = set()
    for match in _NUM_RE.finditer(normalize_news_text(text)):
        whole = match.group(1).replace(",", "")
        frac = match.group(2) or ""
        try:
            value = float(whole + (("." + frac) if frac else ""))
        except ValueError:
            continue
        if value >= 1:
            buckets.add(str(int(value)))
        else:
            buckets.add(f"{value:.2f}")
    return buckets


def _strip_templates(text: str) -> str:
    body = _NUM_RE.sub(" ", normalize_news_text(text))
    for phrase in _TEMPLATE_PHRASES:
        body = body.replace(phrase, " ")
    body = re.sub(r"[^\w\u4e00-\u9fff]+", " ", body)
    return re.sub(r"\s+", " ", body).strip()


def content_tokens(text: str) -> Set[str]:
    body = _strip_templates(text)
    tokens: Set[str] = set(_LATIN_RE.findall(body))
    for chunk in _CJK_RE.findall(body):
        if len(chunk) == 1:
            tokens.add(chunk)
        else:
            tokens.update(chunk[i : i + 2] for i in range(len(chunk) - 1))
    return tokens


def specific_tokens(text: str) -> Set[str]:
    return content_tokens(text) - _GENERIC_TOKENS


def event_key(text: str) -> Optional[str]:
    """Coarse leftover-token key for extra claim fingerprints. None if too coarse."""
    parts = sorted(specific_tokens(text)) + [f"#{n}" for n in sorted(number_buckets(text))]
    if len(parts) < EVENT_KEY_MIN_PARTS:
        return None
    return " ".join(parts)


def claim_blob(title: str, summary: str) -> str:
    title = (title or "").strip()
    summary = (summary or "").strip()
    if title and summary:
        return f"{title}: {summary}"
    return title or summary


def similar_event(left: str, right: str) -> bool:
    left_tokens = content_tokens(left)
    right_tokens = content_tokens(right)
    if not left_tokens or not right_tokens:
        return False
    union = left_tokens | right_tokens
    jaccard = len(left_tokens & right_tokens) / len(union)
    left_nums = number_buckets(left)
    right_nums = number_buckets(right)
    numbers_ok = (not left_nums or not right_nums) or bool(left_nums & right_nums)
    if not numbers_ok:
        return False
    specific = (left_tokens & right_tokens) - _GENERIC_TOKENS
    if jaccard >= NEAR_DUP_JACCARD and len(specific) >= NEAR_DUP_SPECIFIC_MIN:
        return True
    if jaccard >= NEAR_DUP_LOOSE_JACCARD and len(specific) >= NEAR_DUP_LOOSE_SPECIFIC_MIN:
        return True
    return False


def is_material_update(is_update: bool, update_reason: str, previous_summary: str) -> bool:
    """True when the model marked an update and the reason adds a new number or entity."""
    if not is_update:
        return False
    reason = (update_reason or "").strip()
    if not reason:
        return False
    new_nums = number_buckets(reason) - number_buckets(previous_summary)
    new_toks = specific_tokens(reason) - specific_tokens(previous_summary)
    return bool(new_nums) or bool(new_toks)


def is_near_duplicate_blob(
    candidate: str,
    recent_summaries: Iterable[str],
    *,
    is_update: bool = False,
    update_reason: str = "",
) -> bool:
    blob = (candidate or "").strip()
    if not blob:
        return False
    for previous in recent_summaries:
        prev = (previous or "").strip()
        if not prev or not similar_event(blob, prev):
            continue
        if is_material_update(is_update, update_reason, prev):
            continue
        return True
    return False
