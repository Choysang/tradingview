"""Inputs of the E092 live judgement from pulse3's own data: the day's universe and prompts, and the 5-day labels.

Why: ``bf_llm_live_service`` is pure orchestration; this module is the only place that reads the fusion scores, the research bars and the index. The universe is the main-board fusion
score of the decision day (``fusion_scores_current``: tradable names only), the same panel the fusion lane builds (``bf_fusion_service.load_inputs``), and the labels are the fusion
panel's own 5-day label (buy at the D+1 open, sell at the D+6 open, minus the day's mean over the tradable names), i.e. the ``ex5`` of the pre-registration.
Invariants: the day is refused (``ContextMissing``) rather than guessed when the scores, the bars or the index do not reach it; the controls are seeded with the decision date; names without an
industry get no industry line; an unknown value is never replaced by 0. Prompt wording follows the old lab (``rules.build_prompt``).
Not covered: any network request, the day loop and caps (``bf_llm_live_service``).
"""
from __future__ import annotations

import math
from collections.abc import Sequence
from datetime import date, datetime

import numpy as np
import polars as pl
import psycopg

from pulse.adapters.lake import LakeStore
from pulse.application import bindings as B
from pulse.application import bf_fusion_service as S
from pulse.application.bf_llm_live_service import ContextMissing, DayPlan, TRACK_HORIZON
from pulse.application.data_service import DataService
from pulse.domain.fusion import panel as PN
from pulse.domain.fusion.expr import shift
from pulse.domain.llm_live import rules as R

INDEX_SYMBOL = "000001"                      # the index line of the prompt is the Shanghai composite, as in the old lab


def scores_of_day(conn: psycopg.Connection, lake: LakeStore, day: date) -> dict[str, float]:
    """{code: score} of the decision day from the promoted scores; empty when the dataset does not hold the day."""
    cur = S.Store(DataService(conn, lake)).read(S.DS_CURRENT, S.CURRENT_SCHEMA)
    g = cur.filter(pl.col("date") == day)
    return dict(zip(g["code"].to_list(), g["score"].to_list()))


def index_closes(conn: psycopg.Connection, lake: LakeStore, cutoff: datetime, day: date) -> list[float]:
    """Closes of the Shanghai composite up to ``day`` (observed at ``cutoff``), oldest first; empty when the index dataset or the symbol is missing."""
    man = B._ready_manifest(conn, B.index_dataset(), cutoff)
    if man is None:
        return []
    idx = lake.read_snapshot_frames(man["content_hash"])
    if idx is None or idx.height == 0:
        return []
    idx = (idx.filter((pl.col("symbol") == INDEX_SYMBOL) & (pl.col("ingested_at") <= cutoff) & (pl.col("date") <= day))
           .sort("ingested_at").unique("date", keep="last").sort("date"))
    if idx.height == 0 or idx["date"][-1] != day:
        return []
    return [float(c) for c in idx["close"].to_list() if c is not None]


def industry_names(conn: psycopg.Connection, node_ids: Sequence[str]) -> tuple[dict[str, str], list[str]]:
    """({node_id: display name}, notes). The names are cosmetic: a failed lookup is noted on the day row and the prompts simply omit the industry."""
    ids = sorted({n for n in node_ids if n})
    if not ids:
        return {}, []
    try:
        rows = conn.execute("SELECT DISTINCT ON (node_id) node_id, display_name FROM core.taxonomy_node WHERE node_id = ANY(%s) ORDER BY node_id, taxonomy_version DESC", (ids,)).fetchall()
    except psycopg.Error as exc:
        return {}, [f"industry names unavailable ({type(exc).__name__}); prompts carry no industry line"]
    found = {r["node_id"]: r["display_name"] for r in rows if r["display_name"]}
    return found, ([] if found else ["no industry name resolved for the sector nodes; prompts carry no industry line"])


def load_day_plan(conn: psycopg.Connection, lake: LakeStore, day: date, *, cutoff: datetime | None = None) -> DayPlan:
    """The 30 + 10 names of ``day`` with their prompts. Raises ``ContextMissing`` when the scores, the bars or the index do not reach ``day``."""
    scores = scores_of_day(conn, lake, day)
    if not scores:
        raise ContextMissing("scores_not_ready", f"no promoted fusion score for {day}")
    try:
        top, controls = R.pick_universe(scores, day)
    except ValueError as exc:
        raise ContextMissing("too_few_scores", str(exc)) from None
    try:
        loaded = S.load_inputs(conn, lake, cutoff)
    except (S.FusionStop, LookupError) as exc:
        raise ContextMissing("bars_not_ready", str(exc)) from None
    if loaded.session < day:
        raise ContextMissing("bars_not_ready", f"the research bars end {loaded.session}, before {day}")
    closes = index_closes(conn, lake, loaded.cutoff, day)
    try:
        market = R.market_line(day, closes)
    except ValueError as exc:
        raise ContextMissing("market_line_missing", str(exc)) from None
    wanted = top + controls
    long = loaded.long.filter(pl.col("code").is_in(wanted) & (pl.col("date") <= day)).sort(["code", "date"])
    by_code = {code: g for (code,), g in long.partition_by("code", as_dict=True).items()}
    names, notes = industry_names(conn, long["industry"].drop_nulls().unique().to_list()) if "industry" in long.columns else ({}, [])
    jobs = []
    for group, codes in ((R.GROUP_TOP, top), (R.GROUP_CONTROL, controls)):
        for code in codes:
            g = by_code.get(code)
            if g is None or g.height == 0 or g["date"][-1] != day:
                raise ContextMissing("bars_not_ready", f"{code} has no bar on {day}")
            last = g.row(g.height - 1, named=True)
            bars = g.tail(R.BARS_IN_PROMPT).select("date", "close", "pct_chg", "amount", "float_cap", "is_limit_up", "is_limit_down").to_dicts()
            prompt = R.build_prompt(market=market, code=code, name=loaded.names.get(code, code), industry=names.get(last.get("industry") or ""), float_cap=last["float_cap"], bars=bars)
            jobs.append({"id": R.job_id(day, code), "code": code, "name": loaded.names.get(code, code), "group": group, "score": float(scores[code]), "prompt": prompt})
    return DayPlan(day, jobs, market, {**loaded.refs, "scores_dataset": S.DS_CURRENT}, [*loaded.notes, *notes])


def load_labels(conn: psycopg.Connection, lake: LakeStore, days: Sequence[date], *, cutoff: datetime | None = None
                ) -> dict[date, dict[str, tuple[float | None, float | None]]]:
    """{day: {code: (ret5, ex5)}} in percent for the decision days whose D+6 bar exists in the research bars (others are left out: the window is still open)."""
    loaded = S.load_inputs(conn, lake, cutoff)
    panel = PN.to_panel(loaded.long)
    ex = panel.label(5)
    o = panel.f["open"]
    with np.errstate(all="ignore"):
        raw = (shift(o, -TRACK_HORIZON) / shift(o, -1) - 1) * 100
    at = {d: i for i, d in enumerate(panel.dates)}
    out: dict[date, dict[str, tuple[float | None, float | None]]] = {}

    def fin(x: float) -> float | None:
        return float(x) if math.isfinite(x) else None

    for d in days:
        t = at.get(d)
        if t is None or t + TRACK_HORIZON >= panel.T:
            continue
        out[d] = {c: (fin(raw[t, j]), fin(ex[t, j])) for j, c in enumerate(panel.codes)}
    return out
