"""Core orchestration runner for Telegram News Monitor.

Coordinates:
1. Public Telegram web preview scraping (TelegramScraperClient & TelegramWebParser).
2. Local persistent deduplication store (PostRepository / SQLite).
3. Urgency & newsworthiness scoring via Grok (GrokClient / GrokEvaluator).
4. Threshold gating (score >= hotness_threshold and not is_spam).
5. Interactive card alert dispatch to Feishu (FeishuCardBuilder & FeishuWebhookSender).
6. Lifecycle status update in SQLite (score, summary, alert_sent, timestamps).
7. Execution modes: single-pass (`run_once`) and continuous monitoring daemon (`run_forever`).
"""

from __future__ import annotations

import logging
import signal
import time
from typing import Any, Dict, List, Optional

try:
    from loguru import logger
except ImportError:
    logger = logging.getLogger(__name__)  # type: ignore

from tg_news_monitor.config import Settings, get_config
from tg_news_monitor.core.models import NewsEvaluation, TelegramPost
from tg_news_monitor.evaluator.grok_client import GrokClient
from tg_news_monitor.notifier.feishu_card import FeishuCardBuilder
from tg_news_monitor.notifier.webhook_sender import FeishuWebhookSender
from tg_news_monitor.scraper.client import TelegramScraperClient
from tg_news_monitor.scraper.parser import TelegramWebParser
from tg_news_monitor.storage.repository import PostRepository

# Contract aliases
MessageRepository = PostRepository
GrokEvaluator = GrokClient


