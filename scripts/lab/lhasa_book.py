"""P0 follow-up: do the Lhasa / one-day-seat risk signals improve the main book (backend venv)?

The main book is the frozen forward book (fusion_static40_safe, hold 5, 20 slots, 4 buys a day from a top-40
offer list), with and without the 20-day / 0.25 equity-curve gate. A filter removes a stock from day D's
score list when it carries the flag on D, so the next-ranked name takes its place. All flags are known
before the D+1 open: the 龙虎榜 is published on the evening of D, and the foresight score uses D's daily bar.

Filters: lhasa3 (3+ 拉萨 buy seats), lhasa1 (1+), trip1 (1+ one-day seat), lhb (any listing, a control),
foresee20 (the day's top 20 of the walk-forward "tonight 拉萨 3+" model, from 2024-05), combo (lhasa1 or
trip1 or foresee20). Held positions are not sold on a flag: ExitRules has no such rule.

Inputs from lhasa.py: lhasa_flags.parquet, lhasa_preds.parquet. Writes lhasa_book.json.
"""
from __future__ import annotations

import json

import polars as pl

import curve_gate as G
import harness as H
import style_test as S
from harness import CODE, c

SCORES = "fusion_static40_safe.parquet"
HOLD, SLOTS, OFFER, LOOK, LOW = 5, 20, 40, 20, 0.25
PER_DAY = SLOTS // HOLD


def flags() -> pl.DataFrame:
    f = pl.read_parquet(H.LAB / "lhasa_flags.parquet")
    p = pl.read_parquet(H.LAB / "lhasa_preds.parquet").with_columns(
        (c("score").rank("ordinal", descending=True).over("date") <= 20).alias("foresee20")).select(CODE, "date", "foresee20")
    f = f.join(p, on=[CODE, "date"], how="left").with_columns(c("foresee20").fill_null(False), c("trip_buyers").fill_null(0))
    return f.with_columns(
        (c("lhasa") >= 3).alias("lhasa3"), (c("lhasa") >= 1).alias("lhasa1"), (c("trip_buyers") >= 1).alias("trip1"),
        c("lhb_on").alias("lhb"),
    ).with_columns((c("lhasa1") | c("trip1") | c("foresee20")).alias("combo"))


def main() -> None:
    scores = pl.read_parquet(H.LAB / SCORES)
    fl = flags()
    joined = scores.join(fl, on=[CODE, "date"], how="left")
    base_buys = H.to_signals(scores, OFFER).filter(c("score") > OFFER - PER_DAY).join(fl, on=[CODE, "date"], how="left")
    out: dict = {"would_be_buys": base_buys.height,
                 "flagged_share": {k: round(float(base_buys[k].fill_null(False).mean()), 4)
                                   for k in ("lhasa3", "lhasa1", "trip1", "lhb", "foresee20", "combo")}}
    print("share of the ungated book's daily top-4 carrying each flag:", out["flagged_share"], flush=True)
    for name in ("none", "lhasa3", "lhasa1", "trip1", "lhb", "foresee20", "combo"):
        s = scores if name == "none" else joined.filter(~c(name).fill_null(False)).select(scores.columns)
        for p in S.PERIODS:
            eq = H.equity(s, p, hold=HOLD, positions=SLOTS, offer=OFFER)
            for gated in (False, True):
                g = G.gate(eq, look=LOOK, low=LOW) if gated else None
                r = H.evaluate(s, f"lhasa_book:{name}:{p}:{'gate' if gated else 'raw'}", hold=HOLD, positions=SLOTS, offer=OFFER,
                               periods=(p,), note=f"lhasa filter {name}", exposure=g)["periods"][p]
                out[f"{name}|{p}|{'gate' if gated else 'raw'}"] = r
                print(f"{name:9s} {p:6s} {'gate' if gated else 'raw ':4s} total {r['total']:+6.1f}% mdd {r['mdd']:6.1f}% "
                      f"sharpe {r['sharpe']:.2f} trades {r['trades']} excess {r['excess_avg']}", flush=True)
    (H.LAB / "lhasa_book.json").write_text(json.dumps(out, ensure_ascii=False, indent=1, default=str), encoding="utf-8")


if __name__ == "__main__":
    main()
