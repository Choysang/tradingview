"""Round-5 fusion (lab venv): one walk-forward scorer over every factor source, judged on real data.

Sources: the zoo (Alpha158 + A-share extras), GP-mined and LLM-mined factors that passed the
mining stage on PRE only, plus market-state columns so trees can learn regime interactions.
Two scorers, both refit through time using only labels complete before the prediction day:
- lgbm: LightGBM on per-day percentile features and a per-day percentile label (5-day relative
  return), refit every 20 trading days, rows weighted by recency (half-life `HL` days; 0 = flat).
- dyn: AlphaForge-style dynamic linear combination: each day the K factors with the best trailing
  60-day ICIR (ending 6 days back, so labels are complete) are z-scored and summed with ICIR weights.
Outputs .work/lab/fusion_<name>.pt (score grid) and prints daily rank IC and top-decile spread per
period. VERIFY rows are scored only with env VERIFY=1.

Env: FUSION_MODELS (default "dyn,lgbm"), FUSION_HL (comma list, default "0,120"), FUSION_POOL
(path to a JSON list of {"name","expr"} mined factors; default .work/lab/mined_pool.json if present).
"""
from __future__ import annotations

import json
import os
import sys
import time

import lightgbm as lgb
import numpy as np
import torch

import tengine as E
import zoo

H = 5
REFIT, PURGE, START_TEST = 20, H + 1, "2025-01-02"
SEED = 20261101
SFX = os.environ.get("FUSION_TAG", "")  # suffix for score files, e.g. "_mined"


def market_state(d: E.Data) -> dict[str, torch.Tensor]:
    """Same value for every stock on a day: index trend, breadth, limit-up sentiment."""
    idx = d.index_close.to(torch.float32)
    f = d.f
    tr = f["tradable"] > 0
    up = torch.where(tr, (f["ret"] > 0).to(torch.float32), torch.nan)
    live = d.live  # all listed stocks, or with LAB_UNIVERSE=main those past their first NEW_DAYS days
    zt = torch.where(live, f["is_limit_up"], 0).sum(1)
    dt = torch.where(live, f["is_limit_down"], 0).sum(1)
    br = torch.where(live, f["is_broken"], 0).sum(1) / torch.where(live, f["touched_limit"], 0).sum(1).clamp(min=1)
    col = lambda v: E._broadcast(v.to(torch.float32), d.N)  # noqa: E731
    i = idx[:, None]
    out = {
        "M_IDX_R5": col((idx / torch.roll(idx, 5) - 1)), "M_IDX_R20": col(idx / torch.roll(idx, 20) - 1),
        "M_IDX_R60": col(idx / torch.roll(idx, 60) - 1),
        "M_IDX_MA60": col(idx / E.ts_mean(i, 60)[:, 0] - 1), "M_IDX_VOL20": col(E.ts_std(E.ts_delta(i, 1) / i, 20)[:, 0]),
        "M_UP": col(E._nanmean_row(up)), "M_ZT": col(zt), "M_DT": col(dt), "M_BROKEN": col(br),
        "M_ZT_MA5": col(E.ts_mean(zt[:, None].to(torch.float32), 5)[:, 0]),
    }
    for k in ("M_IDX_R5", "M_IDX_R20", "M_IDX_R60"):
        out[k][: int(k.split("R")[-1])] = torch.nan
    out |= {k: col(v) for k, v in sentiment(d).items()}
    return out


