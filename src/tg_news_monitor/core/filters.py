"""Cheap local spam/crypto filters (zero LLM).

Conservative heuristics that drop only clear junk / crypto-related posts
before batch digest evaluation.
"""

from __future__ import annotations

import re
from typing import List, Tuple

from tg_news_monitor.core.models import TelegramPost

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
    # English
    re.compile(r"\b(bitcoin|ethereum|solana|ripple|dogecoin|cardano|polkadot)\b", re.I),
    re.compile(r"\b(btc|eth|sol|xrp|bnb|usdt|usdc|dai)\b", re.I),
    re.compile(r"\b(crypto|cryptocurrency|cryptocurrencies|blockchain|web3|defi|nft|nfts)\b", re.I),
    re.compile(r"\b(altcoin|memecoin|stablecoin|tokenomics|airdrop)\b", re.I),
    re.compile(r"\b(binance|coinbase|okx|bybit|kraken|bitfinex|huobi|gate\.io)\b", re.I),
    re.compile(r"\b(metamask|opensea|uniswap|aave|chainlink)\b", re.I),
    # Chinese
    re.compile(r"(比特币|以太坊|加密货币|加密资产|数字货币|虚拟货币|区块链)", re.I),
    re.compile(r"(代币|山寨币|稳定币|迷因币|狗狗币|莱特币|瑞波币)", re.I),
    re.compile(r"(币安|火币|欧易|抹茶交易所|数字藏品)", re.I),
    re.compile(r"(链上|链游|挖矿收益|矿工费|gas\s*费|聪)", re.I),
]


def _text_looks_crypto(text: str) -> bool:
    """True if text is clearly crypto/blockchain related."""
    t = (text or "").strip()
    if not t:
        return False
    return any(rx.search(t) for rx in _CRYPTO_RES)


def coarse_filter_posts(
    posts: List[TelegramPost],
) -> Tuple[List[TelegramPost], List[TelegramPost], List[TelegramPost]]:
    """Drop obvious spam/noise and crypto posts locally (zero LLM).

    Returns:
        (kept, spam_dropped, crypto_dropped)
    """
    kept: List[TelegramPost] = []
    spam_dropped: List[TelegramPost] = []
    crypto_dropped: List[TelegramPost] = []
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
        kept.append(post)
    return kept, spam_dropped, crypto_dropped


__all__ = [
    "_COARSE_EMPTY_MAX_LEN",
    "_COARSE_SPAM_RES",
    "_CRYPTO_RES",
    "_text_looks_crypto",
    "coarse_filter_posts",
]
