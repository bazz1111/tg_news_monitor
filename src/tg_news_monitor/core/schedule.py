"""Beijing-time (or any IANA) alert windows. Windows may wrap midnight."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

MODE_DAY = "day"
MODE_SHOULDER = "shoulder"
MODE_QUIET = "quiet"


def load_zone(name: str):
    """Resolve an IANA zone; Asia/Shanghai falls back to UTC+8 if tzdata is missing."""
    key = (name or "").strip() or "Asia/Shanghai"
    try:
        return ZoneInfo(key)
    except (ZoneInfoNotFoundError, Exception):
        if key in {"Asia/Shanghai", "Asia/Beijing", "PRC", "CST"}:
            return timezone(timedelta(hours=8))
        return timezone.utc


def parse_hhmm(token: str) -> int:
    """Parse 'HH:MM' or 'HH' into minutes from midnight. Raises ValueError."""
    raw = (token or "").strip()
    if not raw:
        raise ValueError("empty time token")
    parts = raw.split(":")
    if len(parts) == 1:
        hour, minute = parts[0], "0"
    elif len(parts) == 2:
        hour, minute = parts[0], parts[1]
    else:
        raise ValueError(f"invalid time token: {token!r}")
    if not hour.isdigit() or not minute.isdigit():
        raise ValueError(f"invalid time token: {token!r}")
    h = int(hour)
    m = int(minute)
    if h < 0 or h > 23 or m < 0 or m > 59:
        raise ValueError(f"time out of range: {token!r}")
    return h * 60 + m


def parse_hour_window(spec: str) -> Optional[tuple[int, int]]:
    """Parse 'HH:MM-HH:MM' into (start_minute, end_minute). Empty spec → None (disabled).

    A window with start == end is treated as disabled. Wrapping windows (start > end)
    are valid, e.g. 23:00-01:00.
    """
    text = (spec or "").strip()
    if not text:
        return None
    if text.count("-") != 1:
        raise ValueError(f"window must be START-END, got {spec!r}")
    left, right = text.split("-", 1)
    start = parse_hhmm(left)
    end = parse_hhmm(right)
    if start == end:
        return None
    return start, end


def minute_of_day(dt: datetime) -> int:
    return dt.hour * 60 + dt.minute


def minute_in_window(minute: int, start: int, end: int) -> bool:
    """True if minute is in [start, end). Handles midnight wrap when start > end."""
    if start == end:
        return False
    if start < end:
        return start <= minute < end
    return minute >= start or minute < end


def window_id_for(now_local: datetime, start: int, end: int) -> str:
    """Stable id for the currently active window, including wrap-across-midnight.

    Same-day window: local calendar date.
    Wrapping window: date of the start side (if now is after start, today;
    if now is before end, yesterday).
    """
    local_date = now_local.date()
    if start < end:
        return local_date.isoformat()
    if minute_of_day(now_local) >= start:
        return local_date.isoformat()
    return (local_date - timedelta(days=1)).isoformat()


def classify_alert_mode(
    now_local: datetime,
    quiet_hours: str = "01:00-08:00",
    shoulder_hours: str = "23:00-01:00",
) -> str:
    """Return day | shoulder | quiet. Quiet wins overlaps, then shoulder."""
    minute = minute_of_day(now_local)
    quiet = parse_hour_window(quiet_hours)
    if quiet and minute_in_window(minute, quiet[0], quiet[1]):
        return MODE_QUIET
    shoulder = parse_hour_window(shoulder_hours)
    if shoulder and minute_in_window(minute, shoulder[0], shoulder[1]):
        return MODE_SHOULDER
    return MODE_DAY


def day_start_minutes(quiet_hours: str, shoulder_hours: str) -> int:
    """Local minute-of-day when the current day-mode window begins."""
    quiet = parse_hour_window(quiet_hours)
    if quiet:
        return quiet[1]
    shoulder = parse_hour_window(shoulder_hours)
    if shoulder:
        return shoulder[1]
    return 0


def local_now(now: Optional[datetime], tz_name: str) -> datetime:
    clock = now or datetime.now(timezone.utc)
    if clock.tzinfo is None:
        clock = clock.replace(tzinfo=timezone.utc)
    return clock.astimezone(load_zone(tz_name))


@dataclass(frozen=True)
class AlertKnobs:
    mode: str
    is_morning_flush: bool
    min_candidates: int
    max_wait_seconds: int
    min_interval_seconds: int
    hotness_threshold: int
    card_interval_seconds: float
    max_age_seconds: int
    require_urgent_to_evaluate: bool
    allow_urgent_score_bypass: bool
    quiet_card_cap: Optional[int]
    quiet_window_id: Optional[str]


def _cfg_int(config, name: str, default: int) -> int:
    value = getattr(config, name, default)
    if value is None:
        return default
    return int(value)


def _cfg_float(config, name: str, default: float) -> float:
    value = getattr(config, name, default)
    if value is None:
        return default
    return float(value)


def knobs_for(config, mode: str, *, is_morning_flush: bool = False, now_local: Optional[datetime] = None) -> AlertKnobs:
    """Mode-specific digest / send knobs. Morning flush overrides age, score, and gates."""
    quiet_spec = getattr(config, "quiet_hours", "") or ""
    quiet_parsed = parse_hour_window(quiet_spec)
    quiet_window_id = None
    if mode == MODE_QUIET and quiet_parsed and now_local is not None:
        quiet_window_id = window_id_for(now_local, quiet_parsed[0], quiet_parsed[1])

    base = AlertKnobs(
        mode=mode,
        is_morning_flush=False,
        min_candidates=_cfg_int(config, "digest_min_candidates", 3),
        max_wait_seconds=_cfg_int(config, "digest_max_wait_seconds", 900),
        min_interval_seconds=_cfg_int(config, "digest_min_interval_seconds", 180),
        hotness_threshold=_cfg_int(config, "hotness_threshold", 7),
        card_interval_seconds=_cfg_float(config, "digest_card_interval_seconds", 10.0),
        max_age_seconds=_cfg_int(config, "news_max_age_seconds", 1800),
        require_urgent_to_evaluate=False,
        allow_urgent_score_bypass=False,
        quiet_card_cap=None,
        quiet_window_id=quiet_window_id,
    )

    if is_morning_flush:
        return AlertKnobs(
            mode=MODE_DAY,
            is_morning_flush=True,
            min_candidates=1,
            max_wait_seconds=0,
            min_interval_seconds=0,
            hotness_threshold=_cfg_int(config, "morning_flush_hotness_threshold", 7),
            card_interval_seconds=_cfg_float(config, "morning_flush_card_interval_seconds", 10.0),
            max_age_seconds=_cfg_int(config, "morning_flush_max_age_seconds", 7200),
            require_urgent_to_evaluate=False,
            allow_urgent_score_bypass=False,
            quiet_card_cap=None,
            quiet_window_id=None,
        )

    if mode == MODE_SHOULDER:
        return AlertKnobs(
            mode=MODE_SHOULDER,
            is_morning_flush=False,
            min_candidates=_cfg_int(config, "shoulder_digest_min_candidates", 16),
            max_wait_seconds=base.max_wait_seconds,
            min_interval_seconds=_cfg_int(config, "shoulder_digest_min_interval_seconds", 600),
            hotness_threshold=_cfg_int(config, "shoulder_hotness_threshold", 8),
            card_interval_seconds=_cfg_float(config, "shoulder_digest_card_interval_seconds", 15.0),
            max_age_seconds=base.max_age_seconds,
            require_urgent_to_evaluate=False,
            allow_urgent_score_bypass=False,
            quiet_card_cap=None,
            quiet_window_id=None,
        )

    if mode == MODE_QUIET:
        return AlertKnobs(
            mode=MODE_QUIET,
            is_morning_flush=False,
            min_candidates=_cfg_int(config, "quiet_digest_min_candidates", 16),
            max_wait_seconds=base.max_wait_seconds,
            min_interval_seconds=_cfg_int(config, "quiet_digest_min_interval_seconds", 1800),
            hotness_threshold=_cfg_int(config, "quiet_hotness_threshold", 9),
            card_interval_seconds=_cfg_float(config, "quiet_digest_card_interval_seconds", 15.0),
            max_age_seconds=base.max_age_seconds,
            require_urgent_to_evaluate=True,
            allow_urgent_score_bypass=True,
            quiet_card_cap=_cfg_int(config, "quiet_card_cap", 5),
            quiet_window_id=quiet_window_id,
        )

    return base
