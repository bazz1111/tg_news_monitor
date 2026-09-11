"""Cheap local spam/crypto/topic filters (zero LLM).

Spam and crypto rules live here. Topic policy is loaded from an external
per-group YAML at runtime — this module only knows how to load and apply it.
"""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path
from typing import Any, Dict, List, NamedTuple, Optional, Sequence, Tuple, Union

from tg_news_monitor.core.models import TelegramPost

logger = logging.getLogger(__name__)

_COARSE_EMPTY_MAX_LEN = 12
_COARSE_SPAM_RES = [
    re.compile(r"\b(airdrop|presale|whitelist)\b", re.I),
    re.compile(r"(空投|预售|白名单|加群|私密群|收费群|信号群)", re.I),
    re.compile(r"\bvip\s*(group|channel|signal|会员)\b", re.I),
    re.compile(r"^(gm|gn|wagmi|ngmi|lfg|hello|hi|hey)[\s!.？?！]*$", re.I),
    re.compile(r"(1000x|100x\s*gem|guaranteed\s*profit|free\s*money)", re.I),
    re.compile(r"(点击链接领取|免费领币|内幕群)", re.I),
]

_CRYPTO_RES = [
    re.compile(r"\b(bitcoin|ethereum|solana|ripple|dogecoin|cardano|polkadot)\b", re.I),
    re.compile(r"\b(btc|eth|sol|xrp|bnb|usdt|usdc|dai)\b", re.I),
    re.compile(r"\b(crypto|cryptocurrency|cryptocurrencies|blockchain|web3|defi|nft|nfts)\b", re.I),
    re.compile(r"\b(altcoin|memecoin|stablecoin|tokenomics|airdrop)\b", re.I),
    re.compile(r"\b(binance|coinbase|okx|bybit|kraken|bitfinex|huobi|gate\.io)\b", re.I),
    re.compile(r"\b(metamask|opensea|uniswap|aave|chainlink)\b", re.I),
    re.compile(r"(比特币|以太坊|加密货币|加密资产|数字货币|虚拟货币|区块链)", re.I),
    re.compile(r"(代币|山寨币|稳定币|迷因币|狗狗币|莱特币|瑞波币)", re.I),
    re.compile(r"(币安|火币|欧易|抹茶交易所|数字藏品)", re.I),
    re.compile(r"(链上|链游|挖矿收益|矿工费|gas\s*费|中本聪)", re.I),
]

_PathLike = Union[str, Path, None]

# Keep only the latest mtime per path (stale keys are dropped).
_file_cache: Optional[Tuple[str, float, Dict[str, Any]]] = None
# (resolved_path, group_id) -> (mtime, compiled patterns); empty patterns => disabled
_policy_cache: Dict[Tuple[str, str], Tuple[float, Tuple[re.Pattern[str], ...]]] = {}


class TopicFilterPolicy(NamedTuple):
    """Compiled topic policy for one peer group."""

    enabled: bool
    patterns: Tuple[re.Pattern[str], ...]
    group_id: str
    source: Optional[Path]


class CoarseFilterResult(NamedTuple):
    """Buckets from :func:`coarse_filter_posts` (kept first, then drop reasons)."""

    kept: List[TelegramPost]
    spam_dropped: List[TelegramPost]
    crypto_dropped: List[TelegramPost]
    topic_dropped: List[TelegramPost]


def reset_topic_filter_cache() -> None:
    """Drop YAML/mtime caches (tests)."""
    global _file_cache
    _file_cache = None
    _policy_cache.clear()


def resolve_topic_filters_path(explicit: _PathLike = None) -> Optional[Path]:
    """Resolve the topic-policy file. Missing path ⇒ None (filter is a no-op).

    Order: explicit argument, then ``TOPIC_FILTERS_PATH``, then
    ``./topic_filters.yaml``, then a sibling of ``CONFIG_PATH``.
    An explicit or env path that does not exist does not fall through.
    """
    if explicit is not None and str(explicit).strip():
        return _existing_file(Path(str(explicit).strip()))
    env = (os.environ.get("TOPIC_FILTERS_PATH") or os.environ.get("topic_filters_path") or "").strip()
    if env:
        return _existing_file(Path(env))
    cwd = _existing_file(Path("topic_filters.yaml"))
    if cwd is not None:
        return cwd
    cfg = (os.environ.get("CONFIG_PATH") or os.environ.get("CONFIG_FILE") or "").strip()
    if cfg:
        return _existing_file(Path(cfg).expanduser().parent / "topic_filters.yaml")
    return None


def _existing_file(path: Path) -> Optional[Path]:
    try:
        candidate = path.expanduser()
        if candidate.is_file():
            return candidate.resolve()
    except OSError:
        return None
    return None


def _read_topic_file(path: Path) -> Tuple[float, Dict[str, Any]]:
    global _file_cache
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return -1.0, {}
    key = str(path)
    if _file_cache and _file_cache[0] == key and _file_cache[1] == mtime:
        return mtime, _file_cache[2]
    raw = _parse_mapping(path)
    _file_cache = (key, mtime, raw)
    return mtime, raw


