"""Unit test suite for Grok Hot News Evaluator, Summarizer, and Fallback Engine.

Verifies:
- Prompt engineering calibration and JSON schema compliance.
- High-score breaking news evaluation parsing.
- Low-score chatter and advertisement spam filtering (is_spam=True, score<=2).
- Markdown code fence (```json ... ```) extraction and JSON repair.
- Score boundary clamping strictly within [1, 10].
- Multi-stage retry with exponential backoff on HTTP 429 quota exhaustion and network timeouts.
- Level-3 deterministic keyword-based heuristic fallback on total API exhaustion.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
import subprocess
from unittest.mock import MagicMock, patch

import httpx
import pytest

from tg_news_monitor.core.models import NewsEvaluation, TelegramPost
from tg_news_monitor.evaluator.codebuddy_client import CodeBuddyError, CodeBuddyEvaluator
from tg_news_monitor.evaluator.fallback import (
    BREAKING_KEYWORDS,
    MultiStageFallbackHandler,
    calculate_backoff_delay,
    extract_first_json_object,
    heuristic_keyword_fallback,
    normalize_and_validate_evaluation,
    parse_and_repair_evaluation,
    repair_json_string,
    strip_markdown_code_fences,
)
from tg_news_monitor.evaluator.grok_client import (
    GrokClient,
    GrokError,
    GrokNetworkError,
    GrokRateLimitError,
    GrokServerError,
    create_evaluator,
)
from tg_news_monitor.evaluator.prompt import (
    NEWS_EVALUATION_JSON_SCHEMA,
    SYSTEM_PROMPT,
    USER_PROMPT_TEMPLATE,
    build_system_prompt,
    build_user_prompt,
    get_news_evaluation_json_schema,
)
from tests.conftest import (
    GROK_EVAL_SCORE_2_SPAM,
    GROK_EVAL_SCORE_4_CHATTER,
    GROK_EVAL_SCORE_8,
    GROK_EVAL_SCORE_9,
    GROK_EVAL_SCORE_10_CRITICAL,
    make_openai_chat_completion,
)


@pytest.fixture
def sample_breaking_post() -> TelegramPost:
    """Sample breaking news post from whale_alert."""
    return TelegramPost(
        channel="whale_alert",
        message_id=101,
        published_at=datetime(2026, 9, 8, 21, 30, 0, tzinfo=timezone.utc),
        text="🚨 15,000 #ETH (45,120,300 USD) transferred from unknown wallet to #Binance\nhttps://etherscan.io/tx/0xmocked101",
        direct_url="https://t.me/whale_alert/101",
    )


@pytest.fixture
def sample_chatter_post() -> TelegramPost:
    """Sample routine chatter post."""
    return TelegramPost(
        channel="whale_alert",
        message_id=102,
        published_at=datetime(2026, 9, 8, 21, 31, 0, tzinfo=timezone.utc),
        text="Good morning crypto fam! What are your thoughts on BTC today? GM GM ☕ Let's discuss in the chat!",
        direct_url="https://t.me/whale_alert/102",
    )


@pytest.fixture
def sample_spam_post() -> TelegramPost:
    """Sample spam advertising post."""
    return TelegramPost(
        channel="whale_alert",
        message_id=104,
        published_at=datetime(2026, 9, 8, 21, 33, 0, tzinfo=timezone.utc),
        text="🔥 1000x MOON GEM PRESALE IS LIVE! Join VIP alpha signals group now: https://t.me/fake_pump_gem. Free 500 USDT airdrop for first 50 depositors! 🚀💰",
        direct_url="https://t.me/whale_alert/104",
    )


@pytest.fixture
def sample_exploit_post() -> TelegramPost:
    """Sample critical exploit post with breaking keywords."""
    return TelegramPost(
        channel="whale_alert",
        message_id=106,
        published_at=datetime(2026, 9, 8, 21, 35, 0, tzinfo=timezone.utc),
        text="🚨 CRITICAL EXPLOIT: Lending protocol AlphaPool drained for $18M due to price oracle reentrancy flaw. Withdraw funds immediately!",
        direct_url="https://t.me/whale_alert/106",
    )


# ==============================================================================
# 1. Prompt Engineering & JSON Schema Tests
# ==============================================================================

class TestPromptEngineering:
    """Verifies system prompt design, calibration instructions, and schema definitions."""

    def test_system_prompt_scoring_calibration(self) -> None:
        """System prompt must contain explicit 1-10 scoring tiers and calibration criteria."""
        prompt = build_system_prompt()
        assert "1-10" in prompt or "1-2 分" in prompt
        assert "【1-2 分 | 垃圾与闲聊】" in prompt
        assert "【3-4 分 | 弱价值动态】" in prompt
        assert "【5-6 分 | 一般行业快讯】" in prompt
        assert "【7-8 分 | 重大行业要闻】" in prompt
        assert "【9-10 分 | 顶级紧急突发】" in prompt

    def test_system_prompt_spam_filtering_rules(self) -> None:
        """System prompt must instruct LLM to flag spam and constrain score <= 2."""
        prompt = build_system_prompt()
        assert "is_spam: true" in prompt
        assert "商业广告" in prompt
        assert "返佣链接" in prompt
        assert "代币私募" in prompt or "Presale" in prompt

    def test_system_prompt_structured_chinese_briefing(self) -> None:
        """System prompt must require professional Simplified Chinese and structured fields."""
        prompt = build_system_prompt()
        assert "简体中文" in prompt
        assert "summary_bullets" in prompt
        assert "key_takeaways" in prompt
        assert "category" in prompt

    def test_user_prompt_formatting(self, sample_breaking_post: TelegramPost) -> None:
        """User prompt must correctly populate channel, message ID, timestamp, and body text."""
        user_prompt = build_user_prompt(sample_breaking_post)
        assert "@whale_alert" in user_prompt
        assert "#101" in user_prompt
        assert "15,000 #ETH" in user_prompt
        assert "2026-09-08" in user_prompt

    def test_news_evaluation_json_schema_structure(self) -> None:
        """Schema must define all required fields and valid constraints."""
        schema = get_news_evaluation_json_schema()
        assert schema["type"] == "object"
        properties = schema["properties"]
        assert "score" in properties
        assert properties["score"]["minimum"] == 1
        assert properties["score"]["maximum"] == 10
        assert "is_news" in properties
        assert "is_spam" in properties
        assert "title" in properties
        assert "summary_bullets" in properties
        assert "key_takeaways" in properties
        assert "category" in properties
        assert set(schema["required"]).issubset(set(properties.keys()))


# ==============================================================================
# 2. Response Parsing, Repair, Normalization & Clamping Tests
# ==============================================================================

class TestResponseParsingAndNormalization:
    """Verifies parsing of clean JSON, markdown-wrapped JSON, and edge-case repair."""

    def test_parse_high_score_breaking_news(self) -> None:
        """Parses high-score (9/10) breaking news evaluation into NewsEvaluation model."""
        raw_json = json.dumps(GROK_EVAL_SCORE_9, ensure_ascii=False)
        evaluation = parse_and_repair_evaluation(raw_json)

        assert isinstance(evaluation, NewsEvaluation)
        assert evaluation.score == 9
        assert evaluation.is_news is True
        assert evaluation.is_spam is False
        assert "以太坊" in evaluation.title
        assert len(evaluation.summary_bullets) >= 3
        assert len(evaluation.key_takeaways) >= 1
        assert evaluation.category == "突发安全"

    def test_parse_markdown_code_fence_json(self) -> None:
        """Extracts and parses JSON wrapped in ```json ... ``` markdown code fences."""
        inner_json = json.dumps(GROK_EVAL_SCORE_10_CRITICAL, ensure_ascii=False)
        markdown_text = f"```json\n{inner_json}\n```"

        evaluation = parse_and_repair_evaluation(markdown_text)
        assert isinstance(evaluation, NewsEvaluation)
        assert evaluation.score == 10
        assert evaluation.is_news is True
        assert evaluation.is_spam is False
        assert "AlphaPool" in evaluation.title

    def test_parse_markdown_code_fence_without_json_tag(self) -> None:
        """Extracts and parses JSON wrapped in generic ``` ... ``` code fences."""
        inner_json = json.dumps(GROK_EVAL_SCORE_8, ensure_ascii=False)
        markdown_text = f"```\n{inner_json}\n```"

        evaluation = parse_and_repair_evaluation(markdown_text)
        assert isinstance(evaluation, NewsEvaluation)
        assert evaluation.score == 8
        assert evaluation.is_news is True
        assert "ETF" in evaluation.title

    def test_filter_spam_and_advertisements(self) -> None:
        """Advertisements must result in is_spam=True, score<=2, and is_news=False."""
        raw_json = json.dumps(GROK_EVAL_SCORE_2_SPAM, ensure_ascii=False)
        evaluation = parse_and_repair_evaluation(raw_json)

        assert evaluation.is_spam is True
        assert evaluation.is_news is False
        assert evaluation.score <= 2
        assert evaluation.category == "推广广告"

    def test_spam_with_hallucinated_high_score_clamped_to_two(self) -> None:
        """If model hallucinates is_spam=True but score=8, score must be clamped <= 2."""
        spam_dict = {
            "score": 8,
            "is_spam": True,
            "is_news": True,
            "title": "加入VIP群享受万倍财富",
            "summary_bullets": ["广告推广"],
            "key_takeaways": ["无价值"],
            "category": "推广广告",
        }
        raw_json = json.dumps(spam_dict, ensure_ascii=False)
        evaluation = parse_and_repair_evaluation(raw_json)

        assert evaluation.is_spam is True
        assert evaluation.is_news is False
        assert evaluation.score <= 2

    def test_parse_routine_chatter(self) -> None:
        """Routine chatter (score 4) must be correctly classified and not marked spam."""
        raw_json = json.dumps(GROK_EVAL_SCORE_4_CHATTER, ensure_ascii=False)
        evaluation = parse_and_repair_evaluation(raw_json)

        assert evaluation.score == 4
        assert evaluation.is_spam is False
        assert evaluation.is_news is False  # Inferred as non-news chatter (< 7 or routine)
        assert evaluation.category == "日常闲聊"

    def test_score_boundary_clamping_upper(self) -> None:
        """Scores above 10 must be clamped to 10."""
        data = {
            "score": 15,
            "is_news": True,
            "is_spam": False,
            "title": "特大突发黑天鹅事件",
            "summary_bullets": ["受影响严重"],
            "key_takeaways": ["核心冲击"],
            "category": "突发安全",
        }
        evaluation = normalize_and_validate_evaluation(data)
        assert evaluation.score == 10

    def test_score_boundary_clamping_lower(self) -> None:
        """Scores below 1 must be clamped to 1."""
        data = {
            "score": -3,
            "is_news": False,
            "is_spam": False,
            "title": "无意义字符",
            "summary_bullets": ["闲聊"],
            "key_takeaways": ["无影响"],
            "category": "日常闲聊",
        }
        evaluation = normalize_and_validate_evaluation(data)
        assert evaluation.score == 1

    def test_regex_json_repair_trailing_commas(self) -> None:
        """Stage 2 repair removes trailing commas before closing braces/brackets."""
        malformed = """
        {
            "score": 7,
            "is_news": true,
            "is_spam": false,
            "category": "行业快讯",
            "title": "重大监管政策调整",
            "summary_bullets": [
                "要点一",
                "要点二",
            ],
            "key_takeaways": [
                "影响深远",
            ],
        }
        """
        evaluation = parse_and_repair_evaluation(malformed)
        assert evaluation.score == 7
        assert evaluation.title == "重大监管政策调整"
        assert len(evaluation.summary_bullets) == 2

    def test_regex_json_repair_python_literals(self) -> None:
        """Stage 2 repair converts Python True/False/None to valid JSON literals."""
        malformed = """
        {
            "score": 6,
            "is_news": True,
            "is_spam": False,
            "category": "行业快讯",
            "title": "某项目主网上线",
            "summary_bullets": ["主网上线成功"],
            "key_takeaways": ["生态起步"],
            "actionable_insight": None
        }
        """
        evaluation = parse_and_repair_evaluation(malformed)
        assert evaluation.score == 6
        assert evaluation.is_news is True
        assert evaluation.actionable_insight is None

    def test_surrounding_conversational_chatter_extraction(self) -> None:
        """Extracts JSON object when LLM returns surrounding conversational text."""
        llm_response = (
            "Here is the structured evaluation as requested:\n\n"
            '{"score": 8, "is_news": true, "is_spam": false, "category": "宏观监管", '
            '"title": "某国央行降息50个基点", "summary_bullets": ["降息落地"], "key_takeaways": ["利好流动性"]}\n\n'
            "Hope this helps your news monitoring service!"
        )
        evaluation = parse_and_repair_evaluation(llm_response)
        assert evaluation.score == 8
        assert "降息" in evaluation.title


# ==============================================================================
# 3. Deterministic Heuristic Fallback Tests (Stage 3)
# ==============================================================================

class TestHeuristicFallbackEngine:
    """Verifies deterministic Level-3 heuristic triage on total API exhaustion."""

    def test_heuristic_fallback_with_breaking_keyword_exploit(
        self, sample_exploit_post: TelegramPost
    ) -> None:
        """Post containing 'EXPLOIT' and 'drained' triggers provisional score 7 alert."""
        evaluation = heuristic_keyword_fallback(sample_exploit_post)

        assert evaluation.score == 7
        assert evaluation.is_news is True
        assert evaluation.is_spam is False
        assert evaluation.category == "突发安全"
        assert "[降级预警]" in evaluation.title
        assert any("AlphaPool" in b for b in evaluation.summary_bullets)
        assert "Grok API" in evaluation.summary_bullets[2]
        assert evaluation.actionable_insight is not None

    def test_heuristic_fallback_with_chinese_breaking_keyword(self) -> None:
        """Post containing '突发' or '暂停提现' triggers provisional score 7 alert."""
        post = TelegramPost(
            channel="binance_news",
            message_id=205,
            published_at=datetime.now(timezone.utc),
            text="突发：某中心化平台宣布因安全维护暂时暂停提现，团队正紧急排查中。",
            direct_url="https://t.me/binance_news/205",
        )
        evaluation = heuristic_keyword_fallback(post)

        assert evaluation.score == 7
        assert evaluation.is_news is True
        assert evaluation.category == "突发安全"
        assert "突发" in evaluation.title

    def test_heuristic_fallback_without_breaking_keywords(
        self, sample_chatter_post: TelegramPost
    ) -> None:
        """Post without breaking keywords receives score 3, is_news=False (no alert)."""
        evaluation = heuristic_keyword_fallback(sample_chatter_post)

        assert evaluation.score == 3
        assert evaluation.is_news is False
        assert evaluation.is_spam is False
        assert evaluation.category == "日常闲聊"
        assert "未匹配突发高优先级关键词" in evaluation.summary_bullets[0]

    def test_backoff_delay_calculation(self) -> None:
        """Exponential backoff produces strictly increasing baseline with jitter."""
        d0 = calculate_backoff_delay(0, base_delay=2.0, jitter_range=(0.0, 0.1))
        d1 = calculate_backoff_delay(1, base_delay=2.0, jitter_range=(0.0, 0.1))
        d2 = calculate_backoff_delay(2, base_delay=2.0, jitter_range=(0.0, 0.1))

        # 2*(2^0) = 2.0, 2*(2^1) = 4.0, 2*(2^2) = 8.0
        assert 2.0 <= d0 <= 2.1
        assert 4.0 <= d1 <= 4.1
        assert 8.0 <= d2 <= 8.1
        assert d0 < d1 < d2

    def test_multistage_fallback_handler_complete_flow(
        self, sample_exploit_post: TelegramPost
    ) -> None:
        """MultiStageFallbackHandler parses valid JSON, repairs bad JSON, falls back on garbage."""
        handler = MultiStageFallbackHandler(max_retries=3, base_delay=0.1)

        # 1. Valid JSON
        res1 = handler.handle_json_response(
            json.dumps(GROK_EVAL_SCORE_9, ensure_ascii=False), sample_exploit_post
        )
        assert res1.score == 9

        # 2. Garbage text with post -> triggers Stage 3 fallback
        res2 = handler.handle_json_response("This is not JSON at all! Random garbage error text", sample_exploit_post)
        assert res2.score == 7
        assert "[降级预警]" in res2.title

        # 3. Direct API exhaustion call
        res3 = handler.handle_api_exhaustion(sample_exploit_post)
        assert res3.score == 7


# ==============================================================================
# 4. GrokClient Integration & Fault-Injection Tests
# ==============================================================================

class TestGrokClient:
    """Verifies GrokClient HTTP communication, retries, and fallback behaviors."""

    def test_evaluate_post_success_200(self, sample_breaking_post: TelegramPost) -> None:
        """HTTP 200 with OpenAI-compatible response parses into valid NewsEvaluation."""
        mock_payload = make_openai_chat_completion(GROK_EVAL_SCORE_9)

        def mock_handler(request: httpx.Request) -> httpx.Response:
            assert request.url.path.endswith("/chat/completions")
            assert "Bearer test-key" in request.headers["Authorization"]
            body = json.loads(request.content.decode("utf-8"))
            assert body["model"] == "grok-beta"
            assert body["response_format"] == {"type": "json_object"}
            return httpx.Response(200, json=mock_payload)

        transport = httpx.MockTransport(mock_handler)
        client = httpx.Client(transport=transport)

        grok = GrokClient(
            api_key="test-key",
            api_base="https://api.x.ai/v1",
            model="grok-beta",
            http_client=client,
        )

        evaluation = grok.evaluate_post(sample_breaking_post)
        assert isinstance(evaluation, NewsEvaluation)
        assert evaluation.score == 9
        assert evaluation.is_news is True
        assert "以太坊" in evaluation.title

    def test_evaluate_post_markdown_wrapped_openai_response(
        self, sample_exploit_post: TelegramPost
    ) -> None:
        """Grok returning markdown code fences in message.content is parsed cleanly."""
        inner_json = json.dumps(GROK_EVAL_SCORE_10_CRITICAL, ensure_ascii=False)
        mock_payload = make_openai_chat_completion(f"```json\n{inner_json}\n```")

        transport = httpx.MockTransport(lambda req: httpx.Response(200, json=mock_payload))
        client = httpx.Client(transport=transport)

        grok = GrokClient(http_client=client)
        evaluation = grok.evaluate_post(sample_exploit_post)

        assert evaluation.score == 10
        assert "AlphaPool" in evaluation.title

    def test_evaluate_post_429_quota_exhaustion_triggers_level3_fallback(
        self, sample_exploit_post: TelegramPost
    ) -> None:
        """Repeated HTTP 429 quota errors trigger exponential retry, then Stage 3 heuristic fallback."""
        call_count = 0

        def mock_429_handler(request: httpx.Request) -> httpx.Response:
            nonlocal call_count
            call_count += 1
            return httpx.Response(429, json={"error": "Rate limit exceeded, quota exhausted"})

        transport = httpx.MockTransport(mock_429_handler)
        client = httpx.Client(transport=transport)

        # Set base_delay=0.001 to ensure unit test runs in milliseconds
        grok = GrokClient(
            max_retries=3,
            base_delay=0.001,
            http_client=client,
            fallback_on_exhaustion=True,
        )

        evaluation = grok.evaluate_post(sample_exploit_post)

        # Verified 3 retries executed
        assert call_count == 3
        # Verified Stage 3 heuristic fallback activated
        assert evaluation.score == 7
        assert evaluation.is_news is True
        assert "[降级预警]" in evaluation.title

    def test_evaluate_post_429_raises_when_fallback_disabled(
        self, sample_exploit_post: TelegramPost
    ) -> None:
        """When fallback_on_exhaustion=False, GrokRateLimitError is raised on 429."""
        transport = httpx.MockTransport(
            lambda req: httpx.Response(429, json={"error": "Too Many Requests"})
        )
        client = httpx.Client(transport=transport)

        grok = GrokClient(
            max_retries=2,
            base_delay=0.001,
            http_client=client,
            fallback_on_exhaustion=False,
        )

        with pytest.raises(GrokRateLimitError) as exc_info:
            grok.evaluate_post(sample_exploit_post)
        assert "429 Rate Limit" in str(exc_info.value)

    def test_evaluate_post_500_server_error_exhaustion_fallback(
        self, sample_exploit_post: TelegramPost
    ) -> None:
        """HTTP 500 server error across retries triggers Level-3 fallback."""
        call_count = 0

        def mock_500_handler(request: httpx.Request) -> httpx.Response:
            nonlocal call_count
            call_count += 1
            return httpx.Response(500, text="Internal Server Error")

        transport = httpx.MockTransport(mock_500_handler)
        client = httpx.Client(transport=transport)

        grok = GrokClient(
            max_retries=3,
            base_delay=0.001,
            http_client=client,
            fallback_on_exhaustion=True,
        )

        evaluation = grok.evaluate_post(sample_exploit_post)
        assert call_count == 3
        assert evaluation.score == 7

    def test_evaluate_post_network_timeout_exhaustion_fallback(
        self, sample_exploit_post: TelegramPost
    ) -> None:
        """Network timeout across retries triggers Level-3 fallback."""
        call_count = 0

        def mock_timeout_handler(request: httpx.Request) -> httpx.Response:
            nonlocal call_count
            call_count += 1
            raise httpx.ReadTimeout("Socket read timed out")

        transport = httpx.MockTransport(mock_timeout_handler)
        client = httpx.Client(transport=transport)

        grok = GrokClient(
            max_retries=3,
            base_delay=0.001,
            http_client=client,
            fallback_on_exhaustion=True,
        )

        evaluation = grok.evaluate_post(sample_exploit_post)
        assert call_count == 3
        assert evaluation.score == 7

    def test_evaluate_text_convenience_helper(self) -> None:
        """evaluate_text helper wraps raw text into TelegramPost and evaluates."""
        mock_payload = make_openai_chat_completion(GROK_EVAL_SCORE_8)
        transport = httpx.MockTransport(lambda req: httpx.Response(200, json=mock_payload))
        client = httpx.Client(transport=transport)

        grok = GrokClient(http_client=client)
        evaluation = grok.evaluate_text("监管机构批准首只ETF申请", channel="crypto_wire", message_id=50)

        assert evaluation.score == 8
        assert "ETF" in evaluation.title

    def test_evaluate_post_async_success(self, sample_breaking_post: TelegramPost) -> None:
        """Asynchronous evaluation path succeeds via httpx.AsyncClient."""
        mock_payload = make_openai_chat_completion(GROK_EVAL_SCORE_9)
        transport = httpx.MockTransport(lambda req: httpx.Response(200, json=mock_payload))
        async_client = httpx.AsyncClient(transport=transport)

        grok = GrokClient(async_http_client=async_client)

        async def _run() -> NewsEvaluation:
            return await grok.evaluate_post_async(sample_breaking_post)

        evaluation = asyncio.run(_run())

        assert evaluation.score == 9
        assert "以太坊" in evaluation.title

    def test_evaluate_post_async_429_fallback(
        self, sample_exploit_post: TelegramPost
    ) -> None:
        """Asynchronous path falls back to Stage 3 heuristic on 429 quota exhaustion."""
        transport = httpx.MockTransport(lambda req: httpx.Response(429, text="Rate Limited"))
        async_client = httpx.AsyncClient(transport=transport)

        grok = GrokClient(
            max_retries=2,
            base_delay=0.001,
            async_http_client=async_client,
            fallback_on_exhaustion=True,
        )

        async def _run() -> NewsEvaluation:
            return await grok.evaluate_post_async(sample_exploit_post)

        evaluation = asyncio.run(_run())

        assert evaluation.score == 7
        assert "[降级预警]" in evaluation.title


class TestCodeBuddyEvaluator:
    """CodeBuddy CLI path: mocked subprocess only; never call a real CLI."""

    def test_create_evaluator_codebuddy(self):
        evaluator = create_evaluator(
            provider="codebuddy",
            api_key="cb-factory-test",
        )
        assert isinstance(evaluator, CodeBuddyEvaluator)
        assert evaluator.provider == "codebuddy"
        assert evaluator.model == "fast-model"
        assert evaluator.fallback_model == "hy3"
        assert evaluator.api_key == "cb-factory-test"

    def test_create_evaluator_ignores_deepseek_provider(self):
        evaluator = create_evaluator(provider="deepseek", api_key="cb-x", http_client=object())
        assert isinstance(evaluator, CodeBuddyEvaluator)
        assert evaluator.provider == "codebuddy"

    def _cli(self, stdout: str = "", returncode: int = 0, stderr: str = "") -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            args=["codebuddy"], returncode=returncode, stdout=stdout, stderr=stderr
        )

    def test_evaluate_post_primary_success(self, sample_breaking_post: TelegramPost) -> None:
        calls: List[List[str]] = []
        captured_env: dict[str, str] = {}

        def fake_run(cmd, **kwargs):
            calls.append(list(cmd))
            captured_env.update(kwargs.get("env") or {})
            return self._cli(json.dumps(GROK_EVAL_SCORE_9, ensure_ascii=False))

        ev = CodeBuddyEvaluator(api_key="cb-key", run_cli=fake_run)
        evaluation = ev.evaluate_post(sample_breaking_post)

        assert evaluation.score == 9
        assert "以太坊" in evaluation.title
        assert len(calls) == 1
        cmd = calls[0]
        assert cmd[0] == "codebuddy"
        assert "-p" in cmd and "-y" in cmd
        assert cmd[cmd.index("--tools") + 1] == ""
        assert cmd[cmd.index("--output-format") + 1] == "text"
        assert cmd[cmd.index("--model") + 1] == "fast-model"
        assert cmd[cmd.index("--effort") + 1] == "minimal"
        assert captured_env.get("CODEBUDDY_API_KEY") == "cb-key"
        assert "CODEBUDDY_INTERNET_ENVIRONMENT" not in captured_env
        assert "15,000 #ETH" in cmd[-1]

    def test_child_env_strips_internet_flag(
        self, sample_breaking_post: TelegramPost, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("CODEBUDDY_INTERNET_ENVIRONMENT", "2")
        captured_env: dict[str, str] = {}

        def fake_run(cmd, **kwargs):
            captured_env.update(kwargs.get("env") or {})
            return self._cli(json.dumps(GROK_EVAL_SCORE_9, ensure_ascii=False))

        ev = CodeBuddyEvaluator(api_key="cb-key", run_cli=fake_run)
        ev.evaluate_post(sample_breaking_post)
        assert captured_env.get("CODEBUDDY_API_KEY") == "cb-key"
        assert "CODEBUDDY_INTERNET_ENVIRONMENT" not in captured_env

    def test_primary_fail_fallback_success(self, sample_breaking_post: TelegramPost) -> None:
        models: List[str] = []

        def fake_run(cmd, **kwargs):
            model = cmd[cmd.index("--model") + 1]
            models.append(model)
            if model == "fast-model":
                return self._cli(returncode=1, stderr="primary boom")
            return self._cli(json.dumps(GROK_EVAL_SCORE_8, ensure_ascii=False))

        ev = CodeBuddyEvaluator(api_key="cb-key", run_cli=fake_run)
        evaluation = ev.evaluate_post(sample_breaking_post)

        assert models == ["fast-model", "hy3"]
        assert evaluation.score == 8
        assert "ETF" in evaluation.title

    def test_unparseable_primary_then_fallback(self, sample_exploit_post: TelegramPost) -> None:
        models: List[str] = []

        def fake_run(cmd, **kwargs):
            model = cmd[cmd.index("--model") + 1]
            models.append(model)
            if model == "fast-model":
                return self._cli("this is not json at all")
            inner = json.dumps(GROK_EVAL_SCORE_10_CRITICAL, ensure_ascii=False)
            return self._cli(f"```json\n{inner}\n```")

        ev = CodeBuddyEvaluator(api_key="cb-key", run_cli=fake_run)
        evaluation = ev.evaluate_post(sample_exploit_post)
        assert models == ["fast-model", "hy3"]
        assert evaluation.score == 10

    def test_timeout_then_fallback(self, sample_breaking_post: TelegramPost) -> None:
        models: List[str] = []

        def fake_run(cmd, **kwargs):
            model = cmd[cmd.index("--model") + 1]
            models.append(model)
            if model == "fast-model":
                raise subprocess.TimeoutExpired(cmd, kwargs.get("timeout") or 60)
            return self._cli(json.dumps(GROK_EVAL_SCORE_9, ensure_ascii=False))

        ev = CodeBuddyEvaluator(api_key="cb-key", run_cli=fake_run)
        evaluation = ev.evaluate_post(sample_breaking_post)
        assert models == ["fast-model", "hy3"]
        assert evaluation.score == 9

    def test_both_fail_heuristic(self, sample_exploit_post: TelegramPost) -> None:
        models: List[str] = []

        def fake_run(cmd, **kwargs):
            models.append(cmd[cmd.index("--model") + 1])
            return self._cli(returncode=1, stderr="fail")

        ev = CodeBuddyEvaluator(api_key="cb-key", run_cli=fake_run)
        evaluation = ev.evaluate_post(sample_exploit_post)
        assert models == ["fast-model", "hy3"]
        assert evaluation.score == 7
        assert "[降级预警]" in evaluation.title

    def test_empty_output_counts_as_failure(self, sample_exploit_post: TelegramPost) -> None:
        def fake_run(cmd, **kwargs):
            return self._cli(stdout="   ")

        ev = CodeBuddyEvaluator(api_key="cb-key", run_cli=fake_run)
        evaluation = ev.evaluate_post(sample_exploit_post)
        assert evaluation.score == 7

    def test_evaluate_digest_success(self) -> None:
        from datetime import datetime, timezone

        from tg_news_monitor.core.models import TelegramPost

        post = TelegramPost(
            channel="wire",
            message_id=1,
            text="Important current economic news.",
            direct_url="https://t.me/wire/1",
            published_at=datetime.now(timezone.utc),
        )
        digest_json = {
            "headline": "本轮快讯",
            "overview": "测试",
            "has_material_news": True,
            "filtered_note": "",
            "items": [
                {
                    "rank": 1,
                    "channel": "wire",
                    "message_id": 1,
                    "title": "经济要闻",
                    "summary": "重要经济新闻落地。",
                    "category": "宏观财经",
                    "score": 8,
                    "event_at": None,
                    "is_update": False,
                    "summary_bullets": ["重要经济新闻落地。"],
                    "actionable_insight": "观察后续",
                    "bias_overall": "中性",
                    "bias_us": "中性",
                    "bias_cn": "中性",
                    "bias_commodities": "中性",
                    "impact_overall": "无直接影响",
                    "impact_us": "无直接影响",
                    "impact_cn": "无直接影响",
                    "impact_commodities": "无直接影响",
                }
            ],
        }

        def fake_run(cmd, **kwargs):
            return self._cli(json.dumps(digest_json, ensure_ascii=False))

        ev = CodeBuddyEvaluator(api_key="cb-key", run_cli=fake_run)
        brief = ev.evaluate_digest([post])
        assert brief.has_material_news is True
        assert brief.items[0].title == "经济要闻"
        assert "is_update=true" in ev._compose_digest_prompt([post])

    def test_evaluate_digest_both_fail_raises(self) -> None:
        from datetime import datetime, timezone

        post = TelegramPost(
            channel="wire",
            message_id=1,
            text="Important current economic news.",
            direct_url="https://t.me/wire/1",
            published_at=datetime.now(timezone.utc),
        )

        def fake_run(cmd, **kwargs):
            return self._cli(stdout="bad JSON")

        ev = CodeBuddyEvaluator(api_key="cb-key", run_cli=fake_run)
        with pytest.raises(CodeBuddyError):
            ev.evaluate_digest([post])


