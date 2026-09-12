"""Send-time near-duplicate guard for news24 digest cards.

Reproduces the 2026-09-11 diesel pair (walterbloomberg 35376/35377) and
checks that a real numeric update and unrelated macro stories still send.
"""

from __future__ import annotations

from datetime import datetime, timezone

from tg_news_monitor.config import Settings
from tg_news_monitor.core.models import DigestBrief, DigestItem, TelegramPost
from tg_news_monitor.core.near_dup import (
    event_key,
    is_material_update,
    is_near_duplicate_blob,
    sanitize_followup_copy,
    similar_event,
)
from tg_news_monitor.core.policy import DeliveryPolicy
from tg_news_monitor.core.runner import NewsMonitorRunner
from tg_news_monitor.evaluator.prompt import DIGEST_SYSTEM_PROMPT, STORY_DIGEST_SYSTEM_PROMPT
from tg_news_monitor.storage.repository import PostRepository
from tests.test_runner import MockEvaluator, MockScraper, MockWebhookSender

BJ_1541 = datetime(2026, 9, 11, 7, 41, tzinfo=timezone.utc)
BJ_1547 = datetime(2026, 9, 11, 7, 47, tzinfo=timezone.utc)

DIESEL_TITLE_1 = "美国柴油零售均价突破每加仑6美元"
DIESEL_SUMMARY_1 = "美国柴油零售均价升至每加仑6美元。"
DIESEL_BLOB_1 = f"{DIESEL_TITLE_1}: {DIESEL_SUMMARY_1}"
DIESEL_TITLE_2 = "美国柴油价格突破每加仑6美元创历史新高"
DIESEL_SUMMARY_2 = "美国柴油价格突破每加仑6美元，加州部分地区达6.06美元，创历史新高。"
DIESEL_BLOB_2 = f"{DIESEL_TITLE_2}: {DIESEL_SUMMARY_2}"

DIESEL_POST_1 = (
    "US retail diesel average breaks $6 a gallon, Walter Bloomberg says."
)
DIESEL_POST_2 = (
    "US diesel prices top $6 a gallon hitting a record high; California prints $6.06."
)


def _item(
    post: TelegramPost,
    title: str,
    summary: str,
    *,
    is_update: bool = False,
    update_reason: str = "",
    score: int = 8,
    category: str = "宏观财经",
    summary_bullets=None,
) -> DigestItem:
    return DigestItem(
        rank=1,
        channel=post.channel,
        message_id=post.message_id,
        title=title,
        summary=summary,
        category=category,
        score=score,
        event_at=post.published_at,
        is_update=is_update,
        update_reason=update_reason,
        summary_bullets=list(summary_bullets or []),
        impact_overall="能源成本上升",
        impact_us="交运与通胀预期承压",
        impact_cn="无直接影响",
        impact_commodities="柴油相关产品偏多",
    )


def _post(mid: int, text: str, published_at: datetime, channel: str = "walterbloomberg") -> TelegramPost:
    return TelegramPost(
        channel=channel,
        message_id=mid,
        text=text,
        direct_url=f"https://t.me/{channel}/{mid}",
        published_at=published_at,
    )


def _news_settings(db: str, **extra) -> Settings:
    kwargs = dict(
        db_path=db,
        telegram_channels=["walterbloomberg"],
        digest_min_candidates=1,
        digest_max_wait_seconds=0,
        digest_card_interval_seconds=0,
        digest_min_interval_seconds=0,
        news_max_age_seconds=1800,
        hotness_threshold=7,
        quiet_hours="",
        shoulder_hours="",
        morning_flush_enabled=False,
    )
    kwargs.update(extra)
    return Settings(**kwargs)


