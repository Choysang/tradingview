"""M5-E01: reconcile the old and new systems at one shared data cutoff.

Why: the package's M5 exit (traceability/milestones_v32.json, M5-E01) requires a same-cutoff comparison of the
security master and exclusions, prices, signals and the book, where every difference is either explained or listed as
unexplained (never dropped). Top-K overlap is only an auxiliary signal because the two systems rank with different
models.

Usage (server, from C:\\pulse3\\app)::

    python tools/reconcile_m5.py --release <uuid> --as-of 2026-11-09T13:30:00Z --availability reconstructed \
        --old-scores scores_asof.parquet --old-book gated_book.jsonl --warehouse <legacy live dir> --out <dir>

Writes ``m5_e01_<date>.json`` and ``.md``. Reads only; the legacy warehouse and both databases are not modified.
"""
from __future__ import annotations

import argparse
import json
import sys
import uuid
from collections import Counter
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

import polars as pl

from pulse import config
from pulse.adapters.lake import LakeStore
from pulse.adapters.postgres import db
from pulse.adapters.sources.legacy import LegacyWarehouse
from pulse.application import bindings as B
from pulse.application.data_service import DataService
from pulse.application.release_service import BuildConfig, resolve_inputs
from pulse.domain.ranking.universe import apply_universe

PRICE_TOLERANCE = Decimal("0.001")       # relative; legacy closes are forward-adjusted, raw closes are not


def code_map(conn) -> dict[str, str]:
    rows = conn.execute("SELECT DISTINCT instrument_id, source_symbol FROM core.instrument_alias WHERE source_id='legacy_warehouse'")
    return {str(r["instrument_id"]): r["source_symbol"] for r in rows}


def new_rankings(conn, release_id: uuid.UUID, codes: dict[str, str]) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for run in conn.execute("SELECT ranking_id, horizon_id FROM decision.ranking_run WHERE release_id=%s AND ranking_scope='market'",
                            (release_id,)).fetchall():
        ents = conn.execute("SELECT instrument_id FROM decision.ranking_entry WHERE ranking_id=%s ORDER BY alpha_rank",
                            (run["ranking_id"],)).fetchall()
        out[run["horizon_id"]] = [codes.get(str(e["instrument_id"]), str(e["instrument_id"])) for e in ents]
    return out


OLD_MIN_AMOUNT_E8, OLD_MIN_DAYS, OLD_MIN_PRICE = 0.5, 120, 2.0   # old scoring pool: scripts/lab/harness.py tradable()
OLD_ST_WINDOW, OLD_ST_MIN_HITS, OLD_TICK = 60, 2, 0.011            # old st_like(): backend/app/playbooks/library_factor.py


def old_st_like(g: pl.DataFrame) -> bool:
    """The old price-behaviour ST heuristic: >= 2 closes on the exact 5% limit in the last 60 bars, no move above 5.25%."""
    t = g.tail(OLD_ST_WINDOW + 1).with_columns(pl.col("close").shift(1).alias("prev"))
    t = t.with_columns(((pl.col("prev") * 1.05 * 100).round(0) / 100).alias("up"), ((pl.col("prev") * 0.95 * 100).round(0) / 100).alias("dn"),
                       ((pl.col("close") / pl.col("prev") - 1).abs() * 100).alias("pct")).tail(OLD_ST_WINDOW)
    hits = t.filter((((pl.col("close") - pl.col("up")).abs() <= OLD_TICK) & (pl.col("close") >= pl.col("high")))
                    | (((pl.col("close") - pl.col("dn")).abs() <= OLD_TICK) & (pl.col("close") <= pl.col("low")))).height
    return hits >= OLD_ST_MIN_HITS and not bool((t["pct"].fill_null(0) > 5.25).any())


