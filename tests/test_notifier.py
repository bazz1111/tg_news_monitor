"""Unit tests for Feishu (Lark) Interactive Alert Dispatcher.

Tests:
1. Card Schema 2.0 structure validation:
   - Conformance with Feishu Interactive Card Schema 2.0.
   - Validated via MockFeishuReceiver.validate_card_schema.
   - Checks header (title, subtitle, template, ud_icon) and body elements (markdown summary, hr, takeaways, note, action button).
2. Dynamic 4-tier color template & icon mapping:
   - Score 9-10 -> red, alarm_outlined (Breaking / Extreme urgency)
   - Score 7-8 -> orange, bell_outlined (High importance)
   - Score 5-6 -> blue, info_outlined (Standard news)
   - Score 1-4 -> grey, cross_outlined (Low urgency)
   - Score boundary clamping (< 1 and > 10).
3. Visual elements and content formatting:
   - Bulleted summary points rendering with clean markers.
   - Forward attribution in note block when forward_from is present/absent.
   - Action element primary button linking directly to https://t.me/{channel}/{message_id}.
   - Compatibility with domain models (TelegramPost, NewsEvaluation) and plain dictionaries.
4. HMAC-SHA256 signature computation and header inclusion:
   - Deterministic verification against known test vectors.
   - Inclusion of timestamp and sign in payload body and HTTP request headers.
5. Webhook delivery, rate-limit resilience, and retry backoff:
   - Successful delivery with code == 0.
   - Resilience against Feishu application code 19001 (frequency limit exceeded).
   - Resilience against HTTP 429 Too Many Requests with backoff.
   - Server error 5xx retries.
   - Fast failure on non-retryable 400 / code 9499 errors without wasted retries.
   - Exhaustion handling returning False after max_retries.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Dict, List
from unittest.mock import MagicMock

import httpx
import pytest

from tests.conftest import MockFeishuReceiver
from tg_news_monitor.core.models import NewsEvaluation, TelegramPost
from tg_news_monitor.notifier.feishu_card import (
    COLOR_TEMPLATE_MAP,
    HEADER_ICON_MAP,
    FeishuCardBuilder,
    build_card,
    build_feishu_card,
    get_color_template,
    get_header_icon,
)
from tg_news_monitor.notifier.webhook_sender import (
    FeishuWebhookSender,
    Notifier,
    generate_signature,
    send_alert,
)


# ==============================================================================
# Test Fixtures & Sample Domain Data
# ==============================================================================

@pytest.fixture
def sample_telegram_post() -> TelegramPost:
    return TelegramPost(
        channel="whale_alert",
        message_id=40921,
        published_at=datetime(2026, 9, 8, 21, 30, 0, tzinfo=timezone.utc),
        text="🚨 15,000 ETH transferred from unknown wallet to Binance.",
        has_media=False,
        media_type=None,
        direct_url="https://t.me/whale_alert/40921",
        forward_from="CryptoWire News",
    )


@pytest.fixture
def sample_telegram_post_no_forward() -> TelegramPost:
    return TelegramPost(
        channel="coindesk",
        message_id=50123,
        published_at=datetime(2026, 9, 8, 22, 0, 0, tzinfo=timezone.utc),
        text="Federal regulators approve new multi-chain crypto index product.",
        has_media=True,
        media_type="photo",
        direct_url="https://t.me/coindesk/50123",
        forward_from=None,
    )


@pytest.fixture
def sample_news_eval_score_9() -> NewsEvaluation:
    return NewsEvaluation(
        score=9,
        is_news=True,
        is_spam=False,
        title="巨额以太坊异动转入主流交易所",
        summary_bullets=[
            "15,000 枚 ETH（价值约 4,512 万美元）从未知钱包转入币安。",
            "涉事未知钱包持有超 6 个月，疑似巨鲸获利清仓或做市商调仓。",
            "短期现货抛压与借贷清算预期迅速升温。",
        ],
        key_takeaways=[
            "超大额资金进入交易所通常预示短期流动性变化与潜在抛售压力。",
            "对主流公链生态代币短期走势形成压制。",
        ],
        category="突发安全",
        actionable_insight="密切关注链上深度与大额卖单挂单动态，防范剧烈市场波动风险。",
    )


@pytest.fixture
def sample_news_eval_score_7() -> NewsEvaluation:
    return NewsEvaluation(
        score=7,
        is_news=True,
        is_spam=False,
        title="监管机构正式通过多链现货指数产品",
        summary_bullets=[
            "监管机构正式批准涵盖以太坊与索拉纳的双重现货指数基金。",
            "机构投资者申购通道将于下周一正式开放。",
        ],
        key_takeaways=[
            "显著提升长期合规机构资金入场预期与市场合规信任度。",
        ],
        category="宏观监管",
        actionable_insight="留意下周机构资金实际净流入数据。",
    )


# ==============================================================================
# 1. Feishu Interactive Card Schema 2.0 Validation
# ==============================================================================

class TestFeishuCardSchema2:
    """Validates structural compliance with Feishu Card Schema 2.0."""

    def test_schema_2_0_conformance_with_receiver_validator(
        self,
        sample_telegram_post: TelegramPost,
        sample_news_eval_score_9: NewsEvaluation,
    ) -> None:
        """Card must pass MockFeishuReceiver.validate_card_schema strict verification."""
        card_payload = build_card(sample_telegram_post, sample_news_eval_score_9)

        is_valid, msg = MockFeishuReceiver.validate_card_schema(card_payload)
        assert is_valid is True, f"Card schema validation failed: {msg}"

    def test_card_root_and_header_structure(
        self,
        sample_telegram_post: TelegramPost,
        sample_news_eval_score_9: NewsEvaluation,
    ) -> None:
        """Card root must be msg_type='interactive' with schema='2.0' and complete header."""
        card_payload = FeishuCardBuilder.build_card(sample_telegram_post, sample_news_eval_score_9)

        assert card_payload.get("msg_type") == "interactive"
        card = card_payload.get("card", {})
        assert card.get("schema") == "2.0"

        header = card.get("header", {})
        assert header.get("template") == "red"
        assert header.get("title", {}).get("tag") == "plain_text"
        assert "突发安全" in header.get("title", {}).get("content", "")
        assert "巨额以太坊异动" in header.get("title", {}).get("content", "")
        assert header.get("subtitle", {}).get("tag") == "plain_text"
        assert "ud_icon" not in header  # webhook bots reject ud_icon

    def test_card_body_elements_order_and_tags(
        self,
        sample_telegram_post: TelegramPost,
        sample_news_eval_score_9: NewsEvaluation,
    ) -> None:
        """Body elements must include markdown summary, hr divider, takeaways, metadata div, and action button."""
        card_payload = build_feishu_card(sample_telegram_post, sample_news_eval_score_9)
        elements = card_payload["card"]["body"]["elements"]

        # Tag presence check
        tags = [el.get("tag") for el in elements]
        assert "div" in tags
        assert "hr" in tags
        assert tags.count("div") >= 3  # summary, takeaways, metadata
        assert "button" in tags

        # 1. Summary Markdown Element (first div)
        summary_div = elements[0]
        assert summary_div.get("tag") == "div"
        content_0 = summary_div.get("text", {}).get("content", "")
        assert "**🏷️ 资讯分类**" in content_0
        assert "@whale_alert" in content_0
        assert "9 / 10" in content_0
        assert "**📌 核心速览**" in content_0
        assert "• 15,000 枚 ETH" in content_0

        # 2. Divider element
        divider = elements[1]
        assert divider.get("tag") == "hr"

        # 3. Key Takeaways Element
        takeaways_div = elements[2]
        assert takeaways_div.get("tag") == "div"
        content_takeaways = takeaways_div.get("text", {}).get("content", "")
        assert "**💡 关键影响**" in content_takeaways
        assert "**🎯 关注建议**" in content_takeaways

        # 4. Metadata Element (div; Schema 2.0 rejects note)
        note_el = elements[3]
        assert note_el.get("tag") == "div"
        note_text = note_el.get("text", {}).get("content", "")
        assert "📢 来源频道: @whale_alert" in note_text
        assert "2026-09-08 21:30:00 UTC" in note_text
        assert "🆔 消息ID: #40921" in note_text
        assert "🔁 转发自: CryptoWire News" in note_text

        # 5. Button with open_url behavior (Schema 2.0)
        action_el = elements[4]
        assert action_el.get("tag") == "button"
        assert action_el.get("type") == "primary"
        assert "查看 Telegram 原文" in action_el.get("text", {}).get("content", "")
        behaviors = action_el.get("behaviors", [])
        assert behaviors and behaviors[0].get("type") == "open_url"
        assert behaviors[0].get("default_url") == "https://t.me/whale_alert/40921"


# ==============================================================================
# 2. Dynamic 4-Tier Color Mapping & Icon Tests
# ==============================================================================

class TestColorTemplateAndIconMapping:
    """Verifies score-to-color template mapping across all tiers."""

    @pytest.mark.parametrize(
        ("score", "expected_template", "expected_icon"),
        [
            (10, "red", "alarm_outlined"),
            (9, "red", "alarm_outlined"),
            (8, "orange", "bell_outlined"),
            (7, "orange", "bell_outlined"),
            (6, "blue", "info_outlined"),
            (5, "blue", "info_outlined"),
            (4, "grey", "cross_outlined"),
            (3, "grey", "cross_outlined"),
            (2, "grey", "cross_outlined"),
            (1, "grey", "cross_outlined"),
        ],
    )
    def test_color_template_mapping_1_to_10(
        self,
        score: int,
        expected_template: str,
        expected_icon: str,
    ) -> None:
        """Scores 1-10 must map accurately to 4 Feishu color templates and standard icons."""
        assert get_color_template(score) == expected_template
        assert get_header_icon(score) == expected_icon

    def test_score_boundary_clamping(self) -> None:
        """Out-of-bounds scores must be clamped gracefully."""
        # Score > 10 clamped to red tier
        assert get_color_template(15) == "red"
        assert get_header_icon(15) == "alarm_outlined"

        # Score < 1 clamped to grey tier
        assert get_color_template(0) == "grey"
        assert get_color_template(-5) == "grey"
        assert get_header_icon(0) == "cross_outlined"

        # Invalid types default safely to grey
        assert get_color_template("invalid") == "grey"  # type: ignore

    def test_card_header_reflects_color_for_all_scores(
        self,
        sample_telegram_post: TelegramPost,
    ) -> None:
        """Constructed card header template must match score mapping across all tiers."""
        for score in range(1, 11):
            eval_mock = NewsEvaluation(
                score=score,
                is_news=True,
                is_spam=False,
                title=f"Test Headline Score {score}",
                summary_bullets=["Point A"],
                key_takeaways=["Takeaway A"],
                category="行业快讯",
            )
            card = build_card(sample_telegram_post, eval_mock)
            expected_color = get_color_template(score)
            expected_icon = get_header_icon(score)

            assert card["card"]["header"]["template"] == expected_color
            assert "ud_icon" not in card["card"]["header"]


# ==============================================================================
# 3. Content Formatting & Edge Cases
# ==============================================================================

class TestCardFormattingAndEdgeCases:
    """Verifies edge case handling in card construction."""

    def test_forward_attribution_omitted_when_none(
        self,
        sample_telegram_post_no_forward: TelegramPost,
        sample_news_eval_score_7: NewsEvaluation,
    ) -> None:
        """When post has no forward_from, the note element must not mention forward attribution."""
        card = build_card(sample_telegram_post_no_forward, sample_news_eval_score_7)
        elements = card["card"]["body"]["elements"]
        note_el = [el for el in elements if el.get("tag") == "note"][0]
        note_content = note_el["elements"][0]["content"]

        assert "🔁 转发自:" not in note_content
        assert "📢 来源频道: @coindesk" in note_content

    def test_empty_summary_bullets_falls_back_to_text_snippet(
        self,
        sample_telegram_post: TelegramPost,
    ) -> None:
        """If summary_bullets is empty, the card should fall back to a snippet of post.text."""
        eval_no_bullets = NewsEvaluation(
            score=6,
            is_news=True,
            is_spam=False,
            title="Short Title",
            summary_bullets=[],
            key_takeaways=[],
            category="行业快讯",
        )
        card = build_card(sample_telegram_post, eval_no_bullets)
        summary_div = card["card"]["body"]["elements"][0]
        content = summary_div["text"]["content"]

        assert "15,000 ETH transferred" in content

    def test_dict_inputs_compatibility(self) -> None:
        """build_card must support plain dictionary inputs identically to Pydantic models."""
        post_dict = {
            "channel": "binance_announcements",
            "message_id": 9988,
            "published_at": "2026-09-08 20:00:00 UTC",
            "text": "New trading pair listed.",
            "direct_url": "https://t.me/binance_announcements/9988",
            "forward_from": None,
        }
        eval_dict = {
            "score": 8,
            "title": "币安上线全新交易对",
            "category": "重大商业",
            "summary_bullets": ["上线 BTC/FDUSD 零手续费对", "支持现货与合约"],
            "key_takeaways": "为做市商及散户带来更低交易滑点与更高流动性。",
            "actionable_insight": "关注流动性注入节奏。",
        }

        card = build_card(post_dict, eval_dict)
        is_valid, msg = MockFeishuReceiver.validate_card_schema(card)
        assert is_valid is True, f"Dict card schema failed: {msg}"
        assert card["card"]["header"]["template"] == "orange"
        assert "币安上线全新交易对" in card["card"]["header"]["title"]["content"]


# ==============================================================================
# 4. HMAC-SHA256 Signature Verification
# ==============================================================================

class TestHMACSignatureComputation:
    """Verifies Feishu HMAC-SHA256 signature algorithm and header inclusion."""

    def test_signature_against_known_test_vector(self) -> None:
        """Verifies signature generation against known deterministic test vector."""
        ts = 1599360473
        secret = "test_secret"
        expected_sig = "FW06sV98dJlmB07TC2kBBUSpkGrDmWP+mE2IRa7SQhA="

        computed_sig = generate_signature(ts, secret)
        assert computed_sig == expected_sig

        # FeishuWebhookSender static alias check
        assert FeishuWebhookSender.generate_signature(ts, secret) == expected_sig

    def test_empty_secret_returns_empty_signature(self) -> None:
        """When secret is empty or None, signature should be empty."""
        assert generate_signature(1599360473, "") == ""

    def test_request_preparation_includes_signature_in_body_and_headers(self) -> None:
        """prepare_request must inject sign and timestamp into payload body and X-Lark / X-Feishu headers."""
        sender = FeishuWebhookSender(
            webhook_url="https://open.feishu.cn/open-apis/bot/v2/hook/dummy",
            secret="super_secret_token",
        )
        sample_payload = {"msg_type": "interactive", "card": {"schema": "2.0"}}

        headers, prepared_payload = sender.prepare_request(sample_payload)

        # In payload body
        assert "sign" in prepared_payload
        assert "timestamp" in prepared_payload
        assert len(prepared_payload["sign"]) > 0

        # In HTTP request headers
        assert "X-Lark-Signature" in headers
        assert "X-Lark-Timestamp" in headers
        assert "X-Feishu-Signature" in headers
        assert "X-Feishu-Timestamp" in headers

        assert headers["X-Lark-Signature"] == prepared_payload["sign"]
        assert headers["X-Lark-Timestamp"] == prepared_payload["timestamp"]


# ==============================================================================
# 5. Webhook Sender Delivery, Rate-Limit Resilience & Retries
# ==============================================================================

class TestFeishuWebhookSenderDeliveryAndResilience:
    """Tests webhook delivery, code 19001 rate-limiting backoff, HTTP 429 backoff, and failure recovery."""

    def test_successful_delivery_code_0(
        self,
        mock_feishu_receiver: MockFeishuReceiver,
        sample_telegram_post: TelegramPost,
        sample_news_eval_score_9: NewsEvaluation,
    ) -> None:
        """Successful delivery when Feishu returns HTTP 200 with code == 0."""
        def transport_handler(request: httpx.Request) -> httpx.Response:
            payload = json.loads(request.content.decode("utf-8"))
            headers = dict(request.headers)
            res = mock_feishu_receiver.record_call(payload, headers)
            return httpx.Response(res["status_code"], json=res["json"])

        client = httpx.Client(transport=httpx.MockTransport(transport_handler))
        sender = FeishuWebhookSender(
            webhook_url="https://open.feishu.cn/open-apis/bot/v2/hook/mock_token",
            base_delay=0.01,
            http_client=client,
        )

        card_payload = build_card(sample_telegram_post, sample_news_eval_score_9)
        success = sender.send(card_payload)

        assert success is True
        assert mock_feishu_receiver.total_received == 1
        recorded_card = mock_feishu_receiver.get_cards()[0]
        assert recorded_card["card"]["schema"] == "2.0"

    def test_rate_limit_code_19001_exponential_backoff_and_retry_success(
        self,
        mock_feishu_receiver: MockFeishuReceiver,
        sample_telegram_post: TelegramPost,
        sample_news_eval_score_9: NewsEvaluation,
    ) -> None:
        """When Feishu returns code 19001 (frequency limit), sender must back off and retry successfully."""
        # Receiver will return code 19001 on first call, code 0 on second call
        mock_feishu_receiver.simulate_rate_limit(times=1)

        def transport_handler(request: httpx.Request) -> httpx.Response:
            payload = json.loads(request.content.decode("utf-8"))
            headers = dict(request.headers)
            res = mock_feishu_receiver.record_call(payload, headers)
            return httpx.Response(res["status_code"], json=res["json"])

        client = httpx.Client(transport=httpx.MockTransport(transport_handler))
        sender = FeishuWebhookSender(
            webhook_url="https://open.feishu.cn/open-apis/bot/v2/hook/mock_token",
            max_retries=3,
            base_delay=0.01,
            http_client=client,
        )

        card_payload = build_card(sample_telegram_post, sample_news_eval_score_9)
        success = sender.send(card_payload)

        assert success is True
        assert mock_feishu_receiver.total_received == 2

    def test_http_429_too_many_requests_backoff_and_retry_success(
        self,
        sample_telegram_post: TelegramPost,
        sample_news_eval_score_7: NewsEvaluation,
    ) -> None:
        """When receiving HTTP 429, sender must parse Retry-After, back off, and retry successfully."""
        call_count = 0

        def transport_handler(request: httpx.Request) -> httpx.Response:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return httpx.Response(
                    429,
                    headers={"Retry-After": "0"},
                    json={"code": 429, "msg": "too many requests"},
                )
            return httpx.Response(200, json={"code": 0, "msg": "success"})

        client = httpx.Client(transport=httpx.MockTransport(transport_handler))
        sender = FeishuWebhookSender(
            webhook_url="https://open.feishu.cn/open-apis/bot/v2/hook/mock_token",
            max_retries=3,
            base_delay=0.01,
            http_client=client,
        )

        card_payload = build_card(sample_telegram_post, sample_news_eval_score_7)
        success = sender.send(card_payload)

        assert success is True
        assert call_count == 2

    def test_non_retryable_error_aborts_immediately(
        self,
        mock_feishu_receiver: MockFeishuReceiver,
        sample_telegram_post: TelegramPost,
        sample_news_eval_score_9: NewsEvaluation,
    ) -> None:
        """When receiving HTTP 400 or Feishu code 9499 (invalid payload), abort without wasting retries."""
        mock_feishu_receiver.simulate_bad_request(times=1)

        def transport_handler(request: httpx.Request) -> httpx.Response:
            payload = json.loads(request.content.decode("utf-8"))
            headers = dict(request.headers)
            res = mock_feishu_receiver.record_call(payload, headers)
            return httpx.Response(res["status_code"], json=res["json"])

        client = httpx.Client(transport=httpx.MockTransport(transport_handler))
        sender = FeishuWebhookSender(
            webhook_url="https://open.feishu.cn/open-apis/bot/v2/hook/mock_token",
            max_retries=3,
            base_delay=0.01,
            http_client=client,
        )

        card_payload = build_card(sample_telegram_post, sample_news_eval_score_9)
        success = sender.send(card_payload)

        assert success is False
        # Must have aborted after attempt 1 without doing remaining retries
        assert mock_feishu_receiver.total_received == 1

    def test_rate_limit_retry_exhaustion_returns_false(
        self,
        mock_feishu_receiver: MockFeishuReceiver,
        sample_telegram_post: TelegramPost,
        sample_news_eval_score_9: NewsEvaluation,
    ) -> None:
        """When rate limit persists across all max_retries, sender must gracefully return False."""
        mock_feishu_receiver.simulate_rate_limit(times=5)

        def transport_handler(request: httpx.Request) -> httpx.Response:
            payload = json.loads(request.content.decode("utf-8"))
            headers = dict(request.headers)
            res = mock_feishu_receiver.record_call(payload, headers)
            return httpx.Response(res["status_code"], json=res["json"])

        client = httpx.Client(transport=httpx.MockTransport(transport_handler))
        sender = FeishuWebhookSender(
            webhook_url="https://open.feishu.cn/open-apis/bot/v2/hook/mock_token",
            max_retries=3,
            base_delay=0.01,
            http_client=client,
        )

        card_payload = build_card(sample_telegram_post, sample_news_eval_score_9)
        success = sender.send(card_payload)

        assert success is False
        assert mock_feishu_receiver.total_received == 3

    def test_server_error_500_retries_and_recovers(
        self,
        mock_feishu_receiver: MockFeishuReceiver,
        sample_telegram_post: TelegramPost,
        sample_news_eval_score_9: NewsEvaluation,
    ) -> None:
        """When receiving HTTP 500, sender must retry and succeed if server recovers."""
        mock_feishu_receiver.simulate_server_error(times=2)

        def transport_handler(request: httpx.Request) -> httpx.Response:
            payload = json.loads(request.content.decode("utf-8"))
            headers = dict(request.headers)
            res = mock_feishu_receiver.record_call(payload, headers)
            return httpx.Response(res["status_code"], json=res["json"])

        client = httpx.Client(transport=httpx.MockTransport(transport_handler))
        sender = FeishuWebhookSender(
            webhook_url="https://open.feishu.cn/open-apis/bot/v2/hook/mock_token",
            max_retries=3,
            base_delay=0.01,
            http_client=client,
        )

        card_payload = build_card(sample_telegram_post, sample_news_eval_score_9)
        success = sender.send(card_payload)

        assert success is True
        assert mock_feishu_receiver.total_received == 3

    def test_missing_webhook_url_returns_false_safely(self) -> None:
        """If webhook URL is empty, sender logs error and returns False without crashing."""
        sender = FeishuWebhookSender(webhook_url="")
        assert sender.send({"msg_type": "interactive"}) is False

    def test_send_card_convenience_method(
        self,
        mock_feishu_receiver: MockFeishuReceiver,
        sample_telegram_post: TelegramPost,
        sample_news_eval_score_9: NewsEvaluation,
    ) -> None:
        """send_card convenience method builds card and dispatches to webhook in one call."""
        def transport_handler(request: httpx.Request) -> httpx.Response:
            payload = json.loads(request.content.decode("utf-8"))
            headers = dict(request.headers)
            res = mock_feishu_receiver.record_call(payload, headers)
            return httpx.Response(res["status_code"], json=res["json"])

        client = httpx.Client(transport=httpx.MockTransport(transport_handler))
        sender = FeishuWebhookSender(
            webhook_url="https://open.feishu.cn/open-apis/bot/v2/hook/mock_token",
            base_delay=0.01,
            http_client=client,
        )

        success = sender.send_card(sample_telegram_post, sample_news_eval_score_9)
        assert success is True
        assert mock_feishu_receiver.total_received == 1


# ==============================================================================
# 6. Notifier Facade & send_alert Functional Interface Tests
# ==============================================================================

class TestNotifierFacade:
    """Verifies compliance with PROJECT.md Notifier contract."""

    def test_notifier_facade_build_card_and_send_alert(
        self,
        mock_feishu_receiver: MockFeishuReceiver,
        sample_telegram_post: TelegramPost,
        sample_news_eval_score_9: NewsEvaluation,
    ) -> None:
        """Notifier.build_card and Notifier.send_alert contract check."""
        def transport_handler(request: httpx.Request) -> httpx.Response:
            payload = json.loads(request.content.decode("utf-8"))
            headers = dict(request.headers)
            res = mock_feishu_receiver.record_call(payload, headers)
            return httpx.Response(res["status_code"], json=res["json"])

        client = httpx.Client(transport=httpx.MockTransport(transport_handler))

        # 1. Notifier.build_card
        card_json = Notifier.build_card(sample_telegram_post, sample_news_eval_score_9)
        assert card_json["msg_type"] == "interactive"
        assert card_json["card"]["schema"] == "2.0"

        # 2. Notifier.send_alert
        result = Notifier.send_alert(
            card_json,
            webhook_url="https://open.feishu.cn/open-apis/bot/v2/hook/test",
            http_client=client,
        )
        assert result is True
        assert mock_feishu_receiver.total_received == 1

    def test_functional_send_alert_interface(
        self,
        mock_feishu_receiver: MockFeishuReceiver,
        sample_telegram_post: TelegramPost,
        sample_news_eval_score_9: NewsEvaluation,
    ) -> None:
        """send_alert standalone function test."""
        def transport_handler(request: httpx.Request) -> httpx.Response:
            payload = json.loads(request.content.decode("utf-8"))
            headers = dict(request.headers)
            res = mock_feishu_receiver.record_call(payload, headers)
            return httpx.Response(res["status_code"], json=res["json"])

        client = httpx.Client(transport=httpx.MockTransport(transport_handler))
        card_json = build_card(sample_telegram_post, sample_news_eval_score_9)

        result = send_alert(
            card_json,
            webhook_url="https://open.feishu.cn/open-apis/bot/v2/hook/functional",
            http_client=client,
        )
        assert result is True
        assert mock_feishu_receiver.total_received == 1
