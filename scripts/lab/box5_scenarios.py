"""E059: Box5 (5-minute box / breakout) on the recorded week 2026-11-02..06 (backend venv, research only).

    PYTHONPATH=. backend/.venv/Scripts/python.exe scripts/lab/box5_scenarios.py

A  convertible bonds, T+0, long breakouts only, cost 0.04% round trip
B  main-board stocks with a T+1-sellable base: up-break = 正T (long), down-break = 倒T (short), cost 0.15%
C  S_Box5m: sell a paper-replay limit-up holding at the first downward box break, next to the other sell rules

Data: `.work/intraday/<day>/minute_synth.parquet` (bars synthesized from snapshots; most bars rest on ONE snapshot, so
intrabar high/low is mostly unobserved) -> close-confirmed breakouts, entry at the next bar's open, see backend/app/domain/box5.py.
Outputs: `.work/lab/main/box5/{report.md, results.json, trades_A.parquet, trades_B.parquet, c_sells.parquet}`.
"""
from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from backend.app.domain import box5 as B5  # noqa: E402

DAYS = ["2026-11-02", "2026-11-03", "2026-11-04", "2026-11-05", "2026-11-06"]
IN = ROOT / ".work/intraday"
PAPER = ROOT / ".work/lab/main/paper_server"
OUT = ROOT / ".work/lab/main/box5"
N = B5.DAY_BARS
COST_BOND, COST_STOCK = 0.04, 0.15  # round trip, percent
TICK_BOND, TICK_STOCK = 0.001, 0.01
CONVERGED = {"min_redraw": 1, "min_r_frac": 0.004}
WEIGHTS = (0.2, 0.1)
ROUNDS = 100
SEED = 59


def label_to_idx(minute: pd.Series) -> np.ndarray:
    """0931..1130 -> 0..119, 1301..1500 -> 120..239, anything else (0930, 1300) -> -1."""
    mins = minute.str.slice(0, 2).astype(int) * 60 + minute.str.slice(2, 4).astype(int)
    return np.where((mins >= 571) & (mins <= 690), mins - 571, np.where((mins >= 781) & (mins <= 900), mins - 781 + 120, -1))


def idx_label(i: int) -> str:
    m = 571 + i if i < 120 else 781 + (i - 120)
    return f"{m // 60:02d}{m % 60:02d}"


def lim(prev: float, f: float) -> float:
    return float(Decimal(str(prev * f)).quantize(Decimal("0.01")))


@dataclass
class DayData:
    day: str
    codes: np.ndarray
    O: np.ndarray
    H: np.ndarray
    L: np.ndarray
    C: np.ndarray
    S: np.ndarray  # n_snaps (0 on forward-filled bars)
    fv: np.ndarray  # first index with a real bar
    filled: int  # forward-filled bars after the first real one
    real: int
    blocked: np.ndarray  # sealed at the +-10% limit (main board only; all False otherwise)
    names: dict
    prev_close: dict


def official_prev(day: str, prior: DayData | None, daily: pd.DataFrame, quotes0: pd.DataFrame) -> dict[str, tuple[float, float]]:
    """code -> (previous close, tolerance). 11-02: quotes.prev_close. Later days: the previous session's daily-bar close; where
    that bar is missing (11-05 has only 4978 of 5570 rows) the previous day's last minute bar (14:5x price, not the closing
    auction price) with a wider tolerance."""
    if prior is None:
        return {c: (float(v), 0.001) for c, v in quotes0["prev_close"].items() if v > 0}
    p = daily[daily["date"] == DAYS[DAYS.index(day) - 1]].set_index("code")["close"]
    out = {c: (float(v), 0.001) for c, v in p.items()}
    for ci, c in enumerate(prior.codes):
        if c not in out and not np.isnan(prior.C[ci, N - 1]):
            out[c] = (float(prior.C[ci, N - 1]), 0.02)
    return out


