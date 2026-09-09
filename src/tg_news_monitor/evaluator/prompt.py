"""Prompt engineering and schema definitions for Grok hot news evaluator."""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from tg_news_monitor.core.models import TelegramPost, NewsEvaluation, DigestBrief


SYSTEM_PROMPT = """你是一位顶级资深即时财经与科技新闻主编兼应急预警情报专家。
你的任务是对 Telegram 频道抓取的原始快讯进行严格的“去噪、价值评分与结构化中文简报生成”。

### 一、去噪与过滤规则（Noise & Spam Filtering）
加密货币/区块链/Web3/代币相关内容一律视为噪声：`is_spam: true`，`is_news: false`，`score`≤2。
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


DIGEST_SYSTEM_PROMPT = """你是一位顶级资深即时财经与宏观新闻主编。你的任务是对本轮 Telegram 多频道抓取的一批新快讯做【批量去噪 + 精选 + 四维影响分析】，并输出一份结构化中文汇总简报（供单卡飞书版式使用）。

### 一、去噪与去重
1. 过滤广告、引流、空投、闲聊、无信息量复读、纯情绪宣泄。
2. 合并同主题/同事实的重复报道，只保留信息最完整的一条代表（同类事件只保留一条）。
3. 旧闻重复、无新增事实的内容不入选。

### 二、精选规则
1. 仅挑选对本轮有实质市场意义的新闻。
2. items 长度 0–5；有实质新闻时优先输出 3–5 条，按重要性降序 rank=1 最重要。
3. 若全是噪声/无实质增量：items=[] 且 has_material_news=false。
4. 【硬性禁选】任何加密货币/区块链/Web3/代币/交易所币情相关内容一律不入选（含 Bitcoin/ETH/Solana/USDT、币安、链上、NFT、DeFi 等）；即使重要也丢弃，不要用 category=加密货币 输出。

### 三、单卡字段（每条必填）
1. score：紧迫度/影响力 1–10 整数。
2. summary_bullets：3–4 条纯事实要点（含主体、金额、数据或时间），不要观点。
3. summary：一两句事实摘要（兼容字段）。
4. actionable_insight：1–2 句关注建议。
5. 方向标签（只能是 利多 / 利空 / 中性 / 不确定 之一）：
   - bias_overall、bias_us、bias_cn、bias_commodities
6. 四维影响句子（标签之后的简练说明）：
   - impact_overall、impact_us、impact_cn、impact_commodities
   若某维度无明显影响，写“影响有限”或“暂无明显传导”，对应 bias 用“中性”或“不确定”。

### 四、输出要求
1. 全部使用专业简练的简体中文。
2. 必须且仅输出合法 JSON（不要 markdown 代码围栏，不要前后导语），字段严格匹配：
{
  "headline": "本轮汇总标题",
  "overview": "1-3句市场/事件总览",
  "items": [
    {
      "rank": 1,
      "channel": "频道slug",
      "message_id": 123,
      "title": "简明标题",
      "summary": "一两句事实摘要",
      "category": "宏观财经",
      "score": 8,
      "summary_bullets": ["事实要点一", "事实要点二", "事实要点三"],
      "actionable_insight": "关注建议一两句",
      "bias_overall": "利多",
      "bias_us": "中性",
      "bias_cn": "不确定",
      "bias_commodities": "利空",
      "impact_overall": "总体影响说明",
      "impact_us": "美股影响说明",
      "impact_cn": "上证/中国市场影响说明",
      "impact_commodities": "大宗影响说明"
    }
  ],
  "filtered_note": "可选：说明过滤了多少噪声/重复",
  "has_material_news": true
}
3. channel / message_id 必须来自输入帖子，禁止编造。
4. has_material_news 在 items 非空时为 true，为空时为 false。
5. 禁止在 JSON 中输出 Telegram / t.me / 来源频道 / 查看原文 等外链信息。
"""


def build_digest_system_prompt() -> str:
    """Returns the system prompt for batch digest evaluation."""
    return DIGEST_SYSTEM_PROMPT.strip()


def build_digest_user_prompt(posts: List[TelegramPost], max_text_chars: int = 800) -> str:
    """Formats a batch of posts into a single user prompt for digest evaluation."""
    lines: List[str] = [
        f"【本轮待汇总 Telegram 快讯】共 {len(posts)} 条，请去噪、精选 0–5 条；"
        "每条输出 summary_bullets(3-4)、score、actionable_insight、bias_* 标签与 impact_* 句子。",
        "",
    ]
    for idx, post in enumerate(posts, start=1):
        pub_str = (
            post.published_at.isoformat()
            if hasattr(post.published_at, "isoformat")
            else str(post.published_at)
        )
        body = (post.text or "").strip()
        if len(body) > max_text_chars:
            body = body[:max_text_chars] + "…(truncated)"
        lines.append(f"--- 帖子 #{idx} ---")
        lines.append(f"channel: {post.channel}")
        lines.append(f"message_id: {post.message_id}")
        lines.append(f"published_at: {pub_str}")
        lines.append("text:")
        lines.append(body)
        lines.append("")
    lines.append(
        "请仅输出匹配 DigestBrief 的 JSON（含 summary_bullets、score、actionable_insight、bias_*、impact_*）。"
    )
    return "\n".join(lines).strip()
