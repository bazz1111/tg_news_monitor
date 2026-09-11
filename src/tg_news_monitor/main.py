"""Command-line interface entrypoint for Telegram News Monitor.

Usage:
    python -m tg_news_monitor.main [OPTIONS]

Options:
    --once              Run a single monitoring pass and exit.
    --config PATH, -c   Path to custom configuration file (.yaml, .json, or .env).
    --init-db           Initialize SQLite database schema and tables, then exit.
    --healthcheck       Check recent ingest/eval heartbeats and exit (0=ok).
    --version, -v       Print application version and exit.
"""

from __future__ import annotations

import argparse
import logging
import os
import re
import sys
import time
from pathlib import Path
from typing import List, Optional

try:
    from loguru import logger
    HAS_LOGURU = True
except ImportError:
    HAS_LOGURU = False
    logger = logging.getLogger("tg_news_monitor")  # type: ignore

from tg_news_monitor.__version__ import __version__
from tg_news_monitor.config import Settings, get_config
from tg_news_monitor.core.runner import NewsMonitorRunner
from tg_news_monitor.storage.database import (
    InstanceLock,
    get_heartbeat,
    init_db,
    instance_lock_path,
)


_SECRET_FIELD = re.compile(
    r"(?i)(api[_-]?key|app_secret|webhook_secret|secret|token|password)\s*[:=]\s*['\"]?([^\s'\"]+)"
)
_SECRET_TOKEN = re.compile(r"(?i)\b(sk-|cb-|cli_|Bearer\s+)[A-Za-z0-9_\-]{6,}")
_FEISHU_HOOK = re.compile(r"(https://open\.feishu\.cn/open-apis/bot/v2/hook/)[A-Za-z0-9_\-]+")


def redact_secrets(text: str) -> str:
    """Strip secret values from user-visible errors. Do not print key prefixes."""
    if not text:
        return text
    out = _SECRET_FIELD.sub(lambda m: f"{m.group(1)}=***", text)
    out = _SECRET_TOKEN.sub(lambda m: f"{m.group(1)}***", out)
    out = _FEISHU_HOOK.sub(r"\1***", out)
    return out