def sentiment(d: E.Data) -> dict[str, torch.Tensor]:
    """Market-wide sentiment and crowd-psychology series [T] (known at each close):
    premium of yesterday's limit-ups (昨日涨停溢价), highest limit-up streak (空间高度), turnover heat,
    new highs minus new lows, cross-sectional dispersion, co-movement (herding: index variance over the
    mean stock variance, 20 days) and a sentiment-cycle phase code from the limit-up count
    (0 冰点 ice, 1 修复 repair, 2 neutral, 3 高潮 climax, 4 退潮 ebb)."""
    f = d.f
    live = d.live
    zt_prev = E._shift(f["is_limit_up"], 1) > 0
    prem = torch.where(zt_prev & live, f["ret"], torch.nan)
    amt = torch.where(live, f["amount"], 0).sum(1)
    hi60 = f["close"] >= E.ts_max(f["close"], 60)
    lo60 = f["close"] <= E.ts_min(f["close"], 60)
    tr = d.tradable
    n = tr.sum(1).clamp(min=1).to(torch.float32)
    r = torch.where(tr, f["ret"], torch.nan)
    mkt = E._nanmean_row(r)[:, None]
    var_m = E.ts_var(mkt, 20)[:, 0]
    var_s = E._nanmean_row(E.ts_var(f["ret"], 20).masked_fill(~tr, torch.nan))
    zt = torch.where(live, f["is_limit_up"], 0).sum(1)[:, None].to(torch.float32)
    zt5 = E.ts_mean(zt, 5)
    z = (zt5 - E.ts_mean(zt5, 120)) / (E.ts_std(zt5, 120) + 1e-9)
    dz = zt5 - E._shift(zt5, 3)
    phase = torch.full_like(z, 2.0)
    phase = torch.where((z > 0) & (dz < 0), torch.tensor(4.0), phase)
    phase = torch.where(z > 1, torch.tensor(3.0), phase)
    phase = torch.where((z < 0) & (dz > 0), torch.tensor(1.0), phase)
    phase = torch.where(z < -1, torch.tensor(0.0), phase)
    phase = torch.where(torch.isfinite(z), phase, torch.nan)
    return {
        "M_PREMIUM": E._nanmean_row(prem), "M_MAXSTREAK": torch.where(live, f["limit_streak"], 0).amax(1),
        "M_AMT_REL": amt / E.ts_mean(amt[:, None], 20)[:, 0], "M_HL": ((hi60 & tr).sum(1) - (lo60 & tr).sum(1)) / n,
        "M_DISP": torch.from_numpy(np.nanstd(r.numpy(), axis=1)), "M_COMOVE": var_m / var_s,
        "M_ZT_Z": z[:, 0], "M_PHASE": phase[:, 0],
    }


def load_pool() -> dict[str, str]:
    path = os.environ.get("FUSION_POOL", str(E.LAB / "mined_pool.json"))
    if not os.path.exists(path):
        return {}
    pool = json.load(open(path, encoding="utf-8"))
    top = int(os.environ.get("FUSION_POOL_TOP", "0"))
    if top:  # keep the strongest by mining-window |IC| only (never by SELECT), to bound memory
        pool = sorted(pool, key=lambda m: -abs(m["pre_ic"]))[:top]
    out = {f"X_{m['name']}": m["expr"] for m in pool}
    if os.path.exists(E.LAB / "rl_alphagen.json") and os.environ.get("FUSION_ALPHAGEN", "1") == "1":
        for i, r in enumerate(json.load(open(E.LAB / "rl_alphagen.json"))["pool"]):
            out[f"AG_{i}"] = r["expr"]
    return out


def build(d: E.Data) -> tuple[list[str], list[torch.Tensor]]:
    ev = E.Evaluator(d, cache_size=32)
    names, mats = [], []
    exprs = zoo.zoo() | load_pool()
    t0 = time.perf_counter()
    if os.environ.get("FUSION_SAFE") == "1":  # drop factors whose ranks depend on forward-adjusted price levels
        scaled, rows = E.ScaledView(d), list(range(150, d.T, 10))
        safety = {name: E.level_safe(expr, d, scaled, rows) for name, expr in exprs.items()}
        json.dump({n: {"safe": s, "rank_corr": r, "expr": exprs[n]} for n, (s, r) in safety.items()},
                  open(E.LAB / "level_safety.json", "w", encoding="utf-8"), ensure_ascii=False, indent=1)
        exprs = {n: e for n, e in exprs.items() if safety[n][0]}
        print(f"[fusion] level check: {sum(s for s, _ in safety.values())} of {len(safety)} factors safe", flush=True)
    for name, expr in exprs.items():
        x = ev(expr)
        names.append(name)
        mats.append(E.cs_rank(torch.where(d.tradable, x, torch.nan)).to(torch.float16))  # per-day percentile
    for name, x in market_state(d).items():
        names.append(name)
        mats.append(x.to(torch.float16))
    print(f"[fusion] {len(names)} features ({len(exprs)} factors) in {time.perf_counter() - t0:.0f}s", flush=True)
    return names, mats