def load_day(day: str, prev: dict[str, tuple[float, float]], names: dict) -> DayData:
    m = pd.read_parquet(IN / day / "minute_synth.parquet", columns=["code", "minute", "open", "high", "low", "price", "n_snaps"])
    m["i"] = label_to_idx(m["minute"])
    m = m[m["i"] >= 0]
    codes, inv = np.unique(m["code"].to_numpy(), return_inverse=True)
    shape = (len(codes), N)
    arrs = {k: np.full(shape, np.nan) for k in ("O", "H", "L", "C")}
    S = np.zeros(shape, dtype=np.int32)
    i = m["i"].to_numpy()
    for k, col in (("O", "open"), ("H", "high"), ("L", "low"), ("C", "price")):
        arrs[k][inv, i] = m[col].to_numpy()
    S[inv, i] = m["n_snaps"].to_numpy()
    real = ~np.isnan(arrs["C"])
    fv = np.where(real.any(1), real.argmax(1), N)
    close = pd.DataFrame(arrs["C"]).ffill(axis=1).to_numpy()
    after_first = np.arange(N)[None, :] >= fv[:, None]
    gap = after_first & ~real
    for k in ("O", "H", "L"):  # a forward-filled bar is flat at the previous close
        arrs[k] = np.where(gap, close, arrs[k])
    arrs["C"] = np.where(after_first, close, np.nan)
    blocked = np.zeros(shape, dtype=bool)
    for ci, code in enumerate(codes):
        if code[:2] in ("00", "60") and code in prev:
            pc, tol = prev[code]
            up, dn = lim(pc, 1.1), lim(pc, 0.9)
            c = arrs["C"][ci]
            blocked[ci] = (c >= up - tol) | (c <= dn + tol)
    return DayData(day, codes, arrs["O"], arrs["H"], arrs["L"], arrs["C"], S, fv, int(gap.sum()), int(real.sum()), blocked, names, prev)


def bars_of(d: DayData, ci: int, with_block: bool) -> list[B5.Bar]:
    s = int(d.fv[ci])
    o, h, l, c, n = (a[ci, s:].tolist() for a in (d.O, d.H, d.L, d.C, d.S))
    bl = d.blocked[ci, s:].tolist() if with_block else [False] * (N - s)
    return [B5.Bar(s + k, o[k], h[k], l[k], c[k], n[k], bl[k]) for k in range(N - s)]


def trade_rows(day: str, code: str, trades: list[B5.Trade], cost: float, version: str) -> list[dict]:
    rows = []
    for t in trades:
        net = t.gross_pct - cost
        row = {"day": day, "code": code, "version": version, "side": t.side, "redraw": t.redraw_count, "box_high": t.box_high,
               "box_low": t.box_low, "signal_idx": t.signal_idx, "entry_idx": t.entry_idx, "entry_label": idx_label(t.entry_idx),
               "entry": t.entry, "sl": t.sl, "tp": t.tp, "r_frac_pct": t.r_frac * 100, "exit_idx": t.exit_idx,
               "exit_label": idx_label(t.exit_idx), "exit": t.exit_price, "reason": t.reason, "intrabar": t.intrabar_fill,
               "hold": t.hold_bars, "gross_pct": t.gross_pct, "net_pct": net}
        for w in WEIGHTS:
            wt = B5.pos_weight(t.entry, t.r, w)
            row[f"w{int(w * 100):02d}"] = wt
            row[f"pnl{int(w * 100):02d}"] = wt * net
        rows.append(row)
    return rows


def run_universe(d: DayData, sel: np.ndarray, tick: float, cost: float, long_only: bool, with_block: bool) -> list[dict]:
    rows = []
    for ci in np.flatnonzero(sel):
        bars = bars_of(d, ci, with_block)
        if len(bars) < 20:
            continue
        for version, kw in (("1_original", {}), ("2_converged", CONVERGED)):
            ts = B5.run_day(bars, tick, allow_short=not long_only, **kw)
            rows += trade_rows(d.day, d.codes[ci], ts, cost, version)
    return rows


