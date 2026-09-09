"""Shared test fixtures, mocks, and helpers for tg_news_monitor test suite.

Provides:
- Realistic Telegram web preview HTML fixtures and dynamic HTML page generators.
- Calibrated mock Grok (xAI) OpenAI-compatible API responses across scoring tiers.
- In-memory Mock Feishu Webhook Receiver capturing and validating Card Schema 2.0 payloads.
- Temporary SQLite database fixture in WAL mode with strict composite uniqueness.
- Mock configuration and environment variable fixtures.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional
from unittest.mock import MagicMock

import pytest

# Ensure source and project paths are available for imports
PROJECT_ROOT = Path(__file__).resolve().parent.parent
SRC_DIR = PROJECT_ROOT / "src"
for p in (str(SRC_DIR), str(PROJECT_ROOT)):
    if p not in sys.path:
        sys.path.insert(0, p)


# ==============================================================================
# 1. Telegram HTML Web Preview Fixtures & Generator
# ==============================================================================

MOCK_RAW_POST_101 = {
    "channel": "whale_alert",
    "message_id": 101,
    "text": "🚨 15,000 #ETH (45,120,300 USD) transferred from unknown wallet to #Binance\nhttps://etherscan.io/tx/0xmocked101",
    "published_at": "2026-09-08T21:30:00+00:00",
    "has_media": False,
    "media_type": None,
    "is_service": False,
    "forward_from": None,
}

MOCK_RAW_POST_102 = {
    "channel": "whale_alert",
    "message_id": 102,
    "text": "Good morning crypto fam! What are your thoughts on BTC today? GM GM ☕ Let's discuss in the chat!",
    "published_at": "2026-09-08T21:31:00+00:00",
    "has_media": False,
    "media_type": None,
    "is_service": False,
    "forward_from": None,
}

MOCK_RAW_POST_103 = {
    "channel": "whale_alert",
    "message_id": 103,
    "text": "🏛️ BREAKING: Regulators officially approve first combined Ethereum & Solana institutional index ETF fund.",
    "published_at": "2026-09-08T21:32:00+00:00",
    "has_media": False,
    "media_type": None,
    "is_service": False,
    "forward_from": "Regulatory Wire Official",
}

MOCK_RAW_POST_104 = {
    "channel": "whale_alert",
    "message_id": 104,
    "text": "🔥 1000x MOON GEM PRESALE IS LIVE! Join VIP alpha signals group now: https://t.me/fake_pump_gem. Free 500 USDT airdrop for first 50 depositors! 🚀💰",
    "published_at": "2026-09-08T21:33:00+00:00",
    "has_media": False,
    "media_type": None,
    "is_service": False,
    "forward_from": None,
}

MOCK_RAW_POST_105 = {
    "channel": "whale_alert",
    "message_id": 105,
    "text": "Channel photo updated",
    "published_at": "2026-09-08T21:34:00+00:00",
    "has_media": False,
    "media_type": None,
    "is_service": True,
    "forward_from": None,
}

MOCK_RAW_POST_106 = {
    "channel": "whale_alert",
    "message_id": 106,
    "text": "🚨 CRITICAL EXPLOIT: Lending protocol AlphaPool drained for $18M due to price oracle reentrancy flaw. Withdraw funds immediately!",
    "published_at": "2026-09-08T21:35:00+00:00",
    "has_media": False,
    "media_type": None,
    "is_service": False,
    "forward_from": None,
}

MOCK_RAW_POST_MEDIA = {
    "channel": "whale_alert",
    "message_id": 107,
    "text": "Chart analysis showing key support level on ETH/USDT 4-hour timeframe.",
    "published_at": "2026-09-08T21:36:00+00:00",
    "has_media": True,
    "media_type": "photo",
    "is_service": False,
    "forward_from": None,
}


def build_telegram_post_html(post: Dict[str, Any]) -> str:
    """Renders a single Telegram widget message DOM node matching https://t.me/s/{channel}."""
    channel = post.get("channel", "whale_alert")
    msg_id = post.get("message_id", 100)
    text = post.get("text", "")
    pub_at = post.get("published_at", "2026-09-08T21:30:00+00:00")
    is_service = post.get("is_service", False)
    forward_from = post.get("forward_from")
    has_media = post.get("has_media", False)
    media_type = post.get("media_type", "photo")

    if is_service:
        return f"""
    <div class="tgme_widget_message_wrap js-widget_message_wrap">
      <div class="tgme_widget_message tgme_widget_message_service js-widget_message" data-post="{channel}/{msg_id}">
        <div class="tgme_widget_message_bubble">
          <div class="tgme_widget_message_text js-message_text">{text}</div>
        </div>
      </div>
    </div>"""

    forward_html = ""
    if forward_from:
        forward_html = f"""
        <div class="tgme_widget_message_forwarded_from">
          <span class="tgme_widget_message_forwarded_from_name">{forward_from}</span>
        </div>"""

    media_html = ""
    if has_media:
        if media_type == "video":
            media_html = """
        <div class="tgme_widget_message_video_player js-message_video_player">
          <i class="tgme_widget_message_video_thumb" style="background-image:url('https://cdn4.telesco.pe/file/mock_video.jpg')"></i>
        </div>"""
        else:
            media_html = """
        <a class="tgme_widget_message_photo_wrap" style="background-image:url('https://cdn4.telesco.pe/file/mock_photo.jpg')" href="#">
          <div class="tgme_widget_message_photo" style="padding-top:56.25%"></div>
        </a>"""

    return f"""
    <div class="tgme_widget_message_wrap js-widget_message_wrap">
      <div class="tgme_widget_message text_not_supported_wrap js-widget_message" data-post="{channel}/{msg_id}">
        <div class="tgme_widget_message_user">
          <a href="https://t.me/{channel}"><span class="tgme_widget_message_owner_name">@{channel}</span></a>
        </div>
        {forward_html}
        {media_html}
        <div class="tgme_widget_message_text js-message_text" dir="auto">{text}</div>
        <div class="tgme_widget_message_footer js-message_footer">
          <div class="tgme_widget_message_info">
            <span class="tgme_widget_message_meta">
              <a class="tgme_widget_message_date" href="https://t.me/{channel}/{msg_id}">
                <time datetime="{pub_at}" class="time">21:30</time>
              </a>
            </span>
          </div>
        </div>
      </div>
    </div>"""


