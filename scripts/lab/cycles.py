"""Market cycles cut where their leaders end, not by the calendar (lab venv; run with LAB_UNIVERSE=main).

A period lasts as long as its 龙头 (leader) does: a month, three months, whatever the data says. Two
kinds of leader, both on main-board stocks listed at least NEW_DAYS days (no IPO one-word runs), no ST:
1. 主线 (theme) cycles. Each theme (a Sina industry or concept; attribute lists like 融资融券 or regional
   labels are dropped) gets a daily index: sqrt-float-cap-weighted return of its members, minus the
   Shanghai Composite (上证指数). Each day the dominant line is the theme with the best K-day relative gain,
   if at least MIN_GAIN; a new theme takes over after HYST days on top; neighbouring runs of one family (or
   themes sharing members) are one line. The line's leader is the member with the biggest gain from the
   line's start to its own top; the cycle ends on the leader's top (the leader ending first, while followers
   still rise, is the classic end of a 主线), and the days after it until the next line are that line's 退潮.
2. 情绪 (sentiment) cycles by the space leader (空间龙头): a 龙头 run is a consecutive-limit-up run of at
   least MIN_HEIGHT boards that was the market's highest board on its last limit-up day; its cycle lasts from
   its first board to its top close (looking up to 10 days after the run ends, to include a 反包).
Both segmentations look at the whole history (descriptive). Per cycle: length, 上证 and main-board return,
leader, theme, the best strategies (strategy panel cohorts formed inside the cycle), the traditional
best, the main fusion top decile and the median strategy; then per cycle type, which strategies win.
Real-time test (no hindsight): the dominant line is taken as known that day (switching on the HYST-th day);
its leader is the member with the best gain since the line took over; the line ends when that leader
falls LEADER_STOP from its top. While a line is live, 主线跟随 buys its 10 strongest members (5-day
return) at the next open for 5 days; a second rule holds the strategy that has done best since the line
began. Compared with the 10 strongest stocks market-wide and the main fusion. Concept lists are today's
(look-ahead prone), so the test is also run on industries only.
Output .work/lab/main/cycles.json, cycles_theme.csv, cycles_dragon.csv.
"""
from __future__ import annotations

import json
import os
import sys

import numpy as np
import polars as pl
import torch

import fusion
import regime
import tengine as E

K, MIN_GAIN, HYST, LEADER_STOP = 10, 0.05, 3, 0.20
MIN_HEIGHT, MIN_MEMBERS = 4, 8
ATTRIBUTE = {"参股金融", "专精特新", "融资融券", "股权激励", "业绩预升", "重组概念", "金融参股", "业绩预降", "上海本地", "成渝特区",
             "基金重仓", "信托重仓", "QFII重仓", "券商重仓", "保险重仓", "社保重仓", "黄河三角", "含H股", "含B股", "海峡西岸", "整体上市",
             "金融改革", "陕甘宁", "长株潭", "本月解禁", "深圳本地", "皖江区域", "ST板块", "分拆上市", "国企改革", "资产注入", "央企50",
             "雄安新区", "科创50", "恒大概念", "京津冀", "朝鲜改革", "乡村振兴", "海南自贸", "武汉规划", "出口退税", "沿海发展", "前海概念",
             "图们江", "上海自贸", "摘帽概念", "内贸规划", "东亚自贸", "振兴沈阳", "自贸区", "高校背景", "日韩贸易", "外资背景", "超大盘",
             "送转潜力", "未股改", "三沙概念", "三板精选", "天津自贸", "含GDR", "准ST股", "其它行业", "综合行业", "开发区", "海上丝路"}
