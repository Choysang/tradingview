"""Day-by-day replay (逐日仿真) of short-term play on the Shanghai/Shenzhen main board (lab venv, polars).

Each trading day D, after the close, using only bars up to D:
- 龙头 (dragon): a stock on a run of at least DRAGON consecutive limit-ups (main board, not ST, listed >= 60 days).
- its 题材: among the dragon's Sina industry/concepts (attribute labels dropped), the one with the most limit-ups
  in D-2..D; failing that (< 3), a name cluster (other limit-ups sharing a two-character word with its name, e.g.
  新华) or the coming zodiac's homophones (羊年: 阳洋羊扬杨央); else 独立.
- a theme line runs from its first dragon until, for END_QUIET days, no member is on a run of 2+ boards and the
  theme's 3-day limit-up count is below 3. When the dragon breaks and another member is still on a run, that member
  is the new leader (补位龙 / 半路接棒), and the switch is logged.
Strategies decided at D, traded on D+1 with what daily bars can honestly settle:
  S1 龙头打板     buy each dragon at its limit price on D+1. A 一字 day cannot fill; a board that held after being
                  touched fills with probability P_SEAL (queue); a board that broke (炸板) always fills (adverse
                  selection). Sell at the D+2 open.
  S2 龙头开盘买   buy each dragon at the D+1 open unless it opens sealed. Sell at the D+2 open.
  S3 断板低吸     a dragon that broke at D: resting buy 3% below the D close on D+1; sell at the D+2 close.
  S4 补位龙       after a switch in a theme that is still hot, buy the new leader (open, or its board as in S1).
  S5 跟风首板     first boards at D in a live line (at most 3 by turnover): buy at the D+1 open, sell D+2 open.
  S6 生肖谐音     limit-ups at D whose names carry the coming zodiac or its homophones, within 120 trading days
                  before that New Year: buy at the D+1 open, sell at the D+2 open.
  B0 追涨停       every non-一字 limit-up at D: buy at the D+1 open, sell at the D+2 open (the crowd's baseline).
Execution: costs 0.025% commission each side, 0.05% stamp duty on sells, 0.001% transfer, 0.1% slippage on
open/close orders (none on a limit-price order); a sell that meets a sealed limit-down open rolls to the next open
(up to 3 days). Capacity: 2% of the entry day's turnover. After the close of D+2 the trade is scored (复盘).
Theme lists are today's Sina lists (look-ahead prone): the dragon itself is defined by price alone, but S4/S5 pick
other members through the lists, so they are also run with industries only.
Output .work/lab/replay_trades.parquet, replay_lines.csv, replay_journal.md and printed tables.
"""
from __future__ import annotations

import json
import os
import re
import sys
from datetime import date

import numpy as np
import polars as pl

LAB = os.path.join(os.path.dirname(__file__), "..", "..", ".work", "lab")
MAIN = ("600", "601", "603", "605", "000", "001", "002", "003")
DRAGON, END_QUIET, P_SEAL = 4, 3, 0.3
# v2 (round 7, R7-3): heat threshold grows with theme size; a switch needs a 3-board runner.
HEAT_MIN, HEAT_SHARE, SWITCH_MIN = 3, 0.02, 3
BUY_COST, SELL_COST, SLIP = 0.00025 + 0.00001, 0.00025 + 0.0005 + 0.00001, 0.001
ZODIAC = [(date(2024, 2, 10), "龙", "龙隆珑泷陇"), (date(2025, 1, 29), "蛇", "蛇"), (date(2026, 2, 17), "马", "马码玛蚂"),
          (date(2027, 2, 6), "羊", "羊阳洋扬杨央")]
STOP_WORDS = {"股份", "科技", "集团", "控股", "实业", "发展", "中国", "电子", "技术", "信息", "国际", "产业", "能源", "环境",
              "智能", "新材", "材料", "电气", "医药", "生物", "传媒", "文化", "投资", "建设", "工业", "机械", "汽车", "电力",
              "化工", "精密", "装备", "食品", "农业", "旅游", "置业", "地产", "物流", "航空", "通信", "网络", "软件", "数据"}
PERIODS = {"2024H1": ("2024-01-01", "2024-06-30"), "2024H2": ("2024-07-01", "2024-12-31"), "2025H1": ("2025-01-01", "2025-06-30"),
           "2025H2": ("2025-07-01", "2025-12-31"), "2026H1": ("2026-01-01", "2026-06-30"), "SELECT": ("2026-07-01", "2026-08-31"),
           "VERIFY": ("2026-09-01", "2026-10-30")}


