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

Follow-up cards with a new angle are allowed. Before send, overlapping bullets
that only restate a recent delivery are dropped so the card leads with the delta.
Skip only when nothing meaningful remains.

ponytail: 2-gram overlap is not NLU. Low-overlap paraphrases can miss;
``is_update`` + a new number/entity in ``update_reason`` is the escape hatch.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Iterable, List, NamedTuple, Optional, Sequence, Set

NEAR_DUP_JACCARD = 0.45
NEAR_DUP_SPECIFIC_MIN = 1
NEAR_DUP_LOOSE_JACCARD = 0.30
NEAR_DUP_LOOSE_SPECIFIC_MIN = 4
# wechat_photo only. Do not use these on news24.
PHOTO_NEAR_DUP_JACCARD = 0.22
PHOTO_NEAR_DUP_SPECIFIC_MIN = 10
NEAR_DUP_WINDOW_SECONDS = 86400
EVENT_KEY_MIN_PARTS = 3
# update_reason / leftover copy must add a new magnitude or ≥2 leftover tokens.
# One new place-name (加州) on the same $6 print is not a material update.
MATERIAL_UPDATE_MIN_NEW_TOKENS = 2
FOLLOWUP_TITLE_MAX_LEN = 40

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
            "照片为",
            "照片记录",
            "照片显示",
            "画面呈现",
            "画面中",
            "珍贵影像",
            "繁荣景象",
            "历史瞬间",
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
_ERA_RE = re.compile(
    r"(春日|夏日|秋日|冬日|春季|夏季|秋季|冬季|春天|夏天|秋天|冬天|"
    r"民国|清朝|清代|明朝|宋代|唐代|元代)"
)
_ERA_ALIASES = {"清朝": "清代"}
_CN_DIGIT = {
    "零": "0",
    "〇": "0",
    "○": "0",
    "一": "1",
    "二": "2",
    "三": "3",
    "四": "4",
    "五": "5",
    "六": "6",
    "七": "7",
    "八": "8",
    "九": "9",
}
_CN_YEAR_RE = re.compile(r"([零〇○一二三四五六七八九]{2,4})年")
_CN_CENTURY_DECADE_RE = re.compile(r"二十世纪([零〇○一二三四五六七八九十]+)年代")
_LATIN_RE = re.compile(r"[a-z]{2,}")
_CJK_RE = re.compile(r"[\u4e00-\u9fff]+")
_CLAUSE_SPLIT_RE = re.compile(r"[。！？；;：:\n]|，(?=\S)")


def normalize_news_text(text: str) -> str:
    return re.sub(r"\s+", " ", unicodedata.normalize("NFKC", text or "").casefold()).strip()


def era_buckets(text: str) -> Set[str]:
    """Season / dynasty markers so 春日 vs 冬日 is not the same event."""
    return {_ERA_ALIASES.get(m, m) for m in _ERA_RE.findall(normalize_news_text(text))}


def _cn_digit_run_to_int(text: str) -> Optional[int]:
    if not text or any(ch not in _CN_DIGIT for ch in text):
        return None
    return int("".join(_CN_DIGIT[ch] for ch in text))


def _cn_numeral_to_int(text: str) -> Optional[int]:
    """五十→50, 十→10, 一九二六→1926. Digit-run years, not 一千九百."""
    if "十" not in text:
        return _cn_digit_run_to_int(text)
    if text == "十":
        return 10
    left, _, right = text.partition("十")
    tens = 1 if not left else _cn_digit_run_to_int(left)
    ones = 0 if not right else _cn_digit_run_to_int(right)
    if tens is None or ones is None:
        return None
    return tens * 10 + ones


def number_buckets(text: str) -> Set[str]:
    """Integer-truncated magnitudes so 6.06 and 6 share a bucket; 6 vs 7 do not."""
    buckets: Set[str] = set()
    norm = normalize_news_text(text)
    for match in _NUM_RE.finditer(norm):
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
    for match in _CN_YEAR_RE.finditer(norm):
        year = _cn_digit_run_to_int(match.group(1))
        if year is not None and year >= 1:
            buckets.add(str(year))
    for match in _CN_CENTURY_DECADE_RE.finditer(norm):
        decade = _cn_numeral_to_int(match.group(1))
        if decade is not None:
            buckets.add(str(1900 + decade))
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