def generate_mock_telegram_html(posts: List[Dict[str, Any]], channel: str = "whale_alert") -> str:
    """Generates a complete public web preview HTML document for https://t.me/s/{channel}."""
    messages_html = "\n".join(build_telegram_post_html(p) for p in posts)
    return f"""<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8">
  <title>Telegram: Contact @{channel}</title>
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
</head>
<body class="clean_page tgme_channel_page">
  <div class="tgme_page">
    <div class="tgme_channel_info">
      <div class="tgme_channel_info_header">
        <div class="tgme_channel_info_title"><span dir="auto">{channel.upper()}</span></div>
        <div class="tgme_widget_message_pinned_header">Pinned Message</div>
      </div>
    </div>
    <div class="tgme_channel_history js-message_history">
      {messages_html}
    </div>
  </div>
</body>
</html>"""


@pytest.fixture
def mock_html_posts_initial() -> List[Dict[str, Any]]:
    """Initial set of 5 sample messages covering breaking, chatter, major, spam, and service."""
    return [
        MOCK_RAW_POST_101,
        MOCK_RAW_POST_102,
        MOCK_RAW_POST_103,
        MOCK_RAW_POST_104,
        MOCK_RAW_POST_105,
    ]


@pytest.fixture
def mock_html_posts_with_new() -> List[Dict[str, Any]]:
    """Subsequent set containing original 5 messages plus 1 brand-new breaking post (106)."""
    return [
        MOCK_RAW_POST_101,
        MOCK_RAW_POST_102,
        MOCK_RAW_POST_103,
        MOCK_RAW_POST_104,
        MOCK_RAW_POST_105,
        MOCK_RAW_POST_106,
    ]


@pytest.fixture
def mock_telegram_preview_html(mock_html_posts_initial: List[Dict[str, Any]]) -> str:
    """Rendered HTML preview page for initial poll."""
    return generate_mock_telegram_html(mock_html_posts_initial, channel="whale_alert")


@pytest.fixture
def mock_telegram_second_poll_html(mock_html_posts_with_new: List[Dict[str, Any]]) -> str:
    """Rendered HTML preview page for second poll containing re-fed posts and new post 106."""
    return generate_mock_telegram_html(mock_html_posts_with_new, channel="whale_alert")


# ==============================================================================
# 2. Calibrated Mock Grok (xAI) OpenAI-Compatible API Responses
# ==============================================================================

