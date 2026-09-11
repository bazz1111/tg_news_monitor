"""Unit tests for cheap local coarse filters (spam / crypto / topic loader)."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest
import yaml

from tg_news_monitor.core.filters import (
    CoarseFilterResult,
    _text_looks_restricted_topic,
    coarse_filter_posts,
    load_topic_filters,
    reset_topic_filter_cache,
)
from tg_news_monitor.core.models import TelegramPost
from tg_news_monitor.evaluator.prompt import DIGEST_SYSTEM_PROMPT, STORY_DIGEST_SYSTEM_PROMPT

NEWS24_MARKER = "RESTRICTED_WIDGET_TOKEN"
OLD_PHOTOS_MARKER = "OLD_PHOTOS_ONLY_TOKEN"
INVEST_CN = "本周给出目标价并承诺跟单保本收益说明"


def _post(text: str, mid: int = 1, channel: str = "wire") -> TelegramPost:
    return TelegramPost(
        channel=channel,
        message_id=mid,
        published_at=datetime(2026, 9, 11, 12, 0, tzinfo=timezone.utc),
        text=text,
        direct_url=f"https://t.me/{channel}/{mid}",
    )


def _write_policy(path: Path, doc: dict) -> Path:
    path.write_text(yaml.safe_dump(doc, allow_unicode=True, sort_keys=False), encoding="utf-8")
    reset_topic_filter_cache()
    return path


@pytest.fixture
def two_group_policy(tmp_path: Path) -> Path:
    return _write_policy(
        tmp_path / "topic_filters.yaml",
        {
            "default": {"enabled": False, "categories": {}},
            "groups": {
                "news24": {
                    "enabled": True,
                    "categories": {
                        "investment_solicitation": [
                            NEWS24_MARKER,
                            r"目标价|跟单|保本",
                        ],
                    },
                },
                "old_photos": {
                    "enabled": True,
                    "categories": {
                        "demo": [OLD_PHOTOS_MARKER],
                    },
                },
            },
        },
    )


@pytest.fixture(autouse=True)
def _clear_topic_cache(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("TOPIC_FILTERS_PATH", raising=False)
    reset_topic_filter_cache()
    yield
    reset_topic_filter_cache()


KEEP_WIRES = [
    "BREAKING: Fed holds rates, CPI in line with FOMC outlook this month",
    "SpaceX Falcon 9 launched successfully; Rocket Lab schedules Electron",
    "Apple reports quarterly earnings, iPhone revenue rose twelve percent",
]


class TestTopicLoaderNoop:
    def test_missing_file_is_noop(self, tmp_path: Path) -> None:
        missing = tmp_path / "does-not-exist.yaml"
        text = f"long enough sample mentioning {NEWS24_MARKER} in passing"
        policy = load_topic_filters("news24", path=missing)
        assert policy.enabled is False
        assert policy.patterns == ()
        kept, _spam, crypto, topic = coarse_filter_posts(
            [_post(text)], group_id="news24", path=missing
        )
        assert crypto == []
        assert topic == []
        assert [p.text for p in kept] == [text]
        assert not _text_looks_restricted_topic(text, "news24", path=missing)

    def test_disabled_default_for_unknown_group(self, two_group_policy: Path) -> None:
        text = f"long enough sample mentioning {NEWS24_MARKER} in passing"
        kept, _spam, _crypto, topic = coarse_filter_posts(
            [_post(text)], group_id="unknown_desk", path=two_group_policy
        )
        assert topic == []
        assert [p.text for p in kept] == [text]


class TestPerGroupTopicPolicy:
    def test_same_text_dropped_for_news24_kept_for_old_photos(
        self, two_group_policy: Path
    ) -> None:
        text = f"desk note mentions {NEWS24_MARKER} inside a longer wire"
        news_kept, _, _, news_topic = coarse_filter_posts(
            [_post(text)], group_id="news24", path=two_group_policy
        )
        photo_kept, _, _, photo_topic = coarse_filter_posts(
            [_post(text)], group_id="old_photos", path=two_group_policy
        )
        assert [p.text for p in news_topic] == [text]
        assert news_kept == []
        assert photo_topic == []
        assert [p.text for p in photo_kept] == [text]

    def test_old_photos_marker_inverse(self, two_group_policy: Path) -> None:
        text = f"caption mentions {OLD_PHOTOS_MARKER} in a longer line"
        news_kept, _, _, news_topic = coarse_filter_posts(
            [_post(text)], group_id="news24", path=two_group_policy
        )
        photo_kept, _, _, photo_topic = coarse_filter_posts(
            [_post(text)], group_id="old_photos", path=two_group_policy
        )
        assert news_topic == []
        assert [p.text for p in news_kept] == [text]
        assert [p.text for p in photo_topic] == [text]
        assert photo_kept == []

    def test_investment_placeholder_drops_on_news24(self, two_group_policy: Path) -> None:
        kept, _spam, crypto, topic = coarse_filter_posts(
            [_post(INVEST_CN)], group_id="news24", path=two_group_policy
        )
        assert crypto == []
        assert kept == []
        assert [p.text for p in topic] == [INVEST_CN]


class TestExistingCoarseFilters:
    @pytest.mark.parametrize("text", KEEP_WIRES)
    def test_must_keep_macro_and_launch_wires(self, text: str, two_group_policy: Path) -> None:
        kept, spam, crypto, topic = coarse_filter_posts(
            [_post(text)], group_id="news24", path=two_group_policy
        )
        assert spam == []
        assert crypto == []
        assert topic == []
        assert [p.text for p in kept] == [text]

    def test_crypto_still_drops_btc_eth(self, two_group_policy: Path) -> None:
        text = "Bitcoin and ETH rebound as BTC breaks resistance this morning"
        kept, _spam, crypto, topic = coarse_filter_posts(
            [_post(text)], group_id="news24", path=two_group_policy
        )
        assert kept == []
        assert topic == []
        assert [p.text for p in crypto] == [text]

    def test_spam_still_drops_airdrop_presale(self, two_group_policy: Path) -> None:
        text = "1000x GEM PRESALE IS LIVE! Free airdrop for first 50!"
        kept, spam, crypto, topic = coarse_filter_posts(
            [_post(text)], group_id="news24", path=two_group_policy
        )
        assert kept == []
        assert crypto == []
        assert topic == []
        assert [p.text for p in spam] == [text]

    def test_short_text_is_spam_not_topic(self, two_group_policy: Path) -> None:
        kept, spam, crypto, topic = coarse_filter_posts(
            [_post("hi there")], group_id="news24", path=two_group_policy
        )
        assert kept == []
        assert crypto == []
        assert topic == []
        assert len(spam) == 1


class TestCoarseFilterResultShape:
    def test_buckets_are_separate_and_named(self, two_group_policy: Path) -> None:
        posts = [
            _post(KEEP_WIRES[0], 1),
            _post("1000x GEM PRESALE IS LIVE! Free airdrop now!", 2),
            _post("Bitcoin and ETH rebound as BTC breaks resistance", 3),
            _post(f"desk note mentions {NEWS24_MARKER} inside a longer wire", 4),
        ]
        result = coarse_filter_posts(posts, group_id="news24", path=two_group_policy)
        assert isinstance(result, CoarseFilterResult)
        assert [p.message_id for p in result.kept] == [1]
        assert [p.message_id for p in result.spam_dropped] == [2]
        assert [p.message_id for p in result.crypto_dropped] == [3]
        assert [p.message_id for p in result.topic_dropped] == [4]


class TestNewsDigestPromptPolicy:
    def test_news_prompt_honors_site_topic_policy_without_name_lists(self) -> None:
        assert "站点主题策略" in DIGEST_SYSTEM_PROMPT
        assert "受限主题" in DIGEST_SYSTEM_PROMPT
        assert "抖音" in DIGEST_SYSTEM_PROMPT
        assert "宁可漏报" in DIGEST_SYSTEM_PROMPT
        assert "党政军领导人" in DIGEST_SYSTEM_PROMPT
        assert "站点主题策略" not in STORY_DIGEST_SYSTEM_PROMPT
