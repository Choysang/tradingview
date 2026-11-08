"""Price < 100 and float cap < 100 亿 on the main fusion book: with vs without (backend venv).

The user's hot-money funnel, tested as a condition, not a global filter (11-01 decision). Applied to the
score list on the signal day D, before the book picks its names, so the book still fills its slots from
the remaining candidates. Float cap on D is approximated as the latest float cap x close(D) / latest close
(the share count is held at today's value: no history of share changes in the warehouse).
Each variant is shown plain and with the equity-curve gate (20 days / 0.3, built from that variant's own
NAV). Usage: python cap_filter.py fusion_static40_safe.parquet [...]   (writes .work/lab/main/cap_filter.json)
"""
from __future__ import annotations

import json
import sys

import polars as pl

import curve_gate as G
import harness as H
import style_test as S

PRICE_MAX, CAP_MAX = 100.0, 100.0


def funnel(scores: pl.DataFrame) -> pl.DataFrame:
    p = pl.read_parquet(H.BASE / "panel.parquet", columns=["code", "date", "close", "float_cap"])
    last = p.sort("date").group_by("code").agg(pl.col("close").last().alias("last_close"))
    p = p.join(last, on="code").with_columns((pl.col("float_cap") * pl.col("close") / pl.col("last_close")).alias("fcap_d"))
    keep = p.filter((pl.col("close") < PRICE_MAX) & (pl.col("fcap_d") < CAP_MAX)).select("code", "date")
    return scores.join(keep, on=["code", "date"], how="semi")


def main() -> None:
    out: dict = {}
    for name in sys.argv[1:]:
        raw = pl.read_parquet(H.LAB / name)
        for label, s in (("all", raw), ("funnel", funnel(raw))):
            for p in S.PERIODS:
                plain = H.evaluate(s, f"cap:{name}:{label}:{p}", hold=5, positions=20, offer=40, periods=(p,),
                                   note=f"price<{PRICE_MAX:.0f} cap<{CAP_MAX:.0f} {label}")
                eq = H.equity(s, p, hold=5, positions=20, offer=40)
                gated = H.evaluate(s, f"cap:{name}:{label}:{p}:gate", hold=5, positions=20, offer=40, periods=(p,),
                                   note=f"{label} + gate", exposure=G.gate(eq))
                out[f"{name}|{label}|{p}"] = {"plain": plain["periods"][p], "gate": gated["periods"][p]}
                print(H.show(plain), flush=True)
                print(H.show(gated), "| gated", flush=True)
    (H.LAB / "cap_filter.json").write_text(json.dumps(out, ensure_ascii=False, indent=1, default=str), encoding="utf-8")


if __name__ == "__main__":
    main()