GROK_EVAL_SCORE_9 = {
    "is_spam": False,
    "urgency_score": 9,
    "category": "突发安全",
    "title": "巨额以太坊异动转入主流交易所",
    "summary_points": [
        "链上大额转账监控显示，15,000 枚 ETH（价值约 4,512 万美元）从未知钱包转入币安。",
        "涉事未知钱包持有超 6 个月，疑似巨鲸获利清仓或做市商大额调仓。",
        "市场短期现货抛压与借贷清算预期迅速升温。",
    ],
    "key_takeaways": "超大额资金进入交易所通常预示短期流动性变化与潜在抛售压力，对 ETH 短期价格形成压制。",
    "actionable_insight": "密切关注链上深度与大额卖单挂单动态，防范剧烈市场波动风险。",
}

GROK_EVAL_SCORE_8 = {
    "is_spam": False,
    "urgency_score": 8,
    "category": "宏观监管",
    "title": "监管机构批准首只以太坊与索拉纳双重ETF",
    "summary_points": [
        "监管机构正式批准首例涵盖以太坊与 Solana 的复合型 ETF 申请。",
        "首期申购通道将于下周一正式向机构投资者开放。",
        "该决策被视为加密资产多品种合规上市的重大政策突破。",
    ],
    "key_takeaways": "为传统资本布局主流公链开辟合规渠道，显著提升长期市场流动性与机构信任度。",
    "actionable_insight": "留意下周机构资金实际净流入数据与相关公链代币流动性溢价。",
}

GROK_EVAL_SCORE_4_CHATTER = {
    "is_spam": False,
    "urgency_score": 4,
    "category": "日常闲聊",
    "title": "社区日常行情探讨",
    "summary_points": [
        "社区成员交流日常早安问候并探讨比特币日内短期走势。",
        "无官方发布会、无突发链上数据异动，属于常规社区交流。",
    ],
    "key_takeaways": "属于社群日常情绪宣泄与闲聊，对宏观行情无实质引导作用。",
    "actionable_insight": "无需特殊操作，不触发外部告警推送。",
}

GROK_EVAL_SCORE_2_SPAM = {
    "is_spam": True,
    "urgency_score": 2,
    "category": "推广广告",
    "title": "虚假代币预售与跟单引流",
    "summary_points": [
        "发布高倍暴富宣传代币预售，引流用户加入未知外部群组。",
        "包含推广返佣链接，疑似钓鱼及高风险诈骗。",
    ],
    "key_takeaways": "无真实事实依据的引流推广广告，存在极高资金安全风险。",
    "actionable_insight": "直接过滤拦截，严禁扩散转发。",
}

GROK_EVAL_SCORE_5_MINOR = {
    "is_spam": False,
    "urgency_score": 5,
    "category": "行业快讯",
    "title": "某底层协议发布常规补丁版本更新",
    "summary_points": [
        "协议官方发布 v2.4.1 客户端版本，优化底层 RPC 响应延迟。",
        "不涉及硬分叉或破坏性改动，节点可自愿升级。",
    ],
    "key_takeaways": "常规技术维护迭代，对协议整体经济模型及代币价格无直接重大影响。",
    "actionable_insight": "节点运营者按计划择机升级即可。",
}

GROK_EVAL_SCORE_10_CRITICAL = {
    "is_spam": False,
    "urgency_score": 10,
    "category": "突发安全",
    "title": "AlphaPool 协议突发重入漏洞被盗1800万美元",
    "summary_points": [
        "借贷协议 AlphaPool 智能合约遭遇闪电贷重入攻击，被盗金额达 1,800 万美元。",
        "攻击者正将盗取资金迅速兑换为 DAI 并分散转移。",
        "官方紧急呼吁所有流动性提供者立即撤出资金并暂停前端接口。",
    ],
    "key_takeaways": "该漏洞波及主要流动性池，可能触发连锁清算并导致坏账。",
    "actionable_insight": "立即撤出相关资金，严密监控黑客地址资金流向。",
}


def make_openai_chat_completion(
    content_obj: Dict[str, Any] | str,
    model: str = "grok-beta",
    as_json_str: bool = True,
) -> Dict[str, Any]:
    """Wraps an evaluation dictionary or string in an OpenAI-compatible chat completion response."""
    if isinstance(content_obj, dict):
        content_text = json.dumps(content_obj, ensure_ascii=False)
    else:
        content_text = str(content_obj)

    return {
        "id": "chatcmpl-mock-" + os.urandom(4).hex(),
        "object": "chat.completion",
        "created": int(datetime.now(timezone.utc).timestamp()),
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": content_text,
                },
                "finish_reason": "stop",
            }
        ],
        "usage": {
            "prompt_tokens": 120,
            "completion_tokens": 85,
            "total_tokens": 205,
        },
    }


