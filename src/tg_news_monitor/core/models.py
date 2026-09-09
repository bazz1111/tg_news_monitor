"""Domain data models for Telegram News Monitor."""

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from pydantic import BaseModel, ConfigDict, Field


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
