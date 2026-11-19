"""Feed parsers of the native recorder, on recorded answers: the old recorder's mapping, A-share codes only, bad shells rejected."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from pulse.adapters.sources import cls_news, eastmoney_news, ths_news
from pulse.adapters.sources.news_items import BadAnswer, BlockedPage

from .support import recorded

UTC = timezone.utc
KEYS = {"item_id", "title", "body", "url", "source_published_at", "source_modified_at", "codes", "comments", "level"}


def test_tonghuashun_items_from_the_recorded_answer():
    items = ths_news.recorder_items(recorded("ths_push_p1.json"))
    assert len(items) == 30 and all(set(i) == KEYS for i in items)
    first = items[0]
    assert first["item_id"] == "5707311" and first["source_published_at"] == datetime.fromtimestamp(1795052346, UTC)
    assert first["source_modified_at"] is None and first["comments"] is None
    assert first["codes"][:2] == ["688691", "920179"] and first["url"].startswith("https://news.10jqka.com.cn/")
    assert first["level"] is None                                    # import == "3": only "1" is the old recorder's "important"
    assert all(len(c) == 6 and c.isdigit() for i in items for c in i["codes"])
    assert any(i["codes"] for i in items) and any(not i["codes"] for i in items)


def test_eastmoney_items_keep_a_share_refs_only_and_convert_cst():
    items = eastmoney_news.recorder_items(recorded("eastmoney_news_p1.json"))
    assert len(items) == 50 and all(set(i) == KEYS for i in items)
    assert items[0]["source_published_at"] == datetime(2026, 11, 11, 12, 37, 43, tzinfo=UTC)     # 20:37:43 CST
    assert items[0]["codes"] == []                                                              # 105.NVDA and the 90.BK sector are not A shares
    assert items[1]["codes"] == ["300689"] and items[3]["codes"] == ["601866"]                   # 116.02866 (HK) dropped
    assert items[4]["codes"] == ["688766", "688525"]
    assert all(i["comments"] is not None for i in items)


def test_cls_items_keep_empty_title_revision_time_and_codes():
    items = cls_news.recorder_items(recorded("cls_roll_p1.json"))
    assert len(items) == 50 and all(set(i) == KEYS for i in items)
    a, b = items[0], items[1]
    assert a["title"] == "" and a["body"].startswith("财联社11月11日电") and a["level"] == "C"
    assert a["source_published_at"] == datetime.fromtimestamp(1794400821, UTC) == a["source_modified_at"]
    assert b["source_modified_at"] > b["source_published_at"]                                    # a real revision time (ctime 663, modified 690)
    assert ["300302"] in [i["codes"] for i in items]                                              # sz300302 -> 300302


@pytest.mark.parametrize("fn,body", [
    (ths_news.recorder_items, b"<html>login</html>"), (ths_news.recorder_items, b'{"data": {}}'),
    (eastmoney_news.recorder_items, b"<html>"), (eastmoney_news.recorder_items, b'{"code": "0", "data": null}'),
    (cls_news.recorder_items, b'{"errno": "10012", "msg": "sign error"}'), (cls_news.recorder_items, b'{"errno": 0, "data": {}}'),
    (cls_news.recorder_items, b"[1]")])
def test_a_shell_or_vendor_error_is_a_bad_answer_never_an_empty_list(fn, body):
    with pytest.raises(BadAnswer):
        fn(body)


def test_an_empty_list_is_a_real_answer():
    assert ths_news.recorder_items(b'{"data": {"list": []}}') == []
    assert eastmoney_news.recorder_items(b'{"data": {"fastNewsList": []}}') == []
    assert cls_news.recorder_items(b'{"errno": 0, "data": {"roll_data": []}}') == []


@pytest.mark.parametrize("fn", [ths_news.recorder_items, eastmoney_news.recorder_items, cls_news.recorder_items])
def test_an_html_body_is_a_blocked_page_other_text_is_only_a_bad_answer(fn):
    with pytest.raises(BlockedPage):
        fn("﻿ <html><title>captcha</title></html>".encode("utf-8"))
    with pytest.raises(BadAnswer) as e:
        fn(b"Service Unavailable")
    assert not isinstance(e.value, BlockedPage)
