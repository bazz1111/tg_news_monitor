"""Schedule parsing and alert-window classification (Asia/Shanghai, midnight wrap)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from tg_news_monitor.config import Settings
from tg_news_monitor.core.schedule import (
    MODE_DAY,
    MODE_QUIET,
    MODE_SHOULDER,
    classify_alert_mode,
    day_start_minutes,
    knobs_for,
    load_zone,
    minute_in_window,
    minute_of_day,
    parse_hhmm,
    parse_hour_window,
    window_id_for,
)

pytestmark = pytest.mark.real_schedule

SH = load_zone("Asia/Shanghai")


def local(hour: int, minute: int = 0, day: int = 9) -> datetime:
    return datetime(2026, 9, day, hour, minute, tzinfo=SH)


class TestParseHourWindow:
    def test_hhmm_and_hour_only(self):
        assert parse_hhmm("08:00") == 8 * 60
        assert parse_hhmm("23") == 23 * 60
        assert parse_hhmm("0:30") == 30
        assert parse_hour_window("08:00-23:00") == (8 * 60, 23 * 60)
        assert parse_hour_window("23:00-01:00") == (23 * 60, 60)
        assert parse_hour_window("23-1") == (23 * 60, 60)

    def test_empty_and_zero_width_disabled(self):
        assert parse_hour_window("") is None
        assert parse_hour_window("   ") is None
        assert parse_hour_window("08:00-08:00") is None

    def test_invalid_specs(self):
        with pytest.raises(ValueError):
            parse_hour_window("08:00")
        with pytest.raises(ValueError):
            parse_hour_window("25:00-26:00")
        with pytest.raises(ValueError):
            parse_hour_window("08:60-09:00")
        with pytest.raises(ValueError):
            parse_hour_window("night")
        with pytest.raises(ValueError):
            parse_hhmm("12:30:00")


class TestMidnightWrap:
    def test_same_day_half_open(self):
        start, end = 8 * 60, 23 * 60
        assert minute_in_window(8 * 60, start, end)
        assert minute_in_window(12 * 60, start, end)
        assert not minute_in_window(23 * 60, start, end)
        assert not minute_in_window(7 * 60 + 59, start, end)

    def test_wrap_contains_late_and_early(self):
        start, end = 23 * 60, 60
        assert minute_in_window(23 * 60, start, end)
        assert minute_in_window(23 * 60 + 30, start, end)
        assert minute_in_window(0, start, end)
        assert minute_in_window(30, start, end)
        assert not minute_in_window(60, start, end)
        assert not minute_in_window(2 * 60, start, end)
        assert not minute_in_window(22 * 60 + 59, start, end)

    def test_window_id_wraps_to_start_date(self):
        start, end = 23 * 60, 60
        assert window_id_for(local(23, 30, day=8), start, end) == "2026-09-08"
        assert window_id_for(local(0, 30, day=9), start, end) == "2026-09-08"
        # After the wrap ends, id stays on the start-side date until the next 23:00.
        assert window_id_for(local(2, 0, day=9), start, end) == "2026-09-08"


class TestClassifyDefaultBeijingWindows:
    def test_day_shoulder_quiet_boundaries(self):
        assert classify_alert_mode(local(8, 0)) == MODE_DAY
        assert classify_alert_mode(local(12, 0)) == MODE_DAY
        assert classify_alert_mode(local(21, 59)) == MODE_DAY
        assert classify_alert_mode(local(23, 0)) == MODE_SHOULDER
        assert classify_alert_mode(local(23, 30)) == MODE_SHOULDER
        assert classify_alert_mode(local(0, 30)) == MODE_QUIET
        assert classify_alert_mode(local(0, 59)) == MODE_QUIET
        assert classify_alert_mode(local(1, 0)) == MODE_QUIET
        assert classify_alert_mode(local(2, 0)) == MODE_QUIET
        assert classify_alert_mode(local(7, 59)) == MODE_QUIET
        assert classify_alert_mode(local(8, 0)) == MODE_DAY

    def test_empty_windows_always_day(self):
        noon = local(12)
        midnight = local(0, 30)
        assert classify_alert_mode(noon, "", "") == MODE_DAY
        assert classify_alert_mode(midnight, "", "") == MODE_DAY
        assert classify_alert_mode(local(3), "", "23:00-01:00") == MODE_DAY

    def test_quiet_wins_overlap(self):
        # Shoulder 23:00-02:00 overlaps quiet 01:00-08:00 at 01:30.
        assert classify_alert_mode(local(1, 30), "01:00-08:00", "23:00-02:00") == MODE_QUIET
        assert classify_alert_mode(local(0, 30), "01:00-08:00", "23:00-02:00") == MODE_SHOULDER

    def test_utc_instant_converts_via_local_clock(self):
        # 16:00 UTC = 00:00 next day in Shanghai → still shoulder (23:00-01:00).
        utc = datetime(2026, 9, 8, 16, 0, tzinfo=timezone.utc).astimezone(SH)
        assert classify_alert_mode(utc) == MODE_QUIET
        assert minute_of_day(utc) == 0

    def test_day_start_follows_quiet_end(self):
        assert day_start_minutes("01:00-08:00", "23:00-01:00") == 8 * 60
        assert day_start_minutes("", "23:00-01:00") == 60
        assert day_start_minutes("", "") == 0


class TestKnobsAndSettings:
    def test_mode_knobs(self):
        cfg = Settings()
        day = knobs_for(cfg, MODE_DAY)
        assert day.hotness_threshold == 7
        assert day.min_interval_seconds == 180
        assert day.quiet_card_cap is None
        assert not day.require_urgent_to_evaluate

        shoulder = knobs_for(cfg, MODE_SHOULDER)
        assert shoulder.hotness_threshold == 8
        assert shoulder.min_interval_seconds == 600
        assert shoulder.min_candidates == 16
        assert shoulder.card_interval_seconds == 15

        quiet = knobs_for(cfg, MODE_QUIET, now_local=local(2))
        assert quiet.hotness_threshold == 9
        assert quiet.min_interval_seconds == 1800
        assert quiet.require_urgent_to_evaluate
        assert not quiet.allow_urgent_score_bypass
        assert quiet.quiet_card_cap == 5
        assert quiet.quiet_window_id == "2026-09-09"

        flush = knobs_for(cfg, MODE_DAY, is_morning_flush=True)
        assert flush.is_morning_flush
        assert flush.max_age_seconds == 7200
        assert flush.hotness_threshold == 7
        assert flush.min_interval_seconds == 0
        assert flush.card_interval_seconds == 10

    def test_settings_defaults_and_bad_window(self):
        s = Settings()
        assert s.timezone == "Asia/Shanghai"
        assert s.quiet_hours == "00:00-08:00"
        assert s.shoulder_hours == "22:00-00:00"
        assert s.quiet_card_cap == 5
        assert s.morning_flush_max_age_seconds == 7200
        with pytest.raises(ValidationError):
            Settings(quiet_hours="not-a-window")
        assert Settings(quiet_hours="", shoulder_hours="").quiet_hours == ""

    def test_unknown_timezone_errors(self):
        with pytest.raises(ValueError, match="unknown timezone"):
            load_zone("NotA/RealZone")
        shanghai = load_zone("Asia/Shanghai")
        assert shanghai is not None

    def test_zero_quiet_cap_is_preserved(self):
        knobs = knobs_for(SimpleNamespace(quiet_card_cap=0, quiet_hours="01:00-08:00"), MODE_QUIET, now_local=local(3))
        assert knobs.quiet_card_cap == 0