class TestSimilarEventDieselPair:
    def test_diesel_rewrites_are_the_same_event(self):
        assert similar_event(DIESEL_BLOB_1, DIESEL_BLOB_2)
        assert similar_event(DIESEL_TITLE_1, DIESEL_TITLE_2)
        assert is_near_duplicate_blob(DIESEL_BLOB_2, [DIESEL_BLOB_1])

    def test_framing_only_update_reason_is_not_material(self):
        assert not is_material_update(True, "创历史新高", DIESEL_BLOB_1)
        assert not is_material_update(True, "补充加州地区", DIESEL_BLOB_1)
        assert is_near_duplicate_blob(
            DIESEL_BLOB_2,
            [DIESEL_BLOB_1],
            is_update=True,
            update_reason="创历史新高",
        )

    def test_new_dollar_print_is_material_and_can_send(self):
        reason = "全国均价进一步升至每加仑7美元"
        assert is_material_update(True, reason, DIESEL_BLOB_1)
        assert not is_near_duplicate_blob(
            DIESEL_BLOB_2,
            [DIESEL_BLOB_1],
            is_update=True,
            update_reason=reason,
        )

    def test_unrelated_macro_and_gasoline_are_not_near_dups(self):
        fed = "美联储维持利率不变: 美联储宣布将联邦基金利率维持在现有区间。"
        gas = "美国汽油零售均价突破每加仑4美元: 全美汽油均价升至每加仑4美元。"
        assert not similar_event(DIESEL_BLOB_1, fed)
        assert not similar_event(DIESEL_BLOB_1, gas)
        assert not is_near_duplicate_blob(fed, [DIESEL_BLOB_1])
        assert not is_near_duplicate_blob(gas, [DIESEL_BLOB_1])

    def test_rate_hike_25_vs_50_not_collapsed(self):
        a = "美联储加息25个基点: 美联储将利率上调25个基点。"
        b = "美联储加息50个基点: 美联储将利率上调50个基点。"
        assert not similar_event(a, b)

    def test_event_key_matches_title_reword_not_fed(self):
        key1 = event_key(DIESEL_BLOB_1)
        key2 = event_key(DIESEL_TITLE_2)
        assert key1 and key2 and key1 == key2
        assert event_key("美联储维持利率不变: 美联储宣布将联邦基金利率维持在现有区间。") != key1


class TestDeliveryPolicyNearDup:
    def test_claim_and_near_dup_are_per_group(self, tmp_path):
        db = str(tmp_path / "iso.db")
        news = DeliveryPolicy(db, group_id="news24")
        other = DeliveryPolicy(db, group_id="old_stories")
        assert news.claim(DIESEL_POST_1, DIESEL_BLOB_1)
        assert news.is_near_duplicate(DIESEL_TITLE_2, DIESEL_SUMMARY_2)
        assert not other.is_near_duplicate(DIESEL_TITLE_2, DIESEL_SUMMARY_2)
        assert other.claim(DIESEL_POST_2, DIESEL_BLOB_2)

    def test_event_key_blocks_reword_claim(self, tmp_path):
        db = str(tmp_path / "ek.db")
        policy = DeliveryPolicy(db, group_id="news24")
        assert policy.claim(DIESEL_POST_1, DIESEL_BLOB_1)
        assert not policy.claim(DIESEL_POST_2, DIESEL_TITLE_2 + ": " + DIESEL_TITLE_2)
        assert DIESEL_TITLE_1 in policy.history()
        assert policy.history().count(DIESEL_TITLE_1) == 1