def periods(d: E.Data) -> dict[str, slice]:
    from datetime import date
    out = {"2025H1": d.rows(date(2025, 1, 2), date(2025, 6, 30)), "2025H2": d.rows(date(2025, 7, 1), date(2025, 12, 31)),
           "2026H1": d.rows(date(2026, 1, 1), E.PRE_END), "SELECT": d.rows(E.SELECT_START, E.SELECT_END)}
    if os.environ.get("VERIFY") == "1":
        out["VERIFY"] = d.rows(E.VERIFY_START, d.dates[-1 - H - 1])
    return out


def report(d: E.Data, score: torch.Tensor, name: str) -> dict:
    y = d.label(H)
    ic = E.daily_ic(score, y)
    out = {}
    for p, r in periods(d).items():
        s = E.ic_stats(ic, r, H)
        out[p] = {"ic": round(s["ic"], 4), "t": round(s["t"], 2), "top10": round(E.top_spread(score, y, r), 3)}
    print(f"[fusion] {name}: " + " | ".join(f"{p} IC {v['ic']:+.4f} t {v['t']:+.2f} top10 {v['top10']:+.2f}%" for p, v in out.items()), flush=True)
    return out


def dyn(d: E.Data, names: list[str], mats: list[torch.Tensor], k: int = 20, look: int | None = 60) -> torch.Tensor:
    y = d.label(H)
    fac = [i for i, n in enumerate(names) if not n.startswith("M_")]
    ics = torch.stack([E.daily_ic(mats[i].to(torch.float32), y, min_n=100) for i in fac])  # [K, T]
    ics = torch.nan_to_num(ics, nan=0.0)
    cs = torch.cat([torch.zeros(len(fac), 1), ics.cumsum(1)], 1)
    cs2 = torch.cat([torch.zeros(len(fac), 1), (ics ** 2).cumsum(1)], 1)
    score = torch.full((d.T, d.N), torch.nan)
    first = 130  # zoo needs ~120 bars before factors exist
    for t in range((look or 60) + PURGE + first, d.T):
        b = t - PURGE + 1  # ICs of days a..b-1, all labels complete before t
        a = b - look if look else first  # look=None: expanding window (stable weights)
        m = (cs[:, b] - cs[:, a]) / (b - a)
        sd = ((cs2[:, b] - cs2[:, a]) / (b - a) - m ** 2).clamp(min=1e-12).sqrt()
        icir = m / sd
        top = torch.topk(icir.abs(), k).indices
        z = torch.stack([mats[fac[i]][t].to(torch.float32) - 0.5 for i in top.tolist()])
        w = icir[top][:, None]
        score[t] = torch.nansum(z * w, 0) / w.abs().sum()
        score[t] = torch.where(d.tradable[t], score[t], torch.nan)
    return score


def dyn_long(d: E.Data, names: list[str], mats: list[torch.Tensor], k: int = 10, look: int | None = None,
             q: float = 0.1) -> torch.Tensor:
    """Long-only variant: factors are chosen and weighted by the 5-day relative return of their best decile
    (top decile, or bottom decile for a factor used with a negative sign), not by the whole-market IC."""
    y = d.label(H)
    fac = [i for i, n in enumerate(names) if not n.startswith("M_")]
    ok_y = torch.isfinite(y)
    yz = torch.where(ok_y, y, 0)

    def edge(mask):
        mask = mask & ok_y
        n = mask.sum(1)
        return torch.where(n >= 20, (yz * mask).sum(1) / n.clamp(min=1), 0.0)

    hi = torch.stack([edge(mats[i].to(torch.float32) >= 1 - q) for i in fac])  # [K, T]
    lo = torch.stack([edge(mats[i].to(torch.float32) <= q) for i in fac])
    series = torch.cat([hi, lo])  # rows K..2K-1 are the factors used with a negative sign
    cs = torch.cat([torch.zeros(len(series), 1), series.cumsum(1)], 1)
    cs2 = torch.cat([torch.zeros(len(series), 1), (series ** 2).cumsum(1)], 1)
    score = torch.full((d.T, d.N), torch.nan)
    first = 130
    for t in range((look or 60) + PURGE + first, d.T):
        b = t - PURGE + 1
        a = b - look if look else first
        m = (cs[:, b] - cs[:, a]) / (b - a)
        sd = ((cs2[:, b] - cs2[:, a]) / (b - a) - m ** 2).clamp(min=1e-12).sqrt()
        ir = m / sd
        top = torch.topk(ir, k).indices.tolist()
        acc = torch.zeros(d.N)
        wsum = 0.0
        for j in top:
            i, sign = (j, 1.0) if j < len(fac) else (j - len(fac), -1.0)
            w = max(float(ir[j]), 0.0)
            acc += w * sign * (torch.nan_to_num(mats[fac[i]][t].to(torch.float32), nan=0.5) - 0.5)
            wsum += w
        score[t] = torch.where(d.tradable[t], acc / max(wsum, 1e-9), torch.nan)
    return score


