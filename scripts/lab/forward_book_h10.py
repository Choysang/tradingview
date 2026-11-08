"""Shadow of the frozen main book with a 10-day hold (E065; backend venv). The frozen book (forward_book.py) is
not touched and stays the decision of record; this file only records what the same rule would do with HOLD = 10.

Same scores (fusion_static40_safe), 20 slots, top 40 offer, the same equity-curve gate (the shadow's own ungated
NAV at the prior close below its 20-day mean), the same engine fills and costs.
The changes: HOLD = 10, so round(20 / 10) = 2 new names a day; on a gated day 1 of the 2 is kept (0.25 x 2 rounds to
0, which would stop all buying), so the gated shadow carries more exposure than the frozen book on gated days.
The pre-registered comparison is therefore ungated vs ungated (`ungated_nav` in both logs); the gated pair is
reported alongside. Reason: E063 found that halving turnover with a
10-day hold turned the book's net excess positive after a 0.30% round trip (+5.3% a year vs -0.7%), a result seen
after looking at the data, so it is tested forward only (pre-registered in
docs/research/2026-11-07-batch1-preregistration.md, section 5).

Each run appends one line per new decision date to .work/lab/live/shadow_h10.jsonl (same fields as gated_book.jsonl).
Usage: python forward_book_h10.py   (run right after forward_book.py, on the same score file)
"""
from __future__ import annotations

import hashlib
import json

import polars as pl

import curve_gate as G
import forward_book as F
import harness as H

HOLD = 10
SCORES, SLOTS, OFFER, LOOK, LOW = F.SCORES, F.SLOTS, F.OFFER, F.LOOK, F.LOW
LOG = H.BASE / "live" / "shadow_h10.jsonl"
FROZEN = F.FROZEN
GATED_W = 0.5  # share of the day's new buys kept on a gated day (frozen book: 0.25 of 4 = 1 name)


def main() -> None:
    path = H.LAB / SCORES
    scores = pl.read_parquet(path)
    digest = hashlib.sha256(path.read_bytes()).hexdigest()[:16]
    raw = H.equity(scores, "FORWARD", hold=HOLD, positions=SLOTS, offer=OFFER)
    # 0.25 x 2 new buys rounds to 0, which would stop the book on every gated day; keep 1 of 2 instead (w = 0.5).
    w = G.gate(raw, look=LOOK, low=LOW).with_columns(pl.when(pl.col("w") < 1).then(GATED_W).otherwise(1.0).alias("w"))
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
    today = sig.filter(pl.col("date") == last).sort("score", descending=True)
    offer = H.to_signals(scores, OFFER).filter(pl.col("date") == last).sort("score", descending=True)
    keep = per_day if weight >= 1 else round(weight * per_day)
    buys = [c for c in today["code"].to_list() if c not in held][:keep]
    nav = eq.filter(pl.col("date") >= FROZEN)
    row = {"date": last.isoformat(), "gate_w": weight, "new_buys_allowed": keep, "try_buy": buys,
           "offer_top10": offer["code"].head(10).to_list(), "holdings": held, "nav": round(float(eq["strategy"][-1]), 4),
           "nav_since_freeze_pct": round(float(nav["strategy"][-1] / nav["strategy"][0] - 1) * 100, 3) if nav.height > 1 else None,
           "ungated_nav": round(float(raw["strategy"][-1]), 4), "scores_sha256_16": digest,
           "rule": f"SHADOW hold {HOLD}, {SLOTS} slots, top {OFFER}, gate {LOOK}d, keep {GATED_W} of new buys when gated"}
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    print(json.dumps(row, ensure_ascii=False))


if __name__ == "__main__":
    main()
