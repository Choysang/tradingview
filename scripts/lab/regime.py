"""Market regimes at two scales (1 month, 3 months), the best strategies in each regime, and whether a
regime-analog choice works in advance (lab venv).

Per window: index trend and volatility, breadth, limit-up sentiment (count, premium of yesterday's
limit-ups, highest streak, broken rate), turnover heat, new highs minus lows, dispersion, co-movement,
style spreads (small vs big, momentum, low volatility, low turnover, attention, short-term reversal; each
a daily top-minus-bottom decile return) and Shenwan level-1 industry leadership (theme groups: tech,
pharma, finance, cycle, consumer, defence, power equipment; leader excess and concentration).

1. Descriptive: calendar months and calendar quarters are clustered (k-means, k by silhouette); each
   cluster is described by its features and leading industries; the strategy panel (strategy_panel.parquet:
   75 rules + factor deciles) and the main fusion score are averaged per cluster.
2. Predictive (walk-forward by month from 2025-01): the state of the trailing 20 and 60 trading days
   before month m is matched to the 5 most similar past months (states taken the same way, labels complete
   before m); the 5 strategies best in those analog months are held in m. Compared with all strategies,
   last month's top 5, all-history top 5, and a permutation null (random analog months, 500 draws).
Output .work/lab/regime_*.json/csv and printed tables.
"""
from __future__ import annotations

import json
import sys
from datetime import date

import numpy as np
import polars as pl
import torch

import fusion
import tengine as E
import zoo

# Theme groups by level-1 industry name: Shenwan names first, then Sina (新浪行业) names (whichever map is present).
THEMES = {"科技": ("电子", "计算机", "通信", "传媒", "电子器件", "电子信息", "传媒娱乐"), "医药": ("医药生物", "生物制药", "医疗器械"),
          "金融": ("银行", "非银金融", "金融行业"),
          "周期": ("有色金属", "煤炭", "钢铁", "基础化工", "石油石化", "煤炭行业", "钢铁行业", "化工行业", "石油行业", "化纤行业"),
          "消费": ("食品饮料", "家用电器", "美容护理", "商贸零售", "社会服务", "食品行业", "酿酒行业", "家电行业", "商业百货", "酒店旅游", "服装鞋类"),
          "军工": ("国防军工", "飞机制造", "船舶制造"), "新能源": ("电力设备", "输配电气", "发电设备"), "汽车": ("汽车", "汽车制造", "摩托车"),
          "地产链": ("房地产", "建筑材料", "建筑装饰", "建筑建材", "水泥行业", "玻璃行业")}
STYLES = {"小盘": ("SIZE", -1), "动量": ("ROC20", -1), "低波": ("STD20", -1), "低换手": ("TURN_MA20", -1),
          "高关注": ("BEH_ATTN5", 1), "短反转": ("ROC5", 1)}  # (zoo factor, sign so that +1 means the named style)
PURGE = 6


def industry_map() -> pl.DataFrame:
    """code -> level-1 industry and the date it joined (null = unknown, treated as always a member).
    Shenwan map when present (sw_industry.py), else Sina's 新浪行业 (sectors.py, today's membership)."""
    sw = E.LAB / "sw_industry.parquet"
    if sw.exists():
        return pl.read_parquet(sw).select("code", pl.col("sw1").alias("ind"), "joined")
    x = pl.read_parquet(E.LAB / "sectors.parquet").filter(pl.col("kind") == "ind").sort("code", "sector")
    return x.unique("code", keep="first").select("code", pl.col("sector").alias("ind"), pl.lit(None, pl.Utf8).alias("joined"))


def industry_returns(d: E.Data) -> tuple[list[str], torch.Tensor]:
    sw = industry_map()
    names = sorted(x for x in sw["ind"].unique().to_list() if x)
    col = {c: i for i, c in enumerate(d.codes)}
    ind = torch.full((d.N,), -1, dtype=torch.long)
    joined = torch.full((d.N,), 10**9, dtype=torch.long)
    di = {x.isoformat(): i for i, x in enumerate(d.dates)}
    for code, s1, j in zip(sw["code"], sw["ind"], sw["joined"]):
        if code in col and s1 in names:
            ind[col[code]] = names.index(s1)
            joined[col[code]] = next((di[x] for x in sorted(di) if x >= j), 0) if j else 0
    r = torch.where(d.tradable & (d.f["ret"].abs() < 0.35), d.f["ret"], torch.nan)
    t_idx = torch.arange(d.T)[:, None]
    out = torch.full((d.T, len(names)), torch.nan)
    for k in range(len(names)):
        m = (ind == k)[None, :] & (t_idx >= joined[None, :]) & torch.isfinite(r)
        out[:, k] = torch.where(m, r, 0).sum(1) / m.sum(1).clamp(min=1)
        out[m.sum(1) < 5, k] = torch.nan
    return names, out


