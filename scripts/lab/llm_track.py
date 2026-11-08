"""LLM track: the model proposes factor formulas in the safe DSL; the engine scores them.

The model only ever sees TRAIN-period statistics (rank IC of the 20-day forward excess return,
labels that end inside TRAIN). Selection uses TRAIN only; VALID is scored once for the final
composite. The model's training data may cover 2024-2026, so its ideas can carry hindsight:
the lockbox and forward tracking are the real test.
usage: OPENAI_API_KEY=... python llm_track.py <rounds>
"""
import json
import os
import re
import sys
import time

import numpy as np
import polars as pl

import harness as H  # sets sys.path for the backend imports in dsl
import dsl
import llm

ROUNDS = int(sys.argv[1]) if len(sys.argv) > 1 else 5
PER_ROUND = 8
MODEL, EFFORT = "gpt-6.1-terra", "high"
REPAIR_MODEL = "gpt-6-luna"
OUT = H.LAB / "llm_candidates.jsonl"
c = pl.col

t0 = time.perf_counter()
F = H.features(20)
EXISTING = [n for n in F.columns if n not in ("code", "date", "fwd_ex")]
dates = H.ctx().dates
cut = dates[dates.index(H.TRAIN_END) - 22]  # labels end inside TRAIN
TR = F.filter(c("date") <= cut).select("code", "date", *EXISTING, "fwd_ex")
TR = TR.with_columns((c("fwd_ex").rank().over("date")).alias("_y"))
SAMPLE_DATES = TR["date"].unique().sort().gather_every(12).to_list()
EX_RANK = TR.filter(c("date").is_in(SAMPLE_DATES)).select(
    "code", "date", *((c(n).rank().over("date") / c(n).count().over("date")).alias(n) for n in EXISTING))
print(f"train rows {TR.height} to {cut}, {time.perf_counter() - t0:.0f}s", flush=True)


def ic_stats(values: pl.DataFrame) -> dict:
    """values: code, date, v (TRAIN rows only are used)."""
    j = TR.select("code", "date", "_y", "fwd_ex").join(values, on=["code", "date"], how="left")
    cover = float(j["v"].is_not_null().mean())
    # Percentile ranks: raw ranks pooled over dates correlate through each date's stock count.
    j = j.filter(c("v").is_not_null() & c("v").is_finite()).with_columns(
        (c("v").rank().over("date") / c("v").count().over("date")).alias("_v"))
    daily = j.group_by("date").agg(pl.corr("_v", "_y").alias("ic"), pl.len().alias("n")).filter(c("n") >= 100).sort("date")
    ic = daily["ic"].drop_nulls().to_numpy()
    q = j.with_columns((c("_v") / c("_v").max().over("date")).alias("_q"))
    top = q.filter(c("_q") >= 0.8)["fwd_ex"].mean()
    bot = q.filter(c("_q") <= 0.2)["fwd_ex"].mean()
    s = j.filter(c("date").is_in(SAMPLE_DATES)).select("code", "date", "_v").join(EX_RANK, on=["code", "date"])
    red = {}
    for n in EXISTING:
        pair = s.select("_v", n).drop_nulls()
        if pair.height > 100:
            red[n] = abs(float(np.corrcoef(pair["_v"].to_numpy(), pair[n].to_numpy())[0, 1]))
    red = {k: v for k, v in red.items() if np.isfinite(v)}
    worst = max(red.items(), key=lambda kv: kv[1]) if red else ("", 0.0)
    return {"ic": round(float(ic.mean()), 4) if len(ic) else None, "t": round(H.nw_t(ic, 20) or 0.0, 2),
            "top_minus_bottom": round(float(top - bot), 3) if top is not None and bot is not None else None,
            "coverage": round(cover, 3), "closest": worst[0], "closest_corr": round(worst[1], 3)}


def evaluate_expr(expr: pl.Expr) -> pl.DataFrame:
    return (H.ctx().panel.lazy().filter(c("date") <= cut).with_columns(expr.alias("v"))
            .select("code", "date", pl.when(c("v").is_finite()).then(c("v")).alias("v")).collect())


def existing_table() -> str:
    rows = []
    for n in EXISTING:
        m = ic_stats(TR.select("code", "date", c(n).alias("v")))
        rows.append(f"{n}: IC {m['ic']}, t {m['t']}, top20%-bottom20% {m['top_minus_bottom']}")
    return "\n".join(rows)


