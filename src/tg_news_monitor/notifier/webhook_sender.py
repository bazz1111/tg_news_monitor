"""Feishu (Lark) Custom Bot Webhook Sender with Rate-Limit Resilience.

Dispatches interactive card payloads to Feishu webhook endpoints:
- Supports optional HMAC-SHA256 signature verification (timestamp + secret).
- Injects signature in both JSON payload body ("sign", "timestamp") and HTTP headers (X-Lark-Signature, X-Lark-Timestamp).
- Rate-limit resilience: handles HTTP 429 and Feishu response code 19001 (frequency limit exceeded) with exponential backoff and jitter.
- Detailed logging with loguru / standard logging and success verification (code == 0).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import random
import time
from typing import Any, Dict, Optional, Tuple, Union

import httpx

try:
    from loguru import logger
except ImportError:
    import logging
    logger = logging.getLogger(__name__)  # type: ignore

from tg_news_monitor.core.models import NewsEvaluation, TelegramPost
from tg_news_monitor.notifier.feishu_card import FeishuCardBuilder


def generate_signature(timestamp: Union[int, str], secret: str) -> str:
    """Computes HMAC-SHA256 signature for Feishu webhook verification.

    Feishu signature algorithm:
    1. Concatenate timestamp and secret with newline: f"{timestamp}\\n{secret}"
    2. Compute HMAC-SHA256 digest using the concatenated string as key
    3. Base64 encode the resulting digest
    """
    if not secret:
        return ""

    string_to_sign = f"{timestamp}\n{secret}"
    hmac_code = hmac.new(
        string_to_sign.encode("utf-8"),
        digestmod=hashlib.sha256,
    ).digest()
    return base64.b64encode(hmac_code).decode("utf-8")


class FeishuWebhookSender:
    """Dispatches Interactive Cards to Feishu Custom Bot Webhook endpoints."""

    def __init__(
        self,
        webhook_url: Optional[str] = None,
        secret: Optional[str] = None,
        max_retries: int = 3,
        base_delay: float = 2.0,
        max_delay: float = 30.0,
        backoff_factor: float = 2.0,
        timeout: float = 10.0,
        http_client: Optional[httpx.Client] = None,
    ) -> None:
        """Initializes the Feishu webhook sender.

        Args:
            webhook_url: Feishu custom bot webhook endpoint URL.
            secret: Optional bot signing secret for HMAC-SHA256 authentication.
            max_retries: Maximum delivery retry attempts upon rate limit or 5xx.
            base_delay: Initial retry backoff delay in seconds.
            max_delay: Cap on exponential backoff delay.
            backoff_factor: Multiplier for exponential backoff calculation.
            timeout: HTTP request timeout in seconds.
            http_client: Optional injected httpx.Client (e.g. for mock transports in tests).
        """
        self.webhook_url = webhook_url or os.environ.get("FEISHU_WEBHOOK_URL", "")
        self.secret = secret if secret is not None else os.environ.get("FEISHU_SECRET", "")
        self.max_retries = max_retries
        self.base_delay = base_delay
        self.max_delay = max_delay
        self.backoff_factor = backoff_factor
        self.timeout = timeout
        self._external_client = http_client

    @staticmethod
    def generate_signature(timestamp: Union[int, str], secret: str) -> str:
        """Static alias for generate_signature."""
        return generate_signature(timestamp, secret)

    def prepare_request(
        self,
        payload: Dict[str, Any],
        secret: Optional[str] = None,
    ) -> Tuple[Dict[str, str], Dict[str, Any]]:
        """Prepares request headers and body payload with optional HMAC signature.

        Returns:
            Tuple of (headers_dict, prepared_payload_dict)
        """
        active_secret = secret if secret is not None else self.secret
        out_payload = dict(payload)
        headers = {
            "Content-Type": "application/json; charset=utf-8",
            "User-Agent": "tg_news_monitor/notifier (FeishuWebhookSender)",
        }

        if active_secret:
            timestamp = int(time.time())
            sign = generate_signature(timestamp, active_secret)

            # Include in JSON payload
            out_payload["timestamp"] = str(timestamp)
            out_payload["sign"] = sign

            # Also include in HTTP headers for platform verification compatibility
            headers["X-Lark-Signature"] = sign
            headers["X-Lark-Timestamp"] = str(timestamp)
            headers["X-Feishu-Signature"] = sign
            headers["X-Feishu-Timestamp"] = str(timestamp)

        return headers, out_payload

    def calculate_backoff(
        self,
        attempt: int,
        retry_after: Optional[str] = None,
    ) -> float:
        """Calculates exponential backoff delay with jitter.

        Args:
            attempt: 1-based attempt index (1, 2, ...).
            retry_after: Value of HTTP Retry-After header if present.
        """
        if retry_after and retry_after.strip().isdigit():
            return max(0.1, float(retry_after) + random.uniform(0.1, 0.5))

        delay = self.base_delay * (self.backoff_factor ** (attempt - 1))
        delay += random.uniform(0.05, 0.25)
        return max(0.05, min(self.max_delay, delay))

    def send(
        self,
        payload: Dict[str, Any],
        webhook_url: Optional[str] = None,
        secret: Optional[str] = None,
    ) -> bool:
        """Sends payload to Feishu bot webhook with rate-limit and 5xx retries.

        Args:
            payload: Interactive card or message payload dict.
            webhook_url: Optional destination override.
            secret: Optional HMAC secret override.

        Returns:
            True if delivery succeeded (code == 0 or StatusCode == 0), False otherwise.
        """
        target_url = webhook_url or self.webhook_url
        if not target_url:
            logger.error("Feishu webhook URL is not configured; cannot send alert.")
            return False

        headers, prepared_payload = self.prepare_request(payload, secret=secret)
        last_error_reason = "unknown"

        for attempt in range(1, self.max_retries + 1):
            if self._external_client is not None:
                client = self._external_client
                should_close = False
            else:
                client = httpx.Client(timeout=self.timeout)
                should_close = True

            try:
                logger.debug(
                    f"Posting to Feishu webhook (attempt {attempt}/{self.max_retries})"
                )
                response = client.post(
                    target_url,
                    headers=headers,
                    content=json.dumps(prepared_payload, ensure_ascii=False).encode("utf-8"),
                )

                # Parse JSON response body if available
                try:
                    resp_json = response.json()
                except Exception:
                    resp_json = {}

                # Check for Feishu Success (HTTP 200 and code == 0 or StatusCode == 0)
                if response.status_code == 200:
                    code = resp_json.get("code")
                    status_code = resp_json.get("StatusCode")
                    
                    if code == 0 or (code is None and status_code == 0):
                        logger.info("Successfully delivered card alert to Feishu webhook.")
                        return True

                    # Handle Feishu Application-Level Rate Limiting: code 19001
                    if code == 19001:
                        last_error_reason = f"Feishu rate limit exceeded (code 19001: {resp_json.get('msg')})"
                        logger.warning(
                            f"Feishu rate limit encountered (attempt {attempt}/{self.max_retries}): {last_error_reason}. Backing off..."
                        )
                        if attempt < self.max_retries:
                            backoff = self.calculate_backoff(attempt)
                            time.sleep(backoff)
                            continue
                        else:
                            logger.error(f"Feishu rate limit retries exhausted on code 19001: {last_error_reason}")
                            return False

                    # Non-retryable Feishu application errors (e.g. 9499 bad request, 19002 param error)
                    last_error_reason = f"Feishu rejected payload: code={code}, msg={resp_json.get('msg')}"
                    logger.error(
                        f"Non-retryable Feishu error: {last_error_reason}. Aborting retries."
                    )
                    return False

                # Handle HTTP 429 Too Many Requests
                elif response.status_code == 429:
                    retry_after = response.headers.get("Retry-After")
                    last_error_reason = f"HTTP 429 Too Many Requests (Retry-After: {retry_after})"
                    logger.warning(
                        f"HTTP 429 received from Feishu (attempt {attempt}/{self.max_retries}). Backing off..."
                    )
                    if attempt < self.max_retries:
                        backoff = self.calculate_backoff(attempt, retry_after=retry_after)
                        time.sleep(backoff)
                        continue
                    else:
                        logger.error(f"HTTP 429 retries exhausted: {last_error_reason}")
                        return False

                # Handle HTTP 5xx Server Errors
                elif 500 <= response.status_code < 600:
                    last_error_reason = f"HTTP {response.status_code} server error"
                    logger.warning(
                        f"Server error {response.status_code} from Feishu (attempt {attempt}/{self.max_retries}). Retrying..."
                    )
                    if attempt < self.max_retries:
                        backoff = self.calculate_backoff(attempt)
                        time.sleep(backoff)
                        continue
                    else:
                        logger.error(f"Server error retries exhausted: {last_error_reason}")
                        return False

                # Non-retryable HTTP client errors (400 Bad Request, 401, 403, 404)
                else:
                    last_error_reason = f"HTTP client error: {response.status_code} - {response.text}"
                    logger.error(
                        f"Non-retryable HTTP client error: {last_error_reason}. Aborting."
                    )
                    return False

            except (httpx.TimeoutException, httpx.NetworkError, httpx.RequestError) as exc:
                last_error_reason = f"Network exception: {exc}"
                logger.warning(
                    f"Network error dispatching to Feishu (attempt {attempt}/{self.max_retries}): {exc}. Retrying..."
                )
                if attempt < self.max_retries:
                    backoff = self.calculate_backoff(attempt)
                    time.sleep(backoff)
                    continue
                else:
                    logger.error(f"Network error retries exhausted: {exc}")
                    return False

            finally:
                if should_close:
                    client.close()

        logger.error(f"Failed to deliver alert to Feishu after {self.max_retries} attempts. Last reason: {last_error_reason}")
        return False

    def send_card(
        self,
        post: Union[TelegramPost, Dict[str, Any]],
        evaluation: Union[NewsEvaluation, Dict[str, Any]],
        webhook_url: Optional[str] = None,
        secret: Optional[str] = None,
    ) -> bool:
        """Constructs and sends a Feishu Interactive Card Schema 2.0.

        Args:
            post: TelegramPost domain object or dict.
            evaluation: NewsEvaluation result or dict.
            webhook_url: Optional webhook URL override.
            secret: Optional HMAC secret override.

        Returns:
            True if delivered successfully, False otherwise.
        """
        card_payload = FeishuCardBuilder.build_card(post, evaluation)
        return self.send(card_payload, webhook_url=webhook_url, secret=secret)


# ==============================================================================
# Functional Interfaces & Notifier Facade
# ==============================================================================

def send_alert(
    card_json: Dict[str, Any],
    webhook_url: Optional[str] = None,
    secret: Optional[str] = None,
    http_client: Optional[httpx.Client] = None,
) -> bool:
    """Functional interface for sending card alerts to Feishu webhook."""
    sender = FeishuWebhookSender(
        webhook_url=webhook_url,
        secret=secret,
        http_client=http_client,
    )
    return sender.send(card_json)


class Notifier:
    """Notifier facade conforming to PROJECT.md interface specifications."""

    @staticmethod
    def build_card(
        post: Union[TelegramPost, Dict[str, Any]],
        evaluation: Union[NewsEvaluation, Dict[str, Any]],
    ) -> Dict[str, Any]:
        """Builds Feishu card payload."""
        return FeishuCardBuilder.build_card(post, evaluation)

    @staticmethod
    def send_alert(
        card_json: Dict[str, Any],
        webhook_url: Optional[str] = None,
        secret: Optional[str] = None,
        http_client: Optional[httpx.Client] = None,
    ) -> bool:
        """Dispatches card JSON to Feishu webhook."""
        return send_alert(
            card_json,
            webhook_url=webhook_url,
            secret=secret,
            http_client=http_client,
        )
