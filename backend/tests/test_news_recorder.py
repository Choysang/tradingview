import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx

from backend.app.warehouse import intraday as I
from backend.app.warehouse import news as N

UTC = timezone.utc
T0 = datetime(2026, 11, 2, 2, 0, tzinfo=UTC)


def _client(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_sources_parse_and_keep_only_a_share_codes():
    def handler(req: httpx.Request) -> httpx.Response:
        host = req.url.host
        if host == "news.10jqka.com.cn":
            return httpx.Response(200, json={"data": {"list": [{"id": "1", "title": "t", "digest": "d", "ctime": "1793594730",
                                                                  "stock": [{"stockCode": "002695"}], "import": "1"}]}})
        if host == "np-weblist.eastmoney.com":
            return httpx.Response(200, json={"data": {"fastNewsList": [{"code": "9", "title": "e", "summary": "s",
                                  "showTime": "2026-11-02 10:00:00", "stockList": ["1.600600", "90.BK1408", "0.000001"], "pinglun_Num": 3}]}})
        assert "sign" in req.url.params
        return httpx.Response(200, json={"errno": 0, "data": {"roll_data": [{"id": 5, "title": "", "content": "c", "ctime": 1793596039,
                                         "modified_time": 1793596146, "level": "B", "comment_num": 0,
                                         "stock_list": [{"StockID": "sz002695", "name": "x"}]}]}})
    with _client(handler) as c:
        ths, em, cls = N.fetch_ths(c), N.fetch_em(c), N.fetch_cls(c)
    assert ths[0]["codes"] == ["002695"] and ths[0]["level"] == "important"
    assert em[0]["codes"] == ["600600", "000001"] and em[0]["comments"] == 3
    assert em[0]["source_published_at"] == datetime(2026, 11, 2, 2, 0, tzinfo=UTC)  # 10:00 CST
    assert cls[0]["codes"] == ["002695"] and cls[0]["source_modified_at"] > cls[0]["source_published_at"]


def test_bad_answers_raise_source_errors():
    for resp in (httpx.Response(403), httpx.Response(200, text="<html>"), httpx.Response(200, json={"errno": "10012", "msg": "签名错误"})):
        with _client(lambda req, r=resp: r) as c:
            try:
                N.fetch_cls(c)
            except N.SourceError:
                continue
            raise AssertionError(f"no error for {resp}")


def test_watch_merges_expires_and_caps(tmp_path: Path):
    path = N.watch_path(tmp_path)
    N.update_watch(path, ["600001", "600002"], T0, minutes=10)
    N.update_watch(path, ["600003"], T0 + timedelta(minutes=5), minutes=10)
    assert N.read_watch(path, T0 + timedelta(minutes=12)) == {"600003"}  # the first two expired
    N.update_watch(path, [f"6000{i:02d}" for i in range(10, 30)], T0, minutes=10, cap=5)
    assert len(json.loads(path.read_text(encoding="utf-8"))) == 5
    cst = T0.astimezone(timezone(timedelta(hours=8)))
    assert N.read_watch(path, cst) == N.read_watch(path, T0)  # time zones do not change the answer
    path.write_text("{broken", encoding="utf-8")
    assert N.read_watch(path, T0) == set()
    assert N.read_watch(tmp_path / "missing.json", T0) == set()


def test_guba_counts_only_posts_after_receipt():
    posts = [{"post_publish_time": "2026-11-02 10:00:30", "user_id": "a", "post_title": "x", "post_comment_count": 2},
             {"post_publish_time": "2026-11-02 10:01:00", "user_id": "a", "post_title": "y", "post_comment_count": 0},
             {"post_publish_time": "2026-11-02 09:59:00", "user_id": "b", "post_title": "old"}]
    got = N.guba_since(posts, T0)  # 10:00 CST
    assert (got["new_posts"], got["new_authors"], got["comments_on_new"]) == (2, 1, 2)


def test_recorder_primes_then_triggers_and_breaks(tmp_path: Path):
    answers = [[{"item_id": "1", "title": "a", "body": "", "codes": ["600001"]}],
               [{"item_id": "1", "title": "a", "body": "", "codes": ["600001"]},
                {"item_id": "2", "title": "b", "body": "", "codes": ["300750", "600002"]}],
               [{"item_id": "2", "title": "b (corrected)", "body": "", "codes": ["600002"]}]]
    feed = iter(answers)
    src = N.Source("ths.724", lambda c: next(feed), 10)
    rec = N.NewsRecorder(tmp_path / "d", tmp_path, httpx.Client(), [src])
    for i in range(3):
        rec.poll(src, float(i * 10))
    assert (src.new, src.revisions) == (2, 1)
    assert rec.rows[-1]["supersedes"] == rec.rows[1]["revision_id"]
    assert N.read_watch(N.watch_path(tmp_path), datetime.now(UTC)) == {"600002"}  # backlog item 1 did not trigger; 300750 is not main board
    assert len(rec.guba_queue) == 3  # +1, +3, +5 min for the one main-board code
    bad = N.Source("cls.telegraph", lambda c: (_ for _ in ()).throw(N.SourceError("cls errno 10012")), 30)
    for i in range(3):
        rec.poll(bad, float(i))
    assert bad.stopped and "10012" in bad.stopped


def test_intraday_hot_poll_adds_watched_codes(tmp_path: Path):
    rec = I.Recorder.__new__(I.Recorder)
    rec.static, rec.hot, rec.bonds, rec.codes, rec.stats = set(), {"600001"}, {}, ["600001", "600002", "600003"], {}
    rec.watch_seen = set()
    rec.watch_file = N.watch_path(tmp_path)
    N.update_watch(rec.watch_file, ["600002", "300750"], datetime.now(UTC))
    seen = []
    rec.snapshot = lambda kind, codes: seen.append((kind, codes))
    rec.poll_hot("hot")
    rec.poll_hot("auction_hot")  # the auction grid stays on the static pool
    assert seen == [("hot", ["600001", "600002"])]
    assert rec.stats["watch_max"] == 1 and rec.watch_seen == {"600002"}


def test_codes_only_take_whole_codes():
    assert N._codes(["sz301052", "1.600519", "002695", "0.469999x", "46.9999", "BK1408", "sh600519"]) == ["301052", "600519", "002695"]


def test_restart_on_the_same_day_does_not_record_the_backlog_again(tmp_path: Path):
    item = {"item_id": "1", "title": "a", "body": "", "codes": ["600001"]}
    first = N.Source("ths.724", lambda c: [item], 10)
    rec = N.NewsRecorder(tmp_path / "d", tmp_path, httpx.Client(), [first])
    rec.poll(first, 0.0)
    rec.flush(force=True)
    (tmp_path / "d" / "messages" / "part-999999-9999.parquet").write_bytes(b"cut off by a kill")  # a broken part
    again = N.Source("ths.724", lambda c: [item], 10)
    rec2 = N.NewsRecorder(tmp_path / "d", tmp_path, httpx.Client(), [again])
    assert rec2.stats["resumed_items"] == 1 and any("resume" in e for e in rec2.stats["errors"])
    rec2.poll(again, 0.0)
    assert (again.new, again.revisions, rec2.rows) == (0, 0, [])
    changed = N.Source("ths.724", lambda c: [{**item, "title": "a (corrected)"}], 10)
    rec3 = N.NewsRecorder(tmp_path / "d", tmp_path, httpx.Client(), [changed])
    rec3.poll(changed, 0.0)
    assert (changed.new, changed.revisions) == (0, 1)  # an edit made while we were down is a revision, not a new item
