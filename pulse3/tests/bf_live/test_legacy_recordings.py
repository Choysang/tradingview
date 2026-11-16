"""The legacy recorder adapter: read-only, source times kept (naive CST -> UTC), zero price is missing, schema drift is loud."""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

import polars as pl
import pytest

from pulse.adapters.sources import legacy_recordings as LR
from pulse.adapters.sources import registry

from . import support as T

UTC = timezone.utc


def test_reading_never_modifies_the_old_tree(legacy_root):
    q, hot = T.scenario_rows()
    T.write_quotes(legacy_root, "quotes", "part-0001.parquet", q)
    T.write_quotes(legacy_root, "hot", "part-0001.parquet", hot)
    T.write_news(legacy_root, "part-0001.parquet", [T.news_row("n1", "r1", "标题", "正文", datetime(2026, 11, 12, 1, 0), datetime(2026, 11, 12, 1, 1), ["000001"])])
    (legacy_root / "intraday" / T.DAY.isoformat() / "done.json").write_text('{"ok": true}', encoding="utf-8")
    before = T.tree_state(legacy_root)
    rec = LR.LegacyRecordings(legacy_root)
    for stream in ("quotes", "hot"):
        for p in rec.parts(stream, T.DAY):
            rec.read_quotes(p, T.DAY)
    for p in rec.parts("news", T.DAY):
        rec.read_news(p)
    assert rec.done_marker(T.DAY) == {"ok": True}
    assert T.tree_state(legacy_root) == before


def test_quote_times_are_converted_to_utc_and_zero_price_is_missing(legacy_root):
    q, _ = T.scenario_rows()
    T.write_quotes(legacy_root, "quotes", "part-0001.parquet", q)
    rec = LR.LegacyRecordings(legacy_root)
    df = rec.read_quotes(rec.parts("quotes", T.DAY)[0], T.DAY)
    row = df.filter((pl.col("code") == "000001") & (pl.col("quote_at").dt.convert_time_zone("Asia/Shanghai").dt.hour() == 9)).sort("quote_at").row(1, named=True)
    assert row["quote_at"] == datetime(2026, 11, 12, 1, 31, tzinfo=UTC) and row["poll_at"] == row["quote_at"]       # 09:31 CST = 01:31 UTC
    assert row["bid2_px"] is None and row["bid1_px"] == Decimal("10.4900") and row["bid1_lots"] == 800.0              # absent level is NULL, lots stay
    assert df.schema["price"] == pl.Decimal(18, 4) and df.schema["amount_yuan"] == pl.Decimal(20, 2)
    assert df["source_part"][0] == "quotes/part-0001.parquet" and df["source_stream"][0] == "quotes"


def test_missing_required_column_is_a_schema_mismatch(legacy_root):
    q, _ = T.scenario_rows()
    p = T.write_quotes(legacy_root, "quotes", "part-0001.parquet", q)
    pl.read_parquet(p).drop("limit_up").write_parquet(p)
    rec = LR.LegacyRecordings(legacy_root)
    with pytest.raises(LR.SchemaMismatch, match="limit_up"):
        rec.read_quotes(rec.parts("quotes", T.DAY)[0], T.DAY)


def test_partial_file_is_unreadable_not_guessed(legacy_root):
    d = legacy_root / "intraday" / T.DAY.isoformat() / "quotes"
    d.mkdir(parents=True)
    (d / "part-0001.parquet").write_bytes(b"PAR1 half written")
    rec = LR.LegacyRecordings(legacy_root)
    with pytest.raises(LR.PartUnreadable):
        rec.read_quotes(rec.parts("quotes", T.DAY)[0], T.DAY)


def test_news_naive_zone_is_inferred_from_received_at(legacy_root):
    recv = datetime(2026, 11, 12, 1, 5)                                                    # received_at is UTC
    T.write_news(legacy_root, "part-0001.parquet", [T.news_row("a", "r1", "t", "b", datetime(2026, 11, 12, 1, 4), recv, ["000001"])])
    rec = LR.LegacyRecordings(legacy_root)
    m = rec.read_news(rec.parts("news", T.DAY)[0])[0]
    assert m["source_time_zone"] == "UTC" and m["source_published_at"] == datetime(2026, 11, 12, 1, 4, tzinfo=UTC)
    d2 = T.DAY.replace(day=13)
    T.write_news(legacy_root, "part-0001.parquet", [T.news_row("b", "r1", "t", "b", datetime(2026, 11, 13, 9, 4), datetime(2026, 11, 13, 1, 5), [])], day=d2)
    m2 = rec.read_news(rec.parts("news", d2)[0])[0]
    assert m2["source_time_zone"] == "Asia/Shanghai" and m2["source_published_at"] == datetime(2026, 11, 13, 1, 4, tzinfo=UTC)


def test_recorded_news_roundtrips_to_a_news_record_with_exchange_prefixed_refs(legacy_root):
    T.write_news(legacy_root, "part-0001.parquet", [T.news_row("a", "r1", "财联社快讯", "财联社11月12日电，某公司公告", datetime(2026, 11, 12, 1, 4), datetime(2026, 11, 12, 1, 5), ["600000", "000001"])])
    rec = LR.LegacyRecordings(legacy_root)
    m = rec.read_news(rec.parts("news", T.DAY)[0])[0]
    nr = LR.parse_recorded_news(LR.recorded_news_bytes(m)).records[0]
    assert nr.source_id == LR.NEWS_SOURCE_ID and nr.source_record_id == "ths_724:a" and nr.stock_refs == ("sh600000", "sz000001")
    assert nr.own_dispatch and nr.published_at == datetime(2026, 11, 12, 1, 4, tzinfo=UTC)


def test_ext_prefers_the_compacted_file_and_open_interest_is_optional(legacy_root):
    T.write_ext(legacy_root, [T.ext_row("hf_CHA50CFD", "future", 12800.64)], name="quotes_ext.parquet")
    T.write_ext(legacy_root, [T.ext_row("fx_susdcnh", "fx", 6.6736)], name="part-0001.parquet")
    rec = LR.LegacyRecordings(legacy_root)
    parts = rec.parts("ext", T.DAY)
    assert [p.short for p in parts] == ["quotes_ext.parquet"]
    df = rec.read_ext(parts[0], T.DAY)
    assert df["open_interest"][0] is None and df["price"][0] == Decimal("12800.640000") and df["quote_at"][0] == datetime(2026, 11, 12, 2, 0, tzinfo=UTC)


def test_registry_declares_the_source_with_capability_notes():
    spec = registry.spec(LR.SOURCE_ID, LR.EP_QUOTES)
    assert spec.policy["estimated"] is True and spec.policy["not_tick_data"] is True and spec.policy["sampled_interval_seconds_nominal"] == 60
    for ep in (LR.EP_HOT, LR.EP_EXT, LR.EP_GUBA, LR.EP_ALERTS):
        assert registry.spec(LR.SOURCE_ID, ep).policy["mode"] == "legacy_recording_import_read_only"
    assert registry.spec(LR.NEWS_SOURCE_ID, LR.EP_NEWS).policy["revisions"]