class NewsMonitorRunner:
    """Daemon runner coordinating ingestion, deduplication, evaluation, gating, and alerts."""

    def __init__(
        self,
        config: Optional[Settings] = None,
        storage: Optional[PostRepository] = None,
        scraper_client: Optional[TelegramScraperClient] = None,
        evaluator: Optional[GrokClient] = None,
        webhook_sender: Optional[FeishuWebhookSender] = None,
    ) -> None:
        """Initializes runner with configuration and injectable dependencies."""
        self.config = config or get_config()
        self.storage = storage or PostRepository(db_path=self.config.db_path)

        # Scraper client
        if scraper_client is not None:
            self.scraper_client = scraper_client
        else:
            jitter_ratio = (
                min(0.5, self.config.max_jitter_seconds / max(1, self.config.poll_interval_seconds))
                if self.config.poll_interval_seconds > 0
                else 0.2
            )
            self.scraper_client = TelegramScraperClient(
                base_interval=float(self.config.poll_interval_seconds),
                jitter_ratio=jitter_ratio,
            )

        # DeepSeek LLM Evaluator
        if evaluator is not None:
            self.evaluator = evaluator
        else:
            self.evaluator = GrokClient(
                api_key=self.config.deepseek_api_key,
                api_base=self.config.deepseek_api_base,
                model=self.config.deepseek_model,
                provider="deepseek",
            )
            logger.info(
                f"DeepSeek Evaluator initialized: model={self.config.deepseek_model}, "
                f"api_base={self.config.deepseek_api_base}"
            )

        # Feishu webhook sender
        if webhook_sender is not None:
            self.webhook_sender = webhook_sender
        else:
            secret = self.config.feishu_webhook_secret or self.config.feishu_secret
            self.webhook_sender = FeishuWebhookSender(
                webhook_url=self.config.feishu_webhook_url,
                secret=secret,
            )

        self._stop_requested = False
        self.stats: Dict[str, Any] = {
            "total_passes": 0,
            "total_posts_discovered": 0,
            "total_posts_evaluated": 0,
            "total_alerts_sent": 0,
            "errors": [],
        }

    def stop(self) -> None:
        """Signals the continuous daemon loop to exit gracefully."""
        self._stop_requested = True
        logger.info("Graceful stop requested for NewsMonitorRunner.")

    def poll_channel(self, channel: str) -> Dict[str, Any]:
        """Executes a complete monitoring pass for a single target channel.

        1. Fetches public web preview HTML.
        2. Parses Telegram posts.
        3. Filters out already processed posts.
        4. Saves newly discovered raw posts to SQLite.
        5. For each new post:
           - Evaluates via Grok.
           - Updates SQLite with score, summary, and filter status.
           - Gating: if score >= threshold and not spam, sends Feishu interactive card.
           - Updates alert status in SQLite.
        """
        clean_channel = channel.lower().lstrip("@").strip()
        logger.info(f"Checking Telegram channel: @{clean_channel}")

        result: Dict[str, Any] = {
            "channel": clean_channel,
            "success": True,
            "posts_seen": 0,
            "posts_discovered": 0,
            "posts_evaluated": 0,
            "alerts_sent": 0,
            "error": None,
        }

        # 1. Scrape public channel HTML
        try:
            html = self.scraper_client.fetch_channel_html(clean_channel)
        except Exception as exc:
            logger.error(f"Error fetching channel @{clean_channel}: {exc}")
            self.storage.update_channel_state(
                channel=clean_channel,
                success=False,
                error=str(exc),
            )
            result["success"] = False
            result["error"] = str(exc)
            return result

        if html is None:
            err_msg = f"Failed to retrieve HTML for @{clean_channel} (rate limit or network backoff exhausted)"
            logger.warning(err_msg)
            self.storage.update_channel_state(
                channel=clean_channel,
                success=False,
                error=err_msg,
            )
            result["success"] = False
            result["error"] = err_msg
            return result

        # 2. Parse DOM posts
        try:
            posts = TelegramWebParser.parse_channel_page(clean_channel, html)
            if not posts:
                posts = TelegramWebParser.parse_html(html, default_channel=clean_channel)
        except Exception as exc:
            logger.error(f"Error parsing HTML for @{clean_channel}: {exc}")
            self.storage.update_channel_state(
                channel=clean_channel,
                success=False,
                error=f"Parsing error: {exc}",
            )
            result["success"] = False
            result["error"] = f"Parsing error: {exc}"
            return result

        result["posts_seen"] = len(posts)
        if not posts:
            logger.debug(f"No posts extracted from @{clean_channel} HTML.")
            self.storage.update_channel_state(
                channel=clean_channel,
                success=True,
                messages_seen=0,
            )
            return result

        # 3. Filter out already processed posts (Deduplication)
        try:
            unprocessed_posts = self.storage.filter_unprocessed(clean_channel, posts)
        except TypeError:
            # In case storage implementation signature is filter_unprocessed(posts)
            unprocessed_posts = self.storage.filter_unprocessed(posts)

        result["posts_discovered"] = len(unprocessed_posts)
        self.stats["total_posts_discovered"] += len(unprocessed_posts)

        # Update channel state in database
        max_message_id = max((p.message_id for p in posts), default=0)
        self.storage.update_channel_state(
            channel=clean_channel,
            last_message_id=max_message_id,
            success=True,
            messages_seen=len(posts),
        )

        if not unprocessed_posts:
            logger.debug(f"@{clean_channel}: All {len(posts)} messages already processed. Nothing new.")
            return result

        logger.info(f"@{clean_channel}: Discovered {len(unprocessed_posts)} new unprocessed posts.")

        # 4. Save newly discovered raw posts to SQLite
        try:
            self.storage.save_posts(unprocessed_posts)
        except Exception as exc:
            logger.warning(f"Batch save_posts encountered exception: {exc}. Falling back to individual saves.")
            for post in unprocessed_posts:
                self.storage.save_post(post)

        # 5. Process and evaluate each new post
        for post in unprocessed_posts:
            if self._stop_requested:
                break

            try:
                # Evaluate via Grok
                if hasattr(self.evaluator, "evaluate_post"):
                    evaluation = self.evaluator.evaluate_post(post)
                elif hasattr(self.evaluator, "evaluate"):
                    evaluation = self.evaluator.evaluate(post)
                else:
                    raise AttributeError("Evaluator missing evaluate_post / evaluate method")

                result["posts_evaluated"] += 1
                self.stats["total_posts_evaluated"] += 1

                score = evaluation.score
                is_spam = bool(getattr(evaluation, "is_spam", False))
                is_news = bool(getattr(evaluation, "is_news", True))

                # Determine summary string
                if evaluation.summary_bullets:
                    summary_text = "\n".join(f"• {b}" for b in evaluation.summary_bullets)
                else:
                    summary_text = getattr(evaluation, "title", post.text[:200])

                # Filtering decision
                is_filtered = is_spam or (not is_news) or (score < self.config.hotness_threshold)
                filter_reason = None
                if is_spam:
                    filter_reason = "spam"
                elif not is_news:
                    filter_reason = "not_news"
                elif score < self.config.hotness_threshold:
                    filter_reason = f"below_threshold_{score}_lt_{self.config.hotness_threshold}"

                # Update SQLite post record with evaluation details
                self.storage.update_evaluation(
                    channel=clean_channel,
                    message_id=post.message_id,
                    score=score,
                    summary=summary_text,
                    alert_sent=False,
                    is_filtered=is_filtered,
                    filter_reason=filter_reason,
                    key_takeaways=getattr(evaluation, "key_takeaways", []),
                )

                # 6. Threshold Gating & Alert Dispatch
                if score >= self.config.hotness_threshold and not is_spam:
                    logger.info(
                        f"🚨 High urgency post detected! Channel: @{clean_channel}, ID: #{post.message_id}, Score: {score}/10 >= {self.config.hotness_threshold}"
                    )
                    card_payload = FeishuCardBuilder.build_card(post, evaluation)

                    # Send to Feishu
                    send_success = False
                    if hasattr(self.webhook_sender, "send"):
                        send_success = self.webhook_sender.send(card_payload)
                    elif hasattr(self.webhook_sender, "send_alert"):
                        send_success = self.webhook_sender.send_alert(card_payload)
                    elif hasattr(self.webhook_sender, "send_card"):
                        send_success = self.webhook_sender.send_card(post, evaluation)

                    if send_success:
                        self.storage.mark_alert_sent(
                            channel=clean_channel,
                            message_id=post.message_id,
                            score=score,
                            summary=summary_text,
                        )
                        result["alerts_sent"] += 1
                        self.stats["total_alerts_sent"] += 1
                        logger.info(f"✅ Feishu interactive alert dispatched for @{clean_channel}/#{post.message_id}")
                    else:
                        logger.error(f"❌ Failed to dispatch Feishu alert for @{clean_channel}/#{post.message_id}")
                        self.storage.mark_alert_failed(
                            channel=clean_channel,
                            message_id=post.message_id,
                            error_msg="Feishu webhook delivery failed after retries",
                        )
                else:
                    logger.debug(
                        f"Post @{clean_channel}/#{post.message_id} filtered out (score: {score}/{self.config.hotness_threshold}, is_spam: {is_spam}). Stored without webhook alert."
                    )

            except Exception as exc:
                logger.error(f"Error evaluating post @{clean_channel}/#{post.message_id}: {exc}")
                self.storage.mark_alert_failed(
                    channel=clean_channel,
                    message_id=post.message_id,
                    error_msg=f"Evaluation exception: {exc}",
                )

        return result

    def run_once(self) -> Dict[str, Any]:
        """Executes a single polling iteration across all configured Telegram channels.

        Returns:
            Dict containing pass statistics:
            {"channels_polled": int, "posts_discovered": int, "posts_evaluated": int, "alerts_sent": int, "details": list}
        """
        channels = self.config.telegram_channels
        if not channels:
            logger.warning("No Telegram channels configured. Set TELEGRAM_CHANNELS in .env or config.yaml.")
            return {
                "channels_polled": 0,
                "posts_discovered": 0,
                "posts_evaluated": 0,
                "alerts_sent": 0,
                "details": [],
            }

        self.stats["total_passes"] += 1
        pass_summary: Dict[str, Any] = {
            "channels_polled": len(channels),
            "posts_discovered": 0,
            "posts_evaluated": 0,
            "alerts_sent": 0,
            "details": [],
        }

        for idx, channel in enumerate(channels):
            if self._stop_requested:
                break

            channel_stat = self.poll_channel(channel)
            pass_summary["details"].append(channel_stat)
            pass_summary["posts_discovered"] += channel_stat["posts_discovered"]
            pass_summary["posts_evaluated"] += channel_stat["posts_evaluated"]
            pass_summary["alerts_sent"] += channel_stat["alerts_sent"]

            # Inter-channel randomized pause
            if idx < len(channels) - 1 and not self._stop_requested:
                pause = self.config.inter_channel_delay_seconds
                if hasattr(self.scraper_client, "calculate_inter_channel_delay"):
                    pause = self.scraper_client.calculate_inter_channel_delay()
                time.sleep(max(0.1, pause))

        return pass_summary

    def run_forever(self) -> None:
        """Starts the continuous 24/7 monitoring daemon with jittered sleep intervals.

        Handles SIGINT and SIGTERM gracefully.
        """
        logger.info("=" * 60)
        logger.info("Starting Telegram News Monitor 24/7 Daemon")
        logger.info(f"Target Channels : {self.config.telegram_channels}")
        logger.info(f"Poll Interval   : {self.config.poll_interval_seconds}s (max jitter: ±{self.config.max_jitter_seconds}s)")
        logger.info(f"Hotness Gating  : {self.config.hotness_threshold}/10")
        logger.info(f"Grok Model      : {self.config.grok_model} ({self.config.grok_api_base})")
        logger.info(f"Database Path   : {self.config.db_path}")
        logger.info("=" * 60)

        # Register signal handlers for graceful exit
        def _signal_handler(sig: int, frame: Any) -> None:
            logger.info(f"Received signal {sig}. Initiating graceful shutdown...")
            self.stop()

        try:
            signal.signal(signal.SIGINT, _signal_handler)
            signal.signal(signal.SIGTERM, _signal_handler)
        except (ValueError, AttributeError):
            # Non-main thread or unsupported platform
            pass

        while not self._stop_requested:
            try:
                start_time = time.time()
                summary = self.run_once()
                elapsed = time.time() - start_time

                logger.info(
                    f"Completed pass #{self.stats['total_passes']} in {elapsed:.1f}s. "
                    f"Discovered: {summary['posts_discovered']}, "
                    f"Evaluated: {summary['posts_evaluated']}, "
                    f"Alerts Sent: {summary['alerts_sent']}"
                )

                if self._stop_requested:
                    break

                # Calculate randomized jittered sleep
                delay = self.scraper_client.calculate_jittered_delay(
                    float(self.config.poll_interval_seconds)
                )
                logger.debug(f"Sleeping for {delay:.1f}s until next polling pass...")

                # Sleep in short increments to respond quickly to shutdown signals
                sleep_end = time.time() + delay
                while time.time() < sleep_end and not self._stop_requested:
                    time.sleep(0.5)

            except KeyboardInterrupt:
                logger.info("KeyboardInterrupt received. Stopping daemon...")
                self.stop()
                break
            except Exception as exc:
                logger.error(f"Unexpected error in runner main loop: {exc}")
                # Brief backoff before resuming loop
                time.sleep(5.0)

        logger.info("NewsMonitorRunner daemon loop exited cleanly.")
