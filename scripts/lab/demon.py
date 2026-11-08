"""妖股 / theme-speculation engine (lab venv): what makes a limit-up keep running, judged on real fills.

A-share speculative runs are driven by small news, herding and hot money more than by fundamentals, so
the event here is a limit-up close on day t. Features known at the close of t:
  board height (limit_streak), 一字板, turnover, float cap, the stock's return over the last 5/20 days,
  theme heat (limit-ups today among industry peers, and among the 20 most return-correlated stocks: a
  point-in-time stand-in for 炒作题材; today's Sina concept lists are look-ahead prone and only used in a
  side run), market sentiment
  (limit-up count z-score, premium of yesterday's limit-ups, highest streak, broken rate, sentiment phase),
  and 龙虎榜 seats (listed, smart-money buyers, known hot-money trios, institutional net buy).
Outcome: buy at the next open (skipped when it opens sealed at the limit: a 一字板 cannot be bought), sell
at the close of t+1 (r1), the close of t+2 (r2) or the open of t+4 (r3); 0.2% round-trip cost.
1. Event table: mean outcome and fill rate by board height x sentiment phase, and first boards by theme heat.
2. Walk-forward LightGBM (refit every 20 days, labels complete before the refit) ranking events by r2;
   the top 5 events a day are held; reported per period with the share that could be bought. A side run adds
   the concept heat to show how much a look-ahead feature would flatter the result.
3. Complement: daily correlation of the event picks with the main fusion's top decile (same next-open buy,
   close t+2 sell) and a 50/50 daily blend.
Output .work/lab/demon_*.json and printed tables. VERIFY rows only with env VERIFY=1.
"""
from __future__ import annotations

import json
import os
import sys

import lightgbm as lgb
import numpy as np
import polars as pl
import torch

import fusion
import tengine as E

COST = 0.2


def _group_heat(zt: torch.Tensor, member: torch.Tensor) -> torch.Tensor:
    """member [N, G] 0/1. Per stock and day: the most limit-ups among peers (self excluded) in any of its groups."""
    per = zt @ member  # [T, G] limit-ups per group
    heat = torch.full_like(zt, -1.0)
    for g in range(member.shape[1]):
        m = member[:, g] > 0
        if m.any():
            heat[:, m] = torch.maximum(heat[:, m], per[:, g:g + 1] - zt[:, m])
    return torch.where(heat < 0, torch.nan, heat)


def peer_heat(d: E.Data, zt: torch.Tensor, k: int = 20, look: int = 120, every: int = 20) -> torch.Tensor:
    """Point-in-time theme heat without any concept list: every 20 days each stock's 20 peers are the stocks whose
    daily returns over the trailing 120 days correlate most with its own; heat = limit-ups among those peers today.
    Peers are fixed from data up to the day before each refresh, so nothing later leaks in."""
    r = torch.where(d.tradable & (d.f["ret"].abs() < 0.35), d.f["ret"], torch.nan)
    heat = torch.full_like(zt, torch.nan)
    for s in range(look, d.T, every):
        w = r[s - look:s]
        ok = torch.isfinite(w).sum(0) >= look * 0.8
        if ok.sum() <= 4 * k:  # warm-up: too few stocks with a full window
            continue
        x =torch.nan_to_num(w - torch.nanmean(w, 0), nan=0.0)[:, ok]
        x = x / (x.norm(dim=0) + 1e-9)
        c = x.T @ x
        c.fill_diagonal_(-1)
        nb = torch.topk(c, k, dim=1).indices  # [n_ok, k] in ok-space
        cols = torch.nonzero(ok).flatten()
        heat[s:s + every][:, cols] = zt[s:s + every][:, cols[nb]].sum(-1)
    return heat