def _numbers_eras_compatible(left: str, right: str) -> bool:
    left_nums = number_buckets(left)
    right_nums = number_buckets(right)
    numbers_ok = (not left_nums or not right_nums) or bool(left_nums & right_nums)
    if not numbers_ok:
        return False
    left_eras = era_buckets(left)
    right_eras = era_buckets(right)
    return (not left_eras or not right_eras) or bool(left_eras & right_eras)


def similar_event(left: str, right: str) -> bool:
    left_tokens = content_tokens(left)
    right_tokens = content_tokens(right)
    if not left_tokens or not right_tokens:
        return False
    if not _numbers_eras_compatible(left, right):
        return False
    union = left_tokens | right_tokens
    jaccard = len(left_tokens & right_tokens) / len(union)
    specific = (left_tokens & right_tokens) - _GENERIC_TOKENS
    if jaccard >= NEAR_DUP_JACCARD and len(specific) >= NEAR_DUP_SPECIFIC_MIN:
        return True
    if jaccard >= NEAR_DUP_LOOSE_JACCARD and len(specific) >= NEAR_DUP_LOOSE_SPECIFIC_MIN:
        return True
    return False


def similar_photo_event(left: str, right: str) -> bool:
    """wechat_photo only: looser leftover overlap, same number/era compatibility."""
    left_tokens = content_tokens(left)
    right_tokens = content_tokens(right)
    if not left_tokens or not right_tokens:
        return False
    if not _numbers_eras_compatible(left, right):
        return False
    union = left_tokens | right_tokens
    jaccard = len(left_tokens & right_tokens) / len(union)
    specific = (left_tokens & right_tokens) - _GENERIC_TOKENS
    return jaccard >= PHOTO_NEAR_DUP_JACCARD and len(specific) >= PHOTO_NEAR_DUP_SPECIFIC_MIN


def has_new_specific_content(
    text: str,
    previous: str,
    *,
    min_new_tokens: int = MATERIAL_UPDATE_MIN_NEW_TOKENS,
) -> bool:
    """True when text adds a new number bucket or enough leftover entities."""
    new_nums = number_buckets(text) - number_buckets(previous)
    new_toks = specific_tokens(text) - specific_tokens(previous)
    return bool(new_nums) or len(new_toks) >= min_new_tokens


def is_material_update(is_update: bool, update_reason: str, previous_summary: str) -> bool:
    """True when the model marked an update and the reason adds a new number or entity.

    A lone place-name add-on (加州) is not enough; need a new magnitude or a
    longer leftover phrase with at least two specific tokens.
    """
    if not is_update:
        return False
    reason = (update_reason or "").strip()
    if not reason:
        return False
    if number_buckets(reason) - number_buckets(previous_summary):
        return True
    new_toks = specific_tokens(reason) - specific_tokens(previous_summary)
    return len(reason) >= 10 and len(new_toks) >= MATERIAL_UPDATE_MIN_NEW_TOKENS


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


def is_photo_near_duplicate_blob(
    candidate: str,
    recent_summaries: Iterable[str],
    *,
    is_update: bool = False,
    update_reason: str = "",
) -> bool:
    """wechat_photo send-time near-dup. Does not change news Jaccard."""
    blob = (candidate or "").strip()
    if not blob:
        return False
    for previous in recent_summaries:
        prev = (previous or "").strip()
        if not prev or not similar_photo_event(blob, prev):
            continue
        if is_material_update(is_update, update_reason, prev):
            continue
        return True
    return False


def reader_copy_blob(title: str, summary: str, bullets: Optional[Sequence[str]] = None) -> str:
    parts = [claim_blob(title, summary)]
    for raw in bullets or ():
        bullet = str(raw).strip()
        if bullet:
            parts.append(bullet)
    return " ".join(p for p in parts if p)


def lead_title_from_text(text: str, max_len: int = FOLLOWUP_TITLE_MAX_LEN) -> str:
    """Short headline from a kept delta sentence; cut at a clause mark when possible."""
    s = re.sub(r"\s+", " ", (text or "").strip()).lstrip("•-* ")
    if not s:
        return ""
    for sep in ("。", "！", "？", "；", "，"):
        if sep in s:
            head = s.split(sep, 1)[0].strip()
            if head:
                s = head
                break
    if len(s) > max_len:
        s = s[:max_len].rstrip()
    return s


