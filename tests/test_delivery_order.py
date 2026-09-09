from datetime import timedelta

from tg_news_monitor.core.models import DigestBrief
from tests.test_quiet_hours import make_runner, make_post, item_for, sh_time


def test_delivery_follows_channel_time_not_importance(tmp_path):
    now = sh_time(12)
    runner, repo, evaluator, sender = make_runner(tmp_path)
    older = make_post(1, 'A manufacturer announced a new industrial facility.', now - timedelta(minutes=8))
    newer = make_post(2, 'A technology company released a new processor today.', now - timedelta(minutes=2))
    repo.save_posts([newer, older])
    evaluator.digest_builder = lambda posts: DigestBrief(
        headline='test', overview='', has_material_news=True,
        items=[item_for(newer, 10, 'newer', rank=1), item_for(older, 7, 'older', rank=2)],
    )
    sent = []
    runner._send_digest_item_card = lambda item, published_at: sent.append(item.message_id) or True
    assert runner.process_pending(now=now)['alerts_sent'] == 2
    assert sent == [1, 2]
