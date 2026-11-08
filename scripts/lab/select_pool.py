"""Count and select mined factors (lab venv). Mining (GP, LLM) only ever saw PRE; this script is the
one place SELECT is used for factors, with Benjamini-Hochberg control over every candidate tested.

Stages, each counted:
  generated -> valid (parsed, coverage >= 0.6) -> mining pass (|t| >= 3 on the mining window and
  max |corr| with the pool <= 0.7) -> deduplicated across sources (greedy, |corr| <= 0.7)
  -> SELECT test: one-sided NW t in the mining direction, BH at q = 0.05 over all deduplicated
  candidates -> survivors.
Writes .work/lab/mined_pool.json (the deduplicated mining-pass set, chosen without SELECT, used by
fusion) and .work/lab/mined_survivors.json (those that also survived SELECT), and prints the funnel.
"""
from __future__ import annotations

import json
import math
import statistics
import sys

import numpy as np
import torch

import gp_mine as G
import tengine as E

SOURCES = {"gp_pre": "gp_pre.jsonl", "gp_recent": "gp_recent.jsonl", "llm_pre": "llm_mine_pre.jsonl"}
Q = 0.05


def load(path) -> list[dict]:
    if not (E.LAB / path).exists():
        return []
    return [json.loads(x) for x in (E.LAB / path).open(encoding="utf-8")]


def bh(pvals: list[float], q: float) -> list[bool]:
    order = np.argsort(pvals)
    m = len(pvals)
    passed = np.zeros(m, dtype=bool)
    k_max = 0
    for rank, i in enumerate(order, start=1):
        if pvals[i] <= q * rank / m:
            k_max = rank
    for rank, i in enumerate(order, start=1):
        passed[i] = rank <= k_max
    return passed.tolist()


def main() -> None:
    d = E.Data()
    ev = E.Evaluator(d, cache_size=16)
    y = d.label(5)
    sel = d.rows(E.SELECT_START, E.SELECT_END)
    sample = list(range(d.rows(d.dates[0], E.PRE_END).start, d.rows(d.dates[0], E.PRE_END).stop))[::6]
    funnel, cands = {}, []
    for src, path in SOURCES.items():
        rows = load(path)
        valid = [r for r in rows if r.get("t") is not None and not r.get("error") and (r.get("coverage") or 0) >= 0.6]
        passed = [r for r in valid if abs(r["t"]) >= 3 and (r.get("max_corr") or 0) <= G.DIVERSITY]
        funnel[src] = {"generated": len(rows), "valid": len(valid), "mining_pass": len(passed)}
        for r in passed:
            cands.append({**r, "source": src})
    cands.sort(key=lambda r: -r["fit"])
    kept, units = [], []
    for r in cands:  # greedy dedupe across sources by sampled daily rank correlation (PRE days only)
        x = ev(r["expr"])
        u = G._unit(x[sample] * (1 if r["ic"] >= 0 else -1))
        if units:
            mc = float(torch.einsum("sn,hsn->hs", u, torch.stack(units)).mean(1).abs().max())
            if mc > G.DIVERSITY:
                continue
        units.append(u)
        ic = E.daily_ic(x, y)
        s = E.ic_stats(ic, sel, 5)
        sign = 1 if r["ic"] >= 0 else -1
        t_dir = s["t"] * sign if math.isfinite(s["t"]) else float("nan")
        kept.append({**r, "sel_ic": s["ic"], "sel_t": s["t"], "sel_t_dir": t_dir,
                     "p": 1 - statistics.NormalDist().cdf(t_dir) if math.isfinite(t_dir) else 1.0})
    ok = bh([k["p"] for k in kept], Q)
    survivors = [k for k, o in zip(kept, ok) if o]
    for src in SOURCES:
        funnel.setdefault(src, {})
        funnel[src]["dedup"] = sum(1 for k in kept if k["source"] == src)
        funnel[src]["select_survive"] = sum(1 for k in survivors if k["source"] == src)
        funnel[src]["select_same_sign"] = sum(1 for k in kept if k["source"] == src and k["sel_t_dir"] > 0)
    print("funnel:", json.dumps(funnel, indent=1))
    print(f"deduplicated candidates tested on SELECT: {len(kept)}; BH q={Q} survivors: {len(survivors)}")
    for k in sorted(survivors, key=lambda k: -k["sel_t_dir"])[:30]:
        print(f"  {k['source']:9s} pre IC {k['ic']:+.4f} t {k['t']:+.2f} | SELECT IC {k['sel_ic']:+.4f} t {k['sel_t']:+.2f} | {k.get('name') or ''} {k['expr']}")
    name = lambda k, i: (k.get("name") or f"{k['source']}_{i}")  # noqa: E731
    json.dump([{"name": f"{name(k, i)}_{i}", "expr": k["expr"], "source": k["source"], "pre_ic": k["ic"]} for i, k in enumerate(kept)],
              open(E.LAB / "mined_pool.json", "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    json.dump(survivors, open(E.LAB / "mined_survivors.json", "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    json.dump(funnel, open(E.LAB / "mined_funnel.json", "w"), indent=1)


if __name__ == "__main__":
    sys.exit(main())
