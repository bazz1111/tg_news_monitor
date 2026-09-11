"""Centralized configuration management for Telegram News Monitor.

Supports:
- Loading from environment variables with case-insensitive matching.
- Loading from .env files with custom or default paths.
- Loading from YAML or JSON configuration files.
- Full Pydantic validation (threshold bounds, minimum poll intervals, channel parsing).
- Priority order: Explicit kwargs > Environment variables > .env > config.yaml > Defaults.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Annotated, Any, Dict, List, Mapping, Optional, Union

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator

# Attempt to import PyYAML and python-dotenv if available
try:
    import yaml  # type: ignore
    HAS_YAML = True
except ImportError:
    HAS_YAML = False

try:
    import dotenv  # type: ignore
    HAS_DOTENV = True
except ImportError:
    HAS_DOTENV = False

try:
    from pydantic_settings import BaseSettings, SettingsConfigDict  # type: ignore
    HAS_PYDANTIC_SETTINGS = True
except ImportError:
    HAS_PYDANTIC_SETTINGS = False

try:
    from pydantic_settings import NoDecode  # type: ignore
except ImportError:
    NoDecode = None  # type: ignore

# pydantic-settings JSON-decodes list env vars; keep comma-separated TELEGRAM_CHANNELS as text.
_ChannelList = Annotated[List[str], NoDecode] if NoDecode is not None else List[str]

DEFAULT_LEGACY_GROUP_ID = "legacy"
_GROUP_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")

# Per-group knobs that overlay global Settings when not None.
GROUP_OVERRIDE_FIELDS = (
    "quiet_hours",
    "shoulder_hours",
    "hotness_threshold",
    "news_max_age_seconds",
    "digest_min_candidates",
    "digest_max_wait_seconds",
    "digest_min_interval_seconds",
    "digest_card_interval_seconds",
    "digest_max_batch_size",
    "digest_max_calls_per_day",
    "shoulder_hotness_threshold",
    "shoulder_digest_min_interval_seconds",
    "shoulder_digest_min_candidates",
    "shoulder_digest_card_interval_seconds",
    "quiet_hotness_threshold",
    "quiet_digest_min_interval_seconds",
    "quiet_digest_min_candidates",
    "quiet_digest_card_interval_seconds",
    "quiet_alert_interval_seconds",
    "quiet_card_cap",
    "morning_flush_enabled",
    "morning_flush_max_age_seconds",
    "morning_flush_hotness_threshold",
    "morning_flush_card_interval_seconds",
)


def normalize_channel_list(value: Any) -> List[str]:
    """Parses comma-separated channel strings or list of handles, normalizing format."""
    if value is None:
        return []

    if isinstance(value, str):
        value = value.strip()
        if not value:
            return []
        if value.startswith("[") and value.endswith("]"):
            try:
                parsed_list = json.loads(value)
                if isinstance(parsed_list, list):
                    return [str(ch).lower().lstrip("@").strip() for ch in parsed_list if str(ch).strip()]
            except Exception:
                pass
        parts = [p.strip() for p in value.split(",") if p.strip()]
        return [p.lower().lstrip("@") for p in parts if p]

    if isinstance(value, (list, tuple, set)):
        result: List[str] = []
        for ch in value:
            if ch:
                clean = str(ch).lower().lstrip("@").strip()
                if clean:
                    result.append(clean)
        return result

    return [str(value).lower().lstrip("@").strip()]


def lookup_env(name: str, env_lookup: Optional[Mapping[str, Any]] = None) -> str:
    """Resolve an env var by original or lower-case name from a lookup map or os.environ."""
    key = (name or "").strip()
    if not key:
        return ""
    if env_lookup:
        if key in env_lookup and env_lookup[key] is not None:
            return str(env_lookup[key]).strip()
        low = key.lower()
        if low in env_lookup and env_lookup[low] is not None:
            return str(env_lookup[low]).strip()
    return (os.environ.get(key) or os.environ.get(key.upper()) or "").strip()


class CardProfile(BaseModel):
    """Feishu card + digest prompt flavor for one peer group."""

    model_config = ConfigDict(extra="ignore")

    subtitle: str = Field(default="投资情报快报", description="Card header subtitle")
    include_investment_impact: bool = Field(
        default=True,
        description=(
            "Group-level switch for the investment-impact block; "
            "card still requires score>=9 and category in {军事, 地缘政治, 宏观财经}"
        ),
    )
    prompt_overlay: Optional[str] = Field(
        default=None,
        description="Extra text appended to the digest system prompt",
    )
    prompt_variant: Optional[str] = Field(
        default=None,
        description="Digest system-prompt variant key: news | story | wechat_photo",
    )
    embed_images: Optional[bool] = Field(
        default=None,
        description=(
            "Upload images to Feishu and embed img_key in the card. "
            "None = true for wechat_photo, false otherwise. news24 can set true later; "
            "only the wechat_photo send path uploads today."
        ),
    )

    @field_validator("prompt_variant", mode="before")
    @classmethod
    def normalize_prompt_variant(cls, value: Any) -> Optional[str]:
        if value is None:
            return None
        text = str(value).strip().lower()
        if not text:
            return None
        if text not in {"news", "story", "wechat_photo"}:
            raise ValueError("card_profile.prompt_variant must be 'news', 'story', or 'wechat_photo'")
        return text

    @field_validator("embed_images", mode="before")
    @classmethod
    def normalize_embed_images(cls, value: Any) -> Optional[bool]:
        if value is None or value == "":
            return None
        if isinstance(value, bool):
            return value
        text = str(value).strip().lower()
        if text in {"true", "1", "yes", "on"}:
            return True
        if text in {"false", "0", "no", "off"}:
            return False
        raise ValueError("card_profile.embed_images must be a boolean")

    def embed_images_enabled(self) -> bool:
        """Default on for wechat_photo; other variants opt in via embed_images=true."""
        if self.embed_images is not None:
            return bool(self.embed_images)
        return (self.prompt_variant or "").strip().lower() == "wechat_photo"


class Group(BaseModel):
    """Peer-equal Telegram → Feishu pipeline. No group is privileged at runtime."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    id: str = Field(..., description="Stable group id used in logs and SQLite keys")
    name: str = Field(default="", description="Human-readable label")
    channels: List[str] = Field(default_factory=list)
    enabled: bool = Field(default=True)

    webhook_url: Optional[str] = Field(default=None, description="Direct webhook URL (tests / explicit)")
    webhook_secret: Optional[str] = Field(default=None)
    webhook_url_env: Optional[str] = Field(default=None, description="Env var name holding the webhook URL")
    webhook_secret_env: Optional[str] = Field(default=None)

    quiet_hours: Optional[str] = None
    shoulder_hours: Optional[str] = None
    hotness_threshold: Optional[int] = Field(default=None, ge=1, le=10)
    news_max_age_seconds: Optional[int] = Field(default=None, ge=60)
    digest_min_candidates: Optional[int] = Field(default=None, ge=1)
    digest_max_wait_seconds: Optional[int] = Field(default=None, ge=0)
    digest_min_interval_seconds: Optional[int] = Field(default=None, ge=0)
    digest_card_interval_seconds: Optional[float] = Field(default=None, ge=0)
    digest_max_batch_size: Optional[int] = Field(default=None, ge=1, le=50)
    digest_max_calls_per_day: Optional[int] = Field(
        default=None,
        ge=1,
        description="Soft per-group LLM call cap; global DIGEST_MAX_CALLS_PER_DAY is the hard ceiling",
    )
    shoulder_hotness_threshold: Optional[int] = Field(default=None, ge=1, le=10)
    shoulder_digest_min_interval_seconds: Optional[int] = Field(default=None, ge=0)
    shoulder_digest_min_candidates: Optional[int] = Field(default=None, ge=1)
    shoulder_digest_card_interval_seconds: Optional[float] = Field(default=None, ge=0)
    quiet_hotness_threshold: Optional[int] = Field(default=None, ge=1, le=10)
    quiet_digest_min_interval_seconds: Optional[int] = Field(default=None, ge=0)
    quiet_digest_min_candidates: Optional[int] = Field(default=None, ge=1)
    quiet_digest_card_interval_seconds: Optional[float] = Field(default=None, ge=0)
    quiet_alert_interval_seconds: Optional[int] = Field(default=None, ge=60)
    quiet_card_cap: Optional[int] = Field(default=None, ge=0)
    morning_flush_enabled: Optional[bool] = None
    morning_flush_max_age_seconds: Optional[int] = Field(default=None, ge=60)
    morning_flush_hotness_threshold: Optional[int] = Field(default=None, ge=1, le=10)
    morning_flush_card_interval_seconds: Optional[float] = Field(default=None, ge=0)
    card_profile: Optional[CardProfile] = None

    @field_validator("id")
    @classmethod
    def validate_group_id(cls, value: Any) -> str:
        text = str(value or "").strip()
        if not _GROUP_ID_RE.match(text):
            raise ValueError("group id must be 1-64 chars of [A-Za-z0-9_-], starting with alphanumeric")
        return text

    @field_validator("name", mode="before")
    @classmethod
    def normalize_name(cls, value: Any) -> str:
        return str(value or "").strip()

    @field_validator("channels", mode="before")
    @classmethod
    def parse_channels(cls, value: Any) -> List[str]:
        return normalize_channel_list(value)

    @field_validator("quiet_hours", "shoulder_hours", mode="before")
    @classmethod
    def validate_hour_window(cls, value: Any) -> Optional[str]:
        # None = field omitted (inherit global). "" = explicitly disable this group.
        if value is None:
            return None
        text = str(value).strip()
        if not text:
            return ""
        from tg_news_monitor.core.schedule import parse_hour_window

        parse_hour_window(text)
        return text

    @field_validator("webhook_url", "webhook_secret", "webhook_url_env", "webhook_secret_env", mode="before")
    @classmethod
    def empty_str_to_none(cls, value: Any) -> Optional[str]:
        if value is None:
            return None
        text = str(value).strip()
        return text or None

    def display_name(self) -> str:
        return self.name or self.id

    def resolved_card_profile(self) -> CardProfile:
        return self.card_profile or CardProfile()

    def resolve_webhook_url(self, env_lookup: Optional[Mapping[str, Any]] = None) -> str:
        if self.webhook_url:
            return self.webhook_url.strip()
        return lookup_env(self.webhook_url_env or "", env_lookup)

    def resolve_webhook_secret(self, env_lookup: Optional[Mapping[str, Any]] = None) -> Optional[str]:
        if self.webhook_secret:
            return self.webhook_secret
        secret = lookup_env(self.webhook_secret_env or "", env_lookup)
        return secret or None

    def has_resolvable_webhook(self, env_lookup: Optional[Mapping[str, Any]] = None) -> bool:
        return bool(self.resolve_webhook_url(env_lookup))