class TestNews24RunnerDieselGuard:
    def test_second_diesel_card_is_filtered_not_sent(self, tmp_path):
        db = str(tmp_path / "diesel.db")
        repo = PostRepository(db)
        repo.save_post(_post(35376, DIESEL_POST_1, BJ_1541))
        config = _news_settings(db)

        def first(posts):
            p = posts[0]
            return DigestBrief(
                headline="柴油",
                overview="",
                items=[_item(p, DIESEL_TITLE_1, DIESEL_SUMMARY_1)],
                has_material_news=True,
            )

        evaluator = MockEvaluator(digest_builder=first)
        sender = MockWebhookSender()
        runner = NewsMonitorRunner(config, repo, MockScraper(), evaluator, sender)
        assert runner.process_pending(now=BJ_1541)["alerts_sent"] == 1
        assert len(sender.sent_payloads) == 1

        repo.save_post(_post(35377, DIESEL_POST_2, BJ_1547))

        def second(posts):
            p = posts[0]
            return DigestBrief(
                headline="柴油再报",
                overview="",
                items=[_item(p, DIESEL_TITLE_2, DIESEL_SUMMARY_2)],
                has_material_news=True,
            )

        evaluator.digest_builder = second
        assert runner.process_pending(now=BJ_1547)["alerts_sent"] == 0
        assert len(sender.sent_payloads) == 1
        row = repo.get_post("walterbloomberg", 35377)
        assert row["is_filtered"] == 1
        assert row["filter_reason"] == "near_duplicate"
        assert row["alert_sent"] == 0

    def test_material_update_with_new_number_still_sends(self, tmp_path):
        db = str(tmp_path / "update.db")
        repo = PostRepository(db)
        repo.save_post(_post(35376, DIESEL_POST_1, BJ_1541))
        config = _news_settings(db)
        evaluator = MockEvaluator(
            digest_builder=lambda posts: DigestBrief(
                headline="柴油",
                overview="",
                items=[_item(posts[0], DIESEL_TITLE_1, DIESEL_SUMMARY_1)],
                has_material_news=True,
            )
        )
        sender = MockWebhookSender()
        runner = NewsMonitorRunner(config, repo, MockScraper(), evaluator, sender)
        assert runner.process_pending(now=BJ_1541)["alerts_sent"] == 1

        repo.save_post(_post(35378, "US diesel climbs further to $7 a gallon.", BJ_1547))
        evaluator.digest_builder = lambda posts: DigestBrief(
            headline="柴油更新",
            overview="",
            items=[
                _item(
                    posts[0],
                    DIESEL_TITLE_2,
                    DIESEL_SUMMARY_2,
                    is_update=True,
                    update_reason="全国均价进一步升至每加仑7美元",
                )
            ],
            has_material_news=True,
        )
        assert runner.process_pending(now=BJ_1547)["alerts_sent"] == 1
        assert len(sender.sent_payloads) == 2

    def test_unrelated_fed_story_still_sends_after_diesel(self, tmp_path):
        db = str(tmp_path / "fed.db")
        repo = PostRepository(db)
        repo.save_post(_post(35376, DIESEL_POST_1, BJ_1541))
        config = _news_settings(db)
        evaluator = MockEvaluator(
            digest_builder=lambda posts: DigestBrief(
                headline="柴油",
                overview="",
                items=[_item(posts[0], DIESEL_TITLE_1, DIESEL_SUMMARY_1)],
                has_material_news=True,
            )
        )
        sender = MockWebhookSender()
        runner = NewsMonitorRunner(config, repo, MockScraper(), evaluator, sender)
        assert runner.process_pending(now=BJ_1541)["alerts_sent"] == 1

        fed_text = "Federal Reserve holds the federal funds rate unchanged."
        repo.save_post(_post(40001, fed_text, BJ_1547, channel="wire"))
        evaluator.digest_builder = lambda posts: DigestBrief(
            headline="联储",
            overview="",
            items=[
                _item(
                    posts[0],
                    "美联储维持利率不变",
                    "美联储宣布将联邦基金利率维持在现有区间。",
                    category="宏观财经",
                )
            ],
            has_material_news=True,
        )
        assert runner.process_pending(now=BJ_1547)["alerts_sent"] == 1
        assert len(sender.sent_payloads) == 2


AMODEI_TITLE_1 = "Anthropic CEO Amodei呼吁放缓前沿AI"
AMODEI_SUMMARY_1 = "Anthropic首席执行官Amodei呼吁放缓前沿模型部署，并提出三步走监管路径。"
AMODEI_BLOB_1 = f"{AMODEI_TITLE_1}: {AMODEI_SUMMARY_1}"
AMODEI_BULLETS_1 = [
    "Anthropic首席执行官Dario Amodei呼吁放缓前沿人工智能模型的部署节奏。",
    "他提出先评估、再限制、后放开的三步走监管路径。",
]
AMODEI_TITLE_2 = "Amodei再吁放缓并承诺第三方评估永久访问"
AMODEI_SUMMARY_2 = "Amodei再次呼吁放缓前沿模型，并承诺向第三方评估方提供永久访问权限。"
AMODEI_BULLETS_2 = [
    "Anthropic CEO Amodei再次呼吁放缓前沿人工智能模型部署。",
    "他重申先评估、再限制、后放开的三步走监管路径。",
    "Anthropic承诺给予第三方评估方对模型的永久访问权。",
]
AMODEI_REASON = "承诺给予第三方评估方永久访问权"
AMODEI_REHASH_TITLE = "Amodei再度呼吁放缓前沿AI部署"
AMODEI_REHASH_SUMMARY = "Anthropic首席执行官再次呼吁放缓前沿模型并重申三步走路径。"
AMODEI_REHASH_BULLETS = [
    "Amodei再次呼吁放缓前沿人工智能模型的部署节奏。",
    "他重申三步走监管路径。",
]