def lgbm(d: E.Data, names: list[str], mats: list[torch.Tensor], hl: int) -> torch.Tensor:
    y = d.label(H)
    ry = E.cs_rank(y)
    rng = np.random.default_rng(SEED)
    first = d.dates.index(min(x for x in d.dates if x.isoformat() >= START_TEST))
    score = torch.full((d.T, d.N), torch.nan)
    ok = d.tradable & torch.isfinite(ry)
    params = {"objective": "regression", "learning_rate": 0.05, "num_leaves": 63, "min_data_in_leaf": 400,
              "feature_fraction": 0.7, "bagging_fraction": 0.7, "bagging_freq": 1, "lambda_l2": 10.0,
              "verbose": -1, "num_threads": 8, "seed": SEED}
    last = d.T - 1
    for t in range(first, last + 1, REFIT):
        end = t - PURGE  # labels of rows <= end are complete before day t
        tr_rows = torch.nonzero(ok[130: end + 1])
        tr_rows[:, 0] += 130
        if len(tr_rows) > 900_000:
            tr_rows = tr_rows[torch.from_numpy(rng.choice(len(tr_rows), 900_000, replace=False))]
        ti, ni = tr_rows[:, 0], tr_rows[:, 1]
        X = torch.stack([m[ti, ni] for m in mats], 1).to(torch.float32).numpy()
        lab = ry[ti, ni].numpy()
        w = np.exp(-(end - ti.numpy()) / hl) if hl > 0 else None
        model = lgb.train(params, lgb.Dataset(X, lab, weight=w, feature_name=names, free_raw_data=True), num_boost_round=300)
        for u in range(t, min(t + REFIT, last + 1)):
            cols = torch.nonzero(d.tradable[u]).flatten()
            Xu = torch.stack([m[u, cols] for m in mats], 1).to(torch.float32).numpy()
            score[u, cols] = torch.from_numpy(model.predict(Xu, num_threads=8).astype(np.float32))
        print(f"  [lgbm hl={hl}] refit {d.dates[t]} rows {len(ti)}", flush=True)
        if t == first:
            imp = sorted(zip(model.feature_importance("gain"), names), reverse=True)[:15]
            print("  top features:", [n for _, n in imp], flush=True)
    return score


def main() -> None:
    d = E.Data()
    names, mats = build(d)
    models = os.environ.get("FUSION_MODELS", "dyn,lgbm").split(",")
    results = {}
    if "dyn" in models:
        for k in (10, 20, 40):
            s = dyn(d, names, mats, k=k)
            torch.save(s, E.LAB / f"fusion_dyn{k}{SFX}.pt")
            results[f"dyn{k}"] = report(d, s, f"dyn k={k}")
    if "static" in models:  # expanding-window IC weights
        for k in (20, 40):
            s = dyn(d, names, mats, k=k, look=None)
            torch.save(s, E.LAB / f"fusion_static{k}{SFX}.pt")
            results[f"static{k}"] = report(d, s, f"static k={k}")
    if "long" in models:  # long-side (best decile) weights, expanding and trailing 120 days
        for k, look in ((10, None), (20, None), (10, 120)):
            s = dyn_long(d, names, mats, k=k, look=look)
            tag = f"long{k}_{look or 'exp'}"
            torch.save(s, E.LAB / f"fusion_{tag}{SFX}.pt")
            results[tag] = report(d, s, tag)
    if "lgbm" in models:
        for hl in (int(x) for x in os.environ.get("FUSION_HL", "0,120").split(",")):
            s = lgbm(d, names, mats, hl)
            torch.save(s, E.LAB / f"fusion_lgbm_hl{hl}{SFX}.pt")
            results[f"lgbm_hl{hl}"] = report(d, s, f"lgbm hl={hl}")
    tag = "verify" if os.environ.get("VERIFY") == "1" else "select"
    json.dump(results, open(E.LAB / f"fusion_{tag}_{'-'.join(models)}{os.environ.get('FUSION_TAG', '')}.json", "w"), indent=1)


if __name__ == "__main__":
    sys.exit(main())
