"""CodeBuddy CLI evaluator (production LLM path).

Invokes `@tencent-ai/codebuddy-code` non-interactively. Primary model first,
then one fallback-model retry, then the existing local heuristic / raise path.
Does not call DeepSeek or any other HTTP LLM provider.
"""

from __future__ import annotations

import logging
import os
import subprocess
from datetime import datetime, timezone
from typing import Any, Callable, List, Optional

from tg_news_monitor.core.models import DigestBrief, NewsEvaluation, TelegramPost
from tg_news_monitor.evaluator.fallback import (
    MultiStageFallbackHandler,
    heuristic_keyword_fallback,
    parse_and_repair_evaluation,
    parse_digest_brief,
)
from tg_news_monitor.evaluator.prompt import (
    build_system_prompt,
    build_user_prompt,
    compose_digest_prompts,
)

logger = logging.getLogger(__name__)

DEFAULT_MODEL = "deepseek-v4.1-flash"
DEFAULT_FALLBACK_MODEL = "hy3"
DEFAULT_CLI_BIN = "codebuddy"
DEFAULT_EFFORT = "max"
DEFAULT_AUTOCOMPACT = "auto"
DEFAULT_TIMEOUT = 300.0
DIGEST_TIMEOUT_FLOOR = 300.0


class CodeBuddyError(Exception):
    """Raised when CodeBuddy CLI evaluation fails after retries."""


