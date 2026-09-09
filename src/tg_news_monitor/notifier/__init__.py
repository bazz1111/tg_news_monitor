"""Feishu (Lark) Interactive Alert Dispatcher package.

Provides:
- FeishuCardBuilder: Schema 2.0 interactive message card builder with 4-tier color template headers.
- FeishuWebhookSender: Webhook client with HMAC-SHA256 signatures, exponential backoff on HTTP 429 and Feishu code 19001.
- Notifier: High-level facade conforming to project interface contracts.
"""

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

__all__ = [
    "COLOR_TEMPLATE_MAP",
    "HEADER_ICON_MAP",
    "FeishuCardBuilder",
    "FeishuWebhookSender",
    "Notifier",
    "build_card",
    "build_feishu_card",
    "generate_signature",
    "get_color_template",
    "get_header_icon",
    "send_alert",
]
