"""WeChat-ready historical photo material: intake, local safety, caption.

Used by the `old_photos` / `wechat_photo` peer-group path only.
Prefer dropping borderline copy over letting review-unsafe content through.
"""

from __future__ import annotations

import re
from typing import Any, Iterable, List, Optional, Sequence, Tuple

from tg_news_monitor.core.filters import _COARSE_SPAM_RES, _text_looks_crypto
from tg_news_monitor.core.models import TelegramPost

WECHAT_PHOTO_VARIANT = "wechat_photo"
WECHAT_CAPTION_MAX = 100
WECHAT_CARD_MAX_IMAGES = 1
WECHAT_DIGEST_MAX_ITEMS = 2
PHOTO_MEDIA_TYPES = frozenset({"photo", "album"})

_TG_LINK_RE = re.compile(r"https?://(?:www\.)?t\.me/\S+|t\.me/\S+", re.I)
_ELLIPSIS_TAIL_RE = re.compile(r"(?:\.{2,}|…+|⋯+)$")

# Vice / graphic harm — obvious only; image pixels are not inspected.
_VICE_RES = [
    re.compile(
        r"(色情|黄片|裸聊|约炮|性爱视频|成人影片|做爱|口交|porn|onlyfans|\bxxx\b)",
        re.I,
    ),
    re.compile(r"(赌博|赌场|博彩|六合彩|赌球|casino|betting)", re.I),
    re.compile(r"(毒品|冰毒|海洛因|可卡因|大麻|吸毒|fentanyl|methamphetamine|\bmeth\b)", re.I),
    re.compile(r"(斩首|碎尸|肢解|奸杀|血腥屠杀|gore)", re.I),
]

# Mainland WeChat review-unsafe / political. Leaders and contemporary agitation
# are rejected even when framed as "old photos".
_POLITICAL_RES = [
    re.compile(
        r"(习近平|李强|王沪宁|蔡奇|丁薛祥|江泽民|胡锦涛|邓小平|毛泽东|周恩来)",
    ),
    re.compile(r"(总书记|党中央|中共中央|国家主席|政治局常委)"),
    re.compile(r"(六四|天安门事件|法轮功|藏独|疆独|台独|港独|颜色革命|颠覆国家)"),
    re.compile(r"(文化大革命|红卫兵|学习强国|入党宣誓)"),
    re.compile(r"(武统|台海开战|台海局势|武力统一)"),
    re.compile(r"(俄乌|乌克兰战争|哈马斯|加沙冲突|以色列空袭|北约东扩)"),
]

# Live news / markets / conflict — never send on the photo webhook even if
# a digest item was built for the wrong group.
_NEWS_LIKE_CATEGORIES = frozenset({
    "能源",
    "地缘政治",
    "宏观财经",
    "宏观监管",
    "突发安全",
    "突发快讯",
    "全球新闻",
    "宏观快讯",
    "军事",
    "财经",
    "加密货币",
})
_NEWS_LIKE_RES = [
    re.compile(
        r"(原油|布伦特|美伊|油价|冲突|空袭|加息|降息|美联储|战争升级|导弹袭击|地缘政治)",
    ),
]


def is_wechat_photo_variant(variant: str | None) -> bool:
    return (variant or "").strip().lower() == WECHAT_PHOTO_VARIANT


def is_photo_album_post(post: TelegramPost) -> bool:
    """True only when the post has photo/album media and at least one URL."""
    if not getattr(post, "has_media", False):
        return False
    media_type = str(getattr(post, "media_type", None) or "").strip().lower()
    if media_type not in PHOTO_MEDIA_TYPES:
        return False
    return bool(photo_link_urls(getattr(post, "media_urls", None)))


def photo_link_urls(urls: Sequence[str] | None) -> List[str]:
    """Keep extractable http(s) image links; drop t.me / empty / non-http."""
    out: List[str] = []
    seen: set[str] = set()
    for raw in urls or []:
        url = str(raw or "").strip()
        if not url:
            continue
        if _TG_LINK_RE.search(url):
            continue
        if not url.lower().startswith(("http://", "https://")):
            continue
        if url in seen:
            continue
        seen.add(url)
        out.append(url)
    return out


def strip_tg_traces(text: str) -> str:
    cleaned = _TG_LINK_RE.sub("", text or "")
    return re.sub(r"\s+", " ", cleaned).strip()


def wechat_photo_unsafe(text: str) -> bool:
    """True if local text looks vice-related, political, or live-news."""
    t = (text or "").strip()
    if not t:
        return False
    return any(rx.search(t) for rx in (*_VICE_RES, *_POLITICAL_RES, *_NEWS_LIKE_RES))


