"""LLM factor-mining employee (lab venv): an AlphaAgent / R&D-Agent style loop on the tensor engine.

Each round the model gets the factor language, a regime diagnosis computed only from data up to
the mining end date (which zoo styles worked over the last 60 days versus the whole window), the
subtrees the pool already overuses (Alpha-Jungle style frequent-subtree avoidance), and the scored
results of its previous proposals. It returns hypotheses with formulas; each formula is scored by
the same fitness as the GP miner (gp_mine.Miner), whose hall of fame is seeded with the zoo, so a
formula that merely restates a known factor is penalised (AlphaAgent's originality check).

Env: LLM_TAG (default "pre"), LLM_ROUNDS (25), LLM_PER_ROUND (12), LLM_MODEL, LLM_EFFORT, GP_START/GP_END.
Results: .work/lab/llm_mine_<tag>.jsonl (every proposal, with its hypothesis and score).
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
from collections import Counter
from datetime import date

import numpy as np

import gp_mine as G
import llm
import tengine as E
import zoo

TAG = os.environ.get("LLM_TAG", "pre")
ROUNDS, PER_ROUND = int(os.environ.get("LLM_ROUNDS", "25")), int(os.environ.get("LLM_PER_ROUND", "12"))
MODEL, EFFORT = os.environ.get("LLM_MODEL", "gpt-6.1-terra"), os.environ.get("LLM_EFFORT", "high")
OUT = E.LAB / f"llm_mine_{TAG}.jsonl"

SPEC = """You are a quantitative researcher at a top Chinese quant fund, mining daily-frequency stock alpha factors for
the A-share market (about 4,000 tradable stocks a day). A factor is one formula in this language:
Fields (per stock per day, all known at that day's close): $open $high $low $close (forward-adjusted prices),
$volume, $amount (turnover value, 100M CNY), $vwap (typical price), $turn (daily turnover %), $ret (daily return,
fraction), $mkt (that day's mean return of all tradable stocks), $cap (float market cap), $is_limit_up (0/1),
$is_limit_down (0/1), $touched_limit (0/1), $is_broken (touched the limit-up but closed below it, 0/1),
$limit_streak (consecutive limit-up days).
Operators: + - * / and comparisons a>b, a<b (give 1/0); numbers like 0.5.
Unary: Abs Log Sign Sqrt Neg Inv; CSRank (cross-sectional percentile that day), CSZScore, CSDemean.
Binary: Greater(a,b) Less(a,b) (elementwise max/min).
Rolling over each stock's own past bars, window d an integer 2..250: Ref(x,d) (value d days ago), Mean Sum Std Var
Max Min Med Mad Delta(x,d) WMA EMA Slope Rsquare Resi (vs time) Rank(x,d) (percentile of today's value in its own
window) IdxMax IdxMin (1-based position of the extreme, oldest=1), Quantile(x,d,q); pairs: Corr(x,y,d) Cov(x,y,d).
Rules: at most 15 operators+fields per formula; no future data exists in the language; the label is the stock's
next-5-trading-day return (buy next open) minus the market average. Both signs are fine: the sign is learned.
The goal is NEW, economically motivated signals that are not simple restatements of classic price/volume
factors and that are not near-copies of each other."""

ASK = """Propose {n} new factors. Each must have a one-sentence hypothesis (why it should predict 5-day relative
returns in the CURRENT market regime described below) and a formula. Prefer diverse mechanisms: liquidity, limit-up
microstructure, turnover dynamics, overnight vs intraday, volatility asymmetry, crowding, attention, market-relative
behaviour. Avoid the overused building blocks listed below. Reply only with JSON:
[{{"name": "short_snake_case", "hypothesis": "...", "expr": "..."}}, ...]"""


def regime_report(data: E.Data, end: date) -> str:
    """Which zoo factors worked over the 60 days before `end` versus the whole history before it."""
    ev = E.Evaluator(data, cache_size=8)
    y = data.label(5)
    i_end = data.dates.index(max(d for d in data.dates if d <= end)) - 6  # label must be complete by `end`
    recent = list(range(i_end - 60, i_end + 1))
    full = list(range(130, i_end + 1))
    rows = []
    for name in ("ROC5", "ROC20", "ROC60", "STD20", "VMA20", "VSUMP20", "CORR20", "KUP", "KLOW", "SIZE", "TURN_MA20",
                 "TURN_REL5", "AMIHUD20", "IVOL20", "GAP20", "INTRA20", "ZT20", "MAXRET20", "BETA_MKT60", "MA60", "RSV20"):
        x = ev(zoo.zoo()[name])
        ic = E.daily_ic(x, y).numpy()
        rows.append((name, zoo.zoo()[name], float(np.nanmean(ic[recent])), float(np.nanmean(ic[full]))))
    lines = [f"  {n:12s} recent60 IC {a:+.3f} | full IC {b:+.3f} | {expr}" for n, expr, a, b in sorted(rows, key=lambda r: -abs(r[2]))]
    idx = data.index_close.numpy()
    ix = idx[: i_end + 7]
    trend = f"index last 20d {(ix[-1] / ix[-21] - 1) * 100:+.1f}%, last 60d {(ix[-1] / ix[-61] - 1) * 100:+.1f}%"
    return f"Regime up to {end} ({trend}). Rank IC with next-5-day relative return, recent 60 days vs full history:\n" + "\n".join(lines)


def frequent_subtrees(exprs: list[str], top: int = 12) -> list[str]:
    cnt = Counter()
    for e in exprs:
        try:
            node = E.parse(e)
        except E.ExprError:
            continue
        stack = [node]
        while stack:
            n = stack.pop()
            if n[0] not in ("num", "field") and E.size(n) >= 2:
                cnt[G.to_str(n)] += 1
            stack += [a for a in n[1:] if isinstance(a, tuple)]
    return [s for s, c in cnt.most_common(top) if c >= 2]


def main() -> None:
    data = E.Data()
    start = date.fromisoformat(os.environ.get("GP_START", data.dates[0].isoformat()))
    end = date.fromisoformat(os.environ.get("GP_END", E.PRE_END.isoformat()))
    miner = G.Miner(data, start, end, out=E.LAB / f"llm_mine_{TAG}.scores.jsonl")
    z = zoo.zoo()
    for name, expr in z.items():  # seed the hall of fame with the zoo (originality check)
        miner.fitness(E.parse(expr))
    print(f"[llm {TAG}] seeded hof with zoo: {len(miner.hof)} kept of {len(z)}", flush=True)
    report = regime_report(data, end)
    feedback, mine = "", []
    done = sum(1 for _ in OUT.open(encoding="utf-8")) if OUT.exists() else 0
    for rnd in range(ROUNDS):
        overused = frequent_subtrees([e for _, e, _ in miner.hof] + [m["expr"] for m in mine])
        prompt = (f"{report}\n\nOverused building blocks in the current pool (avoid them): {overused}\n\n"
                  f"{feedback}\n\n" + ASK.format(n=PER_ROUND))
        t0 = time.perf_counter()
        try:
            text = llm.ask(prompt, model=MODEL, effort=EFFORT, system=SPEC)
            s, e = text.find("["), text.rfind("]")
            props = json.loads(text[s: e + 1])
        except Exception as exc:  # one bad round should not end the run
            print(f"[llm {TAG}] round {rnd} failed: {str(exc)[:200]}", flush=True)
            continue
        results = []
        for p in props[:PER_ROUND]:
            expr = str(p.get("expr", ""))
            rec = {"round": rnd + done // max(PER_ROUND, 1), "name": p.get("name"), "hypothesis": p.get("hypothesis"), "expr": expr}
            try:
                node = E.parse(expr)
                if E.size(node) > 25:
                    raise E.ExprError("too large")
                fit = miner.fitness(node)
                info = miner.seen[G.to_str(node)]
                rec |= {k: info.get(k) for k in ("fit", "ic", "t", "coverage", "max_corr", "error")}
            except E.ExprError as exc:
                rec["error"] = str(exc)[:160]
            results.append(rec)
            mine.append(rec)
            with OUT.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
        ok = [r for r in results if r.get("t") is not None and abs(r["t"]) >= 3 and (r.get("max_corr") or 1) <= G.DIVERSITY]
        print(f"[llm {TAG}] round {rnd}: {len(results)} proposals, {len(ok)} kept, {time.perf_counter() - t0:.0f}s", flush=True)
        lines = []
        for r in results:
            if r.get("error"):
                lines.append(f"- {r['name']}: REJECTED ({r['error']}) {r['expr']}")
            else:
                verdict = "KEPT" if r in ok else ("too similar to pool" if (r.get("max_corr") or 0) > G.DIVERSITY else "weak")
                lines.append(f"- {r['name']}: IC {r.get('ic') or float('nan'):+.4f} t {r.get('t') or float('nan'):+.2f} "
                             f"max|corr| with pool {r.get('max_corr')} -> {verdict} | {r['expr']}")
        best = sorted([m for m in mine if m.get("fit")], key=lambda m: -m["fit"])[:8]
        feedback = ("Results of your previous round (IC on the mining window; |t|>=3 and max|corr|<=0.7 are kept):\n" + "\n".join(lines)
                    + "\nYour best so far:\n" + "\n".join(f"- {m['name']} fit {m['fit']:.4f} t {m['t']:+.2f}: {m['expr']}" for m in best))
    kept = [m for m in mine if m.get("t") is not None and abs(m["t"]) >= 3 and (m.get("max_corr") or 1) <= G.DIVERSITY]
    print(f"[llm {TAG}] done: {len(mine)} proposals, {sum(1 for m in mine if m.get('error'))} invalid, {len(kept)} kept", flush=True)


if __name__ == "__main__":
    sys.exit(main())
