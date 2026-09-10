from types import SimpleNamespace

from tg_news_monitor.notifier.card_format import (
    format_morning_item_md,
    is_long_summary,
)


def test_is_long_by_chars_and_lines():
    assert not is_long_summary("短摘要")
    assert is_long_summary("字" * 81)
    assert is_long_summary("一行\n二行\n三行")


def test_morning_long_gets_details_without_links():
    item = SimpleNamespace(
        title="复杂事件",
        summary="这是一条超过八十个汉字的摘要内容用于触发长讯详情展示逻辑，后面还会补充更多事实说明。" + "补充。",
        impact_overall="风险偏好承压",
        actionable_insight="",
        summary_bullets=["事实一", "事实二", "事实三"],
    )
    md = format_morning_item_md(1, item, "08:00")
    assert "事件详情" in md
    assert "原文" not in md
    assert "t.me" not in md
    assert "北京时间：08:00" in md


def test_morning_short_stays_compact():
    item = SimpleNamespace(
        title="短讯",
        summary="美联储官员称通胀仍具粘性。",
        impact_overall="美债波动",
        actionable_insight="",
        summary_bullets=[],
    )
    md = format_morning_item_md(2, item, "07:10")
    assert "事件详情" not in md
    assert "原文" not in md
    assert "北京时间：07:10" in md
    assert "事件时间" not in md