def load() -> tuple[pl.DataFrame, list]:
    cols = ["code", "date", "name", "open", "high", "low", "close", "prev_close", "amount", "is_limit_up", "touched_limit",
            "open_at_limit", "open_at_floor", "one_word", "limit_streak", "days_listed", "st_like"]
    p = pl.read_parquet(os.path.join(LAB, "panel.parquet"), columns=cols)
    p = p.filter(pl.col("code").str.slice(0, 3).is_in(MAIN) & ~pl.col("st_like") & (pl.col("days_listed") >= 60))  # price-based ST
    dates = sorted(p["date"].unique().to_list())
    p = p.with_columns(pl.col("date").replace_strict({d: i for i, d in enumerate(dates)}, return_dtype=pl.Int32).alias("t")).sort("code", "t")
    nxt = []
    for k in (1, 2, 3, 4, 5):
        for c in ("open", "high", "low", "close", "amount", "is_limit_up", "touched_limit", "one_word", "open_at_limit", "open_at_floor", "t"):
            nxt.append(pl.col(c).shift(-k).over("code").alias(f"{c}{k}"))
    return p.with_columns(nxt), dates


def themes(industries_only: bool) -> dict[str, list[str]]:
    import cycles as C  # attribute-label list shared with the cycle study
    sec = pl.read_parquet(os.path.join(LAB, "sectors.parquet"))
    if industries_only:
        sec = sec.filter(pl.col("kind") == "ind")
    sec = sec.filter(~pl.col("sector").is_in(list(C.ATTRIBUTE)))
    out: dict[str, list[str]] = {}
    for code, s in zip(sec["code"], sec["sector"]):
        out.setdefault(code, []).append(s)
    return out


def themes_sw() -> tuple[dict[str, list[str]], dict[str, date]]:
    """Shenwan (cninfo, dated) level-2 and level-3 industries. A stock's industry counts only from its
    latest change date on; before it the stock has no industry (unknown, not guessed)."""
    sw = pl.read_parquet(os.path.join(LAB, "sw_industry.parquet"))
    out: dict[str, list[str]] = {}
    joined: dict[str, date] = {}
    for r in sw.iter_rows(named=True):
        out[r["code"]] = [x for x in dict.fromkeys(("申万:" + (r["sw2"] or ""), "申万:" + (r["sw3"] or ""))) if x != "申万:"]
        if r["joined"]:
            joined[r["code"]] = date.fromisoformat(r["joined"][:10])
    return out, joined


def words(name: str) -> set[str]:
    name = re.sub(r"[A-Za-z0-9\s*]", "", name)
    return {name[i:i + 2] for i in range(len(name) - 1)} - STOP_WORDS


def zodiac_chars(d: date, dates: list, t: int) -> tuple[str, str] | None:
    for ny, animal, chars in ZODIAC:
        if d < ny:
            ahead = sum(1 for x in dates[t:] if x < ny)
            return (animal, chars) if ahead <= 120 or dates[-1] < ny else None
    return None


def sell(row: dict, k0: int, at: str) -> tuple[float | None, int]:
    """Exit price from day k0 (open or close), rolling past sealed limit-down opens; returns (price, day index)."""
    for k in range(k0, 6):
        if row.get(f"open{k}") is None:
            return None, -1
        if at == "open" and row.get(f"open_at_floor{k}"):
            continue
        px = row[f"{at}{k}"]
        return px * (1 - SLIP), row[f"t{k}"]
    return None, -1


def entry(row: dict, how: str, rng: np.random.Generator, p_seal: float) -> float | None:
    if row.get("open1") is None:
        return None
    if how == "open":
        return None if row["open_at_limit1"] else row["open1"] * (1 + SLIP)
    if how == "board":
        if not row["touched_limit1"] or row["one_word1"]:
            return None
        if row["is_limit_up1"] and rng.random() >= p_seal:
            return None
        return row["high1"]
    if how == "dip":
        px = row["close"] * 0.97
        return min(row["open1"], px) if row["low1"] <= px else None
    raise ValueError(how)