class GroupSettingsView:
    """Settings duck-type with per-group overlays. Used by knobs_for / QuietHours / runner."""

    def __init__(self, settings: "Settings", group: Group) -> None:
        self._settings = settings
        self.group = group

    @property
    def telegram_channels(self) -> List[str]:
        return list(self.group.channels)

    @property
    def feishu_webhook_url(self) -> str:
        return self.group.resolve_webhook_url(getattr(self._settings, "env_lookup", None))

    @property
    def feishu_webhook_secret(self) -> Optional[str]:
        return self.group.resolve_webhook_secret(getattr(self._settings, "env_lookup", None))

    @property
    def feishu_secret(self) -> Optional[str]:
        return self.feishu_webhook_secret

    def __getattr__(self, name: str) -> Any:
        if name in GROUP_OVERRIDE_FIELDS:
            value = getattr(self.group, name, None)
            if value is not None:
                return value
            # quiet/shoulder: empty YAML ("") or explicit null disables the
            # window. Omitting the field (not in model_fields_set) inherits global.
            if name in {"quiet_hours", "shoulder_hours"} and name in self.group.model_fields_set:
                return ""
        return getattr(self._settings, name)


# ==============================================================================
# Helper Parsers for .env and YAML (Independent of external dependencies)
# ==============================================================================