def old_pool_reasons(wh: LegacyWarehouse, day: date, codes: list[str]) -> dict[str, str]:
    """Why the old tradable() rule leaves a code out, recomputed from the legacy warehouse.

    The legacy ``amount`` column is often null, so the 20-day turnover uses close x volume where it is missing
    (marked ``estimated``)."""
    if not codes:
        return {}
    bars = (wh.read_bars_versions(date(day.year - 1, day.month, 1), codes=codes).filter(pl.col("date") <= day)
            .sort("ingested_at").unique(["code", "date"], keep="last").sort(["code", "date"]))
    lst = wh.read_listings()
    list_date = dict(zip(lst["code"].to_list(), lst["list_date"].to_list()))
    first_day = bars["date"].min() if bars.height else None
    out: dict[str, str] = {}
    for c in codes:
        g = bars.filter(pl.col("code") == c)
        if g.height == 0 or g["date"].max() != day:
            out[c] = "old_rule:no_bar_on_session"
            continue
        last = g.tail(1).row(0, named=True)
        before = 0
        ld = list_date.get(c)
        if ld is not None and ld < g["date"].min():
            before = int(pl.select(pl.business_day_count(pl.lit(ld), pl.lit(g["date"].min()))).item())
        elif ld is None and g["date"].min() == first_day:
            before = OLD_MIN_DAYS
        amt = g.tail(20).select(pl.coalesce(pl.col("amount"), pl.col("close") * pl.col("volume")).mean()).item()
        if last["close"] is not None and last["close"] < OLD_MIN_PRICE:
            out[c] = "old_rule:close_below_2"
        elif g.height + before < OLD_MIN_DAYS:
            out[c] = "old_rule:listed_under_120_sessions"
        elif amt is not None and amt / 1e8 < OLD_MIN_AMOUNT_E8:
            out[c] = "old_rule:turnover_20d_below_0.5e8 (estimated)"
        elif c[:3] not in ("300", "301", "688", "689") and old_st_like(g):
            out[c] = "old_rule:st_like_price_heuristic (new uses the exchange risk-warning flag)"
        else:
            out[c] = "unexplained"
    return out


def revised_after_cutoff(lake: LakeStore, inp) -> set[str]:
    """Instruments whose research series was re-adjusted after the cutoff: the lake keeps only the latest revision,
    so at a reconstructed cutoff their bars are (correctly) not visible."""
    full = lake.read_snapshot_frames(inp.research.manifest["content_hash"])
    late = full.filter((pl.col("revision") > 1) & (pl.col("available_at") > inp.research.decision_as_of))
    return set(late["instrument_id"].unique().to_list())


def securities_section(inp, codes: dict[str, str], old_scored: set[str], ranked: set[str], wh: LegacyWarehouse,
                       lake: LakeStore | None = None) -> dict:
    uni = apply_universe(inp.universe_policy, inp.facts)
    included = {codes.get(str(f.instrument_id)) for f in uni.included}
    why = {codes.get(str(e.instrument_id)): e.reason_codes for e in uni.exclusions}
    old_only = sorted(old_scored - included)
    new_only = sorted(included - old_scored)
    explained = Counter()
    unexplained: list[str] = []
    for c in old_only:
        if c in why:
            explained[f"new_excludes:{why[c][0]}"] += 1
        else:
            unexplained.append(c)
    new_why = old_pool_reasons(wh, inp.research.session_date, new_only)
    bar_counts = inp.research.bars.group_by("instrument_id").len()
    nbars = dict(zip(bar_counts["instrument_id"].to_list(), bar_counts["len"].to_list()))
    late = revised_after_cutoff(lake, inp) if lake is not None else set()

    def why_unranked(iid: str) -> str:
        if nbars.get(iid, 0) > 0:
            return "unexplained"
        if iid in late:
            return "ranking_rule:missing_feature (forward-adjusted history revised after the cutoff; PIT-correct absence)"
        return "ranking_rule:missing_feature (no research bars)"

    not_ranked = {codes.get(str(f.instrument_id)): why_unranked(str(f.instrument_id))
                  for f in uni.included if codes.get(str(f.instrument_id)) not in ranked}
    return {
        "old_scored": len(old_scored), "new_included": len(included), "both": len(old_scored & included),
        "ranked": len(ranked), "included_not_ranked": not_ranked, "ranked_not_included": sorted(ranked - included),
        "old_only": len(old_only), "old_only_explained_by_new_exclusion": dict(explained.most_common()),
        "old_only_unexplained": unexplained,
        "new_only": len(new_only),
        "new_only_explained_by_old_pool_rule": dict(Counter(v for v in new_why.values() if v != "unexplained").most_common()),
        "new_only_unexplained": sorted(c for c, v in new_why.items() if v == "unexplained"),
        "new_exclusion_reasons": dict(Counter(r for e in uni.exclusions for r in e.reason_codes).most_common()),
    }


