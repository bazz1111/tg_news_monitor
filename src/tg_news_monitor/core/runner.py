"""Core orchestration runner for Telegram News Monitor (batch digest mode).

Coordinates:
1. Public Telegram web preview scraping (TelegramScraperClient & TelegramWebParser).
2. Local persistent deduplication store (PostRepository / SQLite).
3. Pending buffer + cheap coarse filter + digest threshold gate.
4. Batch digest evaluation via DeepSeek (one LLM call when gate opens).
5. ONE Feishu card PER DigestItem (multi single cards; no Telegram links).
6. Lifecycle status update in SQLite (evaluation, alert_sent, filtered).
7. Execution modes: single-pass (`run_once`) and continuous monitoring daemon (`run_forever`).
"""

from __future__ import annotations

import logging
import signal
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

try:
    from loguru import logger
except ImportError:
    logger = logging.getLogger(__name__)  # type: ignore

from tg_news_monitor.config import Settings, get_config
from tg_news_monitor.core.filters import (
    _COARSE_EMPTY_MAX_LEN,
    _COARSE_SPAM_RES,
    _CRYPTO_RES,
    _text_looks_crypto,
    coarse_filter_posts,
)
from tg_news_monitor.core.models import DigestBrief, DigestItem, NewsEvaluation, TelegramPost
from tg_news_monitor.evaluator.grok_client import GrokClient
from tg_news_monitor.notifier.feishu_card import FeishuCardBuilder
from tg_news_monitor.notifier.webhook_sender import FeishuWebhookSender
from tg_news_monitor.scraper.client import TelegramScraperClient
from tg_news_monitor.scraper.parser import TelegramWebParser
from tg_news_monitor.storage.repository import PostRepository

# Contract aliases
MessageRepository = PostRepository
GrokEvaluator = GrokClient

# Re-export filter helpers for backward-compatible test/import sites
# (canonical home: tg_news_monitor.core.filters)
__all_filter_exports__ = (
    "_COARSE_EMPTY_MAX_LEN",
    "_COARSE_SPAM_RES",
    "_CRYPTO_RES",
    "_text_looks_crypto",
    "coarse_filter_posts",
)