def _parse_mapping(path: Path) -> Dict[str, Any]:
    try:
        from tg_news_monitor.config import parse_yaml_file

        data = parse_yaml_file(path)
        return data if isinstance(data, dict) else {}
    except Exception as exc:
        logger.warning("topic filter file unreadable (%s): %s", path, exc)
        return {}


def _as_mapping(value: Any) -> Dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _category_strings(categories: Any) -> List[str]:
    out: List[str] = []
    if isinstance(categories, dict):
        items: Sequence[Any] = [v for v in categories.values()]
    elif isinstance(categories, list):
        items = categories
    else:
        return out
    for item in items:
        if isinstance(item, str):
            text = item.strip()
            if text:
                out.append(text)
        elif isinstance(item, (list, tuple)):
            for part in item:
                text = str(part).strip() if part is not None else ""
                if text:
                    out.append(text)
    return out


def _block_for_group(raw: Dict[str, Any], group_id: str) -> Optional[Dict[str, Any]]:
    groups = _as_mapping(raw.get("groups"))
    default = raw.get("default")
    default_map = _as_mapping(default) if default is not None else None
    if default_map is None and not groups and ("categories" in raw or "enabled" in raw):
        default_map = {
            "enabled": raw.get("enabled", False),
            "categories": raw.get("categories") or {},
        }
    if group_id and group_id in groups:
        block = groups[group_id]
        return block if isinstance(block, dict) else None
    return default_map


def _compile_patterns(texts: Sequence[str]) -> Tuple[re.Pattern[str], ...]:
    compiled: List[re.Pattern[str]] = []
    for text in texts:
        try:
            compiled.append(re.compile(text, re.I))
        except re.error as exc:
            logger.warning("skipping invalid topic-filter pattern: %s", exc)
    return tuple(compiled)


def load_topic_filters(
    group_id: Optional[str] = None,
    *,
    path: _PathLike = None,
) -> TopicFilterPolicy:
    """Load and compile the topic policy for ``group_id`` (mtime-cached).

    Missing file, unknown group without ``default``, or ``enabled: false``
    ⇒ disabled policy (no patterns).
    """
    gid = (group_id or "").strip()
    resolved = resolve_topic_filters_path(path)
    if resolved is None:
        return TopicFilterPolicy(False, (), gid, None)
    mtime, raw = _read_topic_file(resolved)
    cache_key = (str(resolved), gid)
    cached = _policy_cache.get(cache_key)
    if cached is not None and cached[0] == mtime:
        patterns = cached[1]
        return TopicFilterPolicy(bool(patterns), patterns, gid, resolved)
    block = _block_for_group(raw, gid)
    if not block or not block.get("enabled", False):
        _policy_cache[cache_key] = (mtime, ())
        return TopicFilterPolicy(False, (), gid, resolved)
    patterns = _compile_patterns(_category_strings(block.get("categories")))
    _policy_cache[cache_key] = (mtime, patterns)
    return TopicFilterPolicy(bool(patterns), patterns, gid, resolved)


def _text_looks_crypto(text: str) -> bool:
    """True if text is clearly crypto/blockchain related."""
    t = (text or "").strip()
    if not t:
        return False
    return any(rx.search(t) for rx in _CRYPTO_RES)


def _text_looks_restricted_topic(
    text: str,
    group_id: Optional[str] = None,
    *,
    path: _PathLike = None,
) -> bool:
    """True if text matches the group's configured topic policy."""
    t = (text or "").strip()
    if not t:
        return False
    policy = load_topic_filters(group_id, path=path)
    if not policy.enabled:
        return False
    return any(rx.search(t) for rx in policy.patterns)


def coarse_filter_posts(
    posts: List[TelegramPost],
    group_id: Optional[str] = None,
    *,
    path: _PathLike = None,
) -> CoarseFilterResult:
    """Drop obvious spam/noise, crypto, and configured-topic posts (zero LLM).

    Returns:
        CoarseFilterResult(kept, spam_dropped, crypto_dropped, topic_dropped)
    """
    kept: List[TelegramPost] = []
    spam_dropped: List[TelegramPost] = []
    crypto_dropped: List[TelegramPost] = []
    topic_dropped: List[TelegramPost] = []
    policy = load_topic_filters(group_id, path=path)
    for post in posts:
        text = (post.text or "").strip()
        if len(text) < _COARSE_EMPTY_MAX_LEN:
            spam_dropped.append(post)
            continue
        if any(rx.search(text) for rx in _COARSE_SPAM_RES):
            spam_dropped.append(post)
            continue
        if _text_looks_crypto(text):
            crypto_dropped.append(post)
            continue
        if policy.enabled and any(rx.search(text) for rx in policy.patterns):
            topic_dropped.append(post)
            continue
        kept.append(post)
    return CoarseFilterResult(kept, spam_dropped, crypto_dropped, topic_dropped)


__all__ = [
    "CoarseFilterResult",
    "TopicFilterPolicy",
    "_COARSE_EMPTY_MAX_LEN",
    "_COARSE_SPAM_RES",
    "_CRYPTO_RES",
    "_text_looks_crypto",
    "_text_looks_restricted_topic",
    "coarse_filter_posts",
    "load_topic_filters",
    "reset_topic_filter_cache",
    "resolve_topic_filters_path",
]