def replay(p: pl.DataFrame, dates: list, tmap: dict[str, list[str]], p_seal: float, seed: int = 7,
           joined: dict[str, date] | None = None, v2: bool = False) -> tuple[pl.DataFrame, list[dict], list[str]]:
    rng = np.random.default_rng(seed)
    size: dict[str, int] = {}
    for ths in tmap.values():
        for th in ths:
            size[th] = size.get(th, 0) + 1

    def thr(th: str) -> int:
        return max(HEAT_MIN, int(np.ceil(HEAT_SHARE * size.get(th, 0)))) if v2 else HEAT_MIN
    by_t = p.partition_by("t", as_dict=True)
    zt_hist: list[dict[str, int]] = []  # per day: theme -> limit-ups
    lines: dict[str, dict] = {}
    closed: list[dict] = []
    trades, journal = [], []
    prev_dragons: dict[str, dict] = {}
    for t, d in enumerate(dates):
        day = by_t.get((t,))
        if day is None:
            zt_hist.append({})
            continue
        rows = day.to_dicts()

        def tm(code: str, d: date = d) -> list[str]:
            return [] if joined and code in joined and d < joined[code] else tmap.get(code, [])

        zt = [r for r in rows if r["is_limit_up"]]
        cnt: dict[str, int] = {}
        for r in zt:
            for th in tm(r["code"]):
                cnt[th] = cnt.get(th, 0) + 1
        zt_hist.append(cnt)
        heat = {}
        for c in zt_hist[-3:]:
            for k, v in c.items():
                heat[k] = heat.get(k, 0) + v
        recent_zt = [r for r in zt] + [r for c in (by_t.get((t - 1,)), by_t.get((t - 2,))) if c is not None for r in c.filter(pl.col("is_limit_up")).to_dicts()]
        zc = zodiac_chars(d, dates, t)

        def theme_of(r: dict) -> str:
            ths = tm(r["code"])
            if ths:
                best = max(ths, key=lambda x: heat.get(x, 0))
                if heat.get(best, 0) >= thr(best):
                    return best
            w = words(r["name"])
            mates = [x for x in recent_zt if x["code"] != r["code"] and w & words(x["name"])]
            if len(mates) >= 2:
                return "名字:" + sorted(w & words(mates[0]["name"]))[0]
            if zc and any(ch in r["name"] for ch in zc[1]) and sum(any(ch in x["name"] for ch in zc[1]) for x in recent_zt) >= 3:
                return f"谐音:{zc[0]}"
            return "独立:" + r["name"]

        dragons = {r["code"]: {**r, "theme": theme_of(r)} for r in rows if (r["limit_streak"] or 0) >= DRAGON and r["is_limit_up"]}
        runners = {r["code"]: r for r in zt if (r["limit_streak"] or 0) >= 2}
        # theme lines: open on a dragon, switch leaders, close after END_QUIET quiet days
        events = []
        for code, r in dragons.items():
            th = r["theme"]
            ln = lines.get(th)
            if ln is None:
                lines[th] = ln = {"theme": th, "start": d, "leaders": [(d, r["name"], r["limit_streak"])], "leader": code, "quiet": 0, "max_height": 0}
                events.append(("新主线", th, r["name"]))
            ln["max_height"] = max(ln["max_height"], r["limit_streak"])
        for th, ln in list(lines.items()):
            members = [x for x in runners.values() if th in tm(x["code"]) or (th.startswith(("名字:", "谐音:", "独立:")) and x["code"] in dragons and dragons[x["code"]]["theme"] == th)]
            cur = ln["leader"]
            if cur not in runners:  # the leader broke today
                alive = sorted(members, key=lambda x: -(x["limit_streak"] or 0))
                if alive and heat.get(th, 0) >= thr(th) and (not v2 or (alive[0]["limit_streak"] or 0) >= SWITCH_MIN):
                    new = alive[0]
                    ln["leader"] = new["code"]
                    ln["leaders"].append((d, new["name"], new["limit_streak"]))
                    events.append(("补位", th, new["name"]))
                    trades += [tr for tr in (signal("S4_补位龙", new, rng, p_seal, d, th),) if tr]
            quiet = not members and heat.get(th, 0) < thr(th)
            ln["quiet"] = ln["quiet"] + 1 if quiet else 0
            if ln["quiet"] >= END_QUIET:
                ln["end"] = d
                closed.append(ln)
                del lines[th]
                events.append(("退潮", th, ""))
        # signals
        for code, r in dragons.items():
            trades += [tr for tr in (signal("S1_龙头打板", r, rng, p_seal, d, r["theme"]), signal("S2_龙头开盘买", r, rng, p_seal, d, r["theme"])) if tr]
        for code, r in prev_dragons.items():
            today = next((x for x in rows if x["code"] == code), None)
            if today is not None and not today["is_limit_up"]:
                tr = signal("S3_断板低吸", today, rng, p_seal, d, r["theme"])
                if tr:
                    trades.append(tr)
        live = set(lines)
        firsts = sorted([r for r in zt if r["limit_streak"] == 1 and not r["one_word"] and any(th in live for th in tm(r["code"]))],
                        key=lambda x: -(x["amount"] or 0))[:3]
        trades += [tr for tr in (signal("S5_跟风首板", r, rng, p_seal, d, "") for r in firsts) if tr]
        if zc:
            for r in zt:
                if any(ch in r["name"] for ch in zc[1]) and not r["one_word"]:
                    tr = signal("S6_生肖谐音", r, rng, p_seal, d, f"谐音:{zc[0]}")
                    if tr:
                        trades.append(tr)
        for r in zt:
            if not r["one_word"]:
                tr = signal("B0_追涨停", r, rng, p_seal, d, "")
                if tr:
                    trades.append(tr)
        prev_dragons = dragons
        journal.append({"date": d.isoformat(), "zt": len(zt), "dragons": [(r["name"], r["limit_streak"], r["theme"]) for r in sorted(dragons.values(), key=lambda x: -x["limit_streak"])],
                        "hot": sorted(heat.items(), key=lambda kv: -kv[1])[:5], "events": events, "lines": sorted(lines)})
    closed += [{**ln, "end": None} for ln in lines.values()]
    return pl.DataFrame(trades) if trades else pl.DataFrame(), closed, journal


