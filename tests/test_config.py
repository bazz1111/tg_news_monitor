"""Unit tests for centralized configuration management in tg_news_monitor.config."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
import pytest
from pydantic import ValidationError, SecretStr

from tg_news_monitor.config import Settings, get_config, parse_dotenv_file, parse_yaml_file


class TestConfigDefaultsAndParsing:
    """Verifies default values and field type conversions."""

    def test_default_values(self, monkeypatch: pytest.MonkeyPatch):
        # Clear any environment variables that might interfere
        for k in list(os.environ.keys()):
            if (
                k.startswith("TELEGRAM_")
                or k.startswith("GROK_")
                or k.startswith("DEEPSEEK_")
                or k.startswith("FEISHU_")
                or k.startswith("POLL_")
                or k.startswith("HOTNESS_")
                or k.startswith("QUIET_")
                or k.startswith("SHOULDER_")
                or k.startswith("MORNING_")
                or k == "TIMEZONE"
            ):
                monkeypatch.delenv(k, raising=False)

        settings = Settings()
        assert settings.poll_interval_seconds == 60
        assert settings.max_jitter_seconds == 15
        assert settings.inter_channel_delay_seconds == 2.0
        assert settings.deepseek_api_base == "https://api.deepseek.com"
        assert settings.deepseek_model == "deepseek-chat"
        assert settings.hotness_threshold == 7
        assert settings.timezone == "Asia/Shanghai"
        assert settings.quiet_hours == "00:00-08:00"
        assert settings.shoulder_hours == "22:00-00:00"
        assert settings.quiet_card_cap == 5
        assert settings.db_path == "data/tg_news.db"
        assert settings.log_level == "INFO"
        assert settings.telegram_channels == []

    def test_comma_separated_channel_string_parsing(self):
        s = Settings(telegram_channels="durov, @telegram, whale_alert , ")
        assert s.telegram_channels == ["durov", "telegram", "whale_alert"]

    def test_list_channel_parsing(self):
        s = Settings(telegram_channels=["@Durov", "TELEGRAM", "  alpha_signals  "])
        assert s.telegram_channels == ["durov", "telegram", "alpha_signals"]

    def test_empty_and_single_channel_parsing(self):
        s_empty = Settings(telegram_channels="")
        assert s_empty.telegram_channels == []

        s_single = Settings(telegram_channels="@tech_news")
        assert s_single.telegram_channels == ["tech_news"]

    def test_json_array_channel_parsing(self):
        s = Settings(telegram_channels='["durov", "@telegram"]')
        assert s.telegram_channels == ["durov", "telegram"]

    def test_secret_str_grok_key_support(self):
        s = Settings(grok_api_key=SecretStr("super-secret-key"))
        assert s.grok_api_key == "super-secret-key"


class TestConfigValidationBounds:
    """Verifies validation rules for thresholds and poll intervals."""

    def test_poll_interval_minimum_validation(self):
        # Valid boundary
        s = Settings(poll_interval_seconds=5)
        assert s.poll_interval_seconds == 5

        # Invalid: < 5
        with pytest.raises(ValidationError):
            Settings(poll_interval_seconds=4)

        with pytest.raises(ValidationError):
            Settings(poll_interval_seconds=0)

        with pytest.raises(ValidationError):
            Settings(poll_interval_seconds=-10)

    def test_hotness_threshold_bounds_validation(self):
        # Valid boundaries (1 - 10)
        s1 = Settings(hotness_threshold=1)
        assert s1.hotness_threshold == 1

        s10 = Settings(hotness_threshold=10)
        assert s10.hotness_threshold == 10

        s7 = Settings(hotness_threshold=7)
        assert s7.hotness_threshold == 7

        # Invalid: < 1 or > 10
        with pytest.raises(ValidationError):
            Settings(hotness_threshold=0)

        with pytest.raises(ValidationError):
            Settings(hotness_threshold=11)


class TestEnvironmentAndSecretSync:
    """Verifies environment variable loading and Feishu secret synchronization."""

    def test_load_from_environment_variables(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("TELEGRAM_CHANNELS", "chan1, chan2")
        monkeypatch.setenv("POLL_INTERVAL_SECONDS", "45")
        monkeypatch.setenv("HOTNESS_THRESHOLD", "9")
        monkeypatch.setenv("GROK_API_KEY", "xai-real-key-abc")
        monkeypatch.setenv("GROK_MODEL", "grok-beta")
        monkeypatch.setenv("FEISHU_WEBHOOK_URL", "https://open.feishu.cn/hook/xyz")
        monkeypatch.setenv("FEISHU_SECRET", "sign-secret-123")
        monkeypatch.setenv("DB_PATH", "custom/storage.db")

        settings = Settings.load()
        assert settings.telegram_channels == ["chan1", "chan2"]
        assert settings.poll_interval_seconds == 45
        assert settings.hotness_threshold == 9
        assert settings.grok_api_key == "xai-real-key-abc"
        assert settings.grok_model == "grok-beta"
        assert settings.feishu_webhook_url == "https://open.feishu.cn/hook/xyz"
        assert settings.feishu_webhook_secret == "sign-secret-123"
        assert settings.feishu_secret == "sign-secret-123"
        assert settings.db_path == "custom/storage.db"

    def test_load_deepseek_from_environment_variables(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-deepseek-env-123")
        monkeypatch.setenv("DEEPSEEK_API_BASE", "https://api.deepseek.com/v1")
        monkeypatch.setenv("DEEPSEEK_MODEL", "deepseek-reasoner")

        settings = Settings.load()
        assert settings.deepseek_api_key == "sk-deepseek-env-123"
        assert settings.deepseek_api_base == "https://api.deepseek.com/v1"
        assert settings.deepseek_model == "deepseek-reasoner"
        assert settings.active_api_key == "sk-deepseek-env-123"
        assert settings.active_api_base == "https://api.deepseek.com/v1"
        assert settings.active_model == "deepseek-reasoner"

    def test_feishu_secret_sync_both_ways(self):
        s1 = Settings(feishu_secret="secret-a")
        assert s1.feishu_webhook_secret == "secret-a"
        assert s1.feishu_secret == "secret-a"

        s2 = Settings(feishu_webhook_secret="secret-b")
        assert s2.feishu_webhook_secret == "secret-b"
        assert s2.feishu_secret == "secret-b"


import shutil
import uuid
from contextlib import contextmanager


@contextmanager
def local_temp_dir():
    tmp_path = Path(__file__).resolve().parent / f".tmp_{uuid.uuid4().hex[:8]}"
    tmp_path.mkdir(parents=True, exist_ok=True)
    try:
        yield tmp_path
    finally:
        shutil.rmtree(tmp_path, ignore_errors=True)


class TestFileConfigurationLoading:
    """Verifies parsing from .env and YAML/JSON configuration files."""

    def test_dotenv_parser_and_loading(self):
        with local_temp_dir() as tmpdir:
            env_path = tmpdir / ".env.test"
            env_path.write_text(
                """
                # Monitoring configuration
                TELEGRAM_CHANNELS="alpha_chan, beta_chan"
                POLL_INTERVAL_SECONDS=90
                HOTNESS_THRESHOLD=8
                GROK_API_KEY="grok-secret-dotenv"
                FEISHU_WEBHOOK_URL=https://open.feishu.cn/test/hook
                """,
                encoding="utf-8",
            )

            # Test parser directly
            parsed_raw = parse_dotenv_file(env_path)
            assert parsed_raw["TELEGRAM_CHANNELS"] == "alpha_chan, beta_chan"
            assert parsed_raw["POLL_INTERVAL_SECONDS"] == "90"

            # Test Settings.load with env_file
            settings = Settings.load(env_file=env_path)
            assert settings.telegram_channels == ["alpha_chan", "beta_chan"]
            assert settings.poll_interval_seconds == 90
            assert settings.hotness_threshold == 8
            assert settings.grok_api_key == "grok-secret-dotenv"
            assert settings.feishu_webhook_url == "https://open.feishu.cn/test/hook"

    def test_yaml_parser_and_loading(self):
        with local_temp_dir() as tmpdir:
            yaml_path = tmpdir / "config.yaml"
            yaml_path.write_text(
                """
                telegram_channels:
                  - durov
                  - telegram
                poll_interval_seconds: 120
                hotness_threshold: 6
                grok_model: grok-beta
                db_path: /custom/path.db
                """,
                encoding="utf-8",
            )

            # Test parser directly
            parsed = parse_yaml_file(yaml_path)
            assert parsed["telegram_channels"] == ["durov", "telegram"]
            assert parsed["poll_interval_seconds"] == 120
            assert parsed["hotness_threshold"] == 6

            # Test Settings.load with config_path
            settings = Settings.load(config_path=yaml_path)
            assert settings.telegram_channels == ["durov", "telegram"]
            assert settings.poll_interval_seconds == 120
            assert settings.hotness_threshold == 6
            assert settings.grok_model == "grok-beta"
            assert settings.db_path == "/custom/path.db"

    def test_precedence_hierarchy(self, monkeypatch: pytest.MonkeyPatch):
        with local_temp_dir() as tmpdir:
            yaml_path = tmpdir / "config.yaml"
            yaml_path.write_text(
                """
                poll_interval_seconds: 100
                hotness_threshold: 5
                log_level: DEBUG
                """,
                encoding="utf-8",
            )

            env_path = tmpdir / ".env"
            env_path.write_text(
                """
                POLL_INTERVAL_SECONDS=80
                HOTNESS_THRESHOLD=6
                """,
                encoding="utf-8",
            )

            # 1. Without env var or override, .env takes precedence over YAML
            s1 = Settings.load(config_path=yaml_path, env_file=env_path)
            assert s1.poll_interval_seconds == 80  # from .env
            assert s1.hotness_threshold == 6       # from .env
            assert s1.log_level == "DEBUG"         # from YAML

            # 2. OS environment variables take precedence over .env and YAML
            monkeypatch.setenv("HOTNESS_THRESHOLD", "8")
            s2 = Settings.load(config_path=yaml_path, env_file=env_path)
            assert s2.hotness_threshold == 8       # from os.environ
            assert s2.poll_interval_seconds == 80  # from .env

            # 3. Explicit kwargs take highest precedence
            s3 = Settings.load(config_path=yaml_path, env_file=env_path, hotness_threshold=10)
            assert s3.hotness_threshold == 10      # from kwargs
            assert s3.poll_interval_seconds == 80



class TestGetConfigFactory:
    """Verifies get_config() helper function."""

    def test_get_config_passes_arguments(self):
        s = get_config(
            telegram_channels="news_wire",
            poll_interval_seconds=25,
            hotness_threshold=9,
        )
        assert s.telegram_channels == ["news_wire"]
        assert s.poll_interval_seconds == 25
        assert s.hotness_threshold == 9


class TestDeepSeekConfig:
    """Verifies pure DeepSeek configuration and defaults."""

    def test_default_deepseek_config(self):
        s = Settings(deepseek_api_key="sk-deepseek-test-key")
        assert s.active_llm_provider == "deepseek"
        assert s.deepseek_api_key == "sk-deepseek-test-key"
        assert s.active_api_key == "sk-deepseek-test-key"
        assert s.deepseek_api_base == "https://api.deepseek.com"
        assert s.active_api_base == "https://api.deepseek.com"
        assert s.deepseek_model == "deepseek-chat"
        assert s.active_model == "deepseek-chat"

    def test_secret_str_deepseek_key_support(self):
        s = Settings(deepseek_api_key=SecretStr("super-secret-deepseek-key"))
        assert s.deepseek_api_key == "super-secret-deepseek-key"
        assert s.active_api_key == "super-secret-deepseek-key"

    def test_custom_deepseek_model_and_base(self):
        s = Settings(
            deepseek_api_key="sk-deepseek-reasoner-key",
            deepseek_api_base="https://api.deepseek.com/v1",
            deepseek_model="deepseek-reasoner",
        )
        assert s.active_llm_provider == "deepseek"
        assert s.active_api_key == "sk-deepseek-reasoner-key"
        assert s.active_api_base == "https://api.deepseek.com/v1"
        assert s.active_model == "deepseek-reasoner"

    def test_legacy_grok_key_mapping_compatibility(self):
        s = Settings(grok_api_key="sk-legacy-key")
        assert s.deepseek_api_key == "sk-legacy-key"
        assert s.active_api_key == "sk-legacy-key"