class NewsMonitorRunner:
    """Daemon runner coordinating ingestion, deduplication, batch digest, and alerts."""

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
                timeout=max(60.0, 30.0),
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

    def _ingest_channel(self, channel: str) -> Tuple[List[TelegramPost], Dict[str, Any]]:
        """Scrape + dedupe + save for one channel. Does NOT evaluate or alert.

        Returns:
            (discovered_posts, channel_stats)
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
            return [], result

        if html is None:
            err_msg = (
                f"Failed to retrieve HTML for @{clean_channel} "
                "(rate limit or network backoff exhausted)"
            )
            logger.warning(err_msg)
            self.storage.update_channel_state(
                channel=clean_channel,
                success=False,
                error=err_msg,
            )
            result["success"] = False
            result["error"] = err_msg
            return [], result

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
            return [], result

        result["posts_seen"] = len(posts)
        if not posts:
            logger.debug(f"No posts extracted from @{clean_channel} HTML.")
            self.storage.update_channel_state(
                channel=clean_channel,
                success=True,
                messages_seen=0,
            )
            return [], result

        try:
            unprocessed_posts = self.storage.filter_unprocessed(clean_channel, posts)
        except TypeError:
            unprocessed_posts = self.storage.filter_unprocessed(posts)

        result["posts_discovered"] = len(unprocessed_posts)
        self.stats["total_posts_discovered"] += len(unprocessed_posts)

        max_message_id = max((p.message_id for p in posts), default=0)
        self.storage.update_channel_state(
            channel=clean_channel,
            last_message_id=max_message_id,
            success=True,
            messages_seen=len(posts),
        )

        if not unprocessed_posts:
            logger.debug(
                f"@{clean_channel}: All {len(posts)} messages already processed. Nothing new."
            )
            return [], result

        logger.info(
            f"@{clean_channel}: Discovered {len(unprocessed_posts)} new unprocessed posts."
        )

        try:
            self.storage.save_posts(unprocessed_posts)
        except Exception as exc:
            logger.warning(
                f"Batch save_posts encountered exception: {exc}. Falling back to individual saves."
            )
            for post in unprocessed_posts:
                self.storage.save_post(post)

        return list(unprocessed_posts), result

    def poll_channel(self, channel: str) -> Dict[str, Any]:
        """Ingest-only channel pass (scrape+dedupe+save). No per-post evaluate/alert.

        Kept for backward-compatible call sites; returns ingest stats only.
        """
        _posts, result = self._ingest_channel(channel)
        return result

    def _selected_keys(self, digest: DigestBrief) -> set:
        keys = set()
        for item in digest.items or []:
            ch = str(item.channel).lower().lstrip("@").strip()
            keys.add((ch, int(item.message_id)))
        return keys

    def _apply_digest_evaluations(
        self,
        posts: List[TelegramPost],
        digest: DigestBrief,
    ) -> None:
        """Mark each post with a short evaluation summary from the digest."""
        selected = {}
        for item in digest.items or []:
            ch = str(item.channel).lower().lstrip("@").strip()
            selected[(ch, int(item.message_id))] = item

        for post in posts:
            ch = post.channel.lower().lstrip("@").strip()
            key = (ch, int(post.message_id))
            item = selected.get(key)
            if item is not None:
                summary = f"[digest#{item.rank}] {item.title}: {item.summary}"
                raw_score = getattr(item, "score", None)
                if raw_score is None:
                    score_val = max(1, min(10, 11 - int(item.rank)))
                else:
                    try:
                        score_val = max(1, min(10, int(raw_score)))
                    except (TypeError, ValueError):
                        score_val = max(1, min(10, 11 - int(item.rank)))
                self.storage.update_evaluation(
                    channel=ch,
                    message_id=post.message_id,
                    score=score_val,
                    summary=summary,
                    alert_sent=False,
                    is_filtered=False,
                    filter_reason=None,
                    key_takeaways=[
                        item.impact_overall,
                        item.impact_us,
                        item.impact_cn,
                        item.impact_commodities,
                    ],
                )
            else:
                note = digest.filtered_note or "filtered"
                self.storage.update_evaluation(
                    channel=ch,
                    message_id=post.message_id,
                    score=1,
                    summary=f"[filtered] {note}"[:500],
                    alert_sent=False,
                    is_filtered=True,
                    filter_reason="digest_filtered",
                    key_takeaways=[],
                )

    def _mark_all_filtered(self, posts: List[TelegramPost], reason: str) -> None:
        for post in posts:
            ch = post.channel.lower().lstrip("@").strip()
            self.storage.update_evaluation(
                channel=ch,
                message_id=post.message_id,
                score=1,
                summary=f"[filtered] {reason}",
                alert_sent=False,
                is_filtered=True,
                filter_reason=reason,
                key_takeaways=[],
            )

    def _send_digest_card(self, digest: DigestBrief) -> bool:
        """Legacy: one combined digest card (kept for compatibility)."""
        payload = FeishuCardBuilder.build_digest_card(digest)
        if hasattr(self.webhook_sender, "send"):
            return bool(self.webhook_sender.send(payload))
        if hasattr(self.webhook_sender, "send_alert"):
            return bool(self.webhook_sender.send_alert(payload))
        logger.error("Webhook sender missing send/send_alert method")
        return False

    def _send_digest_item_card(
        self,
        item: DigestItem,
        published_at: Optional[datetime] = None,
    ) -> bool:
        """Send one Feishu card for a single DigestItem."""
        payload = FeishuCardBuilder.build_digest_item_card(
            item,
            published_at=published_at,
            subtitle="投资情报快报",
        )
        if hasattr(self.webhook_sender, "send"):
            return bool(self.webhook_sender.send(payload))
        if hasattr(self.webhook_sender, "send_alert"):
            return bool(self.webhook_sender.send_alert(payload))
        logger.error("Webhook sender missing send/send_alert method")
        return False

    def _oldest_age_seconds(
        self,
        pending_pairs: List[Tuple[TelegramPost, datetime]],
    ) -> float:
        if not pending_pairs:
            return 0.0
        now = datetime.now(timezone.utc)
        oldest = min(scraped_at for _, scraped_at in pending_pairs)
        if oldest.tzinfo is None:
            oldest = oldest.replace(tzinfo=timezone.utc)
        return max(0.0, (now - oldest).total_seconds())

    def run_once(self) -> Dict[str, Any]:
        """Single pass: ingest all channels, pending+coarse filter, threshold gate, multi cards."""
        channels = self.config.telegram_channels
        if not channels:
            logger.warning(
                "No Telegram channels configured. Set TELEGRAM_CHANNELS in .env or config.yaml."
            )
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

        # 1. Ingest all channels (scrape + dedupe + save)
        for idx, channel in enumerate(channels):
            if self._stop_requested:
                break

            _posts, channel_stat = self._ingest_channel(channel)
            pass_summary["details"].append(channel_stat)
            pass_summary["posts_discovered"] += channel_stat["posts_discovered"]

            if idx < len(channels) - 1 and not self._stop_requested:
                pause = self.config.inter_channel_delay_seconds
                if hasattr(self.scraper_client, "calculate_inter_channel_delay"):
                    pause = self.scraper_client.calculate_inter_channel_delay()
                time.sleep(max(0.1, pause))

        # 2. Load ALL pending posts (this pass new + previously buffered)
        if hasattr(self.storage, "list_pending_with_scraped_at"):
            pending_pairs = self.storage.list_pending_with_scraped_at()
        else:
            pending_posts = (
                self.storage.list_pending_posts()
                if hasattr(self.storage, "list_pending_posts")
                else []
            )
            now = datetime.now(timezone.utc)
            pending_pairs = [(p, now) for p in pending_posts]

        if not pending_pairs:
            logger.info(
                "Batch digest mode: no pending posts (evaluated_at IS NULL); "
                "skipping LLM and Feishu cards."
            )
            return pass_summary

        pending_posts = [p for p, _ in pending_pairs]
        logger.info(
            f"Pending buffer: {len(pending_posts)} unevaluated post(s) after ingest."
        )

        # 3. Cheap local coarse filter (zero LLM): spam + crypto ban
        candidates, spam_dropped, crypto_dropped = coarse_filter_posts(pending_posts)
        if spam_dropped:
            logger.info(
                f"Coarse filter dropped {len(spam_dropped)} spam/noise post(s); "
                f"{len(candidates) + len(crypto_dropped)} remain before crypto filter."
            )
            self._mark_all_filtered(spam_dropped, "coarse_filter")
        if crypto_dropped:
            logger.info(
                f"Crypto filter dropped {len(crypto_dropped)} post(s); "
                f"{len(candidates)} candidate(s) remain."
            )
            self._mark_all_filtered(crypto_dropped, "crypto_filter")

        if not candidates:
            logger.info("No candidates after coarse/crypto filter; skipping LLM and Feishu cards.")
            return pass_summary

        # Rebuild scraped_at map for remaining candidates
        scraped_map = {
            (p.channel.lower().lstrip("@").strip(), int(p.message_id)): scraped_at
            for p, scraped_at in pending_pairs
        }
        candidate_pairs = [
            (
                p,
                scraped_map.get(
                    (p.channel.lower().lstrip("@").strip(), int(p.message_id)),
                    datetime.now(timezone.utc),
                ),
            )
            for p in candidates
        ]

        # 4. Buffering gate
        min_candidates = int(getattr(self.config, "digest_min_candidates", 3) or 3)
        max_wait = int(getattr(self.config, "digest_max_wait_seconds", 900) or 900)
        oldest_age = self._oldest_age_seconds(candidate_pairs)

        if len(candidates) < min_candidates and oldest_age < max_wait:
            logger.info(
                f"Buffering: {len(candidates)} candidate(s) < min_candidates={min_candidates} "
                f"and oldest_age={oldest_age:.0f}s < max_wait={max_wait}s; "
                "skipping LLM, leaving posts unevaluated."
            )
            return pass_summary

        logger.info(
            f"Digest gate open: candidates={len(candidates)} "
            f"(min={min_candidates}), oldest_age={oldest_age:.0f}s "
            f"(max_wait={max_wait}s); calling evaluate_digest."
        )

        # 5. One LLM call for the candidate batch
        if hasattr(self.evaluator, "evaluate_digest"):
            digest = self.evaluator.evaluate_digest(candidates)
        else:
            digest = DigestBrief(
                headline="本轮快讯",
                overview="evaluator 不支持 evaluate_digest",
                items=[],
                has_material_news=False,
                filtered_note="missing_evaluate_digest",
            )

        pass_summary["posts_evaluated"] = len(candidates)
        self.stats["total_posts_evaluated"] += len(candidates)

        # 5b. Hard-drop crypto items even if the model selected them
        if digest.items:
            kept_items = []
            crypto_items = []
            for item in digest.items:
                blob = " ".join(
                    [
                        str(getattr(item, "category", "") or ""),
                        str(getattr(item, "title", "") or ""),
                        str(getattr(item, "summary", "") or ""),
                        " ".join(str(x) for x in (getattr(item, "summary_bullets", None) or [])),
                        str(getattr(item, "actionable_insight", "") or ""),
                    ]
                )
                cat = str(getattr(item, "category", "") or "").strip()
                if cat == "加密货币" or _text_looks_crypto(blob):
                    crypto_items.append(item)
                else:
                    kept_items.append(item)
            if crypto_items:
                logger.info(
                    f"Post-digest crypto filter removed {len(crypto_items)} item(s) "
                    f"before Feishu send."
                )
                for item in crypto_items:
                    ch = str(item.channel).lower().lstrip("@").strip()
                    try:
                        mid = int(item.message_id)
                    except (TypeError, ValueError):
                        continue
                    self.storage.update_evaluation(
                        channel=ch,
                        message_id=mid,
                        score=1,
                        summary="[filtered] crypto_ban",
                        alert_sent=False,
                        is_filtered=True,
                        filter_reason="crypto_filter",
                        key_takeaways=[],
                    )
                digest.items = kept_items
                if not kept_items:
                    digest.has_material_news = False
                    digest.filtered_note = (
                        (digest.filtered_note or "") + "; crypto_ban_all"
                    ).strip("; ")

        # 6. Persist short evaluation summaries
        self._apply_digest_evaluations(candidates, digest)

        # 7. Send ONE card PER DigestItem when material items exist
        if digest.has_material_news and digest.items:
            alerts_ok = 0
            selected = self._selected_keys(digest)
            item_by_key = {}
            for item in digest.items:
                ch = str(item.channel).lower().lstrip("@").strip()
                item_by_key[(ch, int(item.message_id))] = item

            post_by_key = {
                (p.channel.lower().lstrip("@").strip(), int(p.message_id)): p
                for p in candidates
            }

            card_gap = float(
                getattr(self.config, "digest_card_interval_seconds", 10.0) or 0.0
            )
            for idx, item in enumerate(digest.items):
                if idx > 0 and card_gap > 0:
                    logger.info(
                        f"Waiting {card_gap:.0f}s before next Feishu card "
                        f"({idx + 1}/{len(digest.items)})…"
                    )
                    time.sleep(card_gap)
                ch = str(item.channel).lower().lstrip("@").strip()
                mid = int(item.message_id)
                matched_post = post_by_key.get((ch, mid))
                published_at = (
                    matched_post.published_at
                    if matched_post is not None
                    else getattr(item, "published_at", None)
                )
                send_ok = self._send_digest_item_card(item, published_at=published_at)
                if send_ok:
                    alerts_ok += 1
                    summary = f"[digest#{item.rank}] {item.title}"
                    raw_score = getattr(item, "score", None)
                    if raw_score is None:
                        score = max(1, min(10, 11 - int(item.rank)))
                    else:
                        try:
                            score = max(1, min(10, int(raw_score)))
                        except (TypeError, ValueError):
                            score = max(1, min(10, 11 - int(item.rank)))
                    self.storage.mark_alert_sent(
                        channel=ch,
                        message_id=mid,
                        score=score,
                        summary=summary,
                    )
                    logger.info(
                        f"✅ Digest item card sent: #{item.rank} [{item.category}] {item.title!r}"
                    )
                else:
                    logger.error(
                        f"❌ Failed to dispatch digest item card: "
                        f"#{item.rank} {item.title!r}"
                    )
                    self.storage.mark_alert_failed(
                        channel=ch,
                        message_id=mid,
                        error_msg="Feishu item card webhook delivery failed after retries",
                    )

            # Mark non-selected candidates as filtered (already done in _apply_digest_evaluations,
            # but reinforce digest_not_selected for clarity when send path runs)
            for post in candidates:
                ch = post.channel.lower().lstrip("@").strip()
                key = (ch, int(post.message_id))
                if key not in selected:
                    self.storage.update_evaluation(
                        channel=ch,
                        message_id=post.message_id,
                        score=1,
                        summary="[filtered] not_selected_in_digest",
                        alert_sent=False,
                        is_filtered=True,
                        filter_reason="digest_not_selected",
                        key_takeaways=[],
                    )

            pass_summary["alerts_sent"] = alerts_ok
            self.stats["total_alerts_sent"] += alerts_ok
            for detail in pass_summary["details"]:
                if detail.get("posts_discovered", 0) > 0:
                    detail["posts_evaluated"] = detail.get("posts_discovered", 0)
            logger.info(
                f"Multi single-card dispatch done: sent={alerts_ok}/{len(digest.items)} "
                f"(headline={digest.headline!r})"
            )
        else:
            logger.info(
                "Batch digest mode: no material news (empty list / has_material_news=false); "
                "skipping Feishu cards."
            )
            self._mark_all_filtered(candidates, "digest_empty")
            for detail in pass_summary["details"]:
                if detail.get("posts_discovered", 0) > 0:
                    detail["posts_evaluated"] = detail.get("posts_discovered", 0)

        return pass_summary

    def run_forever(self) -> None:
        """Starts the continuous 24/7 monitoring daemon with jittered sleep intervals.

        Handles SIGINT and SIGTERM gracefully.
        """
        min_c = int(getattr(self.config, "digest_min_candidates", 3) or 3)
        max_w = int(getattr(self.config, "digest_max_wait_seconds", 900) or 900)
        card_gap = float(getattr(self.config, "digest_card_interval_seconds", 10.0) or 0.0)
        logger.info("=" * 60)
        logger.info("Starting Telegram News Monitor 24/7 Daemon (batch digest mode)")
        logger.info(f"Target Channels : {self.config.telegram_channels}")
        logger.info(
            f"Poll Interval   : {self.config.poll_interval_seconds}s "
            f"(max jitter: ±{self.config.max_jitter_seconds}s)"
        )
        logger.info(
            f"Alert Mode      : batch LLM + multi single cards; "
            f"min_candidates={min_c}; max_wait={max_w}s; card_gap={card_gap:.0f}s"
        )
        logger.info(
            f"DeepSeek Model  : {self.config.deepseek_model} ({self.config.deepseek_api_base})"
        )
        logger.info(f"Database Path   : {self.config.db_path}")
        logger.info("=" * 60)

        def _signal_handler(sig: int, frame: Any) -> None:
            logger.info(f"Received signal {sig}. Initiating graceful shutdown...")
            self.stop()

        try:
            signal.signal(signal.SIGINT, _signal_handler)
            signal.signal(signal.SIGTERM, _signal_handler)
        except (ValueError, AttributeError):
            pass

        while not self._stop_requested:
            try:
                start_time = time.time()
                summary = self.run_once()
                elapsed = time.time() - start_time

                logger.info(
                    f"[batch digest mode] Completed pass #{self.stats['total_passes']} "
                    f"in {elapsed:.1f}s. "
                    f"Discovered: {summary['posts_discovered']}, "
                    f"Evaluated: {summary['posts_evaluated']}, "
                    f"Alerts Sent: {summary['alerts_sent']} "
                    f"(poll_interval={self.config.poll_interval_seconds}s)"
                )

                if self._stop_requested:
                    break

                delay = self.scraper_client.calculate_jittered_delay(
                    float(self.config.poll_interval_seconds)
                )
                logger.debug(f"Sleeping for {delay:.1f}s until next polling pass...")

                sleep_end = time.time() + delay
                while time.time() < sleep_end and not self._stop_requested:
                    time.sleep(0.5)

            except KeyboardInterrupt:
                logger.info("KeyboardInterrupt received. Stopping daemon...")
                self.stop()
                break
            except Exception as exc:
                logger.error(f"Unexpected error in runner main loop: {exc}")
                time.sleep(5.0)

        logger.info("NewsMonitorRunner daemon loop exited cleanly.")
