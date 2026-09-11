from datetime import datetime, timedelta, timezone

from tg_news_monitor.core.policy import DeliveryPolicy, fingerprint, is_fresh


def test_freshness_budget_and_persistent_duplicate_claim(tmp_path):
    from tg_news_monitor.storage.repository import PostRepository
    from tg_news_monitor.core.policy import urgent
    now = datetime.now(timezone.utc)
    assert is_fresh(now, 1800)
    assert not is_fresh(now - timedelta(hours=2), 1800)
    assert not is_fresh(now + timedelta(minutes=5), 1800)
    assert not is_fresh(now.replace(tzinfo=None), 1800)
    assert not is_fresh(PostRepository._parse_iso_dt(now.replace(tzinfo=None).isoformat()), 1800)
    # 0 = unlimited age; still reject naive and far-future timestamps
    assert is_fresh(now - timedelta(days=400), 0)
    assert not is_fresh(now + timedelta(minutes=5), 0)
    assert not is_fresh(now.replace(tzinfo=None), 0)
    assert not is_fresh(None, 0)
    assert urgent('Central bank announces emergency rate cut')
    assert not urgent('Daily equity market commentary')
    db = str(tmp_path / 'policy.db')
    policy = DeliveryPolicy(db)
    assert policy.reserve_call(180, 2)
    assert not DeliveryPolicy(db).reserve_call(180, 2)
    assert policy.reserve_call(0, 2)
    assert not policy.reserve_call(0, 2)
    assert fingerprint('Fed raises rates 25 bps') != fingerprint('Fed raises rates 50 bps')
    assert policy.claim('Fed raises rates 25 bps', 'Rate decision')
    assert not DeliveryPolicy(db).claim('Fed raises rates 25 bps', 'Another title')
    assert 'Rate decision' in policy.history()
    claimed = policy.seen_many(['Fed raises rates 25 bps', 'Unrelated equity comment'])
    assert fingerprint('Fed raises rates 25 bps') in claimed
    assert fingerprint('Unrelated equity comment') not in claimed


def test_pipeline_stale_duplicate_failure_batch_and_model_output(tmp_path):
    from tg_news_monitor.config import Settings
    from tg_news_monitor.core.models import DigestBrief, DigestItem, TelegramPost
    from tg_news_monitor.core.runner import NewsMonitorRunner
    from tg_news_monitor.storage.repository import PostRepository
    from tests.test_runner import MockScraper, MockEvaluator, MockWebhookSender

    now = datetime.now(timezone.utc)
    db = str(tmp_path / 'pipeline.db')
    repository = PostRepository(db)
    config = Settings(db_path=db, telegram_channels=['wire'], digest_min_candidates=1,
                      digest_max_wait_seconds=0, digest_card_interval_seconds=0,
                      digest_min_interval_seconds=0, digest_max_batch_size=2)
    def post(mid, text, age=0, channel='wire'):
        return TelegramPost(channel=channel, message_id=mid, text=text, direct_url=f'https://t.me/{channel}/{mid}',
                            published_at=now-timedelta(seconds=age))
    repository.save_posts([
        post(1, 'Federal Reserve raises interest rates 25 basis points.', 7200),
        post(2, 'New AI processor doubles benchmark performance.'),
        post(3, 'New AI processor doubles benchmark performance.', channel='other'),
        post(4, 'Global oil supply falls after new pipeline shutdown.'),
        post(5, 'Major bank reports record quarterly net income.'),
    ])
    evaluator = MockEvaluator()
    sender = MockWebhookSender()
    runner = NewsMonitorRunner(config, repository, MockScraper(), evaluator, sender)
    def fail(posts):
        raise RuntimeError('simulated model outage')
    evaluator.digest_builder = fail
    runner.process_pending()
    assert len(repository.list_pending_posts()) == 3  # stale and duplicate removed only
    assert len(evaluator.digest_batches[0]) == 2

    def select(posts):
        def item(p, event_at=now):
            return DigestItem(rank=1, channel=p.channel, message_id=p.message_id,
                              title=p.text, summary='New fact', event_at=event_at,
                              category='全球新闻', impact_overall='', impact_us='',
                              impact_cn='', impact_commodities='')
        return DigestBrief(headline='test', overview='', items=[
            item(posts[0]), item(posts[0]),  # duplicate model key
            item(post(999, 'invented')),  # model invented source
            item(posts[1], now-timedelta(days=1)),  # new post about old event
        ])
    evaluator.digest_builder = select
    assert runner.process_pending()['alerts_sent'] == 1
    assert len(sender.sent_payloads) == 1
    assert len(repository.list_pending_posts()) == 1  # batch overflow retained
    text = evaluator.digest_batches[-1][0].text
    repository.save_post(post(8, text, channel='third'))
    config.digest_min_interval_seconds = 180
    before = len(evaluator.digest_batches)
    runner.process_pending()
    assert len(evaluator.digest_batches) == before
    assert all(p.message_id != 8 for p in repository.list_pending_posts())


def test_model_error_is_not_empty_news_and_request_is_bounded():
    import httpx
    import pytest
    from tg_news_monitor.evaluator.grok_client import GrokClient, GrokError
    from tg_news_monitor.core.models import TelegramPost
    post = TelegramPost(channel='wire', message_id=1, text='Important current economic news.', direct_url='https://t.me/wire/1',
                        published_at=datetime.now(timezone.utc))
    client = GrokClient(api_key='unit-test', max_retries=1)
    client._external_client = httpx.Client(transport=httpx.MockTransport(
        lambda request: httpx.Response(200, json={'choices': [{'message': {'content': 'bad JSON'}}]})))
    assert client._build_digest_request_payload([post])['max_tokens'] == 3000
    with pytest.raises(GrokError):
        client.evaluate_digest([post])
    client._external_client.close()


def test_collector_continues_while_digest_worker_waits(tmp_path, monkeypatch):
    from threading import Event
    from tg_news_monitor.config import Settings
    from tg_news_monitor.core.runner import NewsMonitorRunner
    from tests.test_runner import MockScraper, MockEvaluator, MockWebhookSender
    runner = NewsMonitorRunner(Settings(db_path=str(tmp_path / 'daemon.db')),
                               scraper_client=MockScraper(), evaluator=MockEvaluator(),
                               webhook_sender=MockWebhookSender())
    started, release = Event(), Event()
    polls = []
    def worker():
        started.set()
        assert release.wait(3), 'collector was blocked by evaluator'
    def ingest(ingest_only=False):
        assert ingest_only
        polls.append(True)
        if len(polls) == 2:
            assert started.wait(1)
            release.set()
            runner.stop()
        return {'posts_discovered': 0, 'posts_evaluated': 0, 'alerts_sent': 0}
    monkeypatch.setattr(runner, 'process_pending', worker)
    monkeypatch.setattr(runner, 'run_once', ingest)
    monkeypatch.setattr('tg_news_monitor.core.runner.signal.signal', lambda *args: None)
    runner.run_forever()
    assert len(polls) == 2