def parse_dotenv_file(filepath: Union[str, Path]) -> Dict[str, str]:
    """Parses a .env file into a dictionary of key-value pairs."""
    path = Path(filepath)
    if not path.is_file():
        return {}

    if HAS_DOTENV:
        try:
            return {k: v for k, v in dotenv.dotenv_values(path).items() if v is not None}
        except Exception:
            pass

    values: Dict[str, str] = {}
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue

            if stripped.startswith("export "):
                stripped = stripped[7:].strip()

            if "=" not in stripped:
                continue

            key, val = stripped.split("=", 1)
            key = key.strip()
            val = val.strip()

            # Strip surrounding matching quotes
            if (val.startswith('"') and val.endswith('"')) or (val.startswith("'") and val.endswith("'")):
                val = val[1:-1]
            else:
                # Remove inline comment if not quoted
                if " #" in val:
                    val = val.split(" #", 1)[0].strip()

            values[key] = val

    return values


def parse_yaml_file(filepath: Union[str, Path]) -> Dict[str, Any]:
    """Parses a YAML or JSON configuration file into a dictionary."""
    path = Path(filepath)
    if not path.is_file():
        return {}

    with open(path, "r", encoding="utf-8", errors="replace") as f:
        content = f.read().strip()

    if not content:
        return {}

    if HAS_YAML:
        try:
            parsed = yaml.safe_load(content)
            if isinstance(parsed, dict):
                return parsed
        except Exception:
            pass

    # Fallback to JSON parser
    try:
        parsed_json = json.loads(content)
        if isinstance(parsed_json, dict):
            return parsed_json
    except Exception:
        pass

    # Lightweight genuine pure-Python YAML parser for key-value and list configs
    data: Dict[str, Any] = {}
    current_key: Optional[str] = None
    current_list: Optional[List[Any]] = None

    for raw_line in content.splitlines():
        line = raw_line.rstrip()
        if not line or line.strip().startswith("#"):
            continue

        stripped = line.strip()

        # Handle list items
        if stripped.startswith("- "):
            item_val = stripped[2:].strip()
            # Strip quotes
            if (item_val.startswith('"') and item_val.endswith('"')) or (item_val.startswith("'") and item_val.endswith("'")):
                item_val = item_val[1:-1]
            elif item_val.lower() == "true":
                item_val = True
            elif item_val.lower() == "false":
                item_val = False
            elif re.match(r"^-?\d+$", item_val):
                item_val = int(item_val)
            elif re.match(r"^-?\d+\.\d+$", item_val):
                item_val = float(item_val)

            if current_key and current_list is not None:
                current_list.append(item_val)
            continue

        # Handle key: value or key:
        if ":" in stripped:
            k, v = stripped.split(":", 1)
            k = k.strip()
            v = v.strip()

            # End previous list if switching keys
            if current_key and current_list is not None:
                data[current_key] = current_list
                current_list = None

            if not v:
                # Key begins a list or nested structure
                current_key = k
                current_list = []
            else:
                # Direct value
                current_key = None
                # Strip inline comment
                if " #" in v and not (v.startswith('"') or v.startswith("'")):
                    v = v.split(" #", 1)[0].strip()

                if (v.startswith('"') and v.endswith('"')) or (v.startswith("'") and v.endswith("'")):
                    val: Any = v[1:-1]
                elif v.lower() == "true":
                    val = True
                elif v.lower() == "false":
                    val = False
                elif v.lower() in ("null", "none", "~"):
                    val = None
                elif re.match(r"^-?\d+$", v):
                    val = int(v)
                elif re.match(r"^-?\d+\.\d+$", v):
                    val = float(v)
                elif v.startswith("[") and v.endswith("]"):
                    # Inline JSON array
                    try:
                        val = json.loads(v)
                    except Exception:
                        val = [s.strip().strip("'\"") for s in v[1:-1].split(",") if s.strip()]
                else:
                    val = v

                data[k] = val

    if current_key and current_list is not None:
        data[current_key] = current_list

    return data


