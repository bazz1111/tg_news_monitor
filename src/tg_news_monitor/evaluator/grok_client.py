"""OpenAI-compatible Grok client connecting to https://api.x.ai/v1."""

from __future__ import annotations

import json
import logging
import random
import time
from typing import Any, Callable, Dict, List, Optional, Union

import httpx

from tg_news_monitor.core.models import DigestBrief, NewsEvaluation, TelegramPost
from tg_news_monitor.evaluator.fallback import (
    MultiStageFallbackHandler,
    calculate_backoff_delay,
    heuristic_keyword_fallback,
    parse_and_repair_evaluation,
    strip_markdown_code_fences,
)
from tg_news_monitor.evaluator.prompt import (
    build_digest_system_prompt,
    build_digest_user_prompt,
    build_system_prompt,
    build_user_prompt,
)

logger = logging.getLogger(__name__)

# Check for tenacity availability; provide compatible implementation if missing
try:
    import tenacity
    from tenacity import (
        RetryError,
        retry,
        retry_if_exception_type,
        stop_after_attempt,
        wait_exponential,
    )

    HAS_TENACITY = True
except ImportError:
    HAS_TENACITY = False

    class RetryError(Exception):  # type: ignore[no-redef]
        """Compatibility RetryError raised when retries are exhausted."""

        def __init__(self, cause: Exception) -> None:
            self.cause = cause
            super().__init__(f"Retry exhausted: {cause}")


class GrokError(Exception):
    """Base exception for Grok client failures."""

    pass


