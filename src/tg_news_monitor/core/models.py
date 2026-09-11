"""Domain data models for Telegram News Monitor."""

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from pydantic import BaseModel, ConfigDict, Field, field_validator


class TelegramPost(BaseModel):
    """Represents a message/post scraped from a Telegram public channel web preview."""

    model_config = ConfigDict(extra="ignore", from_attributes=True)

    channel: str = Field(..., description="Channel username/slug without @ prefix")
    message_id: int = Field(..., description="Monotonically increasing Telegram message ID")
    published_at: datetime = Field(..., description="UTC publication timestamp extracted from <time>")
    text: str = Field(..., description="Cleaned, formatted message body text with markdown links preserved")
    has_media: bool = Field(default=False, description="True if post contains photo, video, document, etc.")
    media_type: Optional[str] = Field(default=None, description="Type of media: photo, video, document, poll, album, etc.")
    direct_url: str = Field(..., description="Permanent web link to the message, e.g. https://t.me/{channel}/{id}")
    forward_from: Optional[str] = Field(default=None, description="Name or handle of original source if forwarded")
    media_urls: List[str] = Field(default_factory=list, description="List of extracted media URLs or background image links")
    views: Optional[str] = Field(default=None, description="View count badge string, e.g. 15.2K")
    raw_html: Optional[str] = Field(default=None, description="Raw HTML fragment for forensic or debugging use")
    group_id: Optional[str] = Field(
        default=None,
        description="Peer group id that ingested this post; storage defaults to legacy when unset",
    )


class NewsEvaluation(BaseModel):
    """Evaluation result produced by Grok / LLM evaluating urgency, newsworthiness and summary."""

    model_config = ConfigDict(extra="ignore", from_attributes=True)

    score: int = Field(..., ge=1, le=10, description="Urgency / newsworthiness score on a 1-10 scale")
    is_news: bool = Field(..., description="True if post contains genuine news content")
    is_spam: bool = Field(..., description="True if post is commercial advertisement, token shill, or chat noise")
    title: str = Field(..., max_length=100, description="Concise Chinese headline (< 30-50 chars)")
    summary_bullets: List[str] = Field(default_factory=list, description="3-5 concise bulleted facts in Chinese")
    key_takeaways: List[str] = Field(default_factory=list, description="Core impact analysis bullets or points")
    category: str = Field(default="行业快讯", description="News category, e.g. 突发安全, 宏观监管, 行业快讯")
    actionable_insight: Optional[str] = Field(default=None, description="Actionable risk advisory or observation advice")
    evaluated_at: Optional[datetime] = Field(default_factory=lambda: datetime.now(timezone.utc), description="Evaluation timestamp")


class AlertPayload(BaseModel):
    """Payload encapsulating an evaluated post ready for Feishu interactive card dispatch."""

    model_config = ConfigDict(extra="ignore", from_attributes=True)

    post: TelegramPost = Field(..., description="Original Telegram post")
    evaluation: NewsEvaluation = Field(..., description="Grok evaluation result")
    card_json: Dict[str, Any] = Field(..., description="Feishu Interactive Card Schema 2.0 payload")


class DigestItem(BaseModel):
    """Single ranked item inside a batch digest brief."""

    model_config = ConfigDict(extra="ignore", from_attributes=True)

    rank: int = Field(..., ge=1, le=5, description="Rank position 1-5 (1 = most important)")
    channel: str = Field(..., description="Source channel username without @")
    message_id: int = Field(..., description="Telegram message ID of the source post")
    title: str = Field(..., description="Concise Chinese headline")
    summary: str = Field(default="", description="One-paragraph Chinese summary of the item (compat)")
    category: str = Field(default="行业快讯", description="News category")
    score: Optional[int] = Field(
        default=None,
        ge=1,
        le=10,
        description="Urgency score 1-10; if missing, card falls back to 11-rank",
    )
    summary_bullets: List[str] = Field(
        default_factory=list,
        description="3-4 concise factual bullets for the single-card body",
    )
    actionable_insight: Optional[str] = Field(
        default=None,
        description="1-2 sentence watch / action advice",
    )
    bias_overall: str = Field(
        default="不确定",
        description="Direction tag: 利多|利空|中性|不确定",
    )
    bias_us: str = Field(
        default="不确定",
        description="US equities direction tag: 利多|利空|中性|不确定",
    )
    bias_cn: str = Field(
        default="不确定",
        description="A-shares direction tag: 利多|利空|中性|不确定",
    )
    bias_commodities: str = Field(
        default="不确定",
        description="Commodities direction tag: 利多|利空|中性|不确定",
    )
    impact_overall: str = Field(
        default="无直接影响",
        description="Overall market / industry impact analysis",
    )
    impact_us: str = Field(default="无直接影响", description="Impact on US stocks")
    impact_cn: str = Field(
        default="无直接影响",
        description="Impact on China equities (上证/中国市场)",
    )
    impact_commodities: str = Field(
        default="无直接影响",
        description="Impact on commodities (黄金/原油等)",
    )

    @field_validator(
        "impact_overall",
        "impact_us",
        "impact_cn",
        "impact_commodities",
        mode="before",
    )
    @classmethod
    def _default_null_impact(cls, value: Any) -> Any:
        # LLM JSON sometimes emits null for a skipped dimension.
        if value is None:
            return "无直接影响"
        return value

    event_at: Optional[datetime] = None
    is_update: bool = False
    night_alert: bool = False
    confirmed_source: str = ""
    update_reason: str = ""
    published_at: Optional[datetime] = Field(
        default=None,
        description="UTC publication time; prefer runner post.published_at when building card",
    )
    media_urls: List[str] = Field(
        default_factory=list,
        description="Photo URLs copied from the source post for wechat_photo cards",
    )


class DigestBrief(BaseModel):
    """Batch digest result: filtered ranking of material news for one polling pass."""

    model_config = ConfigDict(extra="ignore", from_attributes=True)

    headline: str = Field(..., description="Card headline summarizing this digest pass")
    overview: str = Field(..., description="Short overview of the pass / market tone")
    items: List[DigestItem] = Field(default_factory=list, description="0-5 ranked material items")
    filtered_note: Optional[str] = Field(
        default=None,
        description="Optional note about filtered duplicate/noise posts",
    )
    has_material_news: bool = Field(
        default=True,
        description="False when no material items remain after filtering",
    )
