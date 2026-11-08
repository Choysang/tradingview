"""E062: re-evaluate daily signals against style-matched same-day baselines (sameday_base.evaluate_matched).

Signals: the product's playbooks (each day's top 20 by signal score; playbooks that need point-in-time
fundamentals are skipped) and the lab signals that came out positive in E058 / E060 (RPS_up, the three
breakout-pullback-to-MA10 rules, APRON, ma_pullback, the main book's daily top 4, regular bottom divergence).
Pre-registered survivor rule (docs/research/2026-11-07-batch1-preregistration.md): 10-day hold, PRE matched
(size x volatility) excess > 0 with p < 0.05 and DEV matched excess >= 0.
Run: cd scripts/lab && LAB_UNIVERSE=main PYTHONPATH=../.. ../../backend/.venv/Scripts/python.exe matched_reeval.py
"""
from __future__ import annotations

import json
import time

import polars as pl

import harness as H
import sameday_base as B
from harness import CODE, c

OUT = H.LAB / "matched_reeval.json"
TOP = 20


def playbook_signals(panel: pl.DataFrame) -> dict[str, pl.DataFrame]:
    from backend.app.playbooks.base import catalog
    out = {}
    lz = panel.lazy()
    for pb in catalog():
        if pb.requires_history:
            continue
        try:
            s = pb.signal(lz, pb.params()).collect()
        except Exception as e:  # noqa: BLE001 - report and go on
            print(f"[E062] {pb.spec.id} failed: {type(e).__name__}: {e}", flush=True)
            continue
        if s.is_empty():
            continue
        out[f"pb:{pb.spec.id}"] = s.sort("score", descending=True).group_by("date", maintain_order=True).head(TOP).select(CODE, "date")
    return out


def lab_signals(panel: pl.DataFrame) -> dict[str, pl.DataFrame]:
    import exit_tails as X
    import sequoia_audit as S
    import trullas_div as T
    p = S.features()
    R = S.rules()
    out = {"lab:RPS_up": S.sig(p, R["RPS_up"]), "lab:APRON": S.sig(p, R["APRON"])}
    for base in ("TURTLE_up", "MAVOL_up", "RPS_up"):
        out[f"lab:{base}_pb10"] = S.pullbacks(p, R[base], 10)
    ent = X.entry_sets(panel)
    out["lab:ma_pullback"] = ent["ma_pullback"]
    out["lab:main_book_top4"] = ent["main_book"]
    out["lab:third_buy_pit"] = ent["third_buy"]
    div = T.entries(panel)
    out["lab:div_regular"] = div.filter(c("pattern") == "regular").select(CODE, "date")
    return out


def main() -> None:
    t0 = time.perf_counter()
    panel = H.ctx().panel.filter(c(CODE).str.slice(0, 3).is_in(H.MAIN_PREFIX)).sort(CODE, "date")
    out = B.add_style(B.outcomes(panel), panel)
    sigs = playbook_signals(panel)
    print(f"[E062] {len(sigs)} playbooks {time.perf_counter() - t0:.0f}s", flush=True)
    sigs |= lab_signals(panel)
    print(f"[E062] +lab signals {time.perf_counter() - t0:.0f}s", flush=True)
    res = {}
    for name, s in sigs.items():
        rows = B.evaluate_matched(s, out, name)
        pre10 = next((r for r in rows if r["period"] == "PRE" and r["hold"] == 10), {})
        res[name] = {"rows": rows, "survives": B.survives(rows),
                     "raw_sig": bool(pre10.get("n") and pre10["ex_all"] > 0 and pre10["p_all"] < 0.05)}
    OUT.write_text(json.dumps(res, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    keys = ("n", "ex_all", "p_all", "ex_sv", "p_sv", "ex_svi", "p_svi")
    print("| signal | raw PRE sig | survives | PRE10 " + " | ".join(keys) + " | DEV10 ex_all | DEV10 ex_sv |\n|" + "---|" * (len(keys) + 5))
    for name, v in sorted(res.items(), key=lambda kv: -next((r.get("ex_sv", -99) for r in kv[1]["rows"] if r["period"] == "PRE" and r["hold"] == 10), -99)):
        pre = next(r for r in v["rows"] if r["period"] == "PRE" and r["hold"] == 10)
        dev = next(r for r in v["rows"] if r["period"] == "DEV" and r["hold"] == 10)
        print(f"| {name} | {v['raw_sig']} | {v['survives']} | " + " | ".join(str(pre.get(k, "")) for k in keys)
              + f" | {dev.get('ex_all', '')} | {dev.get('ex_sv', '')} |")
    raw = [k for k, v in res.items() if v["raw_sig"]]
    surv = [k for k, v in res.items() if v["survives"]]
    print(f"\nraw PRE-significant: {len(raw)}; survivors after matching: {len(surv)}: {surv}")


if __name__ == "__main__":
    main()