class TestFollowupCopySanitize:
    def test_new_angle_keeps_delta_drops_old_premise(self):
        result = sanitize_followup_copy(
            AMODEI_TITLE_2,
            AMODEI_SUMMARY_2,
            AMODEI_BULLETS_2,
            [AMODEI_BLOB_1],
            is_update=True,
            update_reason=AMODEI_REASON,
        )
        assert result.skip is False
        assert result.rewritten is True
        blob = " ".join([result.title, result.summary, *result.bullets])
        assert "永久访问" in blob or "第三方" in blob
        assert not any("放缓" in b and "永久" not in b and "第三方" not in b for b in result.bullets)
        assert "三步走" not in blob
        assert not result.title.startswith("Amodei再吁放缓")

    def test_rephrased_old_facts_are_skipped(self):
        result = sanitize_followup_copy(
            AMODEI_REHASH_TITLE,
            AMODEI_REHASH_SUMMARY,
            AMODEI_REHASH_BULLETS,
            [AMODEI_BLOB_1],
        )
        assert result.skip is True

    def test_diesel_rewrite_still_skipped(self):
        result = sanitize_followup_copy(
            DIESEL_TITLE_2,
            DIESEL_SUMMARY_2,
            [],
            [DIESEL_BLOB_1],
        )
        assert result.skip is True