FAMILY = {"农业": ("育种", "农林", "农药", "化肥", "猪肉", "鸡肉", "水产", "生态农业", "土地流转", "草甘膦"),
          "科技": ("电子", "信息", "芯片", "半导体", "海思", "华为", "鸿蒙", "软件", "5G", "物联网", "苹果", "小米", "通信", "传媒", "游戏",
                  "触摸屏", "耳机", "穿戴", "安全", "宽带", "百度", "电商", "支付", "安防", "汽车电子", "互联"),
          "医药": ("药", "医", "CRO", "CXO", "抗癌", "疫苗", "基因", "免疫", "生物", "流感", "细菌"),
          "新能源": ("光伏", "风", "锂", "电池", "储能", "TOPCon", "HIT", "BC", "钙钛矿", "氢", "充电", "快充", "新能源", "核电", "电网",
                    "电解液", "发电", "输配", "电力"),
          "机器人与制造": ("机器人", "智能机器", "机械", "3D", "仪器", "海工", "船舶", "大飞机", "飞机"),
          "军工": ("军", "航天", "卫星"),
          "资源周期": ("有色", "黄金", "矿", "煤", "钢", "石油", "化工", "化纤", "页岩", "稀缺", "钒", "盐湖", "石墨烯", "碳纤维", "水泥",
                      "玻璃", "建材", "塑料", "陶瓷", "造纸"),
          "消费": ("酒", "食品", "家电", "家具", "服装", "纺织", "旅游", "酒店", "百货", "奢侈", "婴童", "体育", "猪肉", "鸡肉", "水产",
                  "迪士尼", "博彩", "赛马", "食"),
          "金融地产": ("金融", "银行", "券商", "保险", "地产", "房地产", "信托", "互联金融", "民营银行"),
          "汽车": ("汽车", "摩托", "特斯拉"),
          "基建公用": ("公路", "交通", "水利", "供水", "环保", "污水", "垃圾", "固废", "空气", "节能", "建筑", "碳", "循环")}


def family(name: str) -> str:
    for fam, keys in FAMILY.items():
        if any(k in name for k in keys):
            return fam
    return "其他"


def themes(d: E.Data, industries_only: bool = False) -> tuple[list[str], np.ndarray]:
    sec = pl.read_parquet(E.LAB / "sectors.parquet")
    if industries_only:
        sec = sec.filter(pl.col("kind") == "ind")
    col = {c: i for i, c in enumerate(d.codes)}
    names, rows = [], []
    for (kind, name), g in sec.group_by(["kind", "sector"], maintain_order=True):
        if name in ATTRIBUTE:
            continue
        idx = [col[c] for c in g["code"].to_list() if c in col]
        if len(idx) < MIN_MEMBERS:
            continue
        m = np.zeros(d.N, dtype=bool)
        m[idx] = True
        names.append(("行业:" if kind == "ind" else "概念:") + name)
        rows.append(m)
    return names, np.array(rows)