def setup_logging(level_name: str = "INFO") -> None:
    """Configures application logging verbosity."""
    numeric_level = getattr(logging, level_name.upper(), logging.INFO)
    logging.basicConfig(
        level=numeric_level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    if HAS_LOGURU:
        try:
            logger.remove()
            logger.add(
                sys.stderr,
                level=level_name.upper(),
                format="<green>{time:YYYY-MM-DD HH:mm:ss}</green> | <level>{level: <8}</level> | <cyan>{name}</cyan>:<cyan>{line}</cyan> - <level>{message}</level>",
            )
        except Exception:
            pass


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    """Parses command-line arguments."""
    parser = argparse.ArgumentParser(
        prog="tg_news_monitor",
        description="24/7 automated Telegram news monitoring and breaking-event alerting service.",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Run a single polling pass across target channels and exit (ideal for cron or test runs).",
    )
    parser.add_argument(
        "--config",
        "-c",
        type=str,
        default=None,
        help="Path to YAML, JSON, or .env configuration file.",
    )
    parser.add_argument(
        "--init-db",
        action="store_true",
        help="Initialize SQLite database schema and tables, then exit immediately.",
    )
    parser.add_argument(
        "--healthcheck",
        action="store_true",
        help="Exit 0 if recent ingest/eval heartbeats are fresh; non-zero if stale or missing.",
    )
    parser.add_argument(
        "--version",
        "-v",
        action="version",
        version=f"%(prog)s {__version__}",
        help="Show version information and exit.",
    )

    return parser.parse_args(argv)


def _is_dotenv_path(path: Optional[str]) -> bool:
    """Treat as dotenv only when the suffix is exactly .env (not a substring 'env')."""
    if not path:
        return False
    return Path(path).suffix.lower() == ".env"


def _healthcheck_max_age(config: Settings) -> float:
    override = int(getattr(config, "healthcheck_max_age_seconds", 0) or 0)
    if override > 0:
        return float(override)
    poll = max(5, int(getattr(config, "poll_interval_seconds", 60) or 60))
    jitter = max(0, int(getattr(config, "max_jitter_seconds", 15) or 0))
    return float(max(180, poll * 3 + jitter + 60))


def run_healthcheck(config: Settings) -> int:
    """Business heartbeat: recent successful ingest, and a recent eval pass."""
    db_path = config.db_path
    if not db_path or db_path == ":memory:" or not os.path.exists(db_path):
        print("healthcheck: database missing or not initialized", file=sys.stderr)
        return 1
    now = time.time()
    max_age = _healthcheck_max_age(config)
    ingest = get_heartbeat(db_path, "ingest")
    eval_at = get_heartbeat(db_path, "eval")
    if ingest is None:
        print("healthcheck: no ingest heartbeat", file=sys.stderr)
        return 1
    ingest_age = now - ingest
    if ingest_age > max_age:
        print(f"healthcheck: ingest heartbeat stale ({ingest_age:.0f}s > {max_age:.0f}s)", file=sys.stderr)
        return 1
    if eval_at is None:
        print("healthcheck: no eval heartbeat", file=sys.stderr)
        return 1
    eval_age = now - eval_at
    eval_limit = max(max_age, float(int(getattr(config, "digest_max_wait_seconds", 900) or 900) + 120))
    if eval_age > eval_limit:
        print(f"healthcheck: eval heartbeat stale ({eval_age:.0f}s > {eval_limit:.0f}s)", file=sys.stderr)
        return 1
    print(f"healthcheck: ok ingest_age={ingest_age:.0f}s eval_age={eval_age:.0f}s")
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    """Main CLI entrypoint execution flow."""
    args = parse_args(argv)

    try:
        config_path = args.config
        env_path = None
        if _is_dotenv_path(config_path):
            env_path = config_path
            config_path = None

        config = get_config(config_path=config_path, env_file=env_path)
    except Exception as exc:
        print(f"Configuration Error: {redact_secrets(str(exc))}", file=sys.stderr)
        return 1

    setup_logging(config.log_level)

    if args.healthcheck:
        return run_healthcheck(config)

    if args.init_db:
        logger.info(f"Initializing SQLite database schema at: {config.db_path}")
        try:
            init_db(config.db_path, default_group_id=config.legacy_group_id)
            logger.info("Database schema initialized successfully.")
            return 0
        except Exception as exc:
            logger.error(f"Database initialization failed: {redact_secrets(str(exc))}")
            return 1

    runtime_errors = config.runtime_validation_errors(require_webhook=True)
    if runtime_errors:
        for err in runtime_errors:
            logger.error(f"Configuration Error: {redact_secrets(err)}")
        return 1

    lock: Optional[InstanceLock] = None
    if config.db_path and config.db_path != ":memory:":
        lock = InstanceLock(instance_lock_path(config.db_path))
        try:
            lock.acquire()
        except RuntimeError as exc:
            logger.error(str(exc))
            return 1

    try:
        runner = NewsMonitorRunner(config=config)
    except Exception as exc:
        if lock:
            lock.release()
        logger.error(f"Failed to initialize NewsMonitorRunner: {redact_secrets(str(exc))}")
        return 1

    try:
        if args.once:
            logger.info("Running single monitoring pass (--once mode)...")
            try:
                summary = runner.run_once()
                logger.info(
                    f"Single pass completed: channels={summary['channels_polled']}, "
                    f"discovered={summary['posts_discovered']}, "
                    f"evaluated={summary['posts_evaluated']}, "
                    f"alerts_sent={summary['alerts_sent']}"
                )
                return 0
            except Exception as exc:
                logger.error(f"Single pass execution failed: {redact_secrets(str(exc))}")
                return 1

        try:
            runner.run_forever()
            return 0
        except KeyboardInterrupt:
            logger.info("Daemon interrupted by user.")
            return 0
        except Exception as exc:
            logger.error(f"Fatal daemon execution error: {redact_secrets(str(exc))}")
            return 1
    finally:
        if lock:
            lock.release()


if __name__ == "__main__":
    sys.exit(main())