class GrokRateLimitError(GrokError):
    """Raised when Grok API returns HTTP 429 Too Many Requests."""

    def __init__(self, message: str = "Grok API rate limit (429) exceeded", retry_after: Optional[float] = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class GrokServerError(GrokError):
    """Raised when Grok API returns HTTP 5xx Server Error."""

    def __init__(self, status_code: int, message: str = "Grok API server error") -> None:
        super().__init__(f"{message} (HTTP {status_code})")
        self.status_code = status_code


class GrokNetworkError(GrokError):
    """Raised on socket, connection, or request timeout errors."""

    pass


class GrokClient:
    """OpenAI-compatible Grok client connecting to https://api.x.ai/v1.

    Features:
    - OpenAI Chat Completions compatibility (model, temperature, json_object response_format).
    - Multi-stage retry with exponential backoff on HTTP 429 and network timeouts.
    - Markdown code fence removal and resilient JSON repair.
    - Graceful fallback to Level-3 deterministic keyword triage upon quota exhaustion.
    """

    def __init__(
        self,
        api_key: str = "test-api-key",
        api_base: str = "https://api.deepseek.com",
        model: str = "deepseek-chat",
        temperature: float = 0.2,
        timeout: float = 30.0,
        max_retries: int = 3,
        base_delay: float = 2.0,
        fallback_on_exhaustion: bool = True,
        http_client: Optional[httpx.Client] = None,
        async_http_client: Optional[httpx.AsyncClient] = None,
        provider: str = "deepseek",
    ) -> None:
        self.provider = provider
        self.api_key = api_key
        self.api_base = api_base.rstrip("/")
        self.model = model
        self.temperature = temperature
        self.timeout = timeout
        self.max_retries = max_retries
        self.base_delay = base_delay
        self.fallback_on_exhaustion = fallback_on_exhaustion

        self._external_client = http_client
        self._external_async_client = async_http_client
        self.fallback_handler = MultiStageFallbackHandler(
            max_retries=max_retries, base_delay=base_delay
        )

    def _get_client(self) -> httpx.Client:
        """Returns the active HTTP client."""
        if self._external_client is not None:
            return self._external_client
        return httpx.Client(timeout=self.timeout)

    def _get_async_client(self) -> httpx.AsyncClient:
        """Returns the active async HTTP client."""
        if self._external_async_client is not None:
            return self._external_async_client
        return httpx.AsyncClient(timeout=self.timeout)

    def _build_request_payload(self, post: TelegramPost) -> Dict[str, Any]:
        """Constructs the OpenAI-compatible chat completion payload."""
        return {
            "model": self.model,
            "temperature": self.temperature,
            "response_format": {"type": "json_object"},
            # DeepSeek non-thinking mode (ignored by providers that do not support it)
            "thinking": {"type": "disabled"},
            "messages": [
                {"role": "system", "content": build_system_prompt()},
                {"role": "user", "content": build_user_prompt(post)},
            ],
        }

    def _get_headers(self) -> Dict[str, str]:
        """Returns standard headers for OpenAI-compatible API."""
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "User-Agent": "tg-news-monitor/0.1.0",
        }

    def _execute_http_request(self, payload: Dict[str, Any]) -> str:
        """Executes a single synchronous HTTP POST request to Grok API."""
        url = f"{self.api_base}/chat/completions"
        headers = self._get_headers()
        client = self._get_client()

        try:
            response = client.post(url, headers=headers, json=payload)
        except (httpx.TimeoutException, httpx.ConnectTimeout, httpx.ReadTimeout) as err:
            raise GrokNetworkError(f"Request timeout communicating with Grok API: {err}") from err
        except httpx.RequestError as err:
            raise GrokNetworkError(f"Network error communicating with Grok API: {err}") from err

        if response.status_code == 429:
            retry_after_str = response.headers.get("Retry-After")
            retry_after = float(retry_after_str) if retry_after_str and retry_after_str.isdigit() else None
            raise GrokRateLimitError(
                f"Grok API 429 Rate Limit: {response.text}",
                retry_after=retry_after,
            )

        if response.status_code >= 500:
            raise GrokServerError(response.status_code, response.text)

        if response.status_code != 200:
            raise GrokError(
                f"Grok API error HTTP {response.status_code}: {response.text}"
            )

        try:
            data = response.json()
            return self._extract_content_from_openai_payload(data)
        except Exception as e:
            # If JSON parsing of wrapper payload fails, return raw text
            if "choices" in response.text:
                raise
            return response.text

    async def _execute_http_request_async(self, payload: Dict[str, Any]) -> str:
        """Executes a single asynchronous HTTP POST request to Grok API."""
        url = f"{self.api_base}/chat/completions"
        headers = self._get_headers()
        client = self._get_async_client()

        try:
            response = await client.post(url, headers=headers, json=payload)
        except (httpx.TimeoutException, httpx.ConnectTimeout, httpx.ReadTimeout) as err:
            raise GrokNetworkError(f"Request timeout communicating with Grok API: {err}") from err
        except httpx.RequestError as err:
            raise GrokNetworkError(f"Network error communicating with Grok API: {err}") from err

        if response.status_code == 429:
            retry_after_str = response.headers.get("Retry-After")
            retry_after = float(retry_after_str) if retry_after_str and retry_after_str.isdigit() else None
            raise GrokRateLimitError(
                f"Grok API 429 Rate Limit: {response.text}",
                retry_after=retry_after,
            )

        if response.status_code >= 500:
            raise GrokServerError(response.status_code, response.text)

        if response.status_code != 200:
            raise GrokError(
                f"Grok API error HTTP {response.status_code}: {response.text}"
            )

        try:
            data = response.json()
            return self._extract_content_from_openai_payload(data)
        except Exception:
            return response.text

    @staticmethod
    def _extract_content_from_openai_payload(data: Dict[str, Any]) -> str:
        """Extracts assistant message content string from OpenAI chat completions payload."""
        if not isinstance(data, dict):
            return str(data)

        choices = data.get("choices")
        if choices and isinstance(choices, list) and len(choices) > 0:
            first_choice = choices[0]
            if isinstance(first_choice, dict):
                message = first_choice.get("message")
                if isinstance(message, dict):
                    content = message.get("content")
                    if content is not None:
                        return str(content)
        # Fallback if dictionary directly holds evaluation
        return json.dumps(data, ensure_ascii=False)

    def evaluate_post(self, post: TelegramPost) -> NewsEvaluation:
        """Evaluates a Telegram post using Grok API with multi-stage fallback.
        
        1. Stage 1: Retries on 429 rate-limit and network timeouts with exponential backoff.
        2. Stage 2: Markdown fence stripping and regex JSON repair.
        3. Stage 3: Deterministic keyword-based heuristic fallback if retries exhaust.
        """
        payload = self._build_request_payload(post)
        last_error: Optional[Exception] = None

        for attempt in range(self.max_retries):
            try:
                raw_response = self._execute_http_request(payload)
                # Parse & repair JSON response
                return parse_and_repair_evaluation(
                    raw_response, fallback_title=f"来自 @{post.channel} 的快讯"
                )
            except (GrokRateLimitError, GrokServerError, GrokNetworkError) as err:
                last_error = err
                logger.warning(
                    "Grok API call attempt %d/%d failed: %s. Channel: @%s/#%s",
                    attempt + 1,
                    self.max_retries,
                    err,
                    post.channel,
                    post.message_id,
                )
                if attempt < self.max_retries - 1:
                    if isinstance(err, GrokRateLimitError) and err.retry_after is not None:
                        delay = err.retry_after
                    else:
                        delay = calculate_backoff_delay(attempt, base_delay=self.base_delay)
                    time.sleep(delay)
            except Exception as err:
                # Stage 2 parsing error or unexpected error
                last_error = err
                logger.warning(
                    "Grok parsing / response error on attempt %d: %s",
                    attempt + 1,
                    err,
                )
                break

        # Retries exhausted or unrecoverable error encountered
        if self.fallback_on_exhaustion:
            logger.info(
                "Grok API retries exhausted for @%s/#%s. Triggering Level-3 heuristic fallback.",
                post.channel,
                post.message_id,
            )
            return heuristic_keyword_fallback(post)

        if last_error:
            raise last_error
        raise GrokError("Grok evaluation failed without specific exception.")

    async def evaluate_post_async(self, post: TelegramPost) -> NewsEvaluation:
        """Asynchronously evaluates a Telegram post with retry and fallback."""
        import asyncio

        payload = self._build_request_payload(post)
        last_error: Optional[Exception] = None

        for attempt in range(self.max_retries):
            try:
                raw_response = await self._execute_http_request_async(payload)
                return parse_and_repair_evaluation(
                    raw_response, fallback_title=f"来自 @{post.channel} 的快讯"
                )
            except (GrokRateLimitError, GrokServerError, GrokNetworkError) as err:
                last_error = err
                logger.warning(
                    "Grok API async call attempt %d/%d failed: %s",
                    attempt + 1,
                    self.max_retries,
                    err,
                )
                if attempt < self.max_retries - 1:
                    if isinstance(err, GrokRateLimitError) and err.retry_after is not None:
                        delay = err.retry_after
                    else:
                        delay = calculate_backoff_delay(attempt, base_delay=self.base_delay)
                    await asyncio.sleep(delay)
            except Exception as err:
                last_error = err
                break

        if self.fallback_on_exhaustion:
            return heuristic_keyword_fallback(post)

        if last_error:
            raise last_error
        raise GrokError("Grok evaluation failed.")


    def _build_digest_request_payload(self, posts: List[TelegramPost]) -> Dict[str, Any]:
        """Constructs chat completion payload for batch digest evaluation."""
        return {
            "model": self.model,
            "temperature": self.temperature,
            "response_format": {"type": "json_object"},
            # DeepSeek non-thinking mode (ignored by providers that do not support it)
            "thinking": {"type": "disabled"},
            "messages": [
                {"role": "system", "content": build_digest_system_prompt()},
                {"role": "user", "content": build_digest_user_prompt(posts)},
            ],
        }

    def _parse_digest_response(self, raw_response: str) -> DigestBrief:
        """Parse LLM JSON into DigestBrief with light fence stripping."""
        cleaned = strip_markdown_code_fences(raw_response)
        data = json.loads(cleaned)
        if not isinstance(data, dict):
            raise ValueError("Digest response is not a JSON object")
        # Clamp items length defensively
        items = data.get("items") or []
        if isinstance(items, list) and len(items) > 5:
            data["items"] = items[:5]
        brief = DigestBrief.model_validate(data)
        if not brief.items:
            brief.has_material_news = False
        return brief

    def evaluate_digest(self, posts: List[TelegramPost]) -> DigestBrief:
        """Evaluate a batch of posts into one DigestBrief (single LLM call).

        Uses the same retry/backoff pattern as evaluate_post. On parse failure
        returns an empty DigestBrief with has_material_news=False.
        Timeout for this call is at least 60 seconds.
        """
        if not posts:
            return DigestBrief(
                headline="本轮快讯",
                overview="本轮无新帖",
                items=[],
                has_material_news=False,
                filtered_note="empty_batch",
            )

        payload = self._build_digest_request_payload(posts)
        last_error: Optional[Exception] = None
        digest_timeout = max(60.0, float(self.timeout or 30.0))

        for attempt in range(self.max_retries):
            try:
                # Prefer a longer timeout for batch digests without permanently
                # mutating shared external clients when possible.
                url = f"{self.api_base}/chat/completions"
                headers = self._get_headers()
                if self._external_client is not None:
                    response = self._external_client.post(
                        url, headers=headers, json=payload, timeout=digest_timeout
                    )
                    # Reuse status handling from _execute_http_request path
                    if response.status_code == 429:
                        retry_after_str = response.headers.get("Retry-After")
                        retry_after = (
                            float(retry_after_str)
                            if retry_after_str and retry_after_str.isdigit()
                            else None
                        )
                        raise GrokRateLimitError(
                            f"Grok API 429 Rate Limit: {response.text}",
                            retry_after=retry_after,
                        )
                    if response.status_code >= 500:
                        raise GrokServerError(response.status_code, response.text)
                    if response.status_code != 200:
                        raise GrokError(
                            f"Grok API error HTTP {response.status_code}: {response.text}"
                        )
                    try:
                        data = response.json()
                        raw_response = self._extract_content_from_openai_payload(data)
                    except Exception:
                        raw_response = response.text
                else:
                    with httpx.Client(timeout=digest_timeout) as client:
                        try:
                            response = client.post(url, headers=headers, json=payload)
                        except (httpx.TimeoutException, httpx.ConnectTimeout, httpx.ReadTimeout) as err:
                            raise GrokNetworkError(
                                f"Request timeout communicating with Grok API: {err}"
                            ) from err
                        except httpx.RequestError as err:
                            raise GrokNetworkError(
                                f"Network error communicating with Grok API: {err}"
                            ) from err

                        if response.status_code == 429:
                            retry_after_str = response.headers.get("Retry-After")
                            retry_after = (
                                float(retry_after_str)
                                if retry_after_str and retry_after_str.isdigit()
                                else None
                            )
                            raise GrokRateLimitError(
                                f"Grok API 429 Rate Limit: {response.text}",
                                retry_after=retry_after,
                            )
                        if response.status_code >= 500:
                            raise GrokServerError(response.status_code, response.text)
                        if response.status_code != 200:
                            raise GrokError(
                                f"Grok API error HTTP {response.status_code}: {response.text}"
                            )
                        try:
                            data = response.json()
                            raw_response = self._extract_content_from_openai_payload(data)
                        except Exception:
                            raw_response = response.text

                return self._parse_digest_response(raw_response)
            except (GrokRateLimitError, GrokServerError, GrokNetworkError) as err:
                last_error = err
                logger.warning(
                    "Digest API call attempt %d/%d failed: %s (posts=%d)",
                    attempt + 1,
                    self.max_retries,
                    err,
                    len(posts),
                )
                if attempt < self.max_retries - 1:
                    if isinstance(err, GrokRateLimitError) and err.retry_after is not None:
                        delay = err.retry_after
                    else:
                        delay = calculate_backoff_delay(attempt, base_delay=self.base_delay)
                    time.sleep(delay)
            except Exception as err:
                last_error = err
                logger.warning(
                    "Digest parse/response error on attempt %d: %s",
                    attempt + 1,
                    err,
                )
                break

        err_msg = str(last_error) if last_error else "unknown digest failure"
        logger.error("Digest evaluation failed after retries: %s", err_msg)
        return DigestBrief(
            headline="本轮快讯",
            overview="模型解析失败",
            items=[],
            has_material_news=False,
            filtered_note=err_msg,
        )

    def evaluate_text(
        self,
        text: str,
        channel: str = "news_channel",
        message_id: int = 1,
        direct_url: Optional[str] = None,
    ) -> NewsEvaluation:
        """Convenience method to evaluate raw text without pre-constructing a TelegramPost."""
        from datetime import datetime, timezone

        post = TelegramPost(
            channel=channel,
            message_id=message_id,
            published_at=datetime.now(timezone.utc),
            text=text,
            direct_url=direct_url or f"https://t.me/{channel}/{message_id}",
        )
        return self.evaluate_post(post)


# Compatibility aliases for multi-provider support
LLMEvaluatorClient = GrokClient
DeepSeekClient = GrokClient
LLMError = GrokError
LLMRateLimitError = GrokRateLimitError
LLMServerError = GrokServerError
LLMNetworkError = GrokNetworkError


def create_evaluator(
    provider: str = "deepseek",
    api_key: str = "",
    api_base: Optional[str] = None,
    model: Optional[str] = None,
    **kwargs: Any,
) -> GrokClient:
    """Factory function creating an evaluator client for DeepSeek (or OpenAI-compatible API)."""
    normalized_provider = (provider or "deepseek").strip().lower()
    base = api_base or "https://api.deepseek.com"
    default_model = model or "deepseek-chat"
    provider_name = normalized_provider if normalized_provider != "auto" else "deepseek"

    return GrokClient(
        provider=provider_name,
        api_key=api_key,
        api_base=base,
        model=default_model,
        **kwargs,
    )