SYSTEM = (
    "You are a quantitative researcher designing daily-bar stock-selection factors for China A-shares "
    "(T+1, +-10%/20% daily limits, retail-heavy, frequent theme rotations). Work from economic or behavioural "
    "reasoning. Do not rely on memory of what specific stocks, sectors or dates did; the evaluation uses data "
    "you cannot see. Higher factor values must mean higher expected 20-day return; if unsure, the engine "
    "reports the sign and you may flip it."
)


def build_prompt(table: str, history: list[dict]) -> str:
    hist = "\n".join(
        f"- {h['name']}: " + (f"ERROR {h['error']}" if h.get("error") else
                             f"IC {h['ic']}, t {h['t']}, top-bottom {h['top_minus_bottom']}, coverage {h['coverage']}, "
                             f"closest existing {h['closest']} (|corr| {h['closest_corr']})")
        for h in history) or "(none yet)"
    return f"""{dsl.SPEC}

Label: excess 20-day return (buy next open, sell open 20 bars later, minus that day's mean over tradable stocks),
scored by daily rank IC on the training period only. Universe: liquid, listed >= 120 bars, price >= 2, ST excluded.

Existing factors (training-period results):
{table}

Your earlier proposals and their training-period results:
{hist}

Propose {PER_ROUND} NEW factors. Make them diverse, not near-copies of existing factors (|corr| above 0.7 is wasted)
or of each other. Learn from the results above: refine what worked, drop what did not. Keep each one simple
(prefer under 15 nodes). Return ONLY a JSON object:
{{"candidates": [{{"name": "snake_case", "idea": "one sentence", "expr": <node>}}, ...]}}"""


def parse(text: str) -> list[dict]:
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        raise ValueError("no JSON object in reply")
    return json.loads(m.group(0))["candidates"]


table = existing_table()
print(f"existing table {time.perf_counter() - t0:.0f}s", flush=True)
history: list[dict] = []
if OUT.exists():
    history = [json.loads(line) for line in OUT.open(encoding="utf-8")]
if history and os.environ.get("RESCORE") == "1":  # recompute stored candidates with the current scoring code
    for h in history:
        for key in ("ic", "t", "top_minus_bottom", "coverage", "closest", "closest_corr", "error"):
            h.pop(key, None)
        try:
            h.update(ic_stats(evaluate_expr(dsl.compile_node(h["expr"]))))
        except Exception as exc:
            h["error"] = f"{type(exc).__name__}: {str(exc)[:160]}"
        print(f"  rescored r{h['round']} {h['name']:32s} " + (h.get("error") or
              f"IC {h['ic']} t {h['t']} tb {h['top_minus_bottom']} cov {h['coverage']} ~{h['closest']} {h['closest_corr']}"), flush=True)
    OUT.write_text("".join(json.dumps(h, ensure_ascii=False) + "\n" for h in history), encoding="utf-8")
for rnd in range(ROUNDS):
    cur = max((h["round"] for h in history), default=0) + 1
    reply = llm.ask(build_prompt(table, history), model=MODEL, effort=EFFORT, system=SYSTEM)
    try:
        cands = parse(reply)
    except Exception as exc:  # one repair attempt with the fast model
        reply = llm.ask("Return the same content as valid JSON only, no prose:\n" + reply, model=REPAIR_MODEL,
                        effort="max", extra={"service_tier": "priority"})
        cands = parse(reply)
    for cand in cands[:PER_ROUND]:
        rec = {"round": cur, "name": str(cand.get("name"))[:60], "idea": str(cand.get("idea"))[:300],
               "expr": cand.get("expr")}
        try:
            rec.update(ic_stats(evaluate_expr(dsl.compile_node(cand.get("expr")))))
        except Exception as exc:
            rec["error"] = f"{type(exc).__name__}: {str(exc)[:160]}"
        history.append(rec)
        with OUT.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
        print(f"  r{rec['round']} {rec['name']:32s} " + (rec.get("error") or
              f"IC {rec['ic']} t {rec['t']} tb {rec['top_minus_bottom']} cov {rec['coverage']} ~{rec['closest']} {rec['closest_corr']}"),
              flush=True)
    print(f"round {rnd + 1} done {time.perf_counter() - t0:.0f}s", flush=True)
