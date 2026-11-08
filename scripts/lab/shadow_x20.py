"""E085: forward shadow of the R10 X20 basket (docs/research/2026-11-08-batch4-preregistration.md, section 5).

From the first decision day on or after 2026-11-02, every 10 sessions: decide on day D's close, trade at the D+1
open; hold every pool name with a frozen score except the bottom 20% (sameday_base.in_pool, score percentile q > 0.2),
equal weight, against the equal-weight pool (EW). Rebalance cost = (weight sold + weight bought) x SIDE, the user's
account cost a side (USER_COSTS: about 0.126%). Scores: the as-of copy (.work/lab/live/scores_asof.parquet, written
by daily_forward.py), the current file filling dates not archived. Each run rebuilds the path from the start
(deterministic) and appends the days not logged yet to .work/lab/live/shadow_x20.jsonl: date, basket and EW value
marked at that day's close, and on trade days the turnover. The frozen main book is not touched.
Run (daily_forward.py step 7): cd scripts/lab && PULSE_DATA_DIR=<snapshot> LAB_UNIVERSE=main PYTHONPATH=../.. python shadow_x20.py
"""
from __future__ import annotations

import json
from datetime import date

import numpy as np
import polars as pl

import batch3_lib as L
import harness as H
import sameday_base as B
from harness import CODE, c

START = date(2026, 11, 2)
R = 10
SIDE = 0.00126
LOG = H.BASE / "live" / "shadow_x20.jsonl"
ASOF = H.BASE / "live" / "scores_asof.parquet"


def scores() -> pl.DataFrame:
    cur = pl.read_parquet(H.LAB / "fusion_static40_safe.parquet").filter(c("date") >= START).select(CODE, "date", "score")
    if not ASOF.exists():
        return cur
    asof = pl.read_parquet(ASOF).filter(c("date") >= START).select(CODE, "date", "score")
    return pl.concat([asof, cur.filter(~c("date").is_in(asof["date"].unique().to_list()))])


def path(panel: pl.DataFrame, pool: pl.DataFrame) -> list[dict]:
    days = sorted(panel["date"].unique().to_list())
    nxt = {a: b for a, b in zip(days, days[1:])}
    decide = sorted(pool["date"].unique().to_list())[::R]
    trade = [nxt[d] for d in decide if d in nxt]
    if not trade:
        return []
    px = panel.select(CODE, "date", "open", "close")
    opens = {d: dict(zip(g[CODE].to_list(), g["open"].to_list())) for (d,), g in px.filter(c("date").is_in(trade)).partition_by("date", as_dict=True).items()}
    closes = {d: dict(zip(g[CODE].to_list(), g["close"].to_list())) for (d,), g in px.partition_by("date", as_dict=True).items()}
    ranks = {d: dict(zip(g[CODE].to_list(), g["q"].to_list())) for (d,), g in pool.partition_by("date", as_dict=True).items()}
    held = {"X20": set(), "EW": set()}
    base = {"X20": 1.0, "EW": 1.0}  # value at the last trade open, after cost
    last_close: dict[str, float] = {}
    rows = []
    k = 0
    for d in [x for x in days if x >= trade[0]]:
        turn = {}
        if k < len(trade) and d == trade[k]:
            q = ranks[decide[k]]
            o = opens.get(d, {})
            for r in held:
                if held[r]:  # close the old period at today's open (the last close if no open)
                    base[r] *= float(np.mean([(o.get(x) or last_close.get(x, 0)) / held_px[r][x] for x in held[r] if held_px[r].get(x)]))
                new = {x for x, v in q.items() if (v > 0.2 if r == "X20" else True) and o.get(x)}
                cost = L.rebalance_cost(held[r], new, SIDE)
                base[r] *= 1 - cost
                turn[r] = round(cost / SIDE / 2, 4)
                held[r] = new
            held_px = {r: {x: opens[d][x] for x in held[r]} for r in held}
            k += 1
        last_close.update(closes.get(d, {}))
        val = {r: base[r] * float(np.mean([last_close.get(x, held_px[r][x]) / held_px[r][x] for x in held[r]])) if held[r] else base[r]
               for r in held}
        rows.append({"date": d.isoformat(), "x20": round(val["X20"], 6), "ew": round(val["EW"], 6),
                     "excess_pct": round((val["X20"] / val["EW"] - 1) * 100, 3), "n_x20": len(held["X20"]), "n_ew": len(held["EW"]),
                     **({"trade_day": True, "turnover_x20": turn.get("X20"), "turnover_ew": turn.get("EW")} if turn else {})})
    return rows


def main() -> None:
    panel = H.ctx().panel.filter(c(CODE).str.slice(0, 3).is_in(H.MAIN_PREFIX) & (c("date") >= date(2026, 9, 1)))
    out = B.outcomes(panel).filter(c("in_pool") & (c("date") >= START))
    pool = out.join(scores(), on=[CODE, "date"], how="inner").with_columns((c("score").rank() / c("score").count()).over("date").alias("q"))
    rows = path(panel, pool.select(CODE, "date", "q").sort("date", CODE))
    done = {json.loads(x)["date"] for x in LOG.read_text(encoding="utf-8").splitlines() if x.strip()} if LOG.exists() else set()
    new = [r for r in rows if r["date"] not in done]
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("a", encoding="utf-8") as fh:
        for r in new:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")
    print(json.dumps({"days": len(rows), "new": len(new), "last": rows[-1] if rows else None}, ensure_ascii=False))


if __name__ == "__main__":
    main()