def themes(d: E.Data) -> dict[str, torch.Tensor]:
    """heat_ind: limit-ups today among the stock's industry peers (Sina 新浪行业 or Shenwan level 1).
    heat_peer: limit-ups among its 20 most correlated stocks (point in time, see peer_heat).
    heat_cpt: most limit-ups among peers in any of its Sina concepts. Concept lists are today's and a stock often
    joins a concept after it has run, so heat_cpt is look-ahead prone and kept out of the main model."""
    import regime
    col = {c: i for i, c in enumerate(d.codes)}
    zt = torch.nan_to_num(d.f["is_limit_up"], nan=0.0)
    out = {}
    ind = regime.industry_map()
    names = sorted(x for x in ind["ind"].unique().to_list() if x)
    m = torch.zeros((d.N, len(names)))
    for code, g in zip(ind["code"], ind["ind"]):
        if code in col and g in names:
            m[col[code], names.index(g)] = 1
    out["heat_ind"] = _group_heat(zt, m)
    out["heat_peer"] = peer_heat(d, zt)
    sec = E.LAB / "sectors.parquet"
    if sec.exists():
        cp = pl.read_parquet(sec).filter(pl.col("kind") == "concept")
        cn = sorted(cp["sector"].unique().to_list())
        m = torch.zeros((d.N, len(cn)))
        for code, g in zip(cp["code"], cp["sector"]):
            if code in col:
                m[col[code], cn.index(g)] = 1
        out["heat_cpt"] = _group_heat(zt, m)
    else:
        out["heat_cpt"] = torch.full_like(zt, torch.nan)
    return out


def build(d: E.Data) -> pl.DataFrame:
    f = d.f
    o, c = f["open"], f["close"]
    nxt_open = E._shift(o, -1)
    blocked = E._shift(f["open_at_limit"], -1) > 0
    r1 = (E._shift(c, -1) / nxt_open - 1) * 100 - COST
    r2 = (E._shift(c, -2) / nxt_open - 1) * 100 - COST
    r3 = (E._shift(o, -4) / nxt_open - 1) * 100 - COST
    mkt2 = E._nanmean_row(torch.where(d.tradable, (E._shift(c, -2) / nxt_open - 1) * 100, torch.nan))
    heat = themes(d)
    ms = {k: v[:, 0] for k, v in fusion.market_state(d).items()}
    ev = (f["is_limit_up"] > 0) & d.tradable
    ti, ni = torch.nonzero(ev, as_tuple=True)
    g = lambda x: x[ti, ni].numpy()  # noqa: E731
    ret5 = (c / E._shift(c, 5) - 1) * 100
    ret20 = (c / E._shift(c, 20) - 1) * 100
    cols = {
        "t": ti.numpy(), "date": [d.dates[i] for i in ti.tolist()], "code": [d.codes[j] for j in ni.tolist()],
        "streak": g(f["limit_streak"]), "one_word": g(f["one_word"]) if "one_word" in f else np.zeros(len(ti)),
        "turn": g(f["turn"]), "log_cap": np.log(np.clip(g(f["cap"]), 1e-3, None)), "ret5": g(ret5), "ret20": g(ret20),
        "limit20": g(f["limit_ratio"]), "heat_ind": g(heat["heat_ind"]), "heat_peer": g(heat["heat_peer"]), "heat_cpt": g(heat["heat_cpt"]),
        "lhb": g(f["lhb_on"]) if "lhb_on" in f else np.zeros(len(ti)), "smart": g(f["smart_buyers"]) if "smart_buyers" in f else np.zeros(len(ti)),
        "smart_score": g(f["smart_score"]) if "smart_score" in f else np.zeros(len(ti)),
        "trio": g(f["trip_buyers"]) if "trip_buyers" in f else np.zeros(len(ti)), "inst": g(f["inst_net"]) if "inst_net" in f else np.zeros(len(ti)),
        "blocked": g(blocked.to(torch.float32)), "r1": g(r1), "r2": g(r2), "r3": g(r3), "r2_ex": g(r2 - mkt2[:, None]),
    }
    for k in ("M_ZT_Z", "M_PREMIUM", "M_MAXSTREAK", "M_BROKEN", "M_PHASE", "M_IDX_R5", "M_IDX_R20", "M_UP"):
        cols[k] = ms[k][ti].numpy()
    df = pl.DataFrame(cols).with_columns(pl.col("date").cast(pl.Date))
    return df.with_columns(pl.col(pl.Float32, pl.Float64).fill_nan(None))  # polars means do not skip NaN


def period_masks(df: pl.DataFrame, d: E.Data) -> dict[str, pl.Expr]:
    per = fusion.periods(d)
    return {p: pl.col("t").is_between(r.start, r.stop - 1) for p, r in per.items()}


