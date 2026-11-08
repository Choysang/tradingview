"""Pick LLM factors on TRAIN statistics only and score their composite once on the shared book.

Rules (fixed before looking at VALID): |t| >= 3 and coverage >= 0.8 on TRAIN, |corr| < 0.7 with every
existing factor; sign from the TRAIN IC; then greedily add by |t| while the pairwise rank correlation
with already chosen ones stays below 0.6; at most 6. Composite = mean of per-date percentile ranks.
"""
import json

import numpy as np
import polars as pl

import harness as H
import dsl

c = pl.col
MAX_PICK, T_MIN, COV_MIN, RED_MAX, PAIR_MAX = 6, 3.0, 0.8, 0.7, 0.6

cands = [json.loads(line) for line in (H.LAB / "llm_candidates.jsonl").open(encoding="utf-8")]
ok = [h for h in cands if not h.get("error") and h.get("t") is not None and abs(h["t"]) >= T_MIN
      and (h.get("coverage") or 0) >= COV_MIN and (h.get("closest_corr") or 0) < RED_MAX]
ok.sort(key=lambda h: -abs(h["t"]))
print(f"{len(cands)} candidates, {len(ok)} pass the TRAIN screen")

F = H.features(20)
keys = F.select("code", "date")
panel = H.ctx().panel.lazy()
cols = {}
for h in ok:
    sign = 1.0 if h["ic"] > 0 else -1.0
    v = (panel.with_columns((dsl.compile_node(h["expr"]) * sign).alias("v")).select(
        "code", "date", pl.when(c("v").is_finite()).then(c("v")).alias("v")).collect())
    v = keys.join(v, on=["code", "date"], how="left").with_columns((c("v").rank().over("date") / c("v").count().over("date")).alias("v"))
    cols[h["name"]] = v["v"]
sample = F.select("date").with_row_index("i").filter(c("date") <= H.TRAIN_END).filter(c("i") % 50 == 0)["i"].to_numpy()
chosen = []
for h in ok:
    x = cols[h["name"]].to_numpy()[sample]
    good = True
    for name in chosen:
        y = cols[name].to_numpy()[sample]
        m = np.isfinite(x) & np.isfinite(y)
        if m.sum() > 1000 and abs(np.corrcoef(x[m], y[m])[0, 1]) >= PAIR_MAX:
            good = False
            break
    if good:
        chosen.append(h["name"])
        print(f"  pick {h['name']:32s} IC {h['ic']} t {h['t']} ({h['idea'][:90]})")
    if len(chosen) >= MAX_PICK:
        break
score = keys.with_columns(*(cols[n].alias(n) for n in chosen)).with_columns(
    pl.mean_horizontal(*chosen).alias("score")).select("code", "date", "score").drop_nulls()
score.write_parquet(H.LAB / "llm_composite.parquet")
print(H.show(H.evaluate(score, "llm/composite_v1", note=f"{len(chosen)} factors: {', '.join(chosen)}")))