def prices_section(conn, wh: LegacyWarehouse, day: date, inp, codes: dict[str, str]) -> dict:
    old = wh.read_bars_versions(day).filter(pl.col("date") == day).sort("ingested_at").unique("code", keep="last")
    old_close = {c: Decimal(str(v)) for c, v in zip(old["code"], old["close"]) if v is not None}
    new_close = {codes.get(str(i)): v for i, v in inp.raw_close.items()}
    both = sorted(set(old_close) & set(new_close))
    match, adjusted, unexplained = 0, [], []
    for c in both:
        o, n = old_close[c], Decimal(new_close[c])
        rel = abs(o - n) / n if n else Decimal("1")
        if rel <= PRICE_TOLERANCE:
            match += 1
        elif o < n:
            adjusted.append(c)                 # forward adjustment lowers past prices after a later dividend/split
        else:
            unexplained.append({"code": c, "old_close": str(o), "new_raw_close": str(n)})
    return {"date": day.isoformat(), "old_closes": len(old_close), "new_raw_closes": len(new_close), "both": len(both),
            "match_within_0.1pct": match,
            "old_below_raw_explained_as_forward_adjustment": len(adjusted), "forward_adjusted_codes": adjusted,
            "unexplained": unexplained,
            "old_only": len(set(old_close) - set(new_close)), "new_only": len(set(new_close) - set(old_close))}


def _spearman(a: list[str], b: list[str]) -> float | None:
    common = [c for c in a if c in set(b)]
    if len(common) < 3:
        return None
    ra = {c: i for i, c in enumerate([c for c in a if c in set(common)])}
    rb = {c: i for i, c in enumerate([c for c in b if c in set(common)])}
    n = len(common)
    d2 = sum((ra[c] - rb[c]) ** 2 for c in common)
    return round(1 - 6 * d2 / (n * (n * n - 1)), 4)


def signals_section(old_rank: list[str], new: dict[str, list[str]], k: int) -> dict:
    out = {"note": "auxiliary only: the old fusion model and the new baseline ranking are different models", "top_k": k}
    for h, lst in sorted(new.items()):
        top_old, top_new = old_rank[:k], lst[:k]
        out[h] = {"overlap": len(set(top_old) & set(top_new)), "overlap_codes": sorted(set(top_old) & set(top_new)),
                  "spearman_on_common": _spearman(old_rank, lst), "common": len(set(old_rank) & set(lst))}
    return out


def book_section(conn, old_book: dict | None) -> dict:
    pos = conn.execute(
        "SELECT a.source_symbol code, p.quantity FROM execution.position_projection p JOIN core.instrument_alias a"
        " ON a.instrument_id=p.instrument_id AND a.source_id='legacy_warehouse' WHERE p.quantity>0 ORDER BY 1").fetchall()
    accts = conn.execute("SELECT count(*) n FROM execution.account_head").fetchone()["n"]
    new_codes = [r["code"] for r in pos]
    old_codes = sorted((old_book or {}).get("holdings", []))
    return {"old_book_date": None if old_book is None else old_book.get("date"), "old_holdings": old_codes,
            "old_nav": None if old_book is None else old_book.get("nav"), "old_rule": None if old_book is None else old_book.get("rule"),
            "new_accounts": accts, "new_positions": new_codes,
            "explanation": ("different books: the old frozen main book (rule above) has traded since 2026-11-01; the new paper "
                            "account has no fills yet, so positions, fills, costs and NAV cannot match and are not compared "
                            "line by line. A like-for-like book comparison needs the same plan replayed in both ledgers.")}


