"""R7-4: parameter robustness of the equity-curve gate (backend venv).

Grid: moving-average window 10 / 15 / 20 / 30 days x share of new buys kept on gated days 0.5 / 0.25 / 0.
(The chosen rule is 20 days / 0.3; 0.3 of 4 daily buys rounds to 1, the same as 0.25.) The grid is read on
PRE (2023-11 .. 2026-06) only: a plateau there, not a lone peak, is what makes the rule credible.
SELECT and, with VERIFY=1, VERIFY are printed for reference; they choose nothing.
Usage: python gate_grid.py fusion_static40_safe.parquet [more score files]   (writes .work/lab/main/gate_grid.json)
"""
from __future__ import annotations

import json
import sys

import polars as pl

import curve_gate as G
import harness as H
import style_test as S

LOOKS = (10, 15, 20, 30)
LOWS = (0.5, 0.25, 0.0)


def main() -> None:
    out: dict = {}
    for name in sys.argv[1:]:
        s = pl.read_parquet(H.LAB / name)
        for p in S.PERIODS:
            eq = H.equity(s, p, hold=5, positions=20, offer=40)
            base = H.evaluate(s, f"grid:{name}:{p}:none", hold=5, positions=20, offer=40, periods=(p,), note="gate grid baseline")
            b = base["periods"][p]
            out[f"{name}|{p}|none"] = b
            print(f"{name} {p} no gate: total {b['total']:+.1f}% mdd {b['mdd']:.1f}% sharpe {b['sharpe']:.2f}", flush=True)
            for look in LOOKS:
                cells = []
                for low in LOWS:
                    g = G.gate(eq, look=look, low=low)
                    r = H.evaluate(s, f"grid:{name}:{p}:{look}:{low}", hold=5, positions=20, offer=40, periods=(p,),
                                   note=f"gate grid {look}d/{low}", exposure=g)["periods"][p]
                    r["gated_days"] = float((g["w"] < 1).mean())
                    out[f"{name}|{p}|{look}|{low}"] = r
                    cells.append(f"{low:.2f}: {r['total']:+6.1f}% / {r['mdd']:6.1f}% (S {r['sharpe']:.2f}, gated {r['gated_days']:.0%})")
                print(f"  {look:2d}d  " + " | ".join(cells), flush=True)
    (H.LAB / "gate_grid.json").write_text(json.dumps(out, ensure_ascii=False, indent=1, default=str), encoding="utf-8")


if __name__ == "__main__":
    main()