def _clauses(text: str) -> List[str]:
    """Split a delivery blob into clauses so a follow-up sentence can match one fact."""
    parts: List[str] = []
    for raw in _CLAUSE_SPLIT_RE.split(text or ""):
        part = raw.strip(" ，,、:：")
        if len(part) >= 6:
            parts.append(part)
    body = re.sub(r"\s+", " ", (text or "").strip())
    if body:
        parts.append(body)
    seen = set()
    out: List[str] = []
    for part in parts:
        if part not in seen:
            seen.add(part)
            out.append(part)
    return out


def restates_previous(text: str, previous: str) -> bool:
    """True when this sentence matches a recently delivered clause without a new number."""
    body = (text or "").strip()
    prev = (previous or "").strip()
    if not body or not prev:
        return False
    if number_buckets(body) - number_buckets(prev):
        return False
    if similar_event(body, prev):
        return True
    prev_clauses = _clauses(prev)
    for cand in (body, *_clauses(body)):
        for clause in prev_clauses:
            if similar_event(cand, clause):
                return True
    return False


def _clean_recent(recent_summaries: Iterable[str]) -> List[str]:
    out: List[str] = []
    for raw in recent_summaries:
        prev = (raw or "").strip()
        if prev:
            out.append(prev)
    return out


def _restates_any(text: str, recents: Sequence[str]) -> bool:
    return any(restates_previous(text, prev) for prev in recents)


def _delta_against_any(text: str, recents: Sequence[str]) -> bool:
    body = (text or "").strip()
    if not body or not recents:
        return False
    return any(has_new_specific_content(body, prev) for prev in recents)


class FollowupSanitize(NamedTuple):
    skip: bool
    rewritten: bool
    title: str
    summary: str
    bullets: List[str]


def sanitize_followup_copy(
    title: str,
    summary: str,
    bullets: Optional[Sequence[str]] = None,
    recent_summaries: Iterable[str] = (),
    *,
    is_update: bool = False,
    update_reason: str = "",
) -> FollowupSanitize:
    """Drop restated premise from a news digest card; skip if no delta remains.

    Does not tighten Jaccard. A new angle still sends after overlapping bullets
    are stripped. Same-event rewrites with only framing/location stay skipped.
    """
    title = (title or "").strip()
    summary = (summary or "").strip()
    raw_bullets = [str(b).strip() for b in (bullets or ()) if str(b).strip()]
    recents = _clean_recent(recent_summaries)
    if not recents:
        return FollowupSanitize(False, False, title, summary, raw_bullets)

    reader_blob = reader_copy_blob(title, summary, raw_bullets)
    title_summary = claim_blob(title, summary)
    high_overlap = any(
        similar_event(title_summary, prev) or similar_event(reader_blob, prev) for prev in recents
    )
    material = any(is_material_update(is_update, update_reason, prev) for prev in recents)
    kept = [b for b in raw_bullets if not _restates_any(b, recents)]
    stripped_some = len(kept) < len(raw_bullets)

    if not raw_bullets:
        if summary and not _restates_any(summary, recents):
            kept = [summary]
        elif material or _delta_against_any(update_reason, recents):
            seed = (update_reason or "").strip()
            if seed:
                kept = [seed]
        elif high_overlap and not (material or _delta_against_any(title_summary, recents)):
            return FollowupSanitize(True, False, title, summary, raw_bullets)

    if not kept:
        if material or _delta_against_any(update_reason, recents):
            seed = (update_reason or "").strip()
            if seed:
                kept = [seed]
        elif raw_bullets or high_overlap:
            return FollowupSanitize(True, False, title, summary, raw_bullets)
        else:
            return FollowupSanitize(False, False, title, summary, raw_bullets)

    if not (high_overlap or stripped_some or material):
        return FollowupSanitize(False, False, title, summary, raw_bullets)

    new_title = title
    new_summary = summary
    title_old = _restates_any(title, recents)
    summary_old = _restates_any(summary, recents)
    if title_old or is_update or stripped_some:
        lead = lead_title_from_text(kept[0])
        if lead:
            new_title = lead
    if summary_old or is_update or stripped_some:
        new_summary = kept[0]
    rewritten = new_title != title or new_summary != summary or kept != raw_bullets
    return FollowupSanitize(False, rewritten, new_title, new_summary, kept)