@pytest.fixture
def mock_grok_response_score_9() -> Dict[str, Any]:
    """OpenAI-compatible payload for score 9 breaking news."""
    return make_openai_chat_completion(GROK_EVAL_SCORE_9)


@pytest.fixture
def mock_grok_response_score_8() -> Dict[str, Any]:
    """OpenAI-compatible payload for score 8 major news."""
    return make_openai_chat_completion(GROK_EVAL_SCORE_8)


@pytest.fixture
def mock_grok_response_score_4() -> Dict[str, Any]:
    """OpenAI-compatible payload for score 4 chatter."""
    return make_openai_chat_completion(GROK_EVAL_SCORE_4_CHATTER)


@pytest.fixture
def mock_grok_response_score_2_spam() -> Dict[str, Any]:
    """OpenAI-compatible payload for score 2 spam."""
    return make_openai_chat_completion(GROK_EVAL_SCORE_2_SPAM)


@pytest.fixture
def mock_grok_response_score_10() -> Dict[str, Any]:
    """OpenAI-compatible payload for score 10 critical exploit."""
    return make_openai_chat_completion(GROK_EVAL_SCORE_10_CRITICAL)


# ==============================================================================
# 3. Mock Feishu Webhook Receiver & Card Schema 2.0 Validator
# ==============================================================================

