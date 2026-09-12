"""News evaluator: CodeBuddy CLI production path plus leftover HTTP client tests."""

from tg_news_monitor.evaluator.codebuddy_client import (
    CodeBuddyError,
    CodeBuddyEvaluator,
    create_evaluator,
)
from tg_news_monitor.evaluator.fallback import (
    BREAKING_KEYWORDS,
    MultiStageFallbackHandler,
    calculate_backoff_delay,
    extract_first_json_object,
    heuristic_keyword_fallback,
    normalize_and_validate_evaluation,
    parse_and_repair_evaluation,
    parse_digest_brief,
    repair_json_string,
    strip_markdown_code_fences,
)
from tg_news_monitor.evaluator.prompt import (
    DIGEST_SYSTEM_PROMPT,
    NEWS_EVALUATION_JSON_SCHEMA,
    SYSTEM_PROMPT,
    USER_PROMPT_TEMPLATE,
    build_digest_system_prompt,
    build_digest_user_prompt,
    build_system_prompt,
    build_user_prompt,
    get_digest_item_json_schema,
    get_news_evaluation_json_schema,
)

_GROK_EXPORTS = {
    "DeepSeekClient",
    "GrokClient",
    "GrokError",
    "GrokNetworkError",
    "GrokRateLimitError",
    "GrokServerError",
    "LLMEvaluatorClient",
}

__all__ = [
    "CodeBuddyEvaluator",
    "CodeBuddyError",
    "GrokClient",
    "DeepSeekClient",
    "LLMEvaluatorClient",
    "create_evaluator",
    "GrokError",
    "GrokRateLimitError",
    "GrokServerError",
    "GrokNetworkError",
    "parse_digest_brief",
    "DIGEST_SYSTEM_PROMPT",
    "SYSTEM_PROMPT",
    "USER_PROMPT_TEMPLATE",
    "build_digest_system_prompt",
    "build_digest_user_prompt",
    "NEWS_EVALUATION_JSON_SCHEMA",
    "build_system_prompt",
    "build_user_prompt",
    "get_digest_item_json_schema",
    "get_news_evaluation_json_schema",
    "MultiStageFallbackHandler",
    "calculate_backoff_delay",
    "strip_markdown_code_fences",
    "extract_first_json_object",
    "repair_json_string",
    "normalize_and_validate_evaluation",
    "parse_and_repair_evaluation",
    "heuristic_keyword_fallback",
    "BREAKING_KEYWORDS",
]


def __getattr__(name: str):
    """Load the leftover HTTP client only when tests import Grok/DeepSeek names."""
    if name in _GROK_EXPORTS:
        from tg_news_monitor.evaluator import grok_client

        return getattr(grok_client, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
