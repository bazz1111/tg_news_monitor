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
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

try:
    from loguru import logger
except ImportError:
    logger = logging.getLogger(__name__)  # type: ignore

from tg_news_monitor.config import (
    CardProfile,
    DEFAULT_LEGACY_GROUP_ID,
    Group,
    Settings,
    get_config,
)
from tg_news_monitor.core.policy import DeliveryPolicy, fingerprint, is_fresh, urgent
from tg_news_monitor.core import schedule as alert_schedule
from tg_news_monitor.core.quiet_hours import QuietHours, BEIJING
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
from tg_news_monitor.notifier.card_format import format_morning_item_md
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
        self.storage = storage or PostRepository(
            db_path=self.config.db_path,
            default_group_id=getattr(self.config, "legacy_group_id", DEFAULT_LEGACY_GROUP_ID),
        )

        self._policies: Dict[str, DeliveryPolicy] = {}
        self._quiets: Dict[str, QuietHours] = {}
        self._senders: Dict[str, Any] = {}
        self._group_id = getattr(self.config, "legacy_group_id", DEFAULT_LEGACY_GROUP_ID)
        self._group_config: Any = self.config
        self._card_profile = CardProfile()
        self._active_group: Optional[Group] = None

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
                timeout=60.0,
                max_retries=1,
            )
            logger.info(
                f"DeepSeek Evaluator initialized: model={self.config.deepseek_model}, "
                f"api_base={self.config.deepseek_api_base}"
            )

        self._init_group_runtimes(webhook_sender)

        self._stop_requested = False
        self.stats: Dict[str, Any] = {
            "total_passes": 0,
            "total_posts_discovered": 0,
            "total_posts_evaluated": 0,
            "total_alerts_sent": 0,
            "errors": [],
        }

    def _init_group_runtimes(self, webhook_sender: Optional[FeishuWebhookSender]) -> None:
        """Build per-group policy / quiet / webhook objects. Injected sender is shared (tests)."""
        legacy = getattr(self.config, "legacy_group_id", DEFAULT_LEGACY_GROUP_ID)
        enabled = list(self.config.enabled_groups()) if hasattr(self.config, "enabled_groups") else []
        if webhook_sender is not None:
            default_sender = webhook_sender
        else:
            secret = self.config.feishu_webhook_secret or self.config.feishu_secret
            default_sender = FeishuWebhookSender(
                webhook_url=self.config.feishu_webhook_url,
                secret=secret,
                max_retries=1,
            )

        if not enabled:
            self.policy = DeliveryPolicy(self.storage.db_path, group_id=legacy)
            self.quiet = QuietHours(self.storage.db_path, self.config, group_id=legacy)
            self.webhook_sender = default_sender
            self._policies[legacy] = self.policy
            self._quiets[legacy] = self.quiet
            self._senders[legacy] = default_sender
            self._activate_group(None)
            return

        env_lookup = getattr(self.config, "env_lookup", None)
        for group in enabled:
            view = self.config.group_settings(group)
            self._policies[group.id] = DeliveryPolicy(self.storage.db_path, group_id=group.id)
            self._quiets[group.id] = QuietHours(self.storage.db_path, view, group_id=group.id)
            if webhook_sender is not None:
                self._senders[group.id] = webhook_sender
            else:
                self._senders[group.id] = FeishuWebhookSender(
                    webhook_url=group.resolve_webhook_url(env_lookup),
                    secret=group.resolve_webhook_secret(env_lookup),
                    max_retries=1,
                )
        self._activate_group(enabled[0])

    def _activate_group(self, group: Optional[Group]) -> None:
        if group is None:
            self._active_group = None
            self._group_id = getattr(self.config, "legacy_group_id", DEFAULT_LEGACY_GROUP_ID)
            self._group_config = self.config
            self._card_profile = CardProfile()
        else:
            self._active_group = group
            self._group_id = group.id
            self._group_config = self.config.group_settings(group)
            self._card_profile = group.resolved_card_profile()
        if self._group_id in self._policies:
            self.policy = self._policies[self._group_id]
        if self._group_id in self._quiets:
            self.quiet = self._quiets[self._group_id]
        if self._group_id in self._senders:
            self.webhook_sender = self._senders[self._group_id]

    def _process_targets(self) -> List[Optional[Group]]:
        groups = list(self.config.enabled_groups()) if hasattr(self.config, "enabled_groups") else []
        if groups:
            return groups
        # groups configured but all disabled/empty: do not fall back to TELEGRAM_CHANNELS
        if getattr(self.config, "groups", None):
            return []
        return [None]

    def _glog(self, message: str, level: str = "info") -> None:
        prefixed = f"group={self._group_id} {message}"
        log_fn = getattr(logger, level, logger.info)
        log_fn(prefixed)

    def _now(self):
        return datetime.now(timezone.utc)

    def stop(self) -> None:
        """Signals the continuous daemon loop to exit gracefully."""
        self._stop_requested = True
        logger.info("Graceful stop requested for NewsMonitorRunner.")

    def _ingest_channel(
        self,
        channel: str,
        group_id: Optional[str] = None,
    ) -> Tuple[List[TelegramPost], Dict[str, Any]]:
        """Scrape + dedupe + save for one channel. Does NOT evaluate or alert.

        Returns:
            (discovered_posts, channel_stats)
        """
        gid = group_id or self._group_id
        clean_channel = channel.lower().lstrip("@").strip()
        logger.info(f"group={gid} Checking Telegram channel: @{clean_channel}")

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
                group_id=gid,
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
                group_id=gid,
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
                group_id=gid,
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
                group_id=gid,
            )
            return [], result

        try:
            unprocessed_posts = self.storage.filter_unprocessed(
                clean_channel, posts, group_id=gid
            )
        except TypeError:
            try:
                unprocessed_posts = self.storage.filter_unprocessed(posts, group_id=gid)
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
            group_id=gid,
        )

        if not unprocessed_posts:
            logger.debug(
                f"group={gid} @{clean_channel}: All {len(posts)} messages already processed. Nothing new."
            )
            return [], result

        logger.info(
            f"group={gid} @{clean_channel}: Discovered {len(unprocessed_posts)} new unprocessed posts."
        )

        try:
            self.storage.save_posts(unprocessed_posts, group_id=gid)
        except TypeError:
            self.storage.save_posts(unprocessed_posts)
        except Exception as exc:
            logger.warning(
                f"group={gid} Batch save_posts encountered exception: {exc}. Falling back to individual saves."
            )
            for post in unprocessed_posts:
                try:
                    self.storage.save_post(post, group_id=gid)
                except TypeError:
                    self.storage.save_post(post)

        return list(unprocessed_posts), result

    def poll_channel(self, channel: str, group_id: Optional[str] = None) -> Dict[str, Any]:
        """Ingest-only channel pass (scrape+dedupe+save). No per-post evaluate/alert.

        Kept for backward-compatible call sites; returns ingest stats only.
        """
        _posts, result = self._ingest_channel(channel, group_id=group_id)
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
                    group_id=self._group_id,
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
                    group_id=self._group_id,
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
                group_id=self._group_id,
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
        profile = self._card_profile
        payload = FeishuCardBuilder.build_digest_item_card(
            item,
            published_at=published_at,
            subtitle=profile.subtitle or "投资情报快报",
            include_investment_impact=profile.include_investment_impact,
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
        now: Optional[datetime] = None,
    ) -> float:
        if not pending_pairs:
            return 0.0
        clock = now or self._now()
        oldest = min(scraped_at for _, scraped_at in pending_pairs)
        if oldest.tzinfo is None:
            oldest = oldest.replace(tzinfo=timezone.utc)
        return max(0.0, (clock - oldest).total_seconds())

    @staticmethod
    def _digest_item_score(item: DigestItem) -> int:
        raw_score = getattr(item, "score", None)
        if raw_score is None:
            return max(1, min(10, 11 - int(item.rank)))
        try:
            return max(1, min(10, int(raw_score)))
        except (TypeError, ValueError):
            return max(1, min(10, 11 - int(item.rank)))

    def _morning_report(self, pending_pairs, summary, now, live_clock=True):
        cfg = self._group_config
        overnight = [p for p, _ in pending_pairs if self.quiet.in_window(p.published_at, now)]
        ranked = {(p.channel.lower(), p.message_id): (p, i.score or 7) for p, i in self.quiet.archived(now)}
        for post in overnight:
            ranked.setdefault((post.channel.lower(), post.message_id), (post, 7))
        posts = [p for p, _ in sorted(ranked.values(), key=lambda pair: (pair[1], pair[0].published_at), reverse=True)]
        posts, _, _ = coarse_filter_posts(posts)
        unique = {}
        for post in posts:
            unique.setdefault(fingerprint(post.text), post)
        candidates = list(unique.values())[:cfg.digest_max_batch_size]
        if not candidates:
            if self.quiet.claim_morning(now):
                self.quiet.complete_morning(now, 'empty')
            return summary
        group_limit = int(getattr(cfg, "digest_max_calls_per_day", self.config.digest_max_calls_per_day))
        if not self.policy.reserve_call(
            cfg.digest_min_interval_seconds,
            group_limit,
            now,
            global_limit=self.config.digest_max_calls_per_day,
        ):
            return summary
        start, end = self.quiet.window(now)
        self.evaluator.recent_history = ''
        profile = self._card_profile
        self.evaluator.prompt_overlay = profile.prompt_overlay or ""
        self.evaluator.prompt_variant = profile.prompt_variant
        self.evaluator.digest_context = f"生成北京时间夜间摘要，范围 {start.isoformat()} 至 {end.isoformat()}，不是实时快讯。允许复盘已提醒的重大新闻。仅按输入判断最新进展，合并同事件；更正覆盖旧说法，剔除撤回信息，不得声称已核实输入以外的最新状态。精选最多5个事件，不凑数，保留event_at与来源ID。"
        try:
            digest = self.evaluator.evaluate_digest(candidates)
        except Exception as exc:
            logger.error(f'Morning evaluation failed; archive retained: {exc}')
            return summary
        finally:
            self.policy.record_usage(getattr(self.evaluator, 'last_usage', None))
            self.evaluator.digest_context = ''
        summary['posts_evaluated'] = len(candidates)
        self.stats['total_posts_evaluated'] += len(candidates)
        allowed = {(p.channel.lower().lstrip('@'), p.message_id): p for p in candidates}
        items, keys = [], set()
        for item in digest.items if digest.has_material_news else []:
            key = (item.channel.lower().lstrip('@'), item.message_id)
            if key not in allowed or key in keys or not self.quiet.in_window(item.event_at, now):
                continue
            if _text_looks_crypto(item.model_dump_json()) or (item.score or 11 - item.rank) < cfg.morning_flush_hotness_threshold:
                continue
            keys.add(key)
            items.append(item)
        # Never send yesterday's or late-recovered morning report as a current alert.
        checked_at = self._now() if live_clock else now
        if not self.quiet.morning_due(checked_at) or self.quiet.day(checked_at) != self.quiet.day(now):
            return summary
        if not self.quiet.claim_morning(now):
            return summary
        self._mark_all_filtered(overnight, 'morning_processed')
        if not items:
            self.quiet.complete_morning(now, 'empty')
            return summary
        items = sorted(items[:5], key=lambda item: allowed[(item.channel.lower().lstrip('@'), item.message_id)].published_at)
        elements = [{'tag': 'markdown', 'content': f'北京时间 {start:%m-%d %H:%M}—{end:%m-%d %H:%M} · 夜间回顾，非即时快讯；截至本次已采集材料。'}]
        for index, item in enumerate(items[:5], 1):
            post = allowed[(item.channel.lower().lstrip('@'), item.message_id)]
            event_time = item.event_at.astimezone(BEIJING).strftime('%H:%M')
            elements.append({
                'tag': 'markdown',
                'content': format_morning_item_md(index, item, event_time),
            })
        label = ""
        enabled = list(self.config.enabled_groups()) if hasattr(self.config, "enabled_groups") else []
        if self._active_group is not None and len(enabled) > 1:
            label = f"{self._active_group.display_name()} · "
        payload = {'msg_type': 'interactive', 'card': {'schema': '2.0', 'header': {'title': {'tag': 'plain_text', 'content': f'{label}{end:%m月%d日} 夜间摘要'}, 'template': 'blue'}, 'body': {'elements': elements}}}
        try:
            sender = getattr(self.webhook_sender, 'send', None) or self.webhook_sender.send_alert
            sent = bool(sender(payload))
        except Exception as exc:
            logger.error(f'Morning delivery uncertain; do not resend automatically: {exc}')
            sent = False
        self.quiet.complete_morning(now, 'sent' if sent else 'unknown')
        for item in items[:5]:
            post = allowed[(item.channel.lower().lstrip('@'), item.message_id)]
            if self.policy.claim(post.text, item.title + ': ' + item.summary):
                self.policy.complete(post.text, sent)
        summary['alerts_sent'] = int(sent)
        self.stats['total_alerts_sent'] += int(sent)
        return summary

    def run_once(self, ingest_only=False) -> Dict[str, Any]:
        """Single pass: ingest all groups' channels, then per-group pending+digest."""
        targets = [g for g in self._process_targets() if g is None or (g.enabled and g.channels)]
        channel_jobs: List[Tuple[Optional[Group], str]] = []
        for group in targets:
            if group is None:
                for channel in self.config.telegram_channels:
                    channel_jobs.append((None, channel))
            else:
                for channel in group.channels:
                    channel_jobs.append((group, channel))

        if not channel_jobs:
            logger.warning(
                "No enabled groups with channels. Configure groups in config.yaml "
                "or TELEGRAM_CHANNELS + FEISHU_WEBHOOK_URL for legacy synthesis."
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
            "channels_polled": len(channel_jobs),
            "posts_discovered": 0,
            "posts_evaluated": 0,
            "alerts_sent": 0,
            "details": [],
        }

        for idx, (group, channel) in enumerate(channel_jobs):
            if self._stop_requested:
                break
            self._activate_group(group)
            gid = group.id if group is not None else self._group_id
            _posts, channel_stat = self._ingest_channel(channel, group_id=gid)
            channel_stat["group"] = gid
            pass_summary["details"].append(channel_stat)
            pass_summary["posts_discovered"] += channel_stat["posts_discovered"]

            if idx < len(channel_jobs) - 1 and not self._stop_requested:
                pause = self.config.inter_channel_delay_seconds
                time.sleep(max(0.1, pause))

        if ingest_only:
            return pass_summary
        return self.process_pending(pass_summary)

    def process_pending(self, pass_summary=None, now=None):
        pass_summary = pass_summary or {"posts_evaluated": 0, "alerts_sent": 0, "details": []}
        clock = now or self._now()
        if clock.tzinfo is None:
            clock = clock.replace(tzinfo=timezone.utc)
        live_clock = now is None
        totals_eval = 0
        totals_alert = 0
        details = pass_summary.get("details") or []

        for group in self._process_targets():
            self._activate_group(group)
            cfg = self._group_config
            tz_name = getattr(cfg, "timezone", "Asia/Shanghai") or "Asia/Shanghai"
            local = alert_schedule.local_now(clock, tz_name)
            quiet_hours = getattr(cfg, "quiet_hours", "") or ""
            shoulder_hours = getattr(cfg, "shoulder_hours", "") or ""
            mode = alert_schedule.classify_alert_mode(local, quiet_hours, shoulder_hours)
            knobs = alert_schedule.knobs_for(cfg, mode, now_local=local)
            self.policy.sync_quiet_window(mode, knobs.quiet_window_id)
            logger.info(
                f"group={self._group_id} Alert window: mode={knobs.mode} tz={tz_name} "
                f"local={local.isoformat()} flush={knobs.is_morning_flush} "
                f"hotness>={knobs.hotness_threshold} min_interval={knobs.min_interval_seconds}s"
            )
            part = {
                "posts_evaluated": 0,
                "alerts_sent": 0,
                "details": details,
            }
            try:
                part = self._process_pending_with_knobs(part, clock, knobs, live_clock=live_clock)
            finally:
                self.policy.note_mode(mode, clock)
            totals_eval += int(part.get("posts_evaluated") or 0)
            totals_alert += int(part.get("alerts_sent") or 0)

        pass_summary["posts_evaluated"] = totals_eval
        pass_summary["alerts_sent"] = totals_alert
        return pass_summary

    def _process_pending_with_knobs(self, pass_summary, clock, knobs, live_clock=True):
        quiet = knobs.mode == "quiet"
        cfg = self._group_config
        # 2. Load this group's pending posts (never mix groups in one LLM call)
        if hasattr(self.storage, "list_pending_with_scraped_at"):
            try:
                pending_pairs = self.storage.list_pending_with_scraped_at(group_id=self._group_id)
            except TypeError:
                pending_pairs = self.storage.list_pending_with_scraped_at()
        else:
            pending_posts = (
                self.storage.list_pending_posts()
                if hasattr(self.storage, "list_pending_posts")
                else []
            )
            pending_pairs = [(p, clock) for p in pending_posts]

        if self.quiet.morning_due(clock):
            return self._morning_report(pending_pairs, pass_summary, clock, live_clock)

        if not pending_pairs:
            logger.info(
                f"group={self._group_id} Batch digest mode: no pending posts "
                "(evaluated_at IS NULL); skipping LLM and Feishu cards."
            )
            return pass_summary

        pending_posts = [p for p, _ in pending_pairs]
        def eligible(post):
            return (quiet and self.quiet.in_window(post.published_at, clock)) or is_fresh(post.published_at, knobs.max_age_seconds, clock)
        expired = [p for p in pending_posts if not eligible(p)]
        self._mark_all_filtered(expired, "expired_or_invalid_time")
        pending_posts = [p for p in pending_posts if eligible(p)]
        unique, duplicates, seen = [], [], set()
        for post in sorted(pending_posts, key=lambda p: p.published_at, reverse=True):
            key = fingerprint(post.text)
            if key in seen or self.policy.seen(post.text):
                duplicates.append(post)
            else:
                seen.add(key)
                unique.append(post)
        self._mark_all_filtered(duplicates, "duplicate_content")
        pending_posts = unique
        logger.info(
            f"group={self._group_id} Pending buffer: {len(pending_posts)} unevaluated post(s) after ingest."
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
                    clock,
                ),
            )
            for p in candidates
        ]

        min_candidates, max_wait = knobs.min_candidates, knobs.max_wait_seconds
        oldest_age = self._oldest_age_seconds(candidate_pairs, now=clock)
        has_urgent = any(urgent(p.text) and is_fresh(p.published_at, cfg.news_max_age_seconds, clock) for p in candidates)
        if quiet:
            if not has_urgent and oldest_age < cfg.quiet_digest_min_interval_seconds:
                return pass_summary
        elif len(candidates) < min_candidates and oldest_age < max_wait and not has_urgent:
            return pass_summary

        logger.info(
            f"group={self._group_id} Digest gate open: candidates={len(candidates)} "
            f"(min={min_candidates}), oldest_age={oldest_age:.0f}s "
            f"(max_wait={max_wait}s, flush={knobs.is_morning_flush}); calling evaluate_digest."
        )

        interval = cfg.digest_min_interval_seconds if quiet and has_urgent else knobs.min_interval_seconds
        group_limit = int(getattr(cfg, "digest_max_calls_per_day", self.config.digest_max_calls_per_day))
        if not self.policy.reserve_call(
            interval,
            group_limit,
            clock,
            global_limit=self.config.digest_max_calls_per_day,
        ):
            logger.info(
                f"group={self._group_id} LLM cooldown/daily call budget: "
                "pending messages retained until expiry"
            )
            return pass_summary
        candidates = sorted(candidates, key=lambda p: (urgent(p.text), p.published_at), reverse=True)[:cfg.digest_max_batch_size]
        self.evaluator.recent_history = self.policy.history()
        profile = self._card_profile
        self.evaluator.prompt_overlay = profile.prompt_overlay or ""
        self.evaluator.prompt_variant = profile.prompt_variant
        self.evaluator.digest_context = ""
        if quiet:
            start, end = self.quiet.window(clock)
            self.evaluator.digest_context = f"北京时间夜间模式，保存 {start.isoformat()} 至 {end.isoformat()} 的重要新闻用于晨报；窗口内事件允许超过30分钟，不得作为实时快讯。night_alert仅在已确认重大事件时为true；confirmed_source填写正文明确标注的原始来源；普通言论、传闻不准打断。is_update与update_reason只能描述相对历史的实质升级。"
            self.evaluator.recent_history += "\n夜间已收录（有新事实才再选）：\n" + "\n".join(i.title + ': ' + i.summary[:200] for _, i in self.quiet.archived(clock)[-20:])
        # 5. One LLM call for the candidate batch
        if hasattr(self.evaluator, "evaluate_digest"):
            try:
                digest = self.evaluator.evaluate_digest(candidates)
            except Exception as exc:
                logger.error(f"group={self._group_id} Digest failed; pending retained: {exc}")
                return pass_summary
            finally:
                self.policy.record_usage(getattr(self.evaluator, "last_usage", None))
                self.evaluator.prompt_overlay = ""
                self.evaluator.prompt_variant = None
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

        valid = {(p.channel.lower().lstrip("@"), p.message_id) for p in candidates}
        selected_once = set()
        checked = []
        for item in digest.items:
            key = (item.channel.lower().lstrip("@"), item.message_id)
            if key in valid and key not in selected_once:
                selected_once.add(key)
                checked.append(item)
        digest.items = checked
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
                        group_id=self._group_id,
                    )
                digest.items = kept_items
                if not kept_items:
                    digest.has_material_news = False
                    digest.filtered_note = (
                        (digest.filtered_note or "") + "; crypto_ban_all"
                    ).strip("; ")

        # 6. Persist short evaluation summaries
        # Archive before marking evaluated, so an interrupted pass retains the morning material.
        by_key = {(p.channel.lower().lstrip('@'), p.message_id): p for p in candidates}
        for item in digest.items:
            post = by_key[(item.channel.lower().lstrip('@'), item.message_id)]
            if quiet and (item.score or 11 - item.rank) >= cfg.hotness_threshold and self.quiet.in_window(post.published_at, clock) and self.quiet.in_window(item.event_at, clock):
                self.quiet.archive(post, item, clock)
        self._apply_digest_evaluations(candidates, digest)

        # 7. Send ONE card PER DigestItem when material items exist
        if digest.has_material_news and digest.items:
            alerts_ok = 0
            post_by_key = {
                (p.channel.lower().lstrip("@").strip(), int(p.message_id)): p
                for p in candidates
            }

            digest.items.sort(key=lambda item: post_by_key[(item.channel.lower().lstrip('@').strip(), int(item.message_id))].published_at)
            card_gap = float(knobs.card_interval_seconds or 0.0)
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
                send_time = self._now() if live_clock else clock
                send_local = alert_schedule.local_now(send_time, cfg.timezone)
                send_mode = alert_schedule.classify_alert_mode(send_local, cfg.quiet_hours, cfg.shoulder_hours)
                send_knobs = alert_schedule.knobs_for(cfg, send_mode, now_local=send_local)
                if quiet and send_mode != 'quiet':
                    continue
                if not is_fresh(published_at, cfg.news_max_age_seconds, send_time):
                    continue
                if not is_fresh(item.event_at, cfg.news_max_age_seconds, send_time):
                    self._mark_all_filtered([matched_post], 'event_time_unknown_or_expired')
                    continue
                score = self._digest_item_score(item)
                if score < send_knobs.hotness_threshold:
                    continue
                if send_knobs.quiet_card_cap is not None and send_knobs.quiet_window_id:
                    if self.policy.quiet_cards_sent(send_knobs.quiet_window_id) >= send_knobs.quiet_card_cap:
                        logger.info(
                            f"Quiet-hour Feishu card cap reached "
                            f"({send_knobs.quiet_card_cap}/{send_knobs.quiet_window_id}); skipping remaining cards."
                        )
                        break
                if send_mode == 'quiet' and not self.quiet.claim_alert(item, matched_post, send_time):
                    if self.quiet.in_window(published_at, send_time) and self.quiet.in_window(item.event_at, send_time):
                        self.quiet.archive(matched_post, item, send_time)
                    continue
                if not self.policy.claim(matched_post.text, item.title + ": " + item.summary):
                    continue
                try:
                    send_ok = self._send_digest_item_card(item, published_at=published_at)
                except Exception as exc:
                    logger.error(f"Delivery uncertain, do not automatically resend: {exc}")
                    send_ok = False
                self.policy.complete(matched_post.text, send_ok)
                if send_ok:
                    alerts_ok += 1
                    if send_knobs.quiet_card_cap is not None and send_knobs.quiet_window_id:
                        self.policy.record_quiet_card(send_knobs.quiet_window_id)
                    summary = f"[digest#{item.rank}] {item.title}"
                    self.storage.mark_alert_sent(
                        channel=ch,
                        message_id=mid,
                        score=score,
                        summary=summary,
                        group_id=self._group_id,
                    )
                    logger.info(
                        f"group={self._group_id} ✅ Digest item card sent: "
                        f"#{item.rank} [{item.category}] {item.title!r}"
                    )
                else:
                    logger.error(
                        f"group={self._group_id} ❌ Failed to dispatch digest item card: "
                        f"#{item.rank} {item.title!r}"
                    )
                    self.storage.mark_alert_failed(
                        channel=ch,
                        message_id=mid,
                        error_msg="Feishu item card webhook delivery failed after retries",
                        group_id=self._group_id,
                    )

            pass_summary["alerts_sent"] = alerts_ok
            self.stats["total_alerts_sent"] += alerts_ok
            for detail in pass_summary["details"]:
                if detail.get("posts_discovered", 0) > 0:
                    detail["posts_evaluated"] = detail.get("posts_discovered", 0)
            logger.info(
                f"group={self._group_id} Multi single-card dispatch done: "
                f"sent={alerts_ok}/{len(digest.items)} (headline={digest.headline!r})"
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
        max_w = int(self.config.digest_max_wait_seconds)
        card_gap = float(getattr(self.config, "digest_card_interval_seconds", 10.0) or 0.0)
        enabled = list(self.config.enabled_groups()) if hasattr(self.config, "enabled_groups") else []
        logger.info("=" * 60)
        logger.info("Starting Telegram News Monitor 24/7 Daemon (batch digest mode)")
        if enabled:
            for group in enabled:
                logger.info(
                    f"Peer group       : id={group.id} name={group.display_name()!r} "
                    f"channels={group.channels} enabled={group.enabled}"
                )
        else:
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
            f"Quiet Hours     : tz={getattr(self.config, 'timezone', 'Asia/Shanghai')} "
            f"quiet={getattr(self.config, 'quiet_hours', '')!r} "
            f"shoulder={getattr(self.config, 'shoulder_hours', '')!r}"
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

        executor = ThreadPoolExecutor(max_workers=1)
        worker = None
        while not self._stop_requested:
            try:
                start_time = time.time()
                summary = self.run_once(ingest_only=True)
                if worker is None or worker.done():
                    if worker is not None:
                        try:
                            worker.result()
                        except Exception as exc:
                            logger.error(f"Digest worker failed: {exc}")
                    worker = executor.submit(self.process_pending)
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
                delay = max(0.0, delay - elapsed)
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

        executor.shutdown(wait=True)
        logger.info("NewsMonitorRunner daemon loop exited cleanly.")