class CodeBuddyEvaluator:
    """Digest / single-post evaluator backed by the CodeBuddy CLI."""

    def __init__(
        self,
        api_key: str = "",
        model: str = DEFAULT_MODEL,
        fallback_model: str = DEFAULT_FALLBACK_MODEL,
        timeout: float = DEFAULT_TIMEOUT,
        fallback_on_exhaustion: bool = True,
        cli_bin: str = DEFAULT_CLI_BIN,
        extra_env: Optional[dict[str, str]] = None,
        run_cli: Optional[Callable[..., subprocess.CompletedProcess[str]]] = None,
        effort: str = DEFAULT_EFFORT,
        autocompact: str = DEFAULT_AUTOCOMPACT,
    ) -> None:
        self.provider = "codebuddy"
        self.api_key = api_key or ""
        self.model = (model or DEFAULT_MODEL).strip() or DEFAULT_MODEL
        self.fallback_model = (fallback_model or DEFAULT_FALLBACK_MODEL).strip() or DEFAULT_FALLBACK_MODEL
        self.timeout = float(timeout or DEFAULT_TIMEOUT)
        self.fallback_on_exhaustion = fallback_on_exhaustion
        self.cli_bin = (cli_bin or DEFAULT_CLI_BIN).strip() or DEFAULT_CLI_BIN
        self.effort = (effort or DEFAULT_EFFORT).strip() or DEFAULT_EFFORT
        self.autocompact = (autocompact or DEFAULT_AUTOCOMPACT).strip() or DEFAULT_AUTOCOMPACT
        self.extra_env = dict(extra_env or {})
        self._run_cli = run_cli or subprocess.run
        self.fallback_handler = MultiStageFallbackHandler()
        self.prompt_overlay: str = ""
        self.prompt_variant: Optional[str] = None
        self.digest_context: str = ""
        self.recent_history: str = ""
        self.last_usage: Optional[int] = None

    def _child_env(self) -> dict[str, str]:
        """Copy the process env, inject the CLI key, never set the international-site flag."""
        env = {k: v for k, v in os.environ.items() if v is not None}
        env.pop("CODEBUDDY_INTERNET_ENVIRONMENT", None)
        if self.api_key:
            env["CODEBUDDY_API_KEY"] = self.api_key
        env.update(self.extra_env)
        env.pop("CODEBUDDY_INTERNET_ENVIRONMENT", None)
        return env

    def _build_command(self, model: str, prompt: str) -> List[str]:
        return [
            self.cli_bin,
            "-p",
            "-y",
            "--tools",
            "",
            "--output-format",
            "text",
            "--model",
            model,
            "--effort",
            self.effort,
            "--autocompact",
            self.autocompact,
            prompt,
        ]

    def _invoke_model(self, model: str, prompt: str, timeout: float) -> str:
        cmd = self._build_command(model, prompt)
        try:
            completed = self._run_cli(
                cmd,
                capture_output=True,
                text=True,
                timeout=timeout,
                env=self._child_env(),
                check=False,
            )
        except subprocess.TimeoutExpired as err:
            raise CodeBuddyError(f"CodeBuddy CLI timeout after {timeout}s (model={model})") from err
        except OSError as err:
            raise CodeBuddyError(f"CodeBuddy CLI could not be started (model={model}): {err}") from err

        stderr = (completed.stderr or "").strip()
        stdout = completed.stdout or ""
        if completed.returncode != 0:
            snippet = stderr or stdout[:300]
            raise CodeBuddyError(
                f"CodeBuddy CLI exit {completed.returncode} (model={model}): {snippet[:400]}"
            )
        text = stdout.strip()
        if not text:
            raise CodeBuddyError(f"CodeBuddy CLI empty output (model={model})")
        return text

    def _models_to_try(self) -> List[str]:
        models = [self.model]
        if self.fallback_model and self.fallback_model != self.model:
            models.append(self.fallback_model)
        return models

    def _complete_with_fallback(
        self,
        prompt: str,
        parse_fn: Callable[[str], Any],
        timeout: float,
    ) -> Any:
        """Primary then fallback. parse_fn failures (empty/unparseable) also trigger retry."""
        last_error: Optional[Exception] = None
        for index, model in enumerate(self._models_to_try()):
            label = "primary" if index == 0 else "fallback"
            try:
                raw = self._invoke_model(model, prompt, timeout)
                return parse_fn(raw)
            except Exception as err:
                last_error = err
                logger.warning("CodeBuddy %s model %s failed: %s", label, model, err)
        if last_error:
            raise last_error
        raise CodeBuddyError("CodeBuddy evaluation failed without a specific error")

    def _compose_single_prompt(self, post: TelegramPost) -> str:
        return f"{build_system_prompt()}\n\n{build_user_prompt(post)}"

    def _compose_digest_prompt(self, posts: List[TelegramPost]) -> str:
        system, user = compose_digest_prompts(
            posts,
            variant=getattr(self, "prompt_variant", None),
            overlay=getattr(self, "prompt_overlay", None),
            digest_context=getattr(self, "digest_context", "") or "",
            recent_history=getattr(self, "recent_history", "") or "",
        )
        return f"{system}\n\n{user}"

    def evaluate_post(self, post: TelegramPost) -> NewsEvaluation:
        """Evaluate one post: primary → fallback → heuristic (same exhaustion path as before)."""
        prompt = self._compose_single_prompt(post)
        try:
            return self._complete_with_fallback(
                prompt,
                lambda raw: parse_and_repair_evaluation(
                    raw, fallback_title=f"来自 @{post.channel} 的快讯"
                ),
                timeout=self.timeout,
            )
        except Exception as err:
            if self.fallback_on_exhaustion:
                logger.info(
                    "CodeBuddy retries exhausted for @%s/#%s (%s). Level-3 heuristic fallback.",
                    post.channel,
                    post.message_id,
                    err,
                )
                return heuristic_keyword_fallback(post)
            raise

    def evaluate_digest(self, posts: List[TelegramPost]) -> DigestBrief:
        """One CLI call per model attempt. Raises after both fail so pending is retained."""
        if not posts:
            return DigestBrief(
                headline="本轮快讯",
                overview="本轮无新帖",
                items=[],
                has_material_news=False,
                filtered_note="empty_batch",
            )

        self.last_usage = None
        prompt = self._compose_digest_prompt(posts)
        digest_timeout = max(DIGEST_TIMEOUT_FLOOR, float(self.timeout))
        last_error: Optional[Exception] = None
        try:
            brief = self._complete_with_fallback(prompt, parse_digest_brief, timeout=digest_timeout)
            return brief
        except Exception as err:
            last_error = err
            err_msg = str(last_error) if last_error else "unknown digest failure"
            logger.error("Digest evaluation failed after CodeBuddy retries: %s", err_msg)
            raise CodeBuddyError(err_msg) from last_error

    def evaluate_text(
        self,
        text: str,
        channel: str = "news_channel",
        message_id: int = 1,
        direct_url: Optional[str] = None,
    ) -> NewsEvaluation:
        post = TelegramPost(
            channel=channel,
            message_id=message_id,
            published_at=datetime.now(timezone.utc),
            text=text,
            direct_url=direct_url or f"https://t.me/{channel}/{message_id}",
        )
        return self.evaluate_post(post)


def create_evaluator(
    provider: str = "codebuddy",
    api_key: str = "",
    api_base: Optional[str] = None,
    model: Optional[str] = None,
    fallback_model: Optional[str] = None,
    effort: Optional[str] = None,
    autocompact: Optional[str] = None,
    **kwargs: Any,
) -> CodeBuddyEvaluator:
    """Factory: always returns the CodeBuddy CLI evaluator (DeepSeek HTTP is removed)."""
    ignored = {k: kwargs.pop(k) for k in ("http_client", "async_http_client", "api_base") if k in kwargs}
    if ignored or (api_base and str(api_base).strip()):
        logger.debug("Ignoring leftover HTTP evaluator kwargs: %s api_base=%s", list(ignored), api_base)
    normalized = (provider or "codebuddy").strip().lower()
    if normalized not in {"", "codebuddy", "auto"}:
        logger.info("Evaluator provider %r is not a live path; using CodeBuddy CLI", provider)
    return CodeBuddyEvaluator(
        api_key=api_key,
        model=model or DEFAULT_MODEL,
        fallback_model=fallback_model or DEFAULT_FALLBACK_MODEL,
        effort=effort or DEFAULT_EFFORT,
        autocompact=autocompact or DEFAULT_AUTOCOMPACT,
        **kwargs,
    )
