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
from typing import Any, Dict, List, Optional, Union

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

    # Monitored Telegram channels
    telegram_channels: List[str] = Field(
        default_factory=list,
        description="Target Telegram channel handles to monitor without @ prefix",
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

    # DeepSeek API credentials & model
    deepseek_api_key: str = Field(
        default="",
        description="DeepSeek API key (https://platform.deepseek.com/)",
    )
    deepseek_api_base: str = Field(
        default="https://api.deepseek.com",
        description="DeepSeek API base endpoint URL (default: https://api.deepseek.com)",
    )
    deepseek_model: str = Field(
        default="deepseek-chat",
        description="DeepSeek model identifier, e.g. deepseek-chat or deepseek-reasoner",
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
        default="01:00-08:00",
        description="Local quiet window START-END; empty disables. May wrap midnight.",
    )
    shoulder_hours: str = Field(
        default="23:00-01:00",
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
        if value is None:
            return []

        if isinstance(value, str):
            value = value.strip()
            if not value:
                return []
            # Check for JSON-style list string
            if value.startswith("[") and value.endswith("]"):
                try:
                    parsed_list = json.loads(value)
                    if isinstance(parsed_list, list):
                        return [str(ch).lower().lstrip("@").strip() for ch in parsed_list if str(ch).strip()]
                except Exception:
                    pass
            # Split comma-separated string
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

    @field_validator("deepseek_api_key", mode="before")
    @classmethod
    def parse_secret_keys(cls, value: Any) -> str:
        """Accepts str or SecretStr and returns plaintext str."""
        if isinstance(value, SecretStr):
            return value.get_secret_value()
        return str(value) if value is not None else ""

    @model_validator(mode="before")
    @classmethod
    def map_legacy_grok_keys(cls, data: Any) -> Any:
        """Transparently maps legacy grok keys to deepseek if provided."""
        if isinstance(data, dict):
            if not data.get("deepseek_api_key") and data.get("grok_api_key"):
                data["deepseek_api_key"] = data["grok_api_key"]
            if not data.get("deepseek_model") and data.get("grok_model"):
                data["deepseek_model"] = data["grok_model"]
            if not data.get("deepseek_api_base") and data.get("grok_api_base"):
                data["deepseek_api_base"] = data["grok_api_base"]
        return data

    @model_validator(mode="after")
    def sync_feishu_secrets(self) -> Settings:
        """Synchronizes feishu_webhook_secret and feishu_secret aliases."""
        secret = self.feishu_webhook_secret or self.feishu_secret
        if secret and not self.feishu_webhook_secret:
            self.feishu_webhook_secret = secret
        if secret and not self.feishu_secret:
            self.feishu_secret = secret
        return self

    # --------------------------------------------------------------------------
    # DeepSeek LLM Accessors & Backward Compatibility Aliases
    # --------------------------------------------------------------------------

    @property
    def active_llm_provider(self) -> str:
        """Returns the active LLM provider name ('deepseek')."""
        return "deepseek"

    @property
    def active_api_key(self) -> str:
        """Returns the active DeepSeek API key."""
        return self.deepseek_api_key

    @property
    def active_api_base(self) -> str:
        """Returns the active DeepSeek API base URL."""
        return self.deepseek_api_base

    @property
    def active_model(self) -> str:
        """Returns the active DeepSeek model name."""
        return self.deepseek_model

    # Compatibility aliases
    @property
    def grok_api_key(self) -> str:
        return self.deepseek_api_key

    @property
    def grok_api_base(self) -> str:
        return self.deepseek_api_base

    @property
    def grok_model(self) -> str:
        return self.deepseek_model

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

        # 3. Load from OS environment variables (case-insensitive)
        for env_k, env_v in os.environ.items():
            merged_values[env_k.lower()] = env_v

        # 4. Apply explicit constructor / function arguments (highest precedence)
        for arg_k, arg_v in override_kwargs.items():
            if arg_v is not None:
                merged_values[arg_k.lower()] = arg_v

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
