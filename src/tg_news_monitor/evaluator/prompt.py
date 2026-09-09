"""Prompt engineering and schema definitions for Grok hot news evaluator."""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from tg_news_monitor.core.models import TelegramPost, NewsEvaluation


SYSTEM_PROMPT = """你是一位顶级资深即时财经与科技新闻主编兼应急预警情报专家。
你的任务是对 Telegram 频道抓取的原始快讯进行严格的“去噪、价值评分与结构化中文简报生成”。

### 一、去噪与过滤规则（Noise & Spam Filtering）
必须将以下内容标记为 `is_spam: true`，并将 `score`（紧迫度评分）限制在 1-2 分之间，且 `is_news: false`：
1. 明显的商业广告、返佣链接、代币私募（Presale）、免费空投（Airdrop）领取引流。
2. 邀请加入 VIP 群、付费跟单群、搬砖套利机器人推广。
3. 纯粹的日常闲聊、表情符号、问候语（如 "GM", "GN"）、毫无实质事实的无意义发言。
4. 无新闻增量信息的复读言论、纯主观情绪宣泄（如单纯的涨跌情绪吹捧）。

### 二、新闻紧迫度评分量表（Urgency Scoring Scale 1-10）
必须基于事件的“客观突发性”、“实体影响力”、“市场冲击度”严谨打分：
- 【1-2 分 | 垃圾与闲聊】：广告引流、问候刷屏、垃圾链接、无信息量内容。
- 【3-4 分 | 弱价值动态】：个人主观观点、常规市场闲谈、无实质影响力的普通项目进展、旧闻重复。
- 【5-6 分 | 一般行业快讯】：常规企业新闻通告、常规版本更新、定期统计数据发布、无重大波动的常规要闻。
- 【7-8 分 | 重大行业要闻】：头部机构重要政策转向、关键监管法规变动、数额较大的安全事件/攻击（$1M-$50M）、主流交易所重大故障或高管变动、显著影响市场预期的核心事件。
- 【9-10 分 | 顶级紧急突发】：行业灾难级黑天鹅、超大协议被盗或漏洞（> $50M 或核心基础设施受损）、主流大所暂停提现/清算/破产传闻、国家级重磅监管制裁、全球宏观级突发异动。

### 三、内容生成要求（Structured Chinese Briefing）
1. 语言：输出全部使用专业、简练、严谨的简体中文。对于外语原文，提取核心事实并翻译为中文输出。
2. 标题（title）：不超过 30-50 个字，客观陈述事实，严禁标题党与夸张词汇。
3. 核心速览（summary_bullets）：3 至 5 条要点，每条以事实为核心，包含核心实体、金额、数字或时间。
4. 关键看点（key_takeaways）：1 至 3 条要点，深入提炼事件对行业、用户或市场的潜在核心影响。
5. 分类（category）：必须为以下分类之一：["科技/AI", "宏观财经", "加密货币", "地缘政治", "行业快讯", "突发安全", "宏观监管", "日常闲聊", "推广广告"]。
6. 关注建议（actionable_insight）：1 句话，提供客观风险提示或观察建议。

### 四、输出 JSON 格式（必须严格遵守）
你必须且仅能输出符合以下格式的合法 JSON 字符串（不得包含任何多余文字或前后导语）：
{
  "score": 8,
  "is_news": true,
  "is_spam": false,
  "category": "加密货币",
  "title": "简明扼要的新闻标题（30字以内）",
  "summary_bullets": [
    "要点一：核心事实及涉事主体",
    "要点二：关键金额、数据或受影响范围",
    "要点三：官方最新回应或应对措施"
  ],
  "key_takeaways": [
    "分析该事件对行业或市场的深远影响"
  ],
  "actionable_insight": "针对该事件的关注焦点或应对建议"
}
"""

USER_PROMPT_TEMPLATE = """【待评估 Telegram 快讯】
- 来源频道: @{channel}
- 消息编号: #{message_id}
- 发布时间: {published_at}
- 消息原文:
\"\"\"
{text}
\"\"\"
"""

NEWS_EVALUATION_JSON_SCHEMA: Dict[str, Any] = {
    "$schema": "http://json-schema.org/draft-07/schema#",
    "type": "object",
    "properties": {
        "score": {
            "type": "integer",
            "minimum": 1,
            "maximum": 10,
            "description": "Newsworthiness urgency score between 1 and 10",
        },
        "is_news": {
            "type": "boolean",
            "description": "True if the post contains genuine news content",
        },
        "is_spam": {
            "type": "boolean",
            "description": "True if the post is promotional ad, scam, referral link, or routine chatter",
        },
        "category": {
            "type": "string",
            "enum": [
                "科技/AI",
                "宏观财经",
                "加密货币",
                "地缘政治",
                "行业快讯",
                "突发安全",
                "宏观监管",
                "日常闲聊",
                "推广广告",
            ],
            "description": "Event category",
        },
        "title": {
            "type": "string",
            "maxLength": 100,
            "description": "Concise Chinese headline (< 30-50 chars)",
        },
        "summary_bullets": {
            "type": "array",
            "items": {"type": "string"},
            "minItems": 1,
            "maxItems": 5,
            "description": "3-5 concise bulleted facts in Chinese",
        },
        "key_takeaways": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Core impact analysis bullets or points in Chinese",
        },
        "actionable_insight": {
            "type": ["string", "null"],
            "description": "Optional actionable risk advisory or observation advice",
        },
    },
    "required": [
        "score",
        "is_news",
        "is_spam",
        "category",
        "title",
        "summary_bullets",
        "key_takeaways",
    ],
    "additionalProperties": True,
}


def build_system_prompt() -> str:
    """Returns the calibrated system prompt for Grok."""
    return SYSTEM_PROMPT.strip()


def build_user_prompt(post: TelegramPost) -> str:
    """Formats the user prompt for evaluating a specific Telegram post."""
    pub_str = (
        post.published_at.isoformat()
        if hasattr(post.published_at, "isoformat")
        else str(post.published_at)
    )
    return USER_PROMPT_TEMPLATE.format(
        channel=post.channel,
        message_id=post.message_id,
        published_at=pub_str,
        text=post.text,
    ).strip()


def get_news_evaluation_json_schema() -> Dict[str, Any]:
    """Returns the JSON Schema dict for validating or requesting NewsEvaluation."""
    return NEWS_EVALUATION_JSON_SCHEMA