def theme_rs(d: E.Data, M: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Daily log relative strength [T, G] of each theme vs the Shanghai Composite, and the stock returns used."""
    f = d.f
    r = torch.where(d.live & (f["limit_ratio"] < 0.15) & (f["ret"].abs() < 0.35), f["ret"], torch.nan).numpy()
    w = np.sqrt(np.nan_to_num(E._shift(f["cap"], 1).numpy(), nan=0.0))
    ok = np.isfinite(r)
    rw = np.where(ok, r * w, 0.0) @ M.T.astype(np.float64)
    ws = np.where(ok, w, 0.0) @ M.T.astype(np.float64)
    tr = np.where(ws > 0, rw / np.maximum(ws, 1e-12), np.nan)
    idx = d.index_close.numpy()
    sh = np.r_[np.nan, idx[1:] / idx[:-1] - 1]
    lr = np.log1p(np.nan_to_num(tr, nan=0.0)) - np.log1p(np.nan_to_num(sh, nan=0.0))[:, None]
    return np.cumsum(lr, axis=0), r


def leader(close: np.ndarray, members: np.ndarray, a: int, b: int, after: int = 10) -> tuple[int, int, float]:
    """Member with the biggest gain from day a to its own top within [a, b + after]; returns (stock, top day, gain)."""
    end = min(close.shape[0], b + after + 1)
    seg = close[a:end][:, members]
    base = seg[0]
    ok = np.isfinite(base) & (base > 0)
    if not ok.any():
        return -1, b, float("nan")
    top = np.nanmax(seg[:, ok], axis=0) / base[ok] - 1
    j = int(np.nanargmax(top))
    stock = int(np.nonzero(members)[0][np.nonzero(ok)[0][j]])
    day = a + int(np.nanargmax(close[a:end, stock]))
    return stock, day, float(top[j])


def segments(dom: np.ndarray) -> list[tuple[int, int, int]]:
    out, s = [], 0
    for t in range(1, len(dom) + 1):
        if t == len(dom) or dom[t] != dom[s]:
            out.append((int(dom[s]), s, t))
            s = t
    return out


def dominant(L: np.ndarray, realtime: bool) -> np.ndarray:
    """Dominant theme per day: the best K-day relative-strength gain if at least MIN_GAIN, else -1; a new theme
    takes over after HYST days in a row on top (hindsight: from the first of those days; real time: on the last)."""
    g = L - np.vstack([np.full((K, L.shape[1]), np.nan), L[:-K]])
    g = np.where(np.isfinite(g), g, -np.inf)
    raw = np.argmax(g, axis=1)
    raw = np.where(g[np.arange(len(g)), raw] >= np.log1p(MIN_GAIN), raw, -1)
    dom = np.full(len(raw), -1)
    cur, run_of, run = -1, -1, 0
    for t, x in enumerate(raw):
        if x == cur:
            run = 0
        else:
            run = run + 1 if x == run_of else 1
            run_of = x
            if run >= HYST:
                cur = x
                if not realtime:
                    dom[t - HYST + 1: t] = cur
        dom[t] = cur
    return dom


def same_line(names: list[str], M: np.ndarray, g1: int, g2: int) -> bool:
    if g1 == g2:
        return True
    f1, f2 = family(names[g1]), family(names[g2])
    if f1 == f2 and f1 != "其他":
        return True
    return (M[g1] & M[g2]).sum() / max((M[g1] | M[g2]).sum(), 1) >= 0.2


def theme_cycles(d: E.Data, names: list[str], M: np.ndarray, L: np.ndarray) -> list[dict]:
    """主线 cycles in hindsight: runs of one dominant line (themes of one family merged), cut at the leader's top;
    the days after the leader's top and before the next line are 退潮 of that line."""
    close = d.f["close"].numpy()
    merged: list[list] = []
    for g, a, b in segments(dominant(L, realtime=False)):
        if merged and g >= 0 and merged[-1][0] >= 0 and same_line(names, M, merged[-1][3][-1], g):
            merged[-1][2] = b
            merged[-1][3].append(g)
        else:
            merged.append([g, a, b, [g]])
    out = []
    for g, a, b, gs in merged:
        if g < 0:
            out.append({"kind": "无主线", "start": a, "end": b, "themes": [], "leaders": []})
            continue
        gs = list(dict.fromkeys(gs))
        mem = np.any(M[gs], axis=0) & d.live[a].numpy()  # no ST, no new listing as leader
        st, top, gain = leader(close, mem, a, b - 1)
        th = [names[x] for x in gs]
        fam = family(th[0])
        rs = float(np.expm1(np.max([L[b - 1, x] - L[a, x] for x in gs])))
        cut = min(b, top + 1) if st >= 0 else b
        if cut - a >= 1:
            out.append({"kind": "主线", "start": a, "end": cut, "themes": th, "family": fam, "leaders": [d.codes[st]] if st >= 0 else [],
                        "leader_gain": gain, "rs_gain": rs, "leader_top": d.dates[top].isoformat() if st >= 0 else None})
        if cut < b:
            out.append({"kind": "退潮", "start": cut, "end": b, "themes": th, "family": fam, "leaders": [d.codes[st]]})
    return out


def regimes(cyc: list[dict], min_line: int = 10) -> list[dict]:
    """Coarser timeline: main lines of at least `min_line` days stay as 主线期 (with their 退潮 after the leader's
    top); runs of shorter lines and gaps are merged into one 轮动期 (rotation, no lasting leader)."""
    out: list[dict] = []
    for c in cyc:
        keep = (c["kind"] == "主线" and c["end"] - c["start"] >= min_line) or (
            c["kind"] == "退潮" and out and out[-1]["kind"] == "主线期" and out[-1]["themes"] == c["themes"])
        if keep:
            out.append({**c, "kind": "主线期" if c["kind"] == "主线" else "退潮"})
        elif out and out[-1]["kind"] == "轮动期":
            out[-1]["end"] = c["end"]
            out[-1]["themes"] += [t for t in c["themes"][:1] if t not in out[-1]["themes"]]
        else:
            out.append({"kind": "轮动期", "start": c["start"], "end": c["end"], "themes": list(c["themes"][:1]), "leaders": []})
    return out


def dragon_cycles(d: E.Data) -> list[dict]:
    f = d.f
    s = torch.where(d.live & (f["limit_ratio"] < 0.15), f["limit_streak"], 0).nan_to_num(0).numpy()
    close = f["close"].numpy()
    hmax = s.max(1)
    runs = []
    for n in range(d.N):
        col = s[:, n]
        ends = np.nonzero((col >= MIN_HEIGHT) & (np.r_[col[1:], 0] < col))[0]
        for e in ends:
            h = int(col[e])
            if h >= hmax[e]:  # the market's highest board on its last limit-up day
                a = e - h + 1
                win = close[a: min(d.T, e + 11), n]
                top = a + int(np.nanargmax(win)) if np.isfinite(win).any() else e
                runs.append({"n": n, "start": a, "end": e, "height": h, "top": top, "gain": float(close[top, n] / close[a - 1, n] - 1) if a > 0 else float("nan")})
    dom = np.full(d.T, -1)
    best = np.zeros(d.T)
    for k, r in enumerate(runs):
        for t in range(r["start"], r["top"] + 1):
            if r["height"] > best[t]:
                best[t], dom[t] = r["height"], k
    out = []
    for k, a, b in segments(dom):
        if k < 0:
            out.append({"kind": "无龙头", "start": a, "end": b, "leader": None})
            continue
        r = runs[k]
        out.append({"kind": "龙头", "start": a, "end": b, "leader": d.codes[r["n"]], "height": r["height"], "gain": r["gain"]})
    return out


def describe(d: E.Data, cyc: list[dict], snames: list[str], X: np.ndarray, main_x: np.ndarray, mkt: np.ndarray, names: dict) -> None:
    idx = d.index_close.numpy()
    for c in cyc:
        a, b = c["start"], c["end"]
        c["from"], c["to"], c["days"] = d.dates[a].isoformat(), d.dates[b - 1].isoformat(), b - a
        c["sh_ret"] = float((idx[b - 1] / idx[a - 1] - 1) * 100) if a > 0 else float("nan")
        c["mkt_ret"] = float((np.prod(1 + np.nan_to_num(mkt[a:b])) - 1) * 100)
        perf = np.nanmean(X[:, a:b], axis=1) if b - a >= 3 else np.full(len(snames), np.nan)
        order = [i for i in np.argsort(-np.nan_to_num(perf, nan=-99)) if np.isfinite(perf[i])]
        c["best"] = [(snames[i], round(float(perf[i]), 2)) for i in order[:5]]
        c["best_trad"] = [(snames[i], round(float(perf[i]), 2)) for i in order if snames[i].startswith("T:")][:3]
        c["main"] = round(float(np.nanmean(main_x[a:b])), 2) if b - a >= 3 else None
        c["median"] = round(float(np.nanmedian(perf)), 2) if np.isfinite(perf).any() else None
        c["perf"] = perf
        if c.get("leaders"):
            c["leader_names"] = [names.get(x, x) for x in c["leaders"][:3]]
        if c.get("leader"):
            c["leader_name"] = names.get(c["leader"], c["leader"])


def by_type(cyc: list[dict], key, snames: list[str], min_days: int = 5) -> dict:
    groups: dict[str, list[dict]] = {}
    for c in cyc:
        if c["days"] >= min_days and np.isfinite(c["perf"]).any():
            groups.setdefault(key(c), []).append(c)
    out = {}
    for k, cs in groups.items():
        P = np.array([c["perf"] for c in cs])
        mean = np.nanmean(P, axis=0)
        med = np.nanmedian(P, axis=1, keepdims=True)
        wins = (P > med).sum(0)
        order = [i for i in np.argsort(-np.nan_to_num(mean, nan=-99)) if np.isfinite(mean[i])]
        out[k] = {"cycles": len(cs), "days": int(sum(c["days"] for c in cs)),
                  "best": [(snames[i], round(float(mean[i]), 2), f"{int(wins[i])}/{len(cs)}") for i in order[:6]],
                  "main": round(float(np.nanmean([c["main"] for c in cs if c["main"] is not None])), 2),
                  "median": round(float(np.nanmedian(mean)), 2)}
    return out


def realtime(d: E.Data, names: list[str], M: np.ndarray, L: np.ndarray, snames: list[str], X: np.ndarray, main_x: np.ndarray) -> dict:
    """No hindsight: the line is the real-time dominant theme; its leader is the member with the best gain since the
    line took over (as known that day); the line ends when that leader falls LEADER_STOP from its top."""
    close = d.f["close"].numpy()
    y = d.label(5).numpy()
    mom5 = close / np.vstack([np.full((5, d.N), np.nan), close[:-5]]) - 1
    trad = d.tradable.numpy() & np.isfinite(y) & np.isfinite(mom5)
    dom = dominant(L, realtime=True)
    follow, mom_all, select, age = (np.full(d.T, np.nan) for _ in range(4))
    cur, start, lead, top, ended = -1, 0, -1, 0.0, False
    for t in range(d.T):
        if dom[t] != cur:
            if dom[t] >= 0 and not (cur >= 0 and same_line(names, M, cur, dom[t]) and not ended):
                start, lead, top, ended = t - HYST + 1, -1, 0.0, False
            cur = dom[t]
        cand = np.nonzero(trad[t])[0]
        if len(cand) >= 10:
            mom_all[t] = np.nanmean(y[t, cand[np.argsort(-mom5[t, cand])[:10]]])
        if cur < 0 or ended:
            continue
        mem = M[cur] & np.isfinite(close[start]) & np.isfinite(close[t])
        if mem.any():
            j = int(np.argmax(np.where(mem, close[t] / close[start], -np.inf)))
            if j != lead:
                lead, top = j, float(np.nanmax(close[start: t + 1, j]))
            top = max(top, float(close[t, j]))
            if close[t, j] < (1 - LEADER_STOP) * top:
                ended = True
                continue
        c = np.nonzero(M[cur] & trad[t])[0]
        if len(c) >= 3:
            follow[t] = np.nanmean(y[t, c[np.argsort(-mom5[t, c])[:10]]])
            age[t] = t - start
        if t - 6 - start >= 5:
            perf = np.nanmean(X[:, start: t - 5], axis=1)
            if np.isfinite(perf).any():
                select[t] = X[int(np.nanargmax(np.nan_to_num(perf, nan=-99))), t]
    follow, mom_all = follow - 0.2, mom_all - 0.2  # 0.2% round trip, as in the strategy panel
    out = {}
    print("\n[real time] mean 5-day cohort excess % (NW t, days): line follow (10 strongest members) | early (age <= 10d) | late | "
          "market-wide 10 strongest | best-since-line-start strategy | main fusion")
    for p, r in fusion.periods(d).items():
        def st(v):
            v = v[r]
            v = v[np.isfinite(v)]
            return (float(v.mean()), E.nw_t(v, 5), len(v)) if len(v) > 8 else (float("nan"), float("nan"), len(v))
        row = {"follow": st(follow), "early": st(np.where(age <= 10, follow, np.nan)), "late": st(np.where(age > 10, follow, np.nan)),
               "mom_all": st(mom_all), "select": st(select), "main": st(main_x)}
        out[p] = row
        print(f"  {p:7s} " + " | ".join(f"{k} {m:+.2f} ({t:+.1f}, {n})" for k, (m, t, n) in row.items()))
    return out


def main() -> None:
    if E.UNIVERSE != "main":
        print("note: run with LAB_UNIVERSE=main for the main-board study", flush=True)
    d = E.Data()
    nm = pl.read_parquet(E.BASE / "panel.parquet", columns=["code", "name", "date"]).sort("date").unique("code", keep="last")
    names_of = dict(zip(nm["code"], nm["name"]))
    panel = pl.read_parquet(E.LAB / "strategy_panel.parquet")
    snames, X = regime.strategy_month(panel, d)
    score = torch.load(E.LAB / os.environ.get("CYCLE_MAIN", "fusion_static40_safe.pt"))
    y = d.label(5)
    ok = torch.isfinite(score) & torch.isfinite(y)
    rk = E.cs_rank(torch.where(ok, score, torch.nan))
    top = ok & (rk >= 0.9)
    main_x = (torch.where(top, y, 0).sum(1) / top.sum(1).clamp(min=1)).numpy() - 0.2
    main_x[top.sum(1).numpy() < 10] = np.nan
    mkt = E._nanmean_row(torch.where(d.tradable & (d.f["ret"].abs() < 0.35), d.f["ret"], torch.nan)).numpy()
    tnames, M = themes(d)
    L, _ = theme_rs(d, M)
    tc = theme_cycles(d, tnames, M, L)
    dc = dragon_cycles(d)
    rg = regimes(tc)
    describe(d, rg, snames, X, main_x, mkt, names_of)
    describe(d, tc, snames, X, main_x, mkt, names_of)
    describe(d, dc, snames, X, main_x, mkt, names_of)
    print(f"[cycles] {len(tnames)} themes on {d.N} main-board stocks; {sum(c['kind'] == '主线' for c in tc)} main-line cycles, "
          f"{sum(c['kind'] == '龙头' for c in dc)} space-leader cycles")
    print("\n===== 主线 cycles (cut at the leader's top): dates, days, 上证 %, main-board EW %, themes / leaders | best strategies (5-day cohort excess %) | main fusion | median")
    for c in tc:
        if c["days"] < 5:
            continue
        lab = f"{c['kind']} {'/'.join(c['themes'][:3])} 龙头 {'/'.join(c.get('leader_names', []))}" if c["themes"] else c["kind"]
        print(f"  {c['from']}..{c['to']} {c['days']:3d}d 上证 {c['sh_ret']:+6.1f} EW {c['mkt_ret']:+6.1f} {lab}\n"
              f"      best: {'; '.join(f'{n} {v:+.2f}' for n, v in c['best'][:4])} | trad: {'; '.join(f'{n} {v:+.2f}' for n, v in c['best_trad'])} | main {c['main']} | median {c['median']}")
    print("\n===== market periods: 主线期 (line >= 10 days, to its leader's top), 退潮 (after the top), 轮动期 (short lines, gaps)")
    for c in rg:
        lab = f"{'/'.join(c['themes'][:3])} 龙头 {'/'.join(c.get('leader_names', []))}" if c["kind"] != "轮动期" else f"轮动: {'/'.join(c['themes'][:6])}"
        print(f"  {c['from']}..{c['to']} {c['days']:3d}d {c['kind']} 上证 {c['sh_ret']:+6.1f} EW {c['mkt_ret']:+6.1f} {lab}\n"
              f"      best: {'; '.join(f'{n} {v:+.2f}' for n, v in c['best'][:4])} | trad: {'; '.join(f'{n} {v:+.2f}' for n, v in c['best_trad'])} | main {c['main']} | median {c['median']}")
    print("\n===== 情绪 cycles (space leader, first board to top close): dates, days, leader, height, gain")
    for c in dc:
        if c["days"] < 3 or c["kind"] != "龙头":
            continue
        print(f"  {c['from']}..{c['to']} {c['days']:3d}d {c['leader_name']} {c['height']}板 +{c['gain'] * 100:.0f}% 上证 {c['sh_ret']:+5.1f} | "
              f"best: {'; '.join(f'{n} {v:+.2f}' for n, v in c['best'][:3])} | main {c['main']}")
    types = {
        "period_kind": by_type(rg, lambda c: c["kind"], snames),
        "period_family": by_type([c for c in rg if c["kind"] != "轮动期"], lambda c: c["kind"] + "·" + c["family"], snames),
        "period_index_dir": by_type(rg, lambda c: ("上证涨" if c["sh_ret"] > 0 else "上证跌") + "·" + c["kind"], snames),
        "theme_family": by_type([c for c in tc if c["kind"] == "主线"], lambda c: c["family"], snames),
        "theme_vs_none": by_type(tc, lambda c: c["kind"], snames),
        "theme_index_dir": by_type(tc, lambda c: ("上证涨" if c["sh_ret"] > 0 else "上证跌") + "·" + c["kind"], snames),
        "dragon_vs_none": by_type(dc, lambda c: c["kind"], snames, min_days=3),
        "dragon_height": by_type([c for c in dc if c["kind"] == "龙头"], lambda c: "高度≥7" if c["height"] >= 7 else "高度4-6", snames, min_days=3),
    }
    for k, g in types.items():
        print(f"\n===== best by cycle type: {k}")
        for t, v in g.items():
            print(f"  {t:10s} {v['cycles']:3d} cycles {v['days']:4d}d | main {v['main']:+.2f} median {v['median']:+.2f} | "
                  + "; ".join(f"{n} {m:+.2f} ({w})" for n, m, w in v["best"]))
    rt = realtime(d, tnames, M, L, snames, X, main_x)
    ti, Mi = themes(d, industries_only=True)
    Li, _ = theme_rs(d, Mi)
    print("  (industries only, no concept look-ahead:)")
    rt_ind = realtime(d, ti, Mi, Li, snames, X, main_x)
    for c in tc + dc + rg:
        c.pop("perf", None)
    json.dump({"periods": rg, "theme_cycles": tc, "dragon_cycles": dc, "types": types, "realtime": rt, "realtime_industries": rt_ind,
               "params": {"K": K, "MIN_GAIN": MIN_GAIN, "HYST": HYST, "LEADER_STOP": LEADER_STOP, "MIN_HEIGHT": MIN_HEIGHT, "NEW_DAYS": E.NEW_DAYS}},
              open(E.LAB / "cycles.json", "w", encoding="utf-8"), ensure_ascii=False, indent=1, default=float)
    for nm_, cyc in (("cycles_periods.csv", rg), ("cycles_theme.csv", tc), ("cycles_dragon.csv", dc)):
        pl.DataFrame([{k: (json.dumps(v, ensure_ascii=False) if isinstance(v, (list, dict)) else v) for k, v in c.items()} for c in cyc]).write_csv(E.LAB / nm_)


if __name__ == "__main__":
    sys.exit(main())