# ==============================================================================
# Settings Base and Implementation
# ==============================================================================

# Base class selection
_BaseClass = BaseSettings if HAS_PYDANTIC_SETTINGS else BaseModel


class Settings(_BaseClass):
    """Centralized configuration for tg_news_monitor service."""

    model_config = ConfigDict(
        extra="ignore",
        populate_by_name=True,
        case_sensitive=False,
    )

    # Monitored Telegram channels (legacy single-list; ignored as primary once groups is set)
    telegram_channels: _ChannelList = Field(
        default_factory=list,
        description="Target Telegram channel handles to monitor without @ prefix",
    )

    # Peer-equal groups. None = not configured (may synthesize a legacy group).
    # An explicit empty list means "no groups" and does not fall back to TELEGRAM_CHANNELS.
    groups: Optional[List[Group]] = Field(
        default=None,
        description="Peer-equal TG→Feishu pipelines; when set, TELEGRAM_CHANNELS is not primary",
    )
    legacy_group_id: str = Field(
        default=DEFAULT_LEGACY_GROUP_ID,
        description="Id used when synthesizing a group from TELEGRAM_CHANNELS + FEISHU_WEBHOOK_URL",
    )
    env_lookup: Dict[str, str] = Field(
        default_factory=dict,
        description="Case-preserving env snapshot for webhook_url_env resolution",
        exclude=True,
    )

    # Polling frequency & jitter
    poll_interval_seconds: int = Field(
        default=60,
        ge=5,
        description="Polling interval in seconds between passes (minimum 5s)",
    )
    max_jitter_seconds: int = Field(
        default=15,
        ge=0,
        description="Maximum randomized jitter delay added to interval",
    )
    inter_channel_delay_seconds: float = Field(
        default=2.0,
        ge=0.0,
        description="Randomized pause between consecutive channel requests in seconds",
    )

    # CodeBuddy CLI credentials & models (Tencent CodeBuddy Code)
    codebuddy_api_key: str = Field(
        default="",
        description="CodeBuddy CLI credential (CODEBUDDY_API_KEY). Do not set CODEBUDDY_INTERNET_ENVIRONMENT.",
    )
    codebuddy_model: str = Field(
        default="deepseek-v4.1-flash",
        description="Primary CodeBuddy model id (CLI --model)",
    )
    codebuddy_fallback_model: str = Field(
        default="hy3",
        description="Fallback CodeBuddy model id after primary failure",
    )
    codebuddy_cli: str = Field(
        default="codebuddy",
        description="CodeBuddy CLI executable on PATH",
    )
    codebuddy_effort: str = Field(
        default="max",
        description="CodeBuddy CLI --effort (minimal|low|medium|high|xhigh|max)",
    )
    codebuddy_autocompact: str = Field(
        default="auto",
        description="CodeBuddy CLI --autocompact (follow model context window)",
    )
    codebuddy_timeout: float = Field(
        default=300.0,
        gt=0,
        description="CodeBuddy CLI subprocess timeout in seconds",
    )

    # Urgency & hotness threshold gating
    hotness_threshold: int = Field(
        default=7,
        ge=1,
        le=10,
        description="Urgency score threshold (1-10) to trigger interactive Feishu alerts",
    )

    news_max_age_seconds: int = Field(default=1800, ge=60)
    digest_min_interval_seconds: int = Field(default=180, ge=0)
    digest_max_batch_size: int = Field(default=20, ge=1, le=50)
    digest_max_calls_per_day: int = Field(default=288, ge=1)

    # Digest buffering gate (batch LLM + multi single cards)
    digest_min_candidates: int = Field(
        default=3,
        ge=1,
        description="Minimum pending candidates before calling evaluate_digest (unless max wait reached)",
    )
    digest_max_wait_seconds: int = Field(
        default=900,
        ge=0,
        description="Max seconds to buffer pending candidates before forcing a digest LLM call",
    )
    digest_card_interval_seconds: float = Field(
        default=10.0,
        ge=0,
        description="Seconds to wait between Feishu single-card sends in the same digest batch",
    )

    # Elastic quiet hours (local clock; windows may wrap midnight)
    timezone: str = Field(
        default="Asia/Shanghai",
        description="IANA timezone for alert windows (default Asia/Shanghai)",
    )
    quiet_hours: str = Field(
        default="00:00-08:00",
        description="Local quiet window START-END; empty disables. May wrap midnight.",
    )
    shoulder_hours: str = Field(
        default="22:00-00:00",
        description="Local shoulder window START-END; empty disables. May wrap midnight.",
    )
    shoulder_hotness_threshold: int = Field(default=8, ge=1, le=10)
    shoulder_digest_min_interval_seconds: int = Field(default=600, ge=0)
    shoulder_digest_min_candidates: int = Field(default=16, ge=1)
    shoulder_digest_card_interval_seconds: float = Field(default=15.0, ge=0)
    quiet_hotness_threshold: int = Field(default=9, ge=1, le=10)
    quiet_digest_min_interval_seconds: int = Field(default=1800, ge=0)
    quiet_digest_min_candidates: int = Field(default=16, ge=1)
    quiet_digest_card_interval_seconds: float = Field(default=15.0, ge=0)
    quiet_alert_interval_seconds: int = Field(default=1800, ge=60)
    quiet_card_cap: int = Field(
        default=5,
        ge=0,
        description="Max Feishu cards during one quiet window; 0 disables sends in quiet",
    )
    morning_flush_enabled: bool = Field(default=True)
    morning_flush_max_age_seconds: int = Field(default=7200, ge=60)
    morning_flush_hotness_threshold: int = Field(default=7, ge=1, le=10)
    morning_flush_card_interval_seconds: float = Field(default=10.0, ge=0)

    # Feishu (Lark) Webhook dispatcher
    feishu_webhook_url: str = Field(
        default="",
        description="Feishu Custom Bot Webhook URL for interactive card dispatch",
    )
    feishu_webhook_secret: Optional[str] = Field(
        default=None,
        description="Optional Feishu HMAC-SHA256 signing secret",
    )
    feishu_secret: Optional[str] = Field(
        default=None,
        description="Alias for feishu_webhook_secret",
    )
    feishu_app_id: str = Field(
        default="",
        description="Feishu open-platform app id for tenant_access_token + image upload",
    )
    feishu_app_secret: str = Field(
        default="",
        description="Feishu open-platform app secret (never log or commit)",
    )

    # Storage and runtime parameters
    db_path: str = Field(
        default="data/tg_news.db",
        description="Path to persistent SQLite database file",
    )
    log_level: str = Field(
        default="INFO",
        description="Logging verbosity level (DEBUG, INFO, WARNING, ERROR)",
    )
    http_proxy: Optional[str] = Field(
        default=None,
        description="Optional HTTP proxy URL for web requests",
    )
    https_proxy: Optional[str] = Field(
        default=None,
        description="Optional HTTPS proxy URL for web requests",
    )

    # --------------------------------------------------------------------------
    # Field Validators
    # --------------------------------------------------------------------------

    @field_validator("telegram_channels", mode="before")
    @classmethod
    def parse_telegram_channels(cls, value: Any) -> List[str]:
        """Parses comma-separated channel strings or list of handles, normalizing format."""
        return normalize_channel_list(value)

    @field_validator("legacy_group_id")
    @classmethod
    def validate_legacy_group_id(cls, value: Any) -> str:
        text = str(value or "").strip() or DEFAULT_LEGACY_GROUP_ID
        if not _GROUP_ID_RE.match(text):
            raise ValueError("legacy_group_id must be 1-64 chars of [A-Za-z0-9_-], starting with alphanumeric")
        return text

    @field_validator("groups", mode="before")
    @classmethod
    def parse_groups(cls, value: Any) -> Optional[List[Any]]:
        if value is None:
            return None
        if isinstance(value, str):
            text = value.strip()
            if not text:
                return None
            try:
                value = json.loads(text)
            except Exception as exc:
                raise ValueError(f"groups must be a YAML/JSON list: {exc}") from exc
        if isinstance(value, dict):
            value = [value]
        if not isinstance(value, list):
            raise ValueError("groups must be a list of group objects")
        return value

    @field_validator("env_lookup", mode="before")
    @classmethod
    def parse_env_lookup(cls, value: Any) -> Dict[str, str]:
        if not value:
            return {}
        if isinstance(value, dict):
            return {str(k): str(v) for k, v in value.items() if v is not None}
        return {}

    @field_validator("poll_interval_seconds")
    @classmethod
    def validate_poll_interval(cls, value: int) -> int:
        """Enforces minimum 5 seconds polling interval."""
        if value < 5:
            raise ValueError("poll_interval_seconds must be at least 5 seconds")
        return value

    @field_validator("hotness_threshold")
    @classmethod
    def validate_hotness_threshold(cls, value: int) -> int:
        """Enforces 1-10 threshold range."""
        if value < 1 or value > 10:
            raise ValueError("hotness_threshold must be between 1 and 10")
        return value

    @field_validator("quiet_hours", "shoulder_hours")
    @classmethod
    def validate_hour_window(cls, value: Any) -> str:
        """Accepts empty (disabled) or START-END; rejects malformed windows."""
        from tg_news_monitor.core.schedule import parse_hour_window

        if value is None:
            return ""
        text = str(value).strip()
        if not text:
            return ""
        parse_hour_window(text)
        return text

    @field_validator("timezone")
    @classmethod
    def validate_timezone(cls, value: Any) -> str:
        text = str(value or "").strip() or "Asia/Shanghai"
        from tg_news_monitor.core.schedule import load_zone

        load_zone(text)
        return text

    @field_validator("codebuddy_api_key", "feishu_app_id", "feishu_app_secret", mode="before")
    @classmethod
    def parse_secret_keys(cls, value: Any) -> str:
        """Accepts str or SecretStr and returns plaintext str."""
        if isinstance(value, SecretStr):
            return value.get_secret_value()
        return str(value).strip() if value is not None else ""

    @model_validator(mode="after")
    def sync_feishu_secrets(self) -> Settings:
        """Synchronizes feishu_webhook_secret and feishu_secret aliases."""
        secret = self.feishu_webhook_secret or self.feishu_secret
        if secret and not self.feishu_webhook_secret:
            self.feishu_webhook_secret = secret
        if secret and not self.feishu_secret:
            self.feishu_secret = secret
        return self

    @model_validator(mode="after")
    def finalize_peer_groups(self) -> Settings:
        """Synthesize a legacy group when groups is unset; validate peer-group invariants."""
        if self.groups is None:
            if self.telegram_channels:
                self.groups = [
                    Group(
                        id=self.legacy_group_id,
                        name=self.legacy_group_id,
                        channels=list(self.telegram_channels),
                        webhook_url=self.feishu_webhook_url or None,
                        webhook_secret=self.feishu_webhook_secret or self.feishu_secret,
                    )
                ]
            else:
                self.groups = []
        self._validate_peer_groups()
        return self

    def _validate_peer_groups(self) -> None:
        seen_ids: set[str] = set()
        seen_channels: dict[str, str] = {}
        for group in self.groups or []:
            if group.id in seen_ids:
                raise ValueError(f"duplicate group id: {group.id!r}")
            seen_ids.add(group.id)
            for channel in group.channels:
                owner = seen_channels.get(channel)
                if owner is not None:
                    raise ValueError(
                        f"channel @{channel} is assigned to both group {owner!r} and {group.id!r}; "
                        "channel membership must be mutually exclusive"
                    )
                seen_channels[channel] = group.id

    def enabled_groups(self) -> List[Group]:
        """Enabled groups that have at least one channel (empty/disabled are skipped)."""
        return [g for g in (self.groups or []) if g.enabled and g.channels]

    def group_by_id(self, group_id: str) -> Optional[Group]:
        for group in self.groups or []:
            if group.id == group_id:
                return group
        return None

    def group_settings(self, group: Union[str, Group]) -> GroupSettingsView:
        if isinstance(group, str):
            found = self.group_by_id(group)
            if found is None:
                raise KeyError(f"unknown group id: {group!r}")
            group = found
        return GroupSettingsView(self, group)

    def runtime_validation_errors(self, *, require_webhook: bool = True) -> List[str]:
        """Startup checks for enabled groups. Tests may skip webhook via require_webhook=False."""
        errors: List[str] = []
        for group in self.enabled_groups():
            if require_webhook and not group.has_resolvable_webhook(self.env_lookup):
                errors.append(
                    f"group {group.id!r} has channels but no resolvable webhook "
                    f"(set webhook_url or webhook_url_env)"
                )
        return errors

    # --------------------------------------------------------------------------
    # CodeBuddy LLM Accessors
    # --------------------------------------------------------------------------

    @property
    def active_llm_provider(self) -> str:
        """Returns the active LLM provider name ('codebuddy')."""
        return "codebuddy"

    @property
    def active_api_key(self) -> str:
        """Returns the CodeBuddy CLI API key."""
        return self.codebuddy_api_key

    @property
    def active_model(self) -> str:
        """Returns the primary CodeBuddy model id."""
        return self.codebuddy_model

    @property
    def active_fallback_model(self) -> str:
        """Returns the fallback CodeBuddy model id."""
        return self.codebuddy_fallback_model

    # --------------------------------------------------------------------------
    # Multi-source Construction & Loading Factory
    # --------------------------------------------------------------------------

    @classmethod
    def load(
        cls,
        config_path: Optional[Union[str, Path]] = None,
        env_file: Optional[Union[str, Path]] = None,
        **override_kwargs: Any,
    ) -> Settings:
        """Loads and merges configuration from YAML, .env, environment, and overrides.

        Priority hierarchy (highest to lowest):
        1. Explicit override_kwargs
        2. OS environment variables (os.environ)
        3. .env file variables
        4. YAML / JSON config file
        5. Model defaults
        """
        merged_values: Dict[str, Any] = {}
        env_lookup: Dict[str, str] = {}

        def _remember_env(key: str, value: Any) -> None:
            if value is None:
                return
            text = str(value)
            env_lookup[key] = text
            env_lookup[key.lower()] = text

        # 1. Load from YAML / JSON config file if present
        target_config = config_path or os.environ.get("CONFIG_PATH") or os.environ.get("CONFIG_FILE")
        if not target_config:
            # Check default candidate filenames in current working directory
            for candidate in ("config.yaml", "config.yml", "config.json"):
                if Path(candidate).is_file():
                    target_config = candidate
                    break

        if target_config and Path(target_config).is_file():
            yaml_vals = parse_yaml_file(target_config)
            for k, v in yaml_vals.items():
                merged_values[k.lower()] = v

        # 2. Load from .env file if present
        target_env = env_file or os.environ.get("ENV_FILE") or ".env"
        if Path(target_env).is_file():
            env_file_vals = parse_dotenv_file(target_env)
            for k, v in env_file_vals.items():
                merged_values[k.lower()] = v
                _remember_env(k, v)

        # 3. Load from OS environment variables (case-insensitive)
        for env_k, env_v in os.environ.items():
            merged_values[env_k.lower()] = env_v
            _remember_env(env_k, env_v)

        # 4. Apply explicit constructor / function arguments (highest precedence)
        for arg_k, arg_v in override_kwargs.items():
            if arg_v is not None:
                merged_values[arg_k.lower()] = arg_v

        if "env_lookup" not in override_kwargs:
            merged_values["env_lookup"] = env_lookup

        return cls(**merged_values)

    def __init__(self, **data: Any) -> None:
        """Initializes Settings, automatically merging env and .env if not using pydantic_settings."""
        if HAS_PYDANTIC_SETTINGS and isinstance(self, BaseSettings):
            super().__init__(**data)
        else:
            # Emulate BaseSettings automatic environment resolution
            merged: Dict[str, Any] = {}

            # Check default .env in cwd
            if Path(".env").is_file():
                for k, v in parse_dotenv_file(".env").items():
                    merged[k.lower()] = v

            # Merge os.environ
            for k, v in os.environ.items():
                merged[k.lower()] = v

            # Merge explicit kwargs
            for k, v in data.items():
                if v is not None:
                    merged[k.lower()] = v

            super().__init__(**merged)


# ==============================================================================
# Functional Helpers
# ==============================================================================

def get_config(
    config_path: Optional[Union[str, Path]] = None,
    env_file: Optional[Union[str, Path]] = None,
    **kwargs: Any,
) -> Settings:
    """Convenience factory function returning an initialized Settings instance."""
    return Settings.load(config_path=config_path, env_file=env_file, **kwargs)