def touch_sidenote(d: DayData, sel: np.ndarray) -> dict:
    """Bars with >= 3 snapshots in WATCH whose high/low pierced the box while the close did not (trade-free machine)."""
    touch = confirmed = 0
    for ci in np.flatnonzero(sel):
        w = B5.BoxWatcher()
        for b in bars_of(d, ci, False):
            mode, hi, lo = w.mode, w.box_high, w.box_low
            if mode == B5.WATCH and b.n_snaps >= B5.INTRABAR_SNAPS:
                if b.close > hi or b.close < lo:
                    confirmed += 1
                elif b.high > hi or b.low < lo:
                    touch += 1
            w.step(b)
    return {"touch_only_bars": touch, "close_confirmed_multisnap_bars": confirmed}


# ---------------------------------------------------------------- statistics
def stats(df: pd.DataFrame, ndays: int = len(DAYS)) -> dict:
    if df.empty:
        return {"trades": 0}
    win, loss = df.loc[df.net_pct > 0, "net_pct"], df.loc[df.net_pct <= 0, "net_pct"]
    out = {"trades": int(len(df)), "trade_days": int(df.day.nunique()), "win_rate": float((df.net_pct > 0).mean()),
           "mean_gross_pct": float(df.gross_pct.mean()), "mean_net_pct": float(df.net_pct.mean()),
           "median_net_pct": float(df.net_pct.median()),
           "payoff_ratio": float(win.mean() / abs(loss.mean())) if len(win) and len(loss) and loss.mean() != 0 else None,
           "mean_hold_bars": float(df.hold.mean()), "mean_r_pct": float(df.r_frac_pct.mean()),
           "exit_reasons": df.reason.value_counts().to_dict(), "intrabar_fills": int(df.intrabar.sum()),
           "redraw_dist": {"0": int((df.redraw == 0).sum()), "1": int((df.redraw == 1).sum()), "2": int((df.redraw == 2).sum()),
                           "3+": int((df.redraw >= 3).sum())}}
    for w in WEIGHTS:
        k = f"pnl{int(w * 100):02d}"
        per_day = df.groupby("day")[k].sum()
        out[f"pnl_per_day_w{int(w * 100):02d}"] = {d: float(per_day.get(d, 0.0)) for d in DAYS}
        out[f"pnl_total_w{int(w * 100):02d}"] = float(per_day.sum())
        out[f"pnl_mean_day_w{int(w * 100):02d}"] = float(per_day.sum() / ndays)
        out[f"mean_weight_w{int(w * 100):02d}"] = float(df[f"w{int(w * 100):02d}"].mean())
        out[f"pnl_mean_trade_w{int(w * 100):02d}"] = float(df[k].mean())
    out["per_day"] = {d: {"trades": int(len(g)), "mean_net_pct": float(g.net_pct.mean())} for d, g in df.groupby("day")}
    return out


def baseline(days: dict[str, DayData], trades: pd.DataFrame, universe: dict[str, np.ndarray], cost: float, with_block: bool,
             seed: int) -> dict:
    """Same-day random (instrument, entry minute) with the strategy trade's direction and holding length; ROUNDS draws per trade."""
    if trades.empty:
        return {}
    rng = np.random.default_rng(seed)
    tot = np.zeros(ROUNDS)
    cnt = np.zeros(ROUNDS)
    for day, g in trades.groupby("day"):
        d = days[day]
        U = np.flatnonzero(universe[day])
        side, h = g.side.to_numpy()[None, :], g.hold.to_numpy()[None, :]
        shape = (ROUNDS, len(g))
        ci = U[rng.integers(0, len(U), shape)]
        j = np.zeros(shape, dtype=int)
        bad = np.ones(shape, dtype=bool)
        for _ in range(60):
            if not bad.any():
                break
            lo = d.fv[ci]
            hi = N - h
            ok_range = lo <= hi
            jj = lo + np.floor(rng.random(shape) * np.maximum(hi - lo + 1, 1)).astype(int)
            jj = np.clip(jj, 0, N - 1)
            ok = ok_range & (~d.blocked[ci, jj] if with_block else True)
            new_ci = U[rng.integers(0, len(U), shape)]
            j = np.where(bad & ok, jj, j)
            ci = np.where(bad & ~ok, new_ci, ci)
            bad = bad & ~ok
        e = np.clip(j + h - 1, 0, N - 1)
        gross = side * (d.C[ci, e] / d.O[ci, j] - 1) * 100
        ret = np.where(bad, np.nan, gross - cost)
        tot += np.nansum(ret, axis=1)
        cnt += np.sum(~np.isnan(ret), axis=1)
    means = tot / cnt
    return {"rounds": ROUNDS, "mean_net_pct": float(means.mean()), "p": float((means >= trades.net_pct.mean()).mean()),
            "round_means_min": float(means.min()), "round_means_max": float(means.max())}


