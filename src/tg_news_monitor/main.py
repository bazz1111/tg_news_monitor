"""Command-line interface entrypoint for Telegram News Monitor.

Usage:
    python -m tg_news_monitor.main [OPTIONS]

Options:
    --once              Run a single monitoring pass and exit.
    --config PATH, -c   Path to custom configuration file (.yaml, .json, or .env).
    --init-db           Initialize SQLite database schema and tables, then exit.
    --version, -v       Print application version and exit.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
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
from tg_news_monitor.storage.database import init_db


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
        "--version",
        "-v",
        action="version",
        version=f"%(prog)s {__version__}",
        help="Show version information and exit.",
    )

    return parser.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> int:
    """Main CLI entrypoint execution flow."""
    args = parse_args(argv)

    # Load configuration
    try:
        config_path = args.config
        env_path = None
        if config_path and (config_path.endswith(".env") or "env" in Path(config_path).name):
            env_path = config_path
            config_path = None

        config = get_config(config_path=config_path, env_file=env_path)
    except Exception as exc:
        print(f"Configuration Error: {exc}", file=sys.stderr)
        return 1

    # Configure logging
    setup_logging(config.log_level)

    # 1. Database initialization mode
    if args.init_db:
        logger.info(f"Initializing SQLite database schema at: {config.db_path}")
        try:
            init_db(config.db_path, default_group_id=config.legacy_group_id)
            logger.info("Database schema initialized successfully.")
            return 0
        except Exception as exc:
            logger.error(f"Database initialization failed: {exc}")
            return 1

    runtime_errors = config.runtime_validation_errors(require_webhook=True)
    if runtime_errors:
        for err in runtime_errors:
            logger.error(f"Configuration Error: {err}")
        return 1

    # Create and configure runner
    try:
        runner = NewsMonitorRunner(config=config)
    except Exception as exc:
        logger.error(f"Failed to initialize NewsMonitorRunner: {exc}")
        return 1

    # 2. Single-pass execution mode (--once)
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
            logger.error(f"Single pass execution failed: {exc}")
            return 1

    # 3. Continuous background daemon loop (default)
    try:
        runner.run_forever()
        return 0
    except KeyboardInterrupt:
        logger.info("Daemon interrupted by user.")
        return 0
    except Exception as exc:
        logger.error(f"Fatal daemon execution error: {exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
