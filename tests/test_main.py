"""CLI entry: --healthcheck, config failures, .env suffix, instance lock."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from tg_news_monitor.main import _is_dotenv_path, main, redact_secrets, run_healthcheck
from tg_news_monitor.storage.database import InstanceLock, init_db, record_heartbeat


def test_dotenv_suffix_is_exact():
    assert _is_dotenv_path("secrets.env")
    assert _is_dotenv_path("/tmp/prod.env")
    assert not _is_dotenv_path("environment.yaml")
    assert not _is_dotenv_path("config.env.yaml")
    assert not _is_dotenv_path("env.yaml")
    assert not _is_dotenv_path(None)


def test_redact_secrets_hides_values_and_prefixes():
    text = "codebuddy_api_key=cb-supersecretvalue FEISHU_APP_SECRET=abc123"
    out = redact_secrets(text)
    assert "supersecret" not in out
    assert "abc123" not in out
    assert "***" in out
    hook = redact_secrets("https://open.feishu.cn/open-apis/bot/v2/hook/abc-live-token")
    assert "abc-live-token" not in hook


def test_main_invalid_yaml_fails_startup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    bad = tmp_path / "config.yaml"
    bad.write_text("groups: [\n  - id: broken\n    channels: [", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("CONFIG_PATH", raising=False)
    assert main(["--config", str(bad), "--init-db"]) == 1


def test_main_config_groups_must_be_list(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    cfg = tmp_path / "config.yaml"
    cfg.write_text("groups:\n  news24:\n    channels: [wire]\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    assert main(["--config", str(cfg), "--init-db"]) == 1


def test_healthcheck_missing_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from tg_news_monitor.config import Settings

    monkeypatch.chdir(tmp_path)
    config = Settings(_from_load=True, db_path=str(tmp_path / "missing.db"), telegram_channels=[])
    assert run_healthcheck(config) == 1


def test_healthcheck_fresh_heartbeats(tmp_path: Path):
    from tg_news_monitor.config import Settings

    db = tmp_path / "ok.db"
    init_db(str(db))
    record_heartbeat(str(db), "ingest")
    record_heartbeat(str(db), "eval")
    config = Settings(_from_load=True, db_path=str(db), telegram_channels=[])
    assert run_healthcheck(config) == 0


def test_healthcheck_stale_ingest(tmp_path: Path):
    from tg_news_monitor.config import Settings

    db = tmp_path / "stale.db"
    init_db(str(db))
    record_heartbeat(str(db), "ingest", now_ts=1.0)
    record_heartbeat(str(db), "eval", now_ts=1.0)
    config = Settings(
        _from_load=True,
        db_path=str(db),
        telegram_channels=[],
        healthcheck_max_age_seconds=30,
    )
    assert run_healthcheck(config) == 1


def test_instance_lock_second_process_fails(tmp_path: Path):
    import subprocess
    import sys

    lock_path = str(tmp_path / "app.lock")
    first = InstanceLock(lock_path)
    first.acquire()
    try:
        env = os.environ.copy()
        src = str(Path(__file__).resolve().parents[1] / "src")
        env["PYTHONPATH"] = src + os.pathsep + env.get("PYTHONPATH", "")
        probe = (
            "from tg_news_monitor.storage.database import InstanceLock\n"
            f"lock = InstanceLock({lock_path!r})\n"
            "try:\n"
            "    lock.acquire()\n"
            "    print('GOT')\n"
            "except RuntimeError:\n"
            "    print('BLOCKED')\n"
        )
        result = subprocess.run(
            [sys.executable, "-c", probe],
            check=False,
            capture_output=True,
            text=True,
            env=env,
            timeout=10,
        )
        assert "BLOCKED" in result.stdout
    finally:
        first.release()


def test_main_healthcheck_cli(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    db = tmp_path / "cli.db"
    init_db(str(db))
    record_heartbeat(str(db), "ingest")
    record_heartbeat(str(db), "eval")
    env = tmp_path / "app.env"
    env.write_text(f"DB_PATH={db}\nTELEGRAM_CHANNELS=\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("DB_PATH", raising=False)
    assert main(["--config", str(env), "--healthcheck"]) == 0
