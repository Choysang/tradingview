"""Inputs of batch 7: RAW daily bars (native refdata dataset preferred, legacy raw bars as fallback), exact limit flags, scope and the panel the batch 6 machinery reads.

Why: E097 retests E093 H1 with exact limit-up flags from raw prices (pre-registration 2.1), E098 needs the seats and the same fills. ``refdata_core.limit_flags_raw`` is the one source of
the flags (reference price: exchange reference > factor-derived on an ex-right day > raw previous close; rows with an unknown rule, unknown ST state or a reference within 0.01 of a half tick
are NOT classified: no leader, follower, core or buy-ability verdict for them).
Source rule (per call): ``bars_native_raw_1d`` + ``adj_factor_native`` (latest READY snapshot visible at ``as_of``, final bars, latest revision) when it covers the day that is needed
(at least half of the median instrument count that day); else the legacy raw bars (``market.bar_projection``, i.e. dataset ``bars_raw_1d``, since 2025-10-31) with factors synthesised
from the research adjusted bars (factor = raw close / adjusted close, a row whenever it changes by more than 1e-4) so ex-right days are still seen; else None ("no data": the caller
records that, it never guesses). The source used is returned and recorded on every day.
Panel columns (cents Int64, as the batch 6 raw panel): code, date, si, o, h, l, c, v, prev_c, ok_move (= flag_ok), flag_ok, up_c / down_c (exact limit prices, NULL when not classified, which a
sale treats as sellable), lu (closed at the limit-up price), streak (restarts on a not-classified row), amt (close x shares, an estimate), vol20 (std of 20 returns against the reference price),
plus ``in_scope`` / ``cap`` / ``sw2`` from ``bf_batch6_inputs.attach_scope``.
Not covered: ST history (the ST state is the current one, so a past ST period of a now-normal share gets 10 % bands: its limit-ups are missed, never invented), IPO no-limit windows (out of
scope by the 60-session rule).
"""
from __future__ import annotations

import io
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Mapping

import polars as pl
import psycopg

from pulse.adapters.lake import LakeStore
from pulse.application import bindings as B
from pulse.application import market_today_service as MT
from pulse.application import refdata_core as RC
from pulse.application.bf_batch6_inputs import HistoryInputs, attach_scope, load_scope, load_seats, observed_full_sessions
from pulse.domain.batch6 import rules as R6

NATIVE, LEGACY = "native", "legacy"
FACTOR_CHANGE = 1e-4
RAW_COLS = ["instrument_id", "code", "trade_date", "open_price_raw", "high_price_raw", "low_price_raw", "close_price_raw", "volume_shares"]
_DEC = pl.Decimal(38, 12)