class MockFeishuReceiver:
    """Mock receiver recording Feishu bot webhook deliveries with Schema 2.0 validation."""

    def __init__(self) -> None:
        self.received_requests: List[Dict[str, Any]] = []
        self.rate_limit_countdown: int = 0
        self.server_error_countdown: int = 0
        self.bad_request_countdown: int = 0

    def record_call(self, payload: Dict[str, Any], headers: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
        """Simulates receiving a POST to the Feishu webhook and returns response status/body."""
        req_entry = {
            "payload": payload,
            "headers": headers or {},
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        self.received_requests.append(req_entry)

        if self.bad_request_countdown > 0:
            self.bad_request_countdown -= 1
            return {"status_code": 400, "json": {"code": 9499, "msg": "bad request: invalid card json"}}

        if self.rate_limit_countdown > 0:
            self.rate_limit_countdown -= 1
            return {"status_code": 200, "json": {"code": 19001, "msg": "trigger frequency limit"}}

        if self.server_error_countdown > 0:
            self.server_error_countdown -= 1
            return {"status_code": 500, "json": {"code": 500, "msg": "internal server error"}}

        return {"status_code": 200, "json": {"code": 0, "msg": "success"}}

    def simulate_rate_limit(self, times: int = 1) -> None:
        """Configures receiver to return code 19001 for the next `times` calls."""
        self.rate_limit_countdown = times

    def simulate_server_error(self, times: int = 1) -> None:
        """Configures receiver to return HTTP 500 for the next `times` calls."""
        self.server_error_countdown = times

    def simulate_bad_request(self, times: int = 1) -> None:
        """Configures receiver to return HTTP 400 bad request for the next `times` calls."""
        self.bad_request_countdown = times

    @property
    def total_received(self) -> int:
        return len(self.received_requests)

    def get_cards(self) -> List[Dict[str, Any]]:
        """Returns all received interactive cards."""
        return [r["payload"] for r in self.received_requests if "card" in r["payload"]]

    def clear(self) -> None:
        self.received_requests.clear()
        self.rate_limit_countdown = 0
        self.server_error_countdown = 0
        self.bad_request_countdown = 0

    @staticmethod
    def validate_card_schema(payload: Dict[str, Any]) -> tuple[bool, str]:
        """Strictly validates that the payload complies with Feishu Interactive Card Schema 2.0."""
        if not isinstance(payload, dict):
            return False, "Payload must be a JSON dictionary"

        if payload.get("msg_type") != "interactive":
            return False, f"Expected msg_type='interactive', got '{payload.get('msg_type')}'"

        card = payload.get("card")
        if not isinstance(card, dict):
            return False, "Payload must contain a 'card' object"

        if card.get("schema") != "2.0":
            return False, f"Card schema must be '2.0', got '{card.get('schema')}'"

        header = card.get("header")
        if not isinstance(header, dict):
            return False, "Card must have a 'header' object"

        template = header.get("template")
        valid_templates = {"red", "orange", "blue", "grey", "wathet", "turquoise", "carmine"}
        if template not in valid_templates:
            return False, f"Header template '{template}' is not a valid Feishu color token"

        title = header.get("title")
        if not isinstance(title, dict) or "content" not in title:
            return False, "Header must have a 'title' with 'content'"

        body = card.get("body")
        if not isinstance(body, dict) or not isinstance(body.get("elements"), list):
            return False, "Card must have 'body.elements' array"

        elements = body.get("elements", [])
        if len(elements) == 0:
            return False, "Card body elements cannot be empty"

        # Schema 2.0 uses a direct button and open_url behaviors.
        has_button = False
        for el in elements:
            if el.get("tag") == "button":
                has_button = any(b.get("type") == "open_url" and b.get("default_url", "").startswith("https://") for b in el.get("behaviors", [])) or has_button

        if not has_button:
            return False, "Card must include a button with an HTTPS open_url behavior"

        return True, "Schema 2.0 Valid"


@pytest.fixture
def mock_feishu_receiver() -> MockFeishuReceiver:
    """Fixture providing a fresh mock Feishu webhook receiver."""
    return MockFeishuReceiver()


# ==============================================================================
# 4. Temporary SQLite Database in WAL Mode
# ==============================================================================

INIT_SQLITE_SCHEMA = """
PRAGMA journal_mode = WAL;
PRAGMA synchronous = NORMAL;

CREATE TABLE IF NOT EXISTS processed_messages (
    channel TEXT NOT NULL,
    message_id INTEGER NOT NULL,
    published_at TEXT,
    processed_at TEXT DEFAULT (datetime('now')),
    urgency_score INTEGER,
    is_spam BOOLEAN DEFAULT 0,
    alert_dispatched BOOLEAN DEFAULT 0,
    raw_content TEXT,
    PRIMARY KEY (channel, message_id)
);

CREATE INDEX IF NOT EXISTS idx_processed_lookup 
ON processed_messages (channel, message_id);
"""


def init_test_sqlite_db(db_path: Path | str) -> sqlite3.Connection:
    """Initializes SQLite database with WAL mode and composite primary key schema."""
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.executescript(INIT_SQLITE_SCHEMA)
    conn.commit()
    return conn


@pytest.fixture
def temp_sqlite_db_path():
    """Generates a temporary SQLite db file path."""
    tmp_dir = Path(__file__).resolve().parent / f".tmp_test_{uuid.uuid4().hex[:8]}"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    db_path = tmp_dir / "test_tg_monitor.db"
    yield db_path
    if tmp_dir.exists():
        shutil.rmtree(tmp_dir, ignore_errors=True)


@pytest.fixture
def temp_sqlite_conn(temp_sqlite_db_path: Path):
    """Provides an active SQLite connection in WAL mode with initialized schema."""
    conn = init_test_sqlite_db(temp_sqlite_db_path)
    yield conn
    conn.close()


# ==============================================================================
# 5. Environment & Settings Mock Fixture
# ==============================================================================

@pytest.fixture(autouse=True)
def _force_day_alert_mode(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep existing runner tests on daytime knobs unless marked real_schedule."""
    if request.node.get_closest_marker("real_schedule"):
        return
    monkeypatch.setattr(
        "tg_news_monitor.core.schedule.classify_alert_mode",
        lambda *args, **kwargs: "day",
    )
    monkeypatch.setattr(
        "tg_news_monitor.core.policy.DeliveryPolicy.peek_morning_flush",
        lambda self, *args, **kwargs: False,
    )


@pytest.fixture
def mock_env_config(monkeypatch: pytest.MonkeyPatch, temp_sqlite_db_path: Path) -> Dict[str, str]:
    """Sets standard environment variables for tg_news_monitor configuration."""
    env_vars = {
        "TELEGRAM_CHANNELS": "whale_alert",
        "POLL_INTERVAL_SECONDS": "60",
        "POLL_JITTER_SECONDS": "0.1",
        "REQUEST_TIMEOUT_SECONDS": "5.0",
        "MAX_HTTP_RETRIES": "3",
        "USER_AGENTS": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/128.0.0.0 Safari/537.36",
        "GROK_API_KEY": "xai-mock-test-key-12345",
        "GROK_API_BASE": "https://api.x.ai/v1",
        "GROK_MODEL": "grok-beta",
        "GROK_TIMEOUT_SECONDS": "10.0",
        "HOTNESS_THRESHOLD": "7",
        "FEISHU_WEBHOOK_URL": "https://open.feishu.cn/open-apis/bot/v2/hook/mock-webhook-token",
        "FEISHU_SECRET": "",
        "FEISHU_RETRY_COUNT": "3",
        "FEISHU_RETRY_DELAY": "0.1",
        "DATABASE_PATH": str(temp_sqlite_db_path),
        "LOG_LEVEL": "DEBUG",
    }
    for k, v in env_vars.items():
        monkeypatch.setenv(k, v)
    return env_vars
