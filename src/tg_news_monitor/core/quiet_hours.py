"""Beijing quiet hours and a bounded, persistent morning-news buffer."""
from datetime import datetime, timedelta, timezone
import re

from tg_news_monitor.core.models import TelegramPost, DigestItem
from tg_news_monitor.core.policy import urgent
from tg_news_monitor.core import schedule
from tg_news_monitor.storage.database import db_session

BEIJING = timezone(timedelta(hours=8))


class QuietHours:
    def __init__(self, db_path, config):
        self.db_path, self.config = db_path, config
        with db_session(db_path) as conn:
            conn.execute('CREATE TABLE IF NOT EXISTS night_candidates (day TEXT, channel TEXT, message_id INTEGER, post TEXT NOT NULL, item TEXT NOT NULL, PRIMARY KEY(day, channel, message_id))')
            conn.execute('CREATE TABLE IF NOT EXISTS morning_reports (day TEXT PRIMARY KEY, status TEXT NOT NULL)')
            conn.execute('CREATE TABLE IF NOT EXISTS night_alerts (claimed REAL NOT NULL)')

    def is_quiet(self, now):
        local = schedule.local_now(now, self.config.timezone)
        return schedule.classify_alert_mode(local, self.config.quiet_hours, '') == 'quiet'

    def window(self, now):
        local = schedule.local_now(now, self.config.timezone)
        start_min, end_min = schedule.parse_hour_window(self.config.quiet_hours) or (0, 0)
        end = local.replace(hour=end_min // 60, minute=end_min % 60, second=0, microsecond=0)
        if self.is_quiet(now) and start_min > end_min and schedule.minute_of_day(local) >= start_min:
            end += timedelta(days=1)
        start = end.replace(hour=start_min // 60, minute=start_min % 60)
        if start >= end:
            start -= timedelta(days=1)
        return start, end

    def day(self, now):
        return self.window(now)[1].date().isoformat()

    def in_window(self, value, now):
        if value is None or value.tzinfo is None:
            return False
        start, end = self.window(now)
        return start <= value < end and value <= now + timedelta(seconds=120)

    def morning_due(self, now):
        if not self.config.morning_flush_enabled or not schedule.parse_hour_window(self.config.quiet_hours):
            return False
        end = self.window(now)[1]
        if not end <= now < end + timedelta(hours=1):
            return False
        with db_session(self.db_path) as conn:
            return conn.execute('SELECT 1 FROM morning_reports WHERE day=?', (self.day(now),)).fetchone() is None

    def claim_morning(self, now):
        with db_session(self.db_path) as conn:
            return conn.execute('INSERT OR IGNORE INTO morning_reports VALUES (?, ?)', (self.day(now), 'unknown')).rowcount == 1

    def complete_morning(self, now, status):
        with db_session(self.db_path) as conn:
            conn.execute('UPDATE morning_reports SET status=? WHERE day=?', (status, self.day(now)))

    def archive(self, post, item, now):
        with db_session(self.db_path) as conn:
            conn.execute('DELETE FROM night_candidates WHERE day < ?', ((now.astimezone(BEIJING).date() - timedelta(days=3)).isoformat(),))
            conn.execute('INSERT OR REPLACE INTO night_candidates VALUES (?, ?, ?, ?, ?)',
                         (self.day(now), post.channel.lower(), post.message_id, post.model_dump_json(), item.model_dump_json()))

    def archived(self, now):
        with db_session(self.db_path) as conn:
            rows = conn.execute('SELECT post, item FROM night_candidates WHERE day=? ORDER BY rowid', (self.day(now),)).fetchall()
        return [(TelegramPost.model_validate_json(r[0]), DigestItem.model_validate_json(r[1])) for r in rows]

    def claim_alert(self, item, post, now):
        source = item.confirmed_source.strip().lower()
        # A source label in text is evidence of attribution, not independent fact verification.
        trusted = {'reuters', 'associated press', 'afp', 'whitehouse.gov', 'federalreserve.gov', 'sec.gov'}
        attributed = source in trusted and re.search(r'(?<!\w)' + re.escape(source) + r'(?!\w)', post.text.lower())
        if not (urgent(post.text) and (item.score or 0) >= 9 and item.night_alert and attributed):
            return False
        with db_session(self.db_path) as conn:
            conn.execute('BEGIN IMMEDIATE')
            last = conn.execute('SELECT MAX(claimed) FROM night_alerts').fetchone()[0]
            escalation = item.is_update and bool(item.update_reason.strip())
            if last is not None and now.timestamp() - last < self.config.quiet_alert_interval_seconds and not escalation:
                return False
            conn.execute('INSERT INTO night_alerts VALUES (?)', (now.timestamp(),))
        return True