def style_returns(d: E.Data) -> dict[str, torch.Tensor]:
    ev = E.Evaluator(d, cache_size=8)
    r = torch.where(d.tradable & (d.f["ret"].abs() < 0.35), d.f["ret"], torch.nan)
    nxt = E._shift(r, -1)  # return on day t+1 of a decile formed at the close of t
    out = {}
    for name, (fac, sign) in STYLES.items():
        rank = E.cs_rank(torch.where(d.tradable, ev(zoo.zoo()[fac]) * sign, torch.nan))
        hi, lo = (rank >= 0.9) & torch.isfinite(nxt), (rank <= 0.1) & torch.isfinite(nxt)
        spread = torch.where(hi, nxt, 0).sum(1) / hi.sum(1).clamp(min=1) - torch.where(lo, nxt, 0).sum(1) / lo.sum(1).clamp(min=1)
        out[name] = E._shift(spread[:, None], 1)[:, 0]  # realised on day t (formed at t-1)
    return out


def daily_frame(d: E.Data) -> tuple[pl.DataFrame, list[str]]:
    ms = {k: v[:, 0].numpy() for k, v in fusion.market_state(d).items()}
    idx = d.index_close.numpy()
    idx_ret = np.r_[np.nan, idx[1:] / idx[:-1] - 1]
    names, ind = industry_returns(d)
    mkt = E._nanmean_row(torch.where(d.tradable & (d.f["ret"].abs() < 0.35), d.f["ret"], torch.nan)).numpy()
    cols = {"date": d.dates, "idx_ret": idx_ret, "up": ms["M_UP"], "zt": ms["M_ZT"], "dt": ms["M_DT"], "broken": ms["M_BROKEN"],
            "premium": ms["M_PREMIUM"], "maxstreak": ms["M_MAXSTREAK"], "amt_rel": ms["M_AMT_REL"], "hl": ms["M_HL"],
            "disp": ms["M_DISP"], "comove": ms["M_COMOVE"]}
    cols |= {f"S_{k}": v.numpy() for k, v in style_returns(d).items()}
    ind_np = ind.numpy() - mkt[:, None]
    for theme, members in THEMES.items():
        ks = [names.index(m) for m in members if m in names]
        cols[f"T_{theme}"] = np.nanmean(ind_np[:, ks], axis=1)
    for k, n in enumerate(names):
        cols[f"I_{n}"] = ind_np[:, k]
    return pl.DataFrame(cols), names


def window_features(df: pl.DataFrame, a: int, b: int, names: list[str]) -> dict:
    """Features of rows a..b-1 (sums for returns, means for levels)."""
    w = df.slice(a, b - a)
    f = {"idx_ret": float(np.nansum(w["idx_ret"])), "idx_vol": float(np.nanstd(w["idx_ret"]) * np.sqrt(244))}
    for c in ("up", "zt", "dt", "broken", "premium", "maxstreak", "amt_rel", "hl", "disp", "comove"):
        f[c] = float(np.nanmean(w[c]))
    for c in w.columns:
        if c.startswith(("S_", "T_")):
            f[c] = float(np.nansum(w[c]))
    inds = np.array([np.nansum(w[f"I_{n}"]) for n in names])
    f["ind_conc"] = float(np.nanstd(inds))
    f["ind_top"] = float(np.nanmax(inds))
    f["_leaders"] = [names[i] for i in np.argsort(-inds)[:3]]
    f["_laggards"] = [names[i] for i in np.argsort(inds)[:2]]
    return f


FEATS = None


def matrix(rows: list[dict]) -> np.ndarray:
    keys = [k for k in rows[0] if not k.startswith("_")]
    global FEATS
    FEATS = keys
    return np.array([[r[k] for k in keys] for r in rows], dtype=float)


def kmeans(X: np.ndarray, k: int, seed: int = 0, restarts: int = 30) -> tuple[np.ndarray, float]:
    rng = np.random.default_rng(seed)
    best = (None, np.inf)
    for _ in range(restarts):
        c = X[rng.choice(len(X), k, replace=False)]
        for _ in range(100):
            lab = np.argmin(((X[:, None] - c[None]) ** 2).sum(-1), 1)
            new = np.array([X[lab == j].mean(0) if (lab == j).any() else c[j] for j in range(k)])
            if np.allclose(new, c):
                break
            c = new
        inertia = ((X - c[lab]) ** 2).sum()
        if inertia < best[1]:
            best = (lab, inertia)
    return best[0], best[1]


def silhouette(X: np.ndarray, lab: np.ndarray) -> float:
    D = np.sqrt(((X[:, None] - X[None]) ** 2).sum(-1))
    s = []
    for i in range(len(X)):
        same = lab == lab[i]
        if same.sum() <= 1:
            s.append(0.0)
            continue
        a = D[i, same].sum() / (same.sum() - 1)
        b = min(D[i, lab == j].mean() for j in set(lab) if j != lab[i])
        s.append((b - a) / max(a, b))
    return float(np.mean(s))


