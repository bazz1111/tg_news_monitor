"""Grok hot news evaluator, summarizer, and multi-stage fallback engine."""

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
    DeepSeekClient,
    GrokClient,
    GrokError,
    GrokNetworkError,
    GrokRateLimitError,
    GrokServerError,
    LLMEvaluatorClient,
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

__all__ = [
    "GrokClient",
    "DeepSeekClient",
    "LLMEvaluatorClient",
    "create_evaluator",
    "GrokError",
    "GrokRateLimitError",
    "GrokServerError",
    "GrokNetworkError",
    "SYSTEM_PROMPT",
    "USER_PROMPT_TEMPLATE",
    "NEWS_EVALUATION_JSON_SCHEMA",
    "build_system_prompt",
    "build_user_prompt",
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