def run(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="reconcile_m5")
    ap.add_argument("--release", required=True, type=uuid.UUID)
    ap.add_argument("--as-of", required=True)
    ap.add_argument("--availability", choices=("observed", "reconstructed"), default="reconstructed")
    ap.add_argument("--old-scores", required=True, type=Path)
    ap.add_argument("--old-book", type=Path)
    ap.add_argument("--warehouse", required=True, type=Path)
    ap.add_argument("--top-k", type=int, default=40)
    ap.add_argument("--out", required=True, type=Path)
    a = ap.parse_args(argv)
    cfg = config.load()
    conn = db.connect(cfg.dsn, autocommit=True)
    lake = LakeStore(cfg.lake_root)
    as_of = datetime.fromisoformat(a.as_of.replace("Z", "+00:00"))
    pol = B.RECONSTRUCTED_POLICY if a.availability == "reconstructed" else B.AVAILABILITY_POLICY
    try:
        inp = resolve_inputs(DataService(conn, lake), lake, BuildConfig(availability=pol), as_of)
        day = inp.research.session_date
        codes = code_map(conn)
        scores = pl.read_parquet(a.old_scores).filter(pl.col("date") == day).sort("score", descending=True)
        if scores.height == 0:
            print(f"old scores have no rows for {day}", file=sys.stderr)
            return 1
        old_rank = scores["code"].to_list()
        books = [json.loads(x) for x in a.old_book.read_text(encoding="utf-8").splitlines() if x.strip()] if a.old_book else []
        old_book = next((b for b in books if b.get("date") == day.isoformat()), None)
        new = new_rankings(conn, a.release, codes)
        report = {
            "evidence_id": "M5-E01", "session_date": day.isoformat(), "decision_as_of": as_of.isoformat(),
            "availability_policy": pol, "new_release_id": str(a.release),
            "old_inputs": {"scores": a.old_scores.name, "scores_sha256_16": None if old_book is None else old_book.get("scores_sha256_16"),
                           "warehouse": "legacy live warehouse (read-only)"},
            "securities": securities_section(inp, codes, set(old_rank), set(new.get("20td", [])), LegacyWarehouse(a.warehouse), lake),
            "prices": prices_section(conn, LegacyWarehouse(a.warehouse), day, inp, codes),
            "signals": signals_section(old_rank, new, a.top_k),
            "book": book_section(conn, old_book),
        }
    finally:
        conn.close()
    a.out.mkdir(parents=True, exist_ok=True)
    stem = a.out / f"m5_e01_{report['session_date']}"
    stem.with_suffix(".json").write_text(json.dumps(report, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    stem.with_suffix(".md").write_text(render(report), encoding="utf-8")
    print(render(report))
    return 0


def render(r: dict) -> str:
    s, p, g, b = r["securities"], r["prices"], r["signals"], r["book"]
    lines = [f"# M5-E01 reconciliation, session {r['session_date']}", "",
             f"Cutoff {r['decision_as_of']} ({r['availability_policy']}), new release `{r['new_release_id']}`.", "",
             "## Security master and exclusions",
             f"- old scored {s['old_scored']}, new included {s['new_included']}, both {s['both']}; new ranked {s['ranked']}"
             f" (included but not ranked {len(s['included_not_ranked'])}, ranked but not included {len(s['ranked_not_included'])})",
             f"- old only {s['old_only']}: explained by a new exclusion {sum(s['old_only_explained_by_new_exclusion'].values())}"
             f" {s['old_only_explained_by_new_exclusion']}; unexplained {len(s['old_only_unexplained'])}",
             f"- new only {s['new_only']}: explained by the old pool rule {sum(s['new_only_explained_by_old_pool_rule'].values())}"
             f" {s['new_only_explained_by_old_pool_rule']}; unexplained {len(s['new_only_unexplained'])}", "",
             "## Prices",
             f"- both {p['both']}: match within 0.1% {p['match_within_0.1pct']}, old below raw (forward adjustment) "
             f"{p['old_below_raw_explained_as_forward_adjustment']}, unexplained {len(p['unexplained'])}", "",
             "## Signals (auxiliary)", f"- {g['note']}"]
    for h in sorted(k for k in g if k not in ("note", "top_k")):
        lines.append(f"- {h}: top-{g['top_k']} overlap {g[h]['overlap']}, Spearman on {g[h]['common']} common codes {g[h]['spearman_on_common']}")
    lines += ["", "## Book", f"- old ({b['old_book_date']}): holdings {b['old_holdings']}, NAV {b['old_nav']}",
              f"- new: {b['new_accounts']} account(s), positions {b['new_positions']}", f"- {b['explanation']}", ""]
    return "\n".join(lines)


if __name__ == "__main__":
    raise SystemExit(run())