def event_table(df: pl.DataFrame, d: E.Data) -> dict:
    phases = {0: "冰点", 1: "修复", 2: "中性", 3: "高潮", 4: "退潮"}
    out = {}
    pm = period_masks(df, d)
    print("\n[1] limit-up events: next-open buy (sealed open = cannot buy), mean r2 net % (fill rate), by height x sentiment phase")
    for p, mask in pm.items():
        sub = df.filter(mask)
        rows = []
        for h in (1, 2, 3, 4, 5):
            hs = sub.filter((pl.col("streak") >= 5) if h == 5 else (pl.col("streak") == h))
            cells = []
            for ph in range(5):
                x = hs.filter(pl.col("M_PHASE") == ph)
                fill = x.filter(pl.col("blocked") == 0)
                cells.append((ph, x.height, fill.height, float(fill["r2"].mean()) if fill.height else float("nan")))
            fill_all = hs.filter(pl.col("blocked") == 0)
            rows.append({"height": h, "events": hs.height, "filled": fill_all.height, "r2": float(fill_all["r2"].mean()) if fill_all.height else None,
                         "r2_ex": float(fill_all["r2_ex"].mean()) if fill_all.height else None, "by_phase": cells})
            print(f"  {p:7s} {'5+' if h == 5 else h}板: {hs.height:5d} events, filled {fill_all.height / max(hs.height, 1):5.1%}, r2 {rows[-1]['r2'] if rows[-1]['r2'] is not None else float('nan'):+6.2f}% | "
                  + " ".join(f"{phases[ph]} {r:+5.2f}({nf})" for ph, n, nf, r in cells if n))
        heat_rows = []
        first = sub.filter((pl.col("streak") == 1) & (pl.col("blocked") == 0))
        for name, lo, hi in (("0", 0, 0), ("1-2", 1, 2), ("3-5", 3, 5), ("6+", 6, 10**6)):
            x = first.filter(pl.col("heat_peer").is_between(lo, hi))
            heat_rows.append((name, x.height, float(x["r2"].mean()) if x.height else float("nan"), float(x["r2_ex"].mean()) if x.height else float("nan")))
        print(f"  {p:7s} 首板 by correlated-peer limit-ups: " + " | ".join(f"{n}: {m:+.2f}% ex {e:+.2f} ({c})" for n, c, m, e in heat_rows))
        out[p] = {"by_height": rows, "first_board_by_peer_heat": heat_rows}
    return out


def main_daily(d: E.Data) -> np.ndarray:
    """Main fusion top decile, same trade as the events: buy the next open (not if sealed), sell the close of t+2."""
    score = torch.load(E.LAB / os.environ.get("DEMON_MAIN", "fusion_static40_safe.pt"))
    nxt = E._shift(d.f["open"], -1)
    r2 = (E._shift(d.f["close"], -2) / nxt - 1) * 100 - COST
    ok = torch.isfinite(score) & torch.isfinite(r2) & ~(E._shift(d.f["open_at_limit"], -1) > 0) & d.tradable
    rk = E.cs_rank(torch.where(ok, score, torch.nan))
    top = ok & (rk >= 0.9)
    x = torch.where(top, r2, 0).sum(1) / top.sum(1).clamp(min=1)
    x[top.sum(1) < 10] = torch.nan
    return x.numpy()


def complement(daily: pl.DataFrame, d: E.Data) -> dict:
    m = main_daily(d)
    daily = daily.with_columns(pl.Series("main", m[daily["t"].to_numpy()]))
    out = {}
    print("\n[3] complement with the main fusion (daily, same 2-day trade):")
    for p, r in fusion.periods(d).items():
        x = daily.filter(pl.col("t").is_between(r.start, r.stop - 1)).drop_nulls().filter(pl.col("main").is_not_nan())
        if x.height < 10:
            continue
        a, b = x["r2"].to_numpy(), x["main"].to_numpy()
        blend = 0.5 * a + 0.5 * b
        t = lambda v: float(v.mean() / (v.std(ddof=1) / np.sqrt(len(v))))  # noqa: E731
        out[p] = {"days": x.height, "corr": float(np.corrcoef(a, b)[0, 1]), "demon": float(a.mean()), "main": float(b.mean()),
                  "blend": float(blend.mean()), "t_demon": t(a), "t_main": t(b), "t_blend": t(blend)}
        v = out[p]
        print(f"  {p:7s} days {v['days']}: corr {v['corr']:+.2f} | demon {v['demon']:+.2f} (t {v['t_demon']:+.2f}) | "
              f"main {v['main']:+.2f} (t {v['t_main']:+.2f}) | blend {v['blend']:+.2f} (t {v['t_blend']:+.2f})")
    return out