def signal(kind: str, r: dict, rng: np.random.Generator, p_seal: float, d: date, theme: str) -> dict | None:
    how = {"S1_龙头打板": "board", "S2_龙头开盘买": "open", "S3_断板低吸": "dip", "S4_补位龙": "open", "S5_跟风首板": "open",
           "S6_生肖谐音": "open", "B0_追涨停": "open"}[kind]
    px = entry(r, how, rng, p_seal)
    if kind == "S4_补位龙" and px is None:
        px = entry(r, "board", rng, p_seal)
    base = {"kind": kind, "date": d, "code": r["code"], "name": r["name"], "theme": theme, "streak": r["limit_streak"], "filled": px is not None}
    if px is None:
        return base | {"ret": None, "cap_wan": None}
    out, _ = sell(r, 2, "close" if kind == "S3_断板低吸" else "open")
    if out is None:
        return base | {"filled": False, "ret": None, "cap_wan": None}
    ret = (out * (1 - SELL_COST)) / (px * (1 + BUY_COST)) - 1
    return base | {"ret": ret * 100, "cap_wan": (r.get("amount1") or 0) * 0.02 * 1e4}


def summary(tr: pl.DataFrame, label: str) -> dict:
    out = {}
    print(f"\n[{label}] per trade net % (after costs): fills/signals, mean, median, win rate, day-clustered t, capacity (2% of turnover, 万元, median)")
    for kind in sorted(tr["kind"].unique().to_list()):
        k = tr.filter(pl.col("kind") == kind)
        row = {}
        cells = []
        for p, (a, b) in PERIODS.items():
            x = k.filter(pl.col("date").is_between(date.fromisoformat(a), date.fromisoformat(b)))
            f = x.filter(pl.col("filled") & pl.col("ret").is_not_null())
            if f.height < 5:
                cells.append(f"{p} {f.height}/{x.height}")
                continue
            dm = f.group_by("date").agg(pl.col("ret").mean())["ret"].to_numpy()
            t = float(dm.mean() / (dm.std(ddof=1) / np.sqrt(len(dm)))) if len(dm) > 2 and dm.std() > 0 else float("nan")
            row[p] = {"signals": x.height, "fills": f.height, "mean": float(f["ret"].mean()), "median": float(f["ret"].median()),
                      "win": float((f["ret"] > 0).mean()), "t_day": t, "cap_wan": float(f["cap_wan"].median())}
            cells.append(f"{p} {f.height}/{x.height} {row[p]['mean']:+.2f} ({row[p]['median']:+.2f}, {row[p]['win']:.0%}, t {t:+.1f}, {row[p]['cap_wan']:.0f}万)")
        out[kind] = row
        print(f"  {kind}\n      " + "\n      ".join(cells))
    return out