def wechat_looks_like_news(
    item: Any = None,
    *,
    category: str = "",
    text: str = "",
) -> bool:
    """True if a digest item / caption is live news, markets, or conflict."""
    cat = str(category or getattr(item, "category", "") or "").strip()
    if cat in _NEWS_LIKE_CATEGORIES:
        return True
    parts = [cat, text]
    if item is not None:
        parts.extend(
            [
                str(getattr(item, "title", "") or ""),
                str(getattr(item, "summary", "") or ""),
                " ".join(str(x) for x in (getattr(item, "summary_bullets", None) or [])),
            ]
        )
    blob = " ".join(parts)
    return any(rx.search(blob) for rx in _NEWS_LIKE_RES)


def wechat_card_block_reason(item: Any) -> Optional[str]:
    """Hard reject reason before a wechat_photo Feishu send, or None if allowed."""
    blob = " ".join(
        [
            str(getattr(item, "category", "") or ""),
            str(getattr(item, "title", "") or ""),
            str(getattr(item, "summary", "") or ""),
            " ".join(str(x) for x in (getattr(item, "summary_bullets", None) or [])),
        ]
    )
    if wechat_photo_unsafe(blob):
        return "wechat_unsafe"
    if wechat_looks_like_news(item):
        return "wechat_news_like"
    if not photo_link_urls(getattr(item, "media_urls", None)):
        return "no_photo_urls"
    return None


def photo_material_text(post: TelegramPost) -> str:
    """Dedup/claim payload: caption plus photo URLs (empty [PHOTO] texts collide)."""
    urls = "\n".join(photo_link_urls(getattr(post, "media_urls", None)))
    return f"{post.text or ''}\n{urls}"


def normalize_wechat_caption(text: str, max_chars: int = WECHAT_CAPTION_MAX) -> str:
    """Chinese 说明: ≤max_chars, complete sentence(s), no ellipsis ending."""
    s = strip_tg_traces(text or "")
    s = s.strip().lstrip("•-* ")
    if not s:
        return ""
    s = _ELLIPSIS_TAIL_RE.sub("", s).rstrip(" .．⋯")
    s = s.strip()
    if not s:
        return ""
    if s[-1] not in "。！？；":
        s = s + "。"
    if len(s) <= max_chars:
        return s
    window = s[:max_chars]
    for sep in ("。", "！", "？", "；"):
        idx = window.rfind(sep)
        if idx >= 8:
            return window[: idx + 1]
    cut = window.rstrip(" .．…⋯").rstrip()
    if not cut:
        return ""
    if cut[-1] not in "。！？；":
        budget = max_chars - 1
        cut = cut[:budget].rstrip()
        if not cut:
            return ""
        cut = cut + "。"
    return cut[:max_chars]


def wechat_photo_prefilter(
    posts: Iterable[TelegramPost],
) -> Tuple[List[TelegramPost], List[Tuple[TelegramPost, str]]]:
    """Photo-only intake + local safety. Does not drop short captions.

    Returns:
        (kept, [(dropped_post, reason), ...])
    """
    kept: List[TelegramPost] = []
    dropped: List[Tuple[TelegramPost, str]] = []
    for post in posts:
        if not is_photo_album_post(post):
            dropped.append((post, "not_photo_media"))
            continue
        text = post.text or ""
        if wechat_photo_unsafe(text):
            dropped.append((post, "wechat_unsafe"))
            continue
        if wechat_looks_like_news(text=text):
            dropped.append((post, "wechat_news_like"))
            continue
        if any(rx.search(text) for rx in _COARSE_SPAM_RES):
            dropped.append((post, "coarse_filter"))
            continue
        if _text_looks_crypto(text):
            dropped.append((post, "crypto_filter"))
            continue
        kept.append(post)
    return kept, dropped


__all__ = [
    "PHOTO_MEDIA_TYPES",
    "WECHAT_CAPTION_MAX",
    "WECHAT_CARD_MAX_IMAGES",
    "WECHAT_DIGEST_MAX_ITEMS",
    "WECHAT_PHOTO_VARIANT",
    "is_photo_album_post",
    "is_wechat_photo_variant",
    "normalize_wechat_caption",
    "photo_link_urls",
    "photo_material_text",
    "strip_tg_traces",
    "wechat_card_block_reason",
    "wechat_looks_like_news",
    "wechat_photo_prefilter",
    "wechat_photo_unsafe",
]