def scenario_block(df: pd.DataFrame, days, universe, cost, with_block, sides: dict[str, int | None]) -> dict:
    out = {}
    for version, gv in df.groupby("version"):
        out[version] = {}
        for sname, sv in sides.items():
            g = gv if sv is None else gv[gv.side == sv]
            st = stats(g)
            if st["trades"]:
                st["random_baseline"] = baseline(days, g, universe, cost, with_block, SEED)
            out[version][sname] = st
    return out


# ---------------------------------------------------------------- scenario C
def scenario_c(days: dict[str, DayData]) -> tuple[pd.DataFrame, dict]:
    bars = pd.read_parquet(PAPER / "bars_week.parquet")
    bars["date"] = bars["date"].astype(str)
    opens = bars.set_index(["code", "date"])["open"].to_dict()
    nxt = dict(zip(DAYS, DAYS[1:] + ["2026-11-09"]))
    frames = []
    for day in DAYS:
        s = pd.read_parquet(PAPER / day / "sells.parquet")
        s = s[s.status == "FILLED"]
        s["day"] = day
        d = days[day]
        pos = {c: i for i, c in enumerate(d.codes)}
        hold = s[s.rule == "HOLD_close"].set_index("code")
        rows = []
        for code, h in hold.iterrows():
            r = {"day": day, "code": code, "rule": "S_Box5m", "cost": h["cost"], "close": h["price"], "status": "FILLED"}
            if code not in pos:
                r["status"] = "NO_MINUTE_DATA"
                rows.append(r | {"price": np.nan, "triggered": False, "time": None})
                continue
            fs = B5.first_signal(bars_of(d, pos[code], False), -1)
            if fs is None:
                rows.append(r | {"price": h["price"], "triggered": False, "time": None})
            else:
                rows.append(r | {"price": fs[2], "triggered": True, "time": idx_label(fs[1])})
        frames.append(pd.concat([s[["day", "code", "rule", "price", "cost"]].assign(triggered=True),
                                 pd.DataFrame(rows)[["day", "code", "rule", "price", "cost", "triggered", "time"]]
                                 [lambda x: x.price.notna()]]))
    a = pd.concat(frames, ignore_index=True)
    a["nopen"] = [opens.get((c, nxt[d_])) for c, d_ in zip(a.code, a.day)]
    a["vs_next_open"] = (a.price / a.nopen - 1) * 100
    close = a[a.rule == "HOLD_close"].set_index(["day", "code"])["price"]
    a["close"] = [close.get((d_, c)) for d_, c in zip(a.day, a.code)]
    a["vs_close"] = (a.price / a.close - 1) * 100
    a["ret"] = (a.price / a.cost - 1) * 100
    valid = a[a.rule == "HOLD_close"].dropna(subset=["nopen"])[["day", "code"]]
    vkeys = set(zip(valid.day, valid.code))
    res = {"holdings_total": int((a.rule == "HOLD_close").sum()), "holdings_valid_next_open": len(vkeys),
           "dropped_no_next_open": {d_: int(((a.rule == "HOLD_close") & (a.day == d_) & a.nopen.isna()).sum()) for d_ in DAYS}}
    hold_px = a[a.rule == "HOLD_close"].set_index(["day", "code"])["price"]
    rules = ["S_Box5m", "S3_vwap", "Sbreak_tape", "S0_open", "HOLD_close", "S1_decay", "M3_exit", "POLICY_v2"]
    table = {}
    for rule in rules:
        g = a[a.rule == rule]
        g = g[[(d_, c) in vkeys for d_, c in zip(g.day, g.code)]]
        if rule == "S_Box5m":
            trig = g[g.triggered]
        else:
            trig = g
        full = []  # full book: the rule's price where it fired, the close elsewhere
        px = dict(zip(zip(g.day, g.code), g.price))
        for (d_, c) in vkeys:
            p = px.get((d_, c), hold_px[(d_, c)])
            full.append((p / opens[(c, nxt[d_])] - 1) * 100)
            # vs_close / ret for the full book
        fb = pd.DataFrame([{"day": d_, "code": c, "price": px.get((d_, c), hold_px[(d_, c)])} for (d_, c) in vkeys])
        fb["vs_next_open"] = full
        fb["vs_close"] = [(p / hold_px[(d_, c)] - 1) * 100 for d_, c, p in zip(fb.day, fb.code, fb.price)]
        table[rule] = {"n_fired": int(len(trig)), "fired_mean_vs_next_open": float(trig.vs_next_open.mean()) if len(trig) else None,
                       "fired_median_vs_next_open": float(trig.vs_next_open.median()) if len(trig) else None,
                       "fired_share_beats_next_open": float((trig.vs_next_open > 0).mean()) if len(trig) else None,
                       "fired_mean_vs_close": float(trig.vs_close.mean()) if len(trig) else None,
                       "full_n": int(len(fb)), "full_mean_vs_next_open": float(fb.vs_next_open.mean()),
                       "full_share_beats_next_open": float((fb.vs_next_open > 0).mean()),
                       "full_mean_vs_close": float(fb.vs_close.mean())}
        if rule == "S_Box5m":
            table[rule]["fired_median_time"] = str(sorted(trig["time"].dropna())[len(trig) // 2]) if len(trig) else None
            table[rule]["per_day_full_mean_vs_next_open"] = {d_: float(g_.vs_next_open.mean()) for d_, g_ in fb.groupby("day")}
        if rule == "HOLD_close":
            table[rule]["per_day_full_mean_vs_next_open"] = {d_: float(g_.vs_next_open.mean()) for d_, g_ in fb.groupby("day")}
    res["table"] = table
    # reproduce week_sells.parquet (the paper_replay metric) from the per-day sells and the daily bars
    w = pd.read_parquet(PAPER / "week_sells.parquet").groupby("rule")["vs_next_open"].agg(["count", "mean"])
    mine = a[a.rule != "S_Box5m"].groupby("rule")["vs_next_open"].agg(["count", "mean"])
    common = w.index.intersection(mine.index)
    res["reproduces_week_sells"] = bool(np.allclose(w.loc[common, "mean"], mine.loc[common, "mean"]) and (w.loc[common, "count"] == mine.loc[common, "count"]).all())
    return a, res


# ---------------------------------------------------------------- report
def f(x, nd=3, sign=False) -> str:
    if x is None:
        return "-"
    return f"{x:+.{nd}f}" if sign else f"{x:.{nd}f}"


def main_table(block: dict, names: dict[str, str]) -> list[str]:
    lines = ["| 版本 | 方向 | 笔数 | 交易日 | 胜率 | 单笔净均值% | 盈亏比 | 加权盈亏合计 w0.2 | 加权盈亏合计 w0.1 | 单笔加权均值 w0.2 | 随机基线均值% | p | redraw 0/1/2/3+ |",
             "|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for v, sides in block.items():
        for sname, st in sides.items():
            if not st["trades"]:
                lines.append(f"| {v} | {sname} | 0 |  |  |  |  |  |  |  |  |  |  |")
                continue
            bl = st.get("random_baseline", {})
            rd = st["redraw_dist"]
            lines.append(f"| {v} | {sname} | {st['trades']} | {st['trade_days']} | {st['win_rate']:.1%} | {f(st['mean_net_pct'], 3, True)} | "
                         f"{f(st['payoff_ratio'], 2)} | {f(st['pnl_total_w20'], 1, True)} | {f(st['pnl_total_w10'], 1, True)} | "
                         f"{f(st['pnl_mean_trade_w20'], 4, True)} | {f(bl.get('mean_net_pct'), 3, True)} | {f(bl.get('p'), 2)} | "
                         f"{rd['0']}/{rd['1']}/{rd['2']}/{rd['3+']} |")
    return lines


def day_table(block: dict) -> list[str]:
    lines = ["| 版本 | 方向 | " + " | ".join(f"{d[5:]} 笔数 / 单笔净% / 加权合计w0.2" for d in DAYS) + " |", "|---|---|" + "---|" * len(DAYS)]
    for v, sides in block.items():
        for sname, st in sides.items():
            if not st["trades"]:
                continue
            cells = []
            for d in DAYS:
                pd_ = st["per_day"].get(d)
                cells.append(f"{pd_['trades']} / {f(pd_['mean_net_pct'], 3, True)} / {f(st['pnl_per_day_w20'][d], 1, True)}" if pd_ else "0")
            lines.append(f"| {v} | {sname} | " + " | ".join(cells) + " |")
    return lines


def write_report(res: dict) -> None:
    A, Bm, C = res["A_convertible_bonds"], res["B_main_board"], res["C_sell_rule"]
    L = ["# E059 Box5：5 分钟箱体突破状态机（2026-11-02..06，研究，未触服务器）", "",
         "## 数据限制（先读）", "",
         "- 1 分钟 K 线由快照合成，**多数 K 线只落在 1 个快照上**（主板 60 秒整轮询，81-93% 的 bar 满足 high==low），bar 内高低价大多没有观察到。"
         "因此：(1) 突破一律用**收盘确认**；(2) 突破 K 线的止损极值取 `min(low, 上一根收盘)` / `max(high, 上一根收盘)`，SL 在其外 1 tick（股票 0.01，转债 0.001）；"
         "(3) 入场在**下一根 bar 的开盘价**（突破收盘才知道信号），信号落在 1130 的在 1301 开盘成交，落在 1500 的丢弃。",
         "- 出场逐 bar 检查（含入场那根）：收盘越过 SL/TP 则按**收盘价**成交；`n_snaps>=3` 的 bar 高低价可观察，触及则按档位成交；SL 先于 TP；1500 bar 收盘强平。"
         "注意 minute_synth 的 1500 bar 是 14:5x 的最后连续竞价价，不是收盘集合竞价价（与日线收盘有 0.01 级差异，约 17% 的股票）。",
         "- 5 天样本很小，且是同一周同一行情；下面所有 p 值（随机基线 100 轮）分辨率只有 0.01，不构成显著性结论。",
         f"- 盘中触价变体：只能在 `n_snaps>=3` 的 bar（约 15%）上评估。无交易的箱体机在 WATCH 期内，这类 bar 上\"最高/最低刺破箱体但收盘未确认\"的"
         f"样本：转债 {res['intrabar_touch_sidenote']['A']['touch_only_bars']} 根（同期收盘确认的多快照 bar {res['intrabar_touch_sidenote']['A']['close_confirmed_multisnap_bars']} 根），"
         f"主板 {res['intrabar_touch_sidenote']['B']['touch_only_bars']} 根（{res['intrabar_touch_sidenote']['B']['close_confirmed_multisnap_bars']} 根）。"
         "但这些 bar 的 low 与 high 谁先发生未知、入场价只能取箱体边，本次**未回测**该变体，视为不可测。",
         "- 加权盈亏合计 = 把每个品种当作独立账户、逐笔 `pos_weight × 单笔净收益%` 相加（单位：账户%），转债 165 只、主板 3051 只同时开仓并不现实（无容量/资金约束），只用来比较版本，不是可实现收益。",
         f"- 前复权/新股：主板剔除名称含 ST（名称只有 11-02 的 quotes，整周沿用）、周内新上市 {len(res['meta']['new_listings_excluded'])} 只（{', '.join(res['meta']['new_listings_excluded'])}）。"
         "封板 bar（收盘在 昨收×1.1/0.9 四舍五入价）不产生信号、也不能作入场 bar；11-03..06 没有 quotes，昨收取日线上一交易日收盘（11-05 日线只有 4978 行，缺的代码用前日最后一根分钟价、容差 0.02）。",
         "- 随机基线：同一天、同方向、持仓长度（bar 数）取自对应策略成交，持仓长度本身带有止损/止盈的结果依赖；随机侧只做 开盘入场→持仓末根收盘 的收益、不设止损止盈，成本同策略。",
         "- 内部缺失的分钟 bar 用前收向前填平（n_snaps=0），各日填充数见 results.json meta.data。", "",
         "## 场景 A：可转债（T+0，只做多，成本 0.04%）", ""]
    L += main_table(A, {}) + ["", "逐日：", ""] + day_table(A)
    L += ["", "## 场景 B：主板股票（有 T+1 底仓；上破=正T=做多，下破=倒T=做空；成本 0.15%）", ""]
    L += main_table(Bm, {}) + ["", "逐日：", ""] + day_table(Bm)
    t = C["table"]
    L += ["", "## 场景 C：S_Box5m 卖出规则（纸面回放 5 天涨停持仓）", "",
          f"持仓 {C['holdings_total']} 笔（11-02..06 前一交易日主板涨停），次日开盘价可得 {C['holdings_valid_next_open']} 笔；11-06 的持仓没有 11-09 数据，整批剔除"
          f"（各日剔除：{C['dropped_no_next_open']}）。指标与 paper_replay 周表一致：`vs_next_open = (卖价/次日开盘 - 1)×100`，正数 = 今天卖比次日开盘卖更好；"
          f"本脚本由逐日 sells.parquet 重算，结果与 week_sells.parquet 逐规则一致：{C['reproduces_week_sells']}。"
          "S_Box5m = 当日首次**向下**箱体突破（收盘确认，下一根开盘卖），无则收盘价。"
          "「触发口径」只看该规则触发的持仓（paper_replay 周表的口径，各规则 n 不同）；「全账本口径」未触发的持仓按收盘价卖出，所有规则 n 相同、可直接比较。", "",
          "| 规则 | 触发 n | 触发口径 vs次日开盘均值% | 中位% | 胜出占比 | 触发口径 vs收盘均值% | 全账本 n | 全账本 vs次日开盘均值% | 全账本胜出占比 | 全账本 vs收盘均值% |",
          "|---|---|---|---|---|---|---|---|---|---|"]
    for rule, r in t.items():
        L.append(f"| {rule} | {r['n_fired']} | {f(r['fired_mean_vs_next_open'], 2, True)} | {f(r['fired_median_vs_next_open'], 2, True)} | "
                 f"{r['fired_share_beats_next_open']:.0%} | {f(r['fired_mean_vs_close'], 2, True)} | {r['full_n']} | {f(r['full_mean_vs_next_open'], 2, True)} | "
                 f"{r['full_share_beats_next_open']:.0%} | {f(r['full_mean_vs_close'], 2, True)} |")
    sb = t["S_Box5m"]
    L += ["", f"- S_Box5m 触发时间中位数 {sb['fired_median_time']}（最早 0937：箱体 0931-0935，观察从 0936 起）；全账本逐日 vs次日开盘均值%：{ {k: round(v, 2) for k, v in sb['per_day_full_mean_vs_next_open'].items()} }"
          f"，同口径 HOLD_close：{ {k: round(v, 2) for k, v in t['HOLD_close']['per_day_full_mean_vs_next_open'].items()} }（11-06 因无次日开盘不在内）。",
          "- 封死涨停的持仓箱体是平的，离开涨停价的任何一根收盘都算下破，所以 S_Box5m 对封板股几乎是「首次开板收盘确认即卖」。",
          "- 其余规则的 n 与 paper_replay 周表一致（Sbreak_tape = M3-Tape）；5 天、同一批行情，不能据此下结论。", ""]
    (OUT / "report.md").write_text(chr(10).join(L) + chr(10), encoding="utf-8")


# ---------------------------------------------------------------- main
def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    daily = pd.read_parquet(PAPER / "bars_week.parquet", columns=["code", "date", "close"])
    daily["date"] = daily["date"].astype(str)
    q0 = pd.read_parquet(IN / DAYS[0] / "quotes.parquet", columns=["code", "name", "prev_close", "kind"])
    q0 = q0[q0["kind"] == "full"].drop_duplicates("code").set_index("code")
    names = q0["name"].to_dict()  # only 11-02 has quotes.parquet: ST status is taken from that day for the whole week
    days: dict[str, DayData] = {}
    for d in DAYS:
        days[d] = load_day(d, official_prev(d, days[DAYS[DAYS.index(d) - 1]] if d != DAYS[0] else None, daily, q0), names)
    ipo = pd.read_parquet(PAPER / "bars_week.parquet", columns=["code", "date"])
    ipo["date"] = ipo["date"].astype(str)
    first = ipo.groupby("code")["date"].min()
    new_listing = set(first[first > "2026-10-20"].index)  # window starts 10-20: later first bar = fresh listing
    res: dict = {"meta": {"days": DAYS, "cost_bond_pct": COST_BOND, "cost_stock_pct": COST_STOCK, "rounds": ROUNDS, "seed": SEED,
                          "new_listings_excluded": sorted(new_listing), "weights": list(WEIGHTS)}}
    res["meta"]["data"] = {d: {"codes": int(len(x.codes)), "real_bars": x.real, "forward_filled_bars": x.filled} for d, x in days.items()}

    uni_a, uni_b = {}, {}
    rows_a, rows_b = [], []
    side_a = {"long": 1}
    for day, d in days.items():
        is_cb = np.array([c[:2] in ("11", "12") for c in d.codes])
        is_st = np.array([c[:2] in ("00", "60") and "ST" in str(d.names.get(c, "")).upper() for c in d.codes])
        is_main = np.array([c[:2] in ("00", "60") for c in d.codes])
        sel_b = is_main & ~is_st & ~np.array([c in new_listing for c in d.codes])
        uni_a[day], uni_b[day] = is_cb, sel_b
        rows_a += run_universe(d, is_cb, TICK_BOND, COST_BOND, True, False)
        rows_b += run_universe(d, sel_b, TICK_STOCK, COST_STOCK, False, True)
        print(day, "bonds", int(is_cb.sum()), "stocks", int(sel_b.sum()), "st", int(is_st.sum()), flush=True)
    ta, tb = pd.DataFrame(rows_a), pd.DataFrame(rows_b)
    ta.to_parquet(OUT / "trades_A.parquet")
    tb.to_parquet(OUT / "trades_B.parquet")
    res["A_convertible_bonds"] = scenario_block(ta, days, uni_a, COST_BOND, False, side_a)
    res["B_main_board"] = scenario_block(tb, days, uni_b, COST_STOCK, True, {"long_正T": 1, "short_倒T": -1, "combined": None})
    res["blocked_bars_B_universe"] = {d: int(days[d].blocked[uni_b[d]].sum()) for d in DAYS}
    res["B_main_board_universe"] = {d: int(uni_b[d].sum()) for d in DAYS}
    res["A_universe"] = {d: int(uni_a[d].sum()) for d in DAYS}
    ta_cnt = {"touch_only_bars": 0, "close_confirmed_multisnap_bars": 0}
    tb_cnt = dict(ta_cnt)
    for day, d in days.items():
        for tgt, sel in ((ta_cnt, uni_a[day]), (tb_cnt, uni_b[day])):
            r = touch_sidenote(d, sel)
            for k in tgt:
                tgt[k] += r[k]
    res["intrabar_touch_sidenote"] = {"A": ta_cnt, "B": tb_cnt}
    c_df, c_res = scenario_c(days)
    c_df.to_parquet(OUT / "c_sells.parquet")
    res["C_sell_rule"] = c_res
    (OUT / "results.json").write_text(json.dumps(res, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    write_report(res)
    print("done")
    return 0


if __name__ == "__main__":
    sys.exit(main())
