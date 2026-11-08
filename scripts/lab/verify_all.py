"""One-time VERIFY (2026-09-01 .. last bar) for the lab side of round 5 (run with env VERIFY=1).
Pre-registered set (docs/research/2026-11-01-round5-fusion.md section 10): fusion scores' IC, top-decile
spread and light-book return; the two FQI policies (saved models); exposure overlays on the main score;
and the recent-window GP survivors."""
from __future__ import annotations

import json
import os
import sys

import lightgbm as lgb
import numpy as np
import torch

import book as B
import exposure
import fusion
import rl_fqi
import tengine as E

assert os.environ.get("VERIFY") == "1", "run with VERIFY=1"
MAIN = "fusion_static40_mined.pt"
SCORES = [MAIN, "fusion_static40.pt", "fusion_long10_exp.pt", "fusion_static20_mined.pt", "rl_alphagen_rank.pt",
          "fusion_dyn20.pt", "fusion_long10_120.pt"]


def main() -> None:
    d = E.Data()
    y = d.label(5)
    per = fusion.periods(d)
    out = {"scores": {}, "fqi": {}, "exposure": {}, "gp_recent": []}
    for name in SCORES:
        s = torch.load(E.LAB / name)
        ic = E.daily_ic(s, y)
        row = {}
        for p in ("SELECT", "VERIFY"):
            st = E.ic_stats(ic, per[p], 5)
            res = B.fixed_hold(d, s, per[p], hold=5, slots=20)
            res.pop("nav")
            row[p] = {"ic": round(st["ic"], 4), "t": round(st["t"], 2), "top10": round(E.top_spread(s, y, per[p]), 3), "book": res}
        out["scores"][name] = row
        print(f"{name:28s} " + " | ".join(f"{p} IC {v['ic']:+.4f} (t {v['t']:+.2f}) top10 {v['top10']:+.2f}% book {v['book']['total']:+6.2f}% idx {v['book']['index']:+6.2f}% mdd {v['book']['mdd']:.1f}"
                                         for p, v in row.items()), flush=True)
    names, X = rl_fqi.state(d)
    for tag in ("excess_g0.8", "raw_g0.8"):
        w1 = lgb.Booster(model_file=str(E.LAB / f"rl_fqi_{tag}_w1.txt"))
        w0 = lgb.Booster(model_file=str(E.LAB / f"rl_fqi_{tag}_w0.txt"))
        res = rl_fqi.policy_book(d, X, w1, w0, per["VERIFY"])
        res.pop("nav")
        out["fqi"][tag] = res
        print(f"FQI {tag} VERIFY: {res}", flush=True)
    s = torch.load(E.LAB / MAIN)
    for rule, ex in exposure.rules(d).items():
        res = B.fixed_hold(d, s, per["VERIFY"], hold=5, slots=20, exposure=ex)
        res.pop("nav")
        out["exposure"][rule] = res
        print(f"exposure {rule:17s} VERIFY {res['total']:+6.2f}% idx {res['index']:+6.2f}% mdd {res['mdd']:.1f} exp {ex[per['VERIFY']].mean():.2f}", flush=True)
    ev = E.Evaluator(d, cache_size=8)
    rows = [json.loads(x) for x in (E.LAB / "gp_recent.jsonl").open(encoding="utf-8")]
    passed = [r for r in rows if r.get("t") is not None and abs(r["t"]) >= 3 and (r.get("max_corr") or 0) <= 0.7]
    for r in passed:
        ic = E.daily_ic(ev(r["expr"]), y)
        sign = 1 if r["ic"] >= 0 else -1
        rec = {"expr": r["expr"], "mine_ic": r["ic"], **{p: E.ic_stats(ic, per[p], 5)["ic"] * sign for p in ("SELECT", "VERIFY")}}
        out["gp_recent"].append(rec)
        print(f"gp_recent {r['ic']:+.4f} | SELECT dir-IC {rec['SELECT']:+.4f} | VERIFY dir-IC {rec['VERIFY']:+.4f} | {r['expr']}", flush=True)
    print(f"gp_recent: {len(rows)} generated, {len(passed)} passed mining", flush=True)
    json.dump(out, open(E.LAB / "verify_lab.json", "w"), indent=1, default=float)


if __name__ == "__main__":
    sys.exit(main())