def main() -> None:
    p, dates = load()
    res = {}
    for label, ind_only, ps in (("concepts", False, P_SEAL), ("concepts_pseal0", False, 0.0), ("concepts_pseal1", False, 1.0), ("industries", True, P_SEAL)):
        tr, lines, journal = replay(p, dates, themes(ind_only), ps)
        res[label] = summary(tr, f"{label}, P_SEAL={ps}")
        if label == "concepts":
            tr.write_parquet(os.path.join(LAB, "replay_trades.parquet"))
            rows = [{"theme": ln["theme"], "start": ln["start"].isoformat(), "end": ln["end"].isoformat() if ln["end"] else "live",
                     "max_height": ln["max_height"], "switches": len(ln["leaders"]) - 1,
                     "leaders": " → ".join(f"{n}({h}板,{dd.isoformat()[5:]})" for dd, n, h in ln["leaders"])} for ln in lines]
            pl.DataFrame(rows).sort("start").write_csv(os.path.join(LAB, "replay_lines.csv"))
            print("\n[lines since 2026-08-15] theme, start..end, highest board, leader sequence (switches)")
            for r in sorted(rows, key=lambda x: x["start"]):
                if r["start"] >= "2026-08-15" or r["end"] == "live" or r["end"] >= "2026-08-15":
                    print(f"  {r['theme']:14s} {r['start']}..{r['end']:10s} {r['max_height']:2d}板 {r['leaders']}")
            with open(os.path.join(LAB, "replay_journal.md"), "w", encoding="utf-8") as fh:
                fh.write("# 逐日复盘（主板，收盘后，仅用当日及以前数据）\n\n")
                for j in journal[-30:]:
                    dr = "；".join(f"{n} {h}板（{th}）" for n, h, th in j["dragons"][:6]) or "无"
                    ev = "；".join(f"{a} {b} {c}".strip() for a, b, c in j["events"]) or "—"
                    hot = "、".join(f"{k}({v})" for k, v in j["hot"])
                    fh.write(f"- **{j['date']}** 涨停 {j['zt']}｜龙头：{dr}｜事件：{ev}｜3 日热度：{hot}\n")
    json.dump(res, open(os.path.join(LAB, "replay.json"), "w", encoding="utf-8"), ensure_ascii=False, indent=1, default=float)


def line_stats(lines: list[dict], dates: list) -> dict:
    pos = {d: i for i, d in enumerate(dates)}
    dur = np.array([pos[ln["end"] or dates[-1]] - pos[ln["start"]] + 1 for ln in lines])
    return {"lines": len(lines), "median_days": float(np.median(dur)), "over_40_days": int((dur > 40).sum()),
            "longest": int(dur.max()), "switches_mean": float(np.mean([len(ln["leaders"]) - 1 for ln in lines]))}


def main_v2() -> None:
    """R7-3: line ending v2 (size-scaled heat, 3-board switches) and dated Shenwan industries."""
    p, dates = load()
    concepts = themes(False)
    sw, joined = themes_sw()
    both = {c: concepts.get(c, []) + sw.get(c, []) for c in set(concepts) | set(sw)}
    res = {}
    for label, tmap, jn, v2 in (("concepts_v1", concepts, None, False), ("concepts_v2", concepts, None, True),
                                ("sw_v2", sw, joined, True), ("sw_concepts_v2", both, joined, True)):
        tr, lines, _ = replay(p, dates, tmap, P_SEAL, joined=jn, v2=v2)
        st = line_stats(lines, dates)
        print(f"\n[{label}] lines {st['lines']}, median {st['median_days']:.0f} trading days, over 40 days {st['over_40_days']}, "
              f"longest {st['longest']}, switches per line {st['switches_mean']:.1f}", flush=True)
        res[label] = {"lines": st, "trades": summary(tr, f"{label}, P_SEAL={P_SEAL}")}
        if label == "sw_concepts_v2":
            rows = [{"theme": ln["theme"], "start": ln["start"].isoformat(), "end": ln["end"].isoformat() if ln["end"] else "live",
                     "max_height": ln["max_height"], "switches": len(ln["leaders"]) - 1,
                     "leaders": " → ".join(f"{n}({h}板,{dd.isoformat()[5:]})" for dd, n, h in ln["leaders"])} for ln in lines]
            pl.DataFrame(rows).sort("start").write_csv(os.path.join(LAB, "replay_lines_v2.csv"))
            print("\n[lines since 2026-09-01, sw_concepts_v2]")
            for r in sorted(rows, key=lambda x: x["start"]):
                if r["start"] >= "2026-09-01" or r["end"] == "live" or r["end"] >= "2026-09-15":
                    print(f"  {r['theme']:16s} {r['start']}..{r['end']:10s} {r['max_height']:2d}板 {r['leaders'][:220]}")
    json.dump(res, open(os.path.join(LAB, "replay_v2.json"), "w", encoding="utf-8"), ensure_ascii=False, indent=1, default=float)


if __name__ == "__main__":
    sys.exit(main_v2() if sys.argv[1:] == ["v2"] else main())
