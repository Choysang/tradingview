"""The official gated main book for forward tracking from November 2026 (frozen 2026-11-01; backend venv).

Frozen rule (round 7, R7-4, user decision 2026-11-01):
* scores: fusion_static40_safe (main board, .work/lab/main/), refreshed each day by the round-5 pipeline
  (export_panel.py, then fusion.py);
* book: hold 5 days, 20 slots, 4 new names a day from the top 40, the backend engine's fills and costs;
* gate: when the ungated book's NAV at the prior close is below its own 20-day mean, keep 0.25 of the day's
  new buys (round(0.25 x 4) = 1 name); otherwise all 4. Positions already held are never cut.
The book runs without a break from 2024-08-20 (the first fusion score), so the gate's 20 days are real.

Each run appends one line per new decision date to .work/lab/live/gated_book.jsonl: the date, the gate
weight for that date's signals (executed at the next open), the gated book's NAV, its holdings, and the
names it will try to buy. Rerunning is safe (dates already logged are skipped). November onward is the
only clean test; nothing in this file may be tuned after 2026-11-01.
Usage: python forward_book.py
"""
from __future__ import annotations

import hashlib
import json
from datetime import date

import polars as pl

import curve_gate as G
import harness as H

SCORES = "fusion_static40_safe.parquet"
HOLD, SLOTS, OFFER, LOOK, LOW = 5, 20, 40, 20, 0.25
LOG = H.BASE / "live" / "gated_book.jsonl"
FROZEN = date(2026, 11, 1)


def main() -> None:
    path = H.LAB / SCORES
    scores = pl.read_parquet(path)
    digest = hashlib.sha256(path.read_bytes()).hexdigest()[:16]
    raw = H.equity(scores, "FORWARD", hold=HOLD, positions=SLOTS, offer=OFFER)
    w = G.gate(raw, look=LOOK, low=LOW)
    per_day = max(1, round(SLOTS / HOLD))
    start, end = H.period_bounds("FORWARD")
    sig = H.gate_signals(H.to_signals(scores, OFFER), w, OFFER, per_day)
    exits = H.ExitRules(holding_days=HOLD, stop_loss_pct=None, take_profit_pct=None, trailing_stop_pct=None)
    sizing = H.SizingRule(method="equal", max_positions=SLOTS, max_new_per_day=per_day)
    trades, eq = H.simulate(H.ctx().panel, sig, exits, sizing, H.ctx().benchmark, start=start, end=end)
    last = eq["date"].max()
    done = set()
    if LOG.exists():
        done = {json.loads(x)["date"] for x in LOG.read_text(encoding="utf-8").splitlines() if x.strip()}
    if last.isoformat() in done:
        print(f"{last} already logged")
        return
    held = sorted(t.code for t in trades if t.exit_reason == "end")
    weight = float(w.filter(pl.col("date") == last)["w"].item()) if last in w["date"].to_list() else 1.0
    today = sig.filter(pl.col("date") == last).sort("score", descending=True)  # after the gate
    offer = H.to_signals(scores, OFFER).filter(pl.col("date") == last).sort("score", descending=True)
    keep = per_day if weight >= 1 else round(weight * per_day)
    # As in the backtest: the engine fills in list order, skipping names already held.
    buys = [c for c in today["code"].to_list() if c not in held][:keep]
    nav = eq.filter(pl.col("date") >= FROZEN)
    row = {"date": last.isoformat(), "gate_w": weight, "new_buys_allowed": keep, "try_buy": buys,
           "offer_top10": offer["code"].head(10).to_list(), "holdings": held, "nav": round(float(eq["strategy"][-1]), 4),
           "nav_since_freeze_pct": round(float(nav["strategy"][-1] / nav["strategy"][0] - 1) * 100, 3) if nav.height > 1 else None,
           "ungated_nav": round(float(raw["strategy"][-1]), 4), "scores_sha256_16": digest,
           "rule": f"hold {HOLD}, {SLOTS} slots, top {OFFER}, gate {LOOK}d/{LOW}"}
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    print(json.dumps(row, ensure_ascii=False))


if __name__ == "__main__":
    main()
