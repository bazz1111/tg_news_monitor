from datetime import datetime, timedelta, timezone
import pytest
pytestmark = pytest.mark.real_schedule

from tg_news_monitor.core.quiet_hours import QuietHours


def test_beijing_boundaries_and_persistent_morning_claim(tmp_path):
    from tg_news_monitor.config import Settings
    from tg_news_monitor.storage.repository import PostRepository
    repo = PostRepository(str(tmp_path / 'night.db'))
    quiet = QuietHours(repo.db_path, Settings())
    # UTC date differs from the Beijing date at midnight.
    midnight = datetime(2026, 9, 9, 16, tzinfo=timezone.utc)
    assert quiet.is_quiet(midnight)
    assert not quiet.is_quiet(midnight - timedelta(seconds=1))
    assert quiet.is_quiet(midnight + timedelta(hours=8) - timedelta(seconds=1))
    assert not quiet.is_quiet(midnight + timedelta(hours=8))
    assert quiet.morning_due(midnight + timedelta(hours=8))
    assert not quiet.morning_due(midnight + timedelta(hours=9))
    assert quiet.claim_morning(midnight + timedelta(hours=8))
    assert not QuietHours(repo.db_path, Settings()).morning_due(midnight + timedelta(hours=8))
    crossing = QuietHours(repo.db_path, Settings(quiet_hours="23:00-08:00"))
    assert crossing.is_quiet(midnight - timedelta(minutes=30))
    assert not crossing.is_quiet(midnight - timedelta(hours=2))
    assert crossing.window(midnight - timedelta(minutes=30))[0].hour == 23


def test_night_retention_one_morning_card_and_restart(tmp_path):
    from tg_news_monitor.config import Settings
    from tg_news_monitor.core.models import TelegramPost, DigestItem, DigestBrief
    from tg_news_monitor.core.runner import NewsMonitorRunner
    from tg_news_monitor.storage.repository import PostRepository
    from tests.test_runner import MockScraper, MockEvaluator, MockWebhookSender
    repo = PostRepository(str(tmp_path / 'runner.db'))
    midnight = datetime(2026, 9, 9, 16, tzinfo=timezone.utc)
    post = TelegramPost(channel='wire', message_id=1, published_at=midnight + timedelta(minutes=15),
                        text='A major technology company released an improved AI processor.', direct_url='https://t.me/wire/1')
    repo.save_post(post)
    def digest(posts):
        return DigestBrief(headline='test', overview='', items=[DigestItem(
            rank=1, channel=p.channel, message_id=p.message_id, title='AI芯片发布', summary='新产品已发布',
            event_at=p.published_at, score=8, category='AI', impact_overall='关注后续测试',
            impact_us='', impact_cn='', impact_commodities='') for p in posts])
    config = Settings(db_path=repo.db_path, telegram_channels=['wire'], digest_min_candidates=1,
                      digest_max_wait_seconds=0, digest_card_interval_seconds=0)
    evaluator, sender = MockEvaluator(digest_builder=digest), MockWebhookSender()
    runner = NewsMonitorRunner(config, repo, MockScraper(), evaluator, sender)
    clock = [midnight + timedelta(hours=1)]
    runner._now = lambda: clock[0]
    # Deterministic buffering clock independent of the machine running the test.
    runner._oldest_age_seconds = lambda pairs, now=None: 3600
    assert runner.process_pending()['alerts_sent'] == 0
    assert len(runner.quiet.archived(clock[0])) == 1  # >30 minute old news retained for recap
    assert not repo.list_pending_posts()
    assert len(evaluator.digest_batches) == 1
    clock[0] = midnight + timedelta(hours=2)
    assert runner.process_pending()['alerts_sent'] == 0
    assert len(evaluator.digest_batches) == 1

    # Restart before morning: persisted archive is sufficient even with no pending posts.
    restarted = NewsMonitorRunner(config, repo, MockScraper(), evaluator, sender)
    clock[0] = midnight + timedelta(hours=8)
    restarted._now = lambda: clock[0]
    assert restarted.process_pending()['alerts_sent'] == 1
    assert len(sender.sent_payloads) == 1
    assert '夜间摘要' in str(sender.sent_payloads[0])
    assert '非即时快讯' in str(sender.sent_payloads[0])
    assert 't.me' not in str(sender.sent_payloads[0])
    assert '北京时间' in str(sender.sent_payloads[0])
    assert restarted.process_pending()['alerts_sent'] == 0
    assert len(evaluator.digest_batches) == 2  # no duplicate morning LLM call

    # Next day, an uncertain webhook must not be retried after a restart.
    clock[0] += timedelta(days=1)
    post = post.model_copy(update={'message_id': 2, 'published_at': post.published_at + timedelta(days=1)})
    repo.save_post(post)
    sender.should_succeed = False
    assert restarted.process_pending()['alerts_sent'] == 0
    assert len(sender.sent_payloads) == 2
    again = NewsMonitorRunner(config, repo, MockScraper(), evaluator, sender)
    again._now = lambda: clock[0]
    again.process_pending()
    assert len(sender.sent_payloads) == 2