@dataclass
class RawWindow:
    frame: pl.DataFrame                    # RAW_COLS, Decimal prices
    factors: pl.DataFrame | None           # adj_factor_native rows (or synthesised), None = no factor file
    source: str                            # "native" | "legacy" | "legacy_no_factors"
    notes: list[str] = field(default_factory=list)

    def last_day(self) -> date | None:
        return None if self.frame.is_empty() else self.frame["trade_date"].max()

    def covers(self, day: date) -> bool:
        """The day has at least half of the median instrument count of the window's sessions."""
        counts = self.frame.group_by("trade_date").len()
        if counts.is_empty():
            return False
        med = sorted(counts["len"].to_list())[counts.height // 2]
        n = counts.filter(pl.col("trade_date") == day)["len"].to_list()
        return bool(n) and n[0] >= 0.5 * med


def _members(lake: LakeStore, man: Mapping) -> list[pl.DataFrame]:
    m = lake.read_manifest(man["content_hash"])
    return [pl.read_parquet(io.BytesIO(lake.read_object(x))) for x in m.members if x["path"].endswith(".parquet")]


def read_native(conn: psycopg.Connection, lake: LakeStore, as_of: datetime, since: date, code_of: Mapping[str, str]) -> RawWindow | None:
    man = B._ready_manifest(conn, RC.RAW_DATASET, as_of)
    if man is None:
        return None
    parts = []
    for f in _members(lake, man):
        f = f.filter((pl.col("trade_date") >= since) & (pl.col("available_at") <= as_of) & pl.col("is_final") & pl.col("instrument_id").is_in(list(code_of)))
        if f.height:
            parts.append(f)
    if not parts:
        return None
    df = pl.concat(parts).sort("revision").unique(RC.BAR_KEY, keep="last")
    rev = dict(code_of)
    df = df.with_columns(pl.col("instrument_id").replace_strict(rev, default=None, return_dtype=pl.String).alias("code")).select(RAW_COLS)
    fm = B._ready_manifest(conn, RC.FACTOR_DATASET, as_of)
    factors = None
    notes = [f"native raw manifest v{man['manifest_version']}"]
    if fm is not None:
        fr = [f.filter(pl.col("available_at") <= as_of) for f in _members(lake, fm)]
        factors = pl.concat(fr) if fr else None
        notes.append(f"factor manifest v{fm['manifest_version']}")
    else:
        notes.append("no adj_factor_native snapshot: an ex-right day cannot be seen")
    return RawWindow(df, factors, NATIVE if factors is not None else "native_no_factors", notes)


def read_legacy(conn: psycopg.Connection, lake: LakeStore, as_of: datetime, since: date, code_of: Mapping[str, str]) -> RawWindow | None:
    ids = list(code_of)
    rows: list[tuple] = []
    for i in range(0, len(ids), 400):
        series = MT.load_bars(conn, ids[i:i + 400], as_of, B.AVAILABILITY_POLICY, since=since)
        for iid, bars in series.items():
            for b in bars:
                if b.c is None or b.o is None or b.h is None or b.l is None:
                    continue
                rows.append((iid, code_of[iid], b.d, b.o, b.h, b.l, b.c, int(b.v or 0)))
    if not rows:
        return None
    schema = {"instrument_id": pl.String, "code": pl.String, "trade_date": pl.Date, "open_price_raw": _DEC, "high_price_raw": _DEC, "low_price_raw": _DEC, "close_price_raw": _DEC,
              "volume_shares": pl.Int64}
    df = pl.DataFrame(rows, schema=schema, orient="row")
    notes = ["legacy raw bars (market.bar_projection)"]
    try:
        ri = B.load_research_inputs(conn, lake, as_of)
        adj = ri.bars.select("instrument_id", "trade_date", pl.col("close").alias("adj_close"))
        j = df.select("instrument_id", "trade_date", pl.col("close_price_raw").cast(pl.Float64).alias("raw")).join(adj, on=["instrument_id", "trade_date"], how="inner").sort(["instrument_id", "trade_date"])
        j = j.with_columns((pl.col("raw") / pl.col("adj_close")).alias("f")).with_columns((pl.col("f") / pl.col("f").shift(1).over("instrument_id") - 1).abs().alias("chg"))
        keep = j.filter(pl.col("chg").is_null() | (pl.col("chg") > FACTOR_CHANGE))
        factors = pl.DataFrame({"instrument_id": keep["instrument_id"], "effective_from": keep["trade_date"], "factor": keep["f"].cast(pl.Decimal(38, 16)),
                                "available_at": pl.Series([as_of] * keep.height, dtype=pl.Datetime("us", "UTC")), "revision": pl.Series([1] * keep.height, dtype=pl.Int32)})
        notes.append(f"factors synthesised from the research adjusted bars ({ri.manifest['dataset_id']} v{ri.manifest['manifest_version']})")
        return RawWindow(df, factors, LEGACY, notes)
    except LookupError:
        notes.append("no adjusted bars to derive factors from: an ex-right day cannot be seen")
        return RawWindow(df, None, "legacy_no_factors", notes)


def pick_window(conn: psycopg.Connection, lake: LakeStore, as_of: datetime, since: date, code_of: Mapping[str, str], need_day: date | None = None,
                prefer: str = "auto") -> RawWindow | None:
    """Native when it covers ``need_day`` (or exists, when no day is needed), else the legacy raw bars when they cover it, else None."""
    cands = []
    if prefer in ("auto", NATIVE):
        cands.append(read_native)
    if prefer in ("auto", LEGACY):
        cands.append(read_legacy)
    for reader in cands:
        w = reader(conn, lake, as_of, since, code_of)
        if w is not None and (need_day is None or w.covers(need_day)):
            return w
    return None


def _cents(col: str) -> pl.Expr:
    return (pl.col(col).cast(pl.Float64) * 100).round(0).cast(pl.Int64)


def exact_panel(win: RawWindow, st: frozenset[str]) -> tuple[pl.DataFrame, list[date], pl.DataFrame]:
    """(panel without scope columns, observed sessions, the flags frame) from a raw window."""
    frame = win.frame.with_columns(pl.col("code").is_in(list(st)).alias("is_st"))
    flags = RC.limit_flags_raw(frame, win.factors)
    counts = {d: n for d, n in flags.group_by("trade_date").len().iter_rows()}
    sessions = observed_full_sessions(counts)
    si = pl.DataFrame({"date": sessions, "si": list(range(len(sessions)))}, schema={"date": pl.Date, "si": pl.Int64})
    ok = (pl.col("rule_status") == "verified") & ~pl.col("ref_uncertain").fill_null(True)
    f = (flags.sort(["code", "trade_date"]).rename({"trade_date": "date"}).join(si, on="date", how="inner")
         .with_columns(ok.alias("flag_ok"), _cents("open_price_raw").alias("o"), _cents("high_price_raw").alias("h"), _cents("low_price_raw").alias("l"),
                       _cents("close_price_raw").alias("c"), pl.col("volume_shares").alias("v"), _cents("prev_close_raw").alias("prev_c"),
                       pl.col("is_limit_up_close").fill_null(False).alias("_lu"))
         .with_columns(pl.when(pl.col("flag_ok")).then(_cents("limit_up_price")).otherwise(None).alias("up_c"),
                       pl.when(pl.col("flag_ok")).then(_cents("limit_down_price")).otherwise(None).alias("down_c"),
                       (pl.col("flag_ok") & pl.col("_lu")).alias("lu"),
                       (pl.col("c").cast(pl.Float64) / 100.0 * pl.col("v")).alias("amt"),
                       pl.when(pl.col("ref_price").is_not_null()).then(pl.col("close_price_raw").cast(pl.Float64) / pl.col("ref_price").cast(pl.Float64) - 1.0).otherwise(None).alias("_pct")))
    f = f.with_columns(pl.col("_pct").rolling_std(window_size=20, min_samples=10).over("code").alias("vol20"), pl.col("flag_ok").alias("ok_move"), pl.col("_pct").alias("pct"))
    streak, cur, last = [], 0, None
    for code, okf, lu in zip(f["code"].to_list(), f["flag_ok"].to_list(), f["lu"].to_list()):
        if code != last:
            cur, last = 0, code
        cur = (cur + 1 if lu else 0) if okf else 0
        streak.append(cur)
    f = f.with_columns(pl.Series("streak", streak, dtype=pl.Int64))
    cols = ["code", "date", "si", "o", "h", "l", "c", "v", "prev_c", "ok_move", "flag_ok", "up_c", "down_c", "lu", "streak", "amt", "vol20", "pct"]
    return f.select(cols), sessions, flags


def prepare(conn: psycopg.Connection, lake: LakeStore, win: RawWindow, *, as_of: datetime, seats_since: date | None = None, with_seats: bool = False
            ) -> tuple[HistoryInputs, dict]:
    """Panel with scope for a raw window (a ``HistoryInputs`` the batch 6 signal builders read) and a note dict with the counts of rows not classified."""
    code_of, iid_of, scope0, listed_on = load_scope(conn, as_of)
    panel, sessions, flags = exact_panel(win, scope0.st)
    panel, notes, cap_note, sw = attach_scope(panel, sessions, listed_on, scope0.st, conn, lake, as_of, iid_of, require="flag_ok")
    status = flags.with_columns(pl.col("rule_status").str.replace(r" on \d{4}-\d{2}-\d{2}", "").alias("rule_status")).group_by("rule_status").len().sort("len", descending=True)
    non_st = flags.filter(~pl.col("is_st"))
    info = {"rows": panel.height, "flag_not_ok": int(panel["flag_ok"].not_().sum()), "flag_not_ok_non_st": int(non_st.filter(pl.col("rule_status") != "verified").height
                                                                                                          + non_st.filter((pl.col("rule_status") == "verified") & pl.col("ref_uncertain")).height),
            "ref_uncertain": int(flags["ref_uncertain"].sum()), "rule_status": {k: v for k, v in status.iter_rows()}, "ref_method": {k: v for k, v in flags.group_by("ref_method").len().iter_rows() if k},
            "source": win.source, "notes": win.notes}
    if with_seats and sessions:
        seats, lhb = load_seats(conn, code_of, seats_since or sessions[0])
    else:
        seats, lhb = pl.DataFrame(schema={"code": pl.String, "date": pl.Date, "side": pl.String, "seat": pl.String, "amount": pl.Float64}), set()
    inp = HistoryInputs(as_of, sessions, panel, seats, lhb, sw, cap_note, notes + win.notes, win.source)
    return inp, info
