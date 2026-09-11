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
                or k.startswith("CODEBUDDY_")
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
        assert settings.codebuddy_model == "deepseek-v4.1-flash"
        assert settings.codebuddy_fallback_model == "hy3"
        assert settings.codebuddy_cli == "codebuddy"
        assert settings.codebuddy_effort == "max"
        assert settings.codebuddy_autocompact == "auto"
        assert settings.codebuddy_timeout == 300.0
        assert settings.hotness_threshold == 7
        assert settings.news_max_age_seconds == 1800
        assert settings.scrape_history_pages == 1
        assert settings.scrape_history_max_new_posts is None
        assert settings.timezone == "Asia/Shanghai"
        assert settings.quiet_hours == "00:00-08:00"
        assert settings.shoulder_hours == "22:00-00:00"
        assert settings.quiet_card_cap == 5
        assert settings.db_path == "data/tg_news.db"
        assert settings.log_level == "INFO"
        assert settings.telegram_channels == []
        assert settings.feishu_app_id == ""
        assert settings.feishu_app_secret == ""

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

    def test_secret_str_codebuddy_key_support(self):
        s = Settings(codebuddy_api_key=SecretStr("super-secret-key"))
        assert s.codebuddy_api_key == "super-secret-key"


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

    def test_news_max_age_zero_and_scrape_history_knobs(self):
        s = Settings(
            news_max_age_seconds=0,
            scrape_history_pages=1,
            groups=[
                {
                    "id": "old_photos",
                    "channels": ["oldpix"],
                    "webhook_url": "https://example.com/photos",
                    "news_max_age_seconds": 0,
                    "scrape_history_pages": 10,
                    "scrape_history_max_new_posts": 40,
                },
                {
                    "id": "news24",
                    "channels": ["wire"],
                    "webhook_url": "https://example.com/news24",
                },
            ],
        )
        photos = s.group_settings("old_photos")
        news = s.group_settings("news24")
        assert photos.news_max_age_seconds == 0
        assert photos.scrape_history_pages == 10
        assert photos.scrape_history_max_new_posts == 40
        assert news.news_max_age_seconds == 0
        assert news.scrape_history_pages == 1
        assert news.scrape_history_max_new_posts is None
        assert Settings(news_max_age_seconds=0).news_max_age_seconds == 0
        with pytest.raises(ValidationError):
            Settings(news_max_age_seconds=-1)
        with pytest.raises(ValidationError):
            Settings(scrape_history_pages=0)


class TestEnvironmentAndSecretSync:
    """Verifies environment variable loading and Feishu secret synchronization."""

    def test_load_from_environment_variables(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("TELEGRAM_CHANNELS", "chan1, chan2")
        monkeypatch.setenv("POLL_INTERVAL_SECONDS", "45")
        monkeypatch.setenv("HOTNESS_THRESHOLD", "9")
        monkeypatch.setenv("CODEBUDDY_API_KEY", "cb-real-key-abc")
        monkeypatch.setenv("CODEBUDDY_MODEL", "fast-model")
        monkeypatch.setenv("CODEBUDDY_FALLBACK_MODEL", "hy3")
        monkeypatch.setenv("FEISHU_WEBHOOK_URL", "https://open.feishu.cn/hook/xyz")
        monkeypatch.setenv("FEISHU_SECRET", "sign-secret-123")
        monkeypatch.setenv("FEISHU_APP_ID", "cli_test_app")
        monkeypatch.setenv("FEISHU_APP_SECRET", "test_app_secret")
        monkeypatch.setenv("DB_PATH", "custom/storage.db")

        settings = Settings.load()
        assert settings.telegram_channels == ["chan1", "chan2"]
        assert settings.poll_interval_seconds == 45
        assert settings.hotness_threshold == 9
        assert settings.codebuddy_api_key == "cb-real-key-abc"
        assert settings.codebuddy_model == "fast-model"
        assert settings.codebuddy_fallback_model == "hy3"
        assert settings.feishu_webhook_url == "https://open.feishu.cn/hook/xyz"
        assert settings.feishu_webhook_secret == "sign-secret-123"
        assert settings.feishu_secret == "sign-secret-123"
        assert settings.feishu_app_id == "cli_test_app"
        assert settings.feishu_app_secret == "test_app_secret"
        assert settings.db_path == "custom/storage.db"

    def test_load_codebuddy_from_environment_variables(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("CODEBUDDY_API_KEY", "cb-env-123")
        monkeypatch.setenv("CODEBUDDY_MODEL", "fast-model")
        monkeypatch.setenv("CODEBUDDY_FALLBACK_MODEL", "hy3")
        monkeypatch.setenv("CODEBUDDY_EFFORT", "high")
        monkeypatch.setenv("CODEBUDDY_AUTOCOMPACT", "aggressive")
        monkeypatch.setenv("CODEBUDDY_TIMEOUT", "420")

        settings = Settings.load()
        assert settings.codebuddy_api_key == "cb-env-123"
        assert settings.codebuddy_model == "fast-model"
        assert settings.codebuddy_fallback_model == "hy3"
        assert settings.codebuddy_effort == "high"
        assert settings.codebuddy_autocompact == "aggressive"
        assert settings.codebuddy_timeout == 420.0
        assert settings.active_api_key == "cb-env-123"
        assert settings.active_model == "fast-model"
        assert settings.active_fallback_model == "hy3"
        assert settings.active_llm_provider == "codebuddy"

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
                CODEBUDDY_API_KEY="cb-secret-dotenv"
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
            assert settings.codebuddy_api_key == "cb-secret-dotenv"
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
                codebuddy_model: fast-model
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
            assert settings.codebuddy_model == "fast-model"
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


class TestCodeBuddyConfig:
    """Verifies CodeBuddy CLI configuration and defaults."""

    def test_default_codebuddy_config(self):
        s = Settings(codebuddy_api_key="cb-test-key")
        assert s.active_llm_provider == "codebuddy"
        assert s.codebuddy_api_key == "cb-test-key"
        assert s.active_api_key == "cb-test-key"
        assert s.codebuddy_model == "deepseek-v4.1-flash"
        assert s.active_model == "deepseek-v4.1-flash"
        assert s.codebuddy_fallback_model == "hy3"
        assert s.active_fallback_model == "hy3"
        assert s.codebuddy_effort == "max"
        assert s.codebuddy_autocompact == "auto"
        assert s.codebuddy_timeout == 300.0

    def test_secret_str_codebuddy_key_on_settings(self):
        s = Settings(codebuddy_api_key=SecretStr("super-secret-codebuddy-key"))
        assert s.codebuddy_api_key == "super-secret-codebuddy-key"
        assert s.active_api_key == "super-secret-codebuddy-key"

    def test_custom_codebuddy_models(self):
        s = Settings(
            codebuddy_api_key="cb-custom-key",
            codebuddy_model="fast-model",
            codebuddy_fallback_model="hy3",
        )
        assert s.active_llm_provider == "codebuddy"
        assert s.active_api_key == "cb-custom-key"
        assert s.active_model == "fast-model"
        assert s.active_fallback_model == "hy3"

    def test_legacy_deepseek_keys_are_ignored(self):
        s = Settings(deepseek_api_key="sk-legacy-key")
        assert s.codebuddy_api_key == ""
        assert s.active_api_key == ""
        assert s.active_llm_provider == "codebuddy"