def test_night_alert_needs_source_and_cooldown_with_real_escalation(tmp_path):
    from tg_news_monitor.config import Settings
    from tg_news_monitor.core.models import TelegramPost, DigestItem
    from tg_news_monitor.storage.repository import PostRepository
    repo = PostRepository(str(tmp_path / 'alerts.db'))
    quiet = QuietHours(repo.db_path, Settings())
    now = datetime(2026, 9, 9, 17, tzinfo=timezone.utc)
    post = TelegramPost(channel='wire', message_id=1, published_at=now,
                        text='Reuters confirms a major earthquake and emergency response.', direct_url='https://t.me/wire/1')
    item = DigestItem(rank=1, channel='wire', message_id=1, title='地震', summary='官方确认',
                      score=10, category='突发', event_at=now, night_alert=True,
                      impact_overall='', impact_us='', impact_cn='', impact_commodities='')
    assert not quiet.claim_alert(item, post, now)  # score alone cannot break quiet hours
    item.confirmed_source = 'Reuters'
    assert quiet.claim_alert(item, post, now)
    assert not QuietHours(repo.db_path, Settings()).claim_alert(item, post, now + timedelta(minutes=1))
    item.is_update = True
    assert not quiet.claim_alert(item, post, now + timedelta(minutes=2))  # label alone insufficient
    item.update_reason = 'New official tsunami warning after the initial earthquake bulletin'
    assert quiet.claim_alert(item, post, now + timedelta(minutes=3))


def test_quiet_batch_waits_and_morning_failure_is_recoverable(tmp_path):
    from tg_news_monitor.config import Settings
    from tg_news_monitor.core.models import TelegramPost
    from tg_news_monitor.core.runner import NewsMonitorRunner
    from tg_news_monitor.storage.repository import PostRepository
    from tests.test_runner import MockScraper, MockEvaluator, MockWebhookSender
    repo = PostRepository(str(tmp_path / 'failure.db'))
    now = datetime(2026, 9, 9, 18, tzinfo=timezone.utc)
    repo.save_post(TelegramPost(channel='wire', message_id=1, published_at=now,
                               text='New economic data released by the statistical office.', direct_url='https://t.me/wire/1'))
    evaluator = MockEvaluator()
    runner = NewsMonitorRunner(Settings(db_path=repo.db_path, digest_min_candidates=1, digest_max_wait_seconds=0),
                               repo, MockScraper(), evaluator, MockWebhookSender())
    runner._now = lambda: now
    runner._oldest_age_seconds = lambda pairs, now=None: 60
    runner.process_pending()
    assert not evaluator.digest_batches
    assert repo.list_pending_posts()

    morning = now.replace(hour=0) + timedelta(days=1)
    runner._now = lambda: morning
    def fail(posts):
        raise RuntimeError('model unavailable')
    evaluator.digest_builder = fail
    runner.process_pending()
    assert runner.quiet.morning_due(morning)
    assert repo.list_pending_posts()


def test_night_exception_and_call_crossing_morning_boundary(tmp_path):
    from tg_news_monitor.config import Settings
    from tg_news_monitor.core.models import TelegramPost, DigestItem, DigestBrief
    from tg_news_monitor.core.runner import NewsMonitorRunner
    from tg_news_monitor.storage.repository import PostRepository
    from tests.test_runner import MockScraper, MockEvaluator, MockWebhookSender
    repo = PostRepository(str(tmp_path / 'boundary.db'))
    clock = [datetime(2026, 9, 9, 17, tzinfo=timezone.utc)]
    def post(mid):
        return TelegramPost(channel='wire', message_id=mid, published_at=clock[0],
                            text=f'Reuters reports earthquake confirmed in region {mid}.', direct_url=f'https://t.me/wire/{mid}')
    def digest(posts):
        return DigestBrief(headline='test', overview='', items=[DigestItem(
            rank=1, channel=p.channel, message_id=p.message_id, title='地震确认', summary='应急响应启动',
            score=9, event_at=p.published_at, night_alert=True, confirmed_source='Reuters', category='突发',
            impact_overall='', impact_us='', impact_cn='', impact_commodities='') for p in posts])
    evaluator, sender = MockEvaluator(digest_builder=digest), MockWebhookSender()
    runner = NewsMonitorRunner(Settings(db_path=repo.db_path, digest_min_candidates=1, digest_card_interval_seconds=0),
                               repo, MockScraper(), evaluator, sender)
    runner._now = lambda: clock[0]
    repo.save_post(post(1))
    assert runner.process_pending()['alerts_sent'] == 1
    clock[0] += timedelta(minutes=4)
    repo.save_post(post(2))
    assert runner.process_pending()['alerts_sent'] == 0  # 30-minute notification cooldown
    assert len(runner.quiet.archived(clock[0])) == 2
    clock[0] = datetime(2026, 9, 9, 23, 59, tzinfo=timezone.utc)
    repo.save_post(post(3))
    def slow_digest(posts):
        result = digest(posts)
        clock[0] += timedelta(minutes=2)
        return result
    evaluator.digest_builder = slow_digest
    assert runner.process_pending()['alerts_sent'] == 0  # 08:01: archive, not another instant alert
    assert len(sender.sent_payloads) == 1
    assert len(runner.quiet.archived(clock[0])) == 3
