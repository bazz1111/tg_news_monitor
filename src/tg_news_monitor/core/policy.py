"""Small, persistent cost and delivery guards. UTC days define call budgets."""
import hashlib
import re
import unicodedata
from datetime import datetime, timedelta, timezone

from tg_news_monitor.storage.database import db_session


def fingerprint(text):
    # Preserve numbers and negation: changes to either may be material updates.
    text = re.sub(r'\s+', ' ', unicodedata.normalize('NFKC', text).casefold()).strip()
    return hashlib.sha256(text.encode()).hexdigest()


def is_fresh(value, max_age, now=None):
    if value is None or value.tzinfo is None:
        return False
    age = ((now or datetime.now(timezone.utc)) - value).total_seconds()
    return -120 <= age <= max_age


def urgent(text):
    # A cheap scheduling hint, never permission to bypass score/age/budget guards.
    return bool(re.search(r'紧急降息|央行降息|导弹袭击|强震|核事故|交易暂停|熔断|emergency rate cut|missile strike|earthquake|trading halt|nuclear incident', text, re.I))


class DeliveryPolicy:
    def __init__(self, db_path):
        self.db_path = db_path
        with db_session(db_path) as conn:
            conn.execute('CREATE TABLE IF NOT EXISTS digest_calls (started REAL NOT NULL, tokens INTEGER)')
            conn.execute('CREATE TABLE IF NOT EXISTS delivery_claims (fingerprint TEXT PRIMARY KEY, claimed REAL NOT NULL, summary TEXT NOT NULL, status TEXT NOT NULL DEFAULT \'unknown\')')
            conn.execute(
                'CREATE TABLE IF NOT EXISTS alert_schedule_state ('
                'id INTEGER PRIMARY KEY CHECK (id = 1), '
                'last_mode TEXT, '
                'last_mode_at REAL, '
                'quiet_window_id TEXT, '
                'quiet_cards_sent INTEGER NOT NULL DEFAULT 0, '
                'flush_date TEXT)'
            )
            conn.execute('INSERT OR IGNORE INTO alert_schedule_state(id, quiet_cards_sent) VALUES (1, 0)')

    def reserve_call(self, interval, daily_limit, now=None):
        now = now or datetime.now(timezone.utc)
        with db_session(self.db_path) as conn:
            conn.execute('BEGIN IMMEDIATE')
            last = conn.execute('SELECT MAX(started) FROM digest_calls').fetchone()[0]
            count = conn.execute('SELECT COUNT(*) FROM digest_calls WHERE started >= ?', (now.replace(hour=0, minute=0, second=0, microsecond=0).timestamp(),)).fetchone()[0]
            if (last is not None and now.timestamp() - last < interval) or count >= daily_limit:
                return False
            self.call_id = conn.execute('INSERT INTO digest_calls(started) VALUES (?)', (now.timestamp(),)).lastrowid
        return True

    def record_usage(self, tokens):
        with db_session(self.db_path) as conn:
            conn.execute('UPDATE digest_calls SET tokens=? WHERE rowid=?', (tokens, self.call_id))

    def seen(self, text):
        cutoff = datetime.now(timezone.utc).timestamp() - 86400
        with db_session(self.db_path) as conn:
            return conn.execute('SELECT 1 FROM delivery_claims WHERE fingerprint=? AND claimed>=?', (fingerprint(text), cutoff)).fetchone() is not None

    def claim(self, text, summary):
        now = datetime.now(timezone.utc).timestamp()
        with db_session(self.db_path) as conn:
            conn.execute('DELETE FROM delivery_claims WHERE claimed < ?', (now - 86400,))
            return conn.execute('INSERT OR IGNORE INTO delivery_claims(fingerprint,claimed,summary) VALUES(?,?,?)', (fingerprint(text), now, summary[:300])).rowcount == 1

    def complete(self, text, sent):
        with db_session(self.db_path) as conn:
            conn.execute('UPDATE delivery_claims SET status=? WHERE fingerprint=?', ('sent' if sent else 'unknown', fingerprint(text)))

    def history(self):
        # ponytail: bounded recent context; semantic recall declines beyond 20 events.
        with db_session(self.db_path) as conn:
            rows = conn.execute('SELECT summary FROM delivery_claims WHERE claimed>=? ORDER BY claimed DESC LIMIT 20', (datetime.now(timezone.utc).timestamp() - 86400,)).fetchall()
        return '\n'.join(row[0] for row in rows)

    def _schedule_row(self, conn):
        return conn.execute('SELECT * FROM alert_schedule_state WHERE id=1').fetchone()

    def note_mode(self, mode, now):
        ts = (now or datetime.now(timezone.utc)).timestamp()
        with db_session(self.db_path) as conn:
            conn.execute(
                'UPDATE alert_schedule_state SET last_mode=?, last_mode_at=? WHERE id=1',
                (mode, ts),
            )

    def peek_morning_flush(self, mode, now_local, day_start_minute):
        """True once when crossing into day mode (or waking after today's day start)."""
        if mode != 'day':
            return False
        flush_date = now_local.date().isoformat()
        day_start = now_local.replace(
            hour=day_start_minute // 60,
            minute=day_start_minute % 60,
            second=0,
            microsecond=0,
        )
        if now_local < day_start:
            day_start = day_start - timedelta(days=1)
        with db_session(self.db_path) as conn:
            row = self._schedule_row(conn)
            if row is None:
                return False
            if row['flush_date'] == flush_date:
                return False
            last_mode = row['last_mode']
            last_at = row['last_mode_at']
            if last_mode is None or last_at is None:
                return False
            if last_mode != 'day':
                return True
            return float(last_at) < day_start.timestamp()

    def mark_morning_flush(self, now_local):
        with db_session(self.db_path) as conn:
            conn.execute(
                'UPDATE alert_schedule_state SET flush_date=? WHERE id=1',
                (now_local.date().isoformat(),),
            )

    def sync_quiet_window(self, mode, window_id):
        with db_session(self.db_path) as conn:
            if mode != 'quiet':
                conn.execute(
                    'UPDATE alert_schedule_state SET quiet_window_id=NULL, quiet_cards_sent=0 WHERE id=1'
                )
                return
            row = self._schedule_row(conn)
            if row is None or row['quiet_window_id'] != window_id:
                conn.execute(
                    'UPDATE alert_schedule_state SET quiet_window_id=?, quiet_cards_sent=0 WHERE id=1',
                    (window_id,),
                )

    def quiet_cards_sent(self, window_id):
        with db_session(self.db_path) as conn:
            row = self._schedule_row(conn)
            if row is None or row['quiet_window_id'] != window_id:
                return 0
            return int(row['quiet_cards_sent'] or 0)

    def record_quiet_card(self, window_id):
        with db_session(self.db_path) as conn:
            conn.execute('BEGIN IMMEDIATE')
            row = self._schedule_row(conn)
            if row is None or row['quiet_window_id'] != window_id:
                conn.execute(
                    'UPDATE alert_schedule_state SET quiet_window_id=?, quiet_cards_sent=1 WHERE id=1',
                    (window_id,),
                )
            else:
                conn.execute(
                    'UPDATE alert_schedule_state SET quiet_cards_sent=quiet_cards_sent+1 WHERE id=1'
                )