FEATS = ["streak", "one_word", "turn", "log_cap", "ret5", "ret20", "limit20", "heat_ind", "heat_peer", "lhb", "smart", "smart_score", "trio", "inst",
         "M_ZT_Z", "M_PREMIUM", "M_MAXSTREAK", "M_BROKEN", "M_PHASE", "M_IDX_R5", "M_IDX_R20", "M_UP"]


def walk_forward(df: pl.DataFrame, d: E.Data, feats: list[str], tag: str, top: int = 5, refit: int = 20) -> tuple[dict, pl.DataFrame]:
    df = df.sort("t")
    first = d.dates.index(min(x for x in d.dates if x.isoformat() >= "2025-01-02"))
    params = {"objective": "regression", "learning_rate": 0.05, "num_leaves": 15, "min_data_in_leaf": 100, "feature_fraction": 0.8,
              "bagging_fraction": 0.8, "bagging_freq": 1, "lambda_l2": 5.0, "verbose": -1, "num_threads": 8, "seed": 3}
    preds = np.full(df.height, np.nan)
    t = df["t"].to_numpy()
    X = df.select(feats).to_numpy().astype(float)
    y = np.clip(df["r2"].to_numpy(), -20, 30)
    ok_y = np.isfinite(y) & (df["blocked"].to_numpy() == 0)
    for s in range(first, d.T, refit):
        tr = ok_y & (t <= s - 4)  # r2 ends at the close of t+2, complete before day s
        model = lgb.train(params, lgb.Dataset(X[tr], y[tr], feature_name=feats), num_boost_round=200)
        te = (t >= s) & (t < s + refit)
        if te.any():
            preds[te] = model.predict(X[te])
    imp = sorted(zip(model.feature_importance("gain"), feats), reverse=True)[:10]
    print(f"\n[2:{tag}] walk-forward event model; top features:", [n for _, n in imp])
    df = df.with_columns(pl.Series("pred", preds))
    picks = (df.filter(pl.col("pred").is_not_null()).with_columns(pl.col("pred").rank("ordinal", descending=True).over("t").alias("rk"))
             .filter(pl.col("rk") <= top))
    out = {}
    for p, mask in period_masks(df, d).items():
        allp = df.filter(mask & (pl.col("blocked") == 0))
        pk = picks.filter(mask)
        filled = pk.filter(pl.col("blocked") == 0)
        daily = filled.group_by("t").agg(pl.col("r2").mean()).sort("t")
        out[p] = {"days": daily.height, "picks": pk.height, "fill_rate": filled.height / max(pk.height, 1),
                  "mean_r2": float(filled["r2"].mean()) if filled.height else None, "mean_r2_ex": float(filled["r2_ex"].mean()) if filled.height else None,
                  "all_events_r2": float(allp["r2"].mean()) if allp.height else None,
                  "t_daily": float(daily["r2"].mean() / (daily["r2"].std() / np.sqrt(daily.height))) if daily.height > 5 else None}
        v = out[p]
        print(f"  {p:7s} top{top}/day: {v['picks']} picks, filled {v['fill_rate']:.0%}, r2 {v['mean_r2'] if v['mean_r2'] is not None else float('nan'):+.2f}% "
              f"(ex {v['mean_r2_ex'] if v['mean_r2_ex'] is not None else float('nan'):+.2f}, daily t {v['t_daily'] if v['t_daily'] is not None else float('nan'):+.2f}) "
              f"| all filled events {v['all_events_r2'] if v['all_events_r2'] is not None else float('nan'):+.2f}%")
    df.select("date", "code", "pred", "r2", "blocked").write_parquet(E.LAB / f"demon_preds_{tag}.parquet")
    daily = picks.filter(pl.col("blocked") == 0).group_by("t").agg(pl.col("r2").mean()).sort("t")
    return out, daily


def main() -> None:
    d = E.Data()
    df = build(d)
    print(f"[demon] {df.height} limit-up events; blocked next open {df['blocked'].mean():.1%}; LHB listed {df['lhb'].mean():.1%}")
    res = {"events": event_table(df, d)}
    res["model"], daily = walk_forward(df, d, FEATS, "pit")
    res["model_with_concepts"], _ = walk_forward(df, d, FEATS + ["heat_cpt"], "concept_lookahead")
    res["complement"] = complement(daily, d)
    tag = "_verify" if os.environ.get("VERIFY") == "1" else ""
    json.dump(res, open(E.LAB / f"demon{tag}.json", "w", encoding="utf-8"), ensure_ascii=False, indent=1, default=float)


if __name__ == "__main__":
    sys.exit(main())