def describe(cent: dict, z: dict) -> str:
    parts = []
    parts.append(f"指数{cent['idx_ret'] * 100:+.1f}%")
    top = sorted(((k, v) for k, v in z.items() if k.startswith(("S_", "T_"))), key=lambda kv: -abs(kv[1]))[:3]
    parts += [f"{k[2:]}{'强' if v > 0 else '弱'}" for k, v in top]
    if z["zt"] > 0.7:
        parts.append("涨停多")
    if z["dt"] > 0.7:
        parts.append("跌停多")
    if z["idx_vol"] > 0.7:
        parts.append("高波动")
    return "，".join(parts)


def blocks(d: E.Data, months: int) -> list[tuple[str, int, int]]:
    out, cur = [], None
    for i, x in enumerate(d.dates):
        key = f"{x.year}-{((x.month - 1) // months) * months + 1:02d}"
        if key != cur:
            out.append([key, i, i + 1])
            cur = key
        else:
            out[-1][2] = i + 1
    return [tuple(b) for b in out if b[1] >= 140]  # skip warm-up (zoo needs ~120 bars)


def strategy_month(panel: pl.DataFrame, d: E.Data) -> tuple[list[str], np.ndarray]:
    names = panel["strategy"].unique(maintain_order=True).to_list()
    X = panel["X"].to_numpy().reshape(len(names), d.T)
    return names, X


def main() -> None:
    d = E.Data()
    df, ind_names = daily_frame(d)
    panel = pl.read_parquet(E.LAB / "strategy_panel.parquet")
    snames, X = strategy_month(panel, d)
    main_score = torch.load(E.LAB / "fusion_static40_mined.pt")
    y = d.label(5)
    m = torch.isfinite(main_score) & torch.isfinite(y)
    r = E.cs_rank(torch.where(m, main_score, torch.nan))
    top = m & (r >= 0.9)
    main_x = (torch.where(top, y, 0).sum(1) / top.sum(1).clamp(min=1)).numpy()
    main_x[top.sum(1).numpy() < 10] = np.nan
    result = {}
    for scale, months in (("1m", 1), ("3m", 3), ("6m", 6)):
        bl = blocks(d, months)
        rows = [window_features(df, a, b, ind_names) for _, a, b in bl]
        F = matrix(rows)
        Z = (F - F.mean(0)) / (F.std(0) + 1e-12)
        if months == 1:
            themes = [k for k in FEATS if k.startswith("T_")]
            print("\n===== theme excess return by month (% summed daily, equal-weight industry minus market):")
            print("  month   idx%  " + " ".join(f"{k[2:]:>5s}" for k in themes) + "  leaders")
            for (b, _, _), r in zip(bl, rows):
                print(f"  {b} {r['idx_ret'] * 100:+6.1f} " + " ".join(f"{r[k] * 100:+5.1f}" for k in themes) + "  " + "/".join(r["_leaders"]))
        if months == 6:  # too few half-years to cluster: each half-year is described on its own
            k_best, lab = len(bl), np.arange(len(bl))
            print(f"\n===== {scale} blocks: {len(bl)} (each its own group)")
        else:
            scores = {}
            for k in (3, 4, 5, 6):
                if k < len(bl) - 1:
                    lab, _ = kmeans(Z, k)
                    scores[k] = (silhouette(Z, lab), lab)
            k_best = max(scores, key=lambda k: scores[k][0])
            lab = scores[k_best][1]
            print(f"\n===== {scale} blocks: {len(bl)}, k={k_best} (silhouette {scores[k_best][0]:.2f}; " +
                  ", ".join(f"k={k}:{s:.2f}" for k, (s, _) in scores.items()) + ")")
        clusters = []
        for j in range(k_best):
            members = [i for i in range(len(bl)) if lab[i] == j]
            cent = {k: float(np.mean([rows[i][k] for i in members])) for k in FEATS}
            zc = {k: float(Z[members, FEATS.index(k)].mean()) for k in FEATS}
            days = [t for i in members for t in range(bl[i][1], bl[i][2])]
            strat = np.nanmean(X[:, days], axis=1)
            order = [s for s in np.argsort(-np.nan_to_num(strat, nan=-99)) if np.isfinite(strat[s])]
            trad = [s for s in order if snames[s].startswith("T:")]
            winmonths = {snames[s]: int(sum(np.nanmean(X[s, bl[i][1]:bl[i][2]]) > np.nanmedian(np.nanmean(X[:, bl[i][1]:bl[i][2]], axis=1)) for i in members)) for s in order[:8]}
            info = {"cluster": j, "desc": describe(cent, zc), "blocks": [bl[i][0] for i in members],
                    "leaders": [rows[i]["_leaders"] for i in members],
                    "top": [(snames[s], round(float(strat[s]), 2), winmonths[snames[s]]) for s in order[:8]],
                    "top_traditional": [(snames[s], round(float(strat[s]), 2)) for s in trad[:5]],
                    "main_fusion_top10": round(float(np.nanmean(main_x[days])), 3), "median_strategy": round(float(np.nanmedian(strat)), 3)}
            clusters.append(info)
            print(f"\n[{scale} cluster {j}] {info['desc']}")
            print("  blocks:", ", ".join(f"{b}({'/'.join(L)})" for b, L in zip(info["blocks"], info["leaders"])))
            print("  best (mean 5-day excess per cohort %, months above median / months):",
                  "; ".join(f"{n} {v:+.2f} ({w}/{len(members)})" for n, v, w in info["top"][:6]))
            print("  best traditional:", "; ".join(f"{n} {v:+.2f}" for n, v in info["top_traditional"]))
            print(f"  main fusion top decile {info['main_fusion_top10']:+.3f} | median strategy {info['median_strategy']:+.3f}")
        result[scale] = {"k": k_best, "clusters": clusters, "timeline": [(b[0], int(l), rows[i]["_leaders"]) for i, (b, l) in enumerate(zip(bl, lab))]}
    result["analog"] = analog_test(d, df, ind_names, snames, X)
    json.dump(result, open(E.LAB / "regime_result.json", "w", encoding="utf-8"), ensure_ascii=False, indent=1, default=float)