class TestPromptNudgeAndWechatBypass:
    def test_news_and_story_prompts_require_is_update_for_repeats(self):
        needle = "仅当 is_update=true 且 update_reason 写明新增关键事实"
        assert needle in DIGEST_SYSTEM_PROMPT
        assert needle in STORY_DIGEST_SYSTEM_PROMPT
        from tg_news_monitor.evaluator.prompt import compose_digest_prompts

        system, user = compose_digest_prompts(
            [_post(1, "placeholder news text here", BJ_1541)]
        )
        assert "is_update=true" in user
        assert "update_reason" in user
        assert "只写本次新增事实" in system
        assert "禁止复述" in user
        assert "只写新增事实" in user

    def test_followup_with_new_fact_sends_stripped_card(self, tmp_path):
        db = str(tmp_path / "amodei.db")
        repo = PostRepository(db)
        repo.save_post(_post(50001, "Amodei calls to slow down frontier AI.", BJ_1541, channel="wire"))
        config = _news_settings(db)
        evaluator = MockEvaluator(
            digest_builder=lambda posts: DigestBrief(
                headline="放缓",
                overview="",
                items=[
                    _item(
                        posts[0],
                        AMODEI_TITLE_1,
                        AMODEI_SUMMARY_1,
                        category="科技/AI",
                        summary_bullets=AMODEI_BULLETS_1,
                    )
                ],
                has_material_news=True,
            )
        )
        sender = MockWebhookSender()
        runner = NewsMonitorRunner(config, repo, MockScraper(), evaluator, sender)
        assert runner.process_pending(now=BJ_1541)["alerts_sent"] == 1

        repo.save_post(
            _post(
                50002,
                "Anthropic pledges permanent third-party evaluator access.",
                BJ_1547,
                channel="wire",
            )
        )
        evaluator.digest_builder = lambda posts: DigestBrief(
            headline="评估访问",
            overview="",
            items=[
                _item(
                    posts[0],
                    AMODEI_TITLE_2,
                    AMODEI_SUMMARY_2,
                    is_update=True,
                    update_reason=AMODEI_REASON,
                    category="科技/AI",
                    summary_bullets=AMODEI_BULLETS_2,
                )
            ],
            has_material_news=True,
        )
        assert runner.process_pending(now=BJ_1547)["alerts_sent"] == 1
        assert len(sender.sent_payloads) == 2
        second = str(sender.sent_payloads[1])
        assert "永久访问" in second or "第三方" in second
        assert "三步走" not in second
        # Reader-facing bullets should not reopen with the already-delivered slowdown.
        assert "再次呼吁放缓" not in second

    def test_followup_with_only_rephrased_old_facts_is_filtered(self, tmp_path):
        db = str(tmp_path / "amodei_rehash.db")
        repo = PostRepository(db)
        repo.save_post(_post(50001, "Amodei calls to slow down frontier AI.", BJ_1541, channel="wire"))
        config = _news_settings(db)
        evaluator = MockEvaluator(
            digest_builder=lambda posts: DigestBrief(
                headline="放缓",
                overview="",
                items=[
                    _item(
                        posts[0],
                        AMODEI_TITLE_1,
                        AMODEI_SUMMARY_1,
                        category="科技/AI",
                        summary_bullets=AMODEI_BULLETS_1,
                    )
                ],
                has_material_news=True,
            )
        )
        sender = MockWebhookSender()
        runner = NewsMonitorRunner(config, repo, MockScraper(), evaluator, sender)
        assert runner.process_pending(now=BJ_1541)["alerts_sent"] == 1

        repo.save_post(_post(50003, "Amodei again urges a slowdown.", BJ_1547, channel="wire"))
        evaluator.digest_builder = lambda posts: DigestBrief(
            headline="再放缓",
            overview="",
            items=[
                _item(
                    posts[0],
                    AMODEI_REHASH_TITLE,
                    AMODEI_REHASH_SUMMARY,
                    category="科技/AI",
                    summary_bullets=AMODEI_REHASH_BULLETS,
                )
            ],
            has_material_news=True,
        )
        assert runner.process_pending(now=BJ_1547)["alerts_sent"] == 0
        assert len(sender.sent_payloads) == 1
        row = repo.get_post("wire", 50003)
        assert row["is_filtered"] == 1
        assert row["filter_reason"] == "near_duplicate"

    def test_wechat_photo_similar_captions_still_send(self, tmp_path):
        db = str(tmp_path / "photo.db")
        repo = PostRepository(db)
        now = BJ_1541
        captions = [
            (10, "上海石库门弄堂春日，衣裳晾在竹竿上。"),
            (11, "上海石库门弄堂冬日，衣裳晾在竹竿上。"),
        ]
        for mid, text in captions:
            repo.save_post(
                TelegramPost(
                    channel="oldpix",
                    message_id=mid,
                    text=text,
                    has_media=True,
                    media_type="photo",
                    media_urls=[f"https://cdn.example.com/{mid}.jpg"],
                    direct_url=f"https://t.me/oldpix/{mid}",
                    published_at=now,
                    group_id="old_photos",
                ),
                group_id="old_photos",
            )

        def builder(posts):
            items = []
            for i, p in enumerate(posts, start=1):
                items.append(
                    DigestItem(
                        rank=i,
                        channel=p.channel,
                        message_id=p.message_id,
                        title=p.text[:12],
                        summary=p.text,
                        category="历史影像",
                        score=8,
                        event_at=p.published_at,
                        impact_overall="无直接影响",
                        impact_us="无直接影响",
                        impact_cn="无直接影响",
                        impact_commodities="无直接影响",
                        media_urls=list(p.media_urls or []),
                    )
                )
            return DigestBrief(headline="影像", overview="", items=items, has_material_news=True)

        config = Settings(
            db_path=db,
            digest_min_candidates=1,
            digest_max_wait_seconds=0,
            digest_card_interval_seconds=0,
            digest_min_interval_seconds=0,
            news_max_age_seconds=86400,
            morning_flush_enabled=False,
            quiet_hours="",
            shoulder_hours="",
            groups=[
                {
                    "id": "old_photos",
                    "channels": ["oldpix"],
                    "webhook_url": "https://example.com/photos",
                    "hotness_threshold": 7,
                    "digest_min_candidates": 1,
                    "digest_max_wait_seconds": 0,
                    "digest_min_interval_seconds": 0,
                    "digest_card_interval_seconds": 0,
                    "news_max_age_seconds": 86400,
                    "morning_flush_enabled": False,
                    "quiet_hours": "",
                    "shoulder_hours": "",
                    "card_profile": {
                        "subtitle": "公众号图片素材",
                        "include_investment_impact": False,
                        "prompt_variant": "wechat_photo",
                    },
                }
            ],
        )
        sender = MockWebhookSender()
        runner = NewsMonitorRunner(
            config, repo, MockScraper(), MockEvaluator(digest_builder=builder), sender
        )
        summary = runner.process_pending(now=now)
        assert summary["alerts_sent"] == 2
        assert len(sender.sent_payloads) == 2
