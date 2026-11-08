"""E064: is the LLM's judgement usable in reverse? (pre-registered in docs/research/2026-11-07-batch1-preregistration.md)

No API calls: reads the 600 recorded cards (2026-07-01 ~ 10-22, 20 dates x 30 names) in three variants
(anonymous K-line card = main test; named card with announcements; anonymous + theme topology) and the
2026-10-30 forward batch (window incomplete until the 11-09 open: provisional, not part of the verdict).
Label: buy at the D+1 open, sell at the D+5 close, minus the same-day main-board pool mean (sameday_base.outcomes);
the cards include ChiNext / STAR names, labelled against the same main-board mean.
Main metric: rank IC of -p per date, t over the 20 dates; pass if t >= 2. Secondary: drop the top 20% p of each
date; the remaining names' mean excess minus all names', date-block bootstrap p < 0.05.
Run: cd scripts/lab && LAB_UNIVERSE=main PYTHONPATH=../.. ../../backend/.venv/Scripts/python.exe llm_reverse.py
"""
from __future__ import annotations

import json
from datetime import date

import numpy as np
import polars as pl

import harness as H
import sameday_base as B
from harness import CODE, c

FILES = {"anonymous": "judge_verdicts.jsonl", "named_pit": "judge_verdicts_pit.jsonl", "topology": "judge_verdicts_topo.jsonl"}
OUT = H.LAB / "llm_reverse.json"


def labels() -> pl.DataFrame:
    out = B.outcomes(H.ctx().panel)  # every board: the cards include ChiNext / STAR names; excess vs the main-board pool
    m = out.filter(c("in_pool") & c("r5").is_finite()).group_by("date").agg(c("r5").mean().alias("pool5"))
    return out.join(m, on="date").select(CODE, "date", (c("r5") - c("pool5")).alias("ex5"), "in_pool")


def test(v: pl.DataFrame, lab: pl.DataFrame, rng: np.random.Generator) -> dict:
    d = v.join(lab, on=[CODE, "date"], how="inner").filter(c("ex5").is_finite())
    ic = d.group_by("date").agg(pl.corr((-c("p")).rank(), c("ex5").rank()).alias("ic"), pl.len().alias("n")).filter(c("n") >= 10)
    x = ic["ic"].drop_nans().to_numpy()
    t = float(x.mean() / (x.std(ddof=1) / np.sqrt(x.size))) if x.size > 2 else None
    d = d.with_columns((c("p").rank("ordinal", descending=True).over("date") <= (pl.len().over("date") * 0.2).round(0)).alias("top_p"))
    per = d.group_by("date").agg(c("ex5").mean().alias("all"), c("ex5").filter(~c("top_p")).mean().alias("rest"),
                                 c("ex5").filter(c("top_p")).mean().alias("top"))
    diff = (per["rest"] - per["all"]).to_numpy()
    bi = rng.integers(0, diff.size, size=(B.N_BOOT, diff.size))
    acts = d.group_by("action").agg(pl.len().alias("n"), c("ex5").mean().round(3).alias("ex5")).sort("action")
    return {"cards": d.height, "dates": int(x.size), "ic_reversed": round(float(x.mean()), 4), "t": round(t, 2) if t is not None else None,
            "pass": bool(t is not None and t >= 2), "veto_gain": round(float(diff.mean()), 3),
            "veto_p": round(float((diff[bi].mean(axis=1) <= 0).mean()), 4), "top20_ex5": round(float(per["top"].mean()), 3),
            "by_action": {a: {"n": n, "ex5": e} for a, n, e in acts.iter_rows()}}


def forward(lab_bars: pl.DataFrame) -> dict:
    rows = [json.loads(x) for x in (H.BASE / "live" / "decisions.jsonl").open(encoding="utf-8")]
    f = pl.DataFrame([{CODE: r["code"], "group": r.get("group"), "p": float(r["p_outperform"])} for r in rows if r.get("p_outperform") is not None])
    b = lab_bars.filter(c("date").is_between(date(2026, 11, 2), date(2026, 11, 6)))
    o = b.filter(c("date") == date(2026, 11, 2)).select(CODE, c("open").alias("o"))
    cl = b.filter(c("date") == date(2026, 11, 6)).select(CODE, c("close").alias("cl"))
    j = f.join(o, on=CODE).join(cl, on=CODE).with_columns(((c("cl") / c("o") - 1) * 100).alias("ret"))
    rho = j.select(pl.corr((-c("p")).rank(), c("ret").rank())).item()
    return {"names": j.height, "ic_reversed_provisional": round(float(rho), 3), "se_approx": round(1 / np.sqrt(max(j.height - 3, 1)), 2),
            "note": "11-02 open to 11-06 close; the 5-day window ends at the 11-09 open"}


def main() -> None:
    lab = labels()
    rng = np.random.default_rng(B.SEED)
    res = {}
    for name, f in FILES.items():
        v = pl.read_ndjson(H.BASE / f).select(c("id").str.slice(0, 10).str.to_date().alias("date"), c("id").str.slice(11).alias(CODE),
                                             c("p").cast(pl.Float64), "action")
        res[name] = test(v, lab, rng)
        print(name, json.dumps(res[name], ensure_ascii=False), flush=True)
    res["forward_20261030"] = forward(pl.read_parquet(H.LAB / "paper_server" / "bars_week.parquet"))
    print("forward", res["forward_20261030"])
    OUT.write_text(json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")


if __name__ == "__main__":
    main()