def analog_test(d: E.Data, df: pl.DataFrame, ind_names: list[str], snames: list[str], X: np.ndarray, K: int = 5, draws: int = 500) -> dict:
    bl = blocks(d, 1)
    start = next(i for i, b in enumerate(bl) if b[0] >= "2025-01")
    month_perf = np.array([np.nanmean(X[:, a:b], axis=1) for _, a, b in bl]).T  # [S, months]
    rng = np.random.default_rng(11)
    out = {}
    for look in (20, 60):
        states = [window_features(df, max(0, a - look), a, ind_names) for _, a, _ in bl]
        S = matrix(states)
        realized, base_all, base_last, base_hist, null = [], [], [], [], []
        months = []
        for m in range(start, len(bl)):
            a_m = bl[m][1]
            cand = [j for j in range(len(bl)) if bl[j][2] + PURGE <= a_m and j < m]
            if len(cand) < K + 3:
                continue
            mu, sd = S[cand].mean(0), S[cand].std(0) + 1e-12
            dist = (((S[cand] - mu) / sd - (S[m] - mu) / sd) ** 2).sum(1)
            near = [cand[i] for i in np.argsort(dist)[:K]]
            pick = np.argsort(-np.nan_to_num(np.nanmean(month_perf[:, near], axis=1), nan=-99))[:5]
            cur = month_perf[:, m]
            realized.append(np.nanmean(cur[pick]))
            base_all.append(np.nanmean(cur))
            last = cand[-1]
            base_last.append(np.nanmean(cur[np.argsort(-np.nan_to_num(month_perf[:, last], nan=-99))[:5]]))
            base_hist.append(np.nanmean(cur[np.argsort(-np.nan_to_num(np.nanmean(month_perf[:, cand], axis=1), nan=-99))[:5]]))
            rnd = []
            for _ in range(draws):
                rs = list(rng.choice(cand, K, replace=False))
                rnd.append(np.nanmean(cur[np.argsort(-np.nan_to_num(np.nanmean(month_perf[:, rs], axis=1), nan=-99))[:5]]))
            null.append(rnd)
            months.append(bl[m][0])
        null = np.array(null)  # [months, draws]
        actual = float(np.mean(realized))
        null_means = null.mean(0)
        p = float((null_means >= actual).mean())
        out[look] = {"months": months, "analog": realized, "all": base_all, "last_top5": base_last, "hist_top5": base_hist,
                     "mean": {"analog": actual, "all": float(np.mean(base_all)), "last_top5": float(np.mean(base_last)),
                              "hist_top5": float(np.mean(base_hist)), "null_mean": float(null_means.mean())}, "p_value": p}
        print(f"\n[analog look {look}d] months {months[0]}..{months[-1]} ({len(months)}): analog top5 {actual:+.3f} | "
              f"all {np.mean(base_all):+.3f} | last-month top5 {np.mean(base_last):+.3f} | all-history top5 {np.mean(base_hist):+.3f} | "
              f"random-analog null {null_means.mean():+.3f}, p = {p:.3f}")
        print("   per month analog − all:", " ".join(f"{m}:{a - b:+.2f}" for m, a, b in zip(months, realized, base_all)))
    return out


if __name__ == "__main__":
    sys.exit(main())
