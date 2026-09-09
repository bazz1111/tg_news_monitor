"""Telegram scraper and HTML parser package."""

from tg_news_monitor.scraper.client import TelegramScraperClient
from tg_news_monitor.scraper.parser import TelegramWebParser

__all__ = ["TelegramScraperClient", "TelegramWebParser"]
