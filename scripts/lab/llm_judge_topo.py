"""R7-5 third variant: the anonymous card plus point-in-time theme topology, still no code, name or date.

Same 600 cards as llm_judge.py (anonymous K-line card) and llm_judge_pit.py (named card with announcements),
same model and effort, same labels. The added block describes the stock's Shenwan level-2 group on day D only
from data known at D's close: the group's limit-ups today and over 5 sessions, its rank among all groups,
its highest board today, whether this stock is the group's leader, and how long the group has had a 4-board.
A stock's Shenwan group counts only from its dated join (replay.themes_sw); before it the group is unknown.

The base card is passed verbatim, so the three variants differ only in what is added. (Its listing-age line
was built before the 2026-11-01 days_listed fix and shows bar counts; identical in all three variants.)

Stages: `ask` fills judge_verdicts_topo.jsonl (resumable), `score` prints the three-way comparison.
"""
from __future__ import annotations

import json
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date

import numpy as np
import polars as pl

import llm
import llm_judge as J
import llm_judge_pit as P
from harness import CODE, LAB, c, ctx
from replay import themes_sw

VERDICTS = LAB / "judge_verdicts_topo.jsonl"
SYSTEM = J.SYSTEM.replace("只依据卡片内容判断", "卡片末尾附有当日所属申万二级行业的涨停热度与梯队位置。只依据卡片内容判断")
HOT_BOARD = 4  # a group "has a line" once one of its stocks reaches this many boards


def topology() -> dict[tuple[str, str], str]:
    """(code, date) -> topology text for every card's day, from the panel up to that day."""
    cards = [json.loads(x) for x in J.CARDS.open(encoding="utf-8")]
    days = sorted({date.fromisoformat(x["date"]) for x in cards})
    groups, joined = themes_sw()
    k = ctx()
    first = k.dates[max(0, k.dates.index(days[0]) - 40)]
    p = k.panel.filter((c("date") >= first) & (c("date") <= days[-1])).select(CODE, "date", "is_limit_up", "limit_streak", "pct_chg")
    sw = pl.DataFrame({CODE: list(groups), "grp": [g[0] if g else None for g in groups.values()],
                       "joined": [joined.get(x) for x in groups]}, schema={CODE: pl.String, "grp": pl.String, "joined": pl.Date})
    p = p.join(sw, on=CODE, how="left").with_columns(
        pl.when(c("joined").is_not_null() & (c("joined") <= c("date"))).then(c("grp")).alias("grp"))
    p = p.with_columns(c("is_limit_up").fill_null(False), c("limit_streak").fill_null(0))
    daily = p.filter(c("grp").is_not_null()).group_by("grp", "date").agg(
        c("is_limit_up").sum().alias("zt"), c("limit_streak").max().alias("top"), c("pct_chg").mean().alias("avg"),
        pl.len().alias("n")).sort("grp", "date")
    daily = daily.with_columns(
        c("zt").rolling_sum(5, min_samples=1).over("grp").alias("zt5"),
        (c("top") >= HOT_BOARD).alias("hot"))
    out: dict[tuple[str, str], str] = {}
    for day in days:
        d = daily.filter(c("date") == day).with_columns(c("zt").rank("min", descending=True).alias("rank"))
        ngrp = d.height
        hist = daily.filter((c("date") <= day) & (c("date") >= k.dates[max(0, k.dates.index(day) - 19)]))
        hot_days = hist.filter(c("hot")).group_by("grp").agg(pl.len().alias("hot20"), c("date").max().alias("last_hot"))
        d = d.join(hot_days, on="grp", how="left")
        mkt_top = int(p.filter(c("date") == day)["limit_streak"].max() or 0)
        today = p.filter(c("date") == day)
        g_by = {r["grp"]: r for r in d.iter_rows(named=True)}
        for r in today.filter(c(CODE).is_in([x["code"] for x in cards if x["date"] == day.isoformat()])).iter_rows(named=True):
            g = g_by.get(r["grp"]) if r["grp"] else None
            if g is None:
                text = f"题材拓扑（D 日）：申万二级行业未知；全市场最高连板 {mkt_top}"
            else:
                peers = today.filter(c("grp") == r["grp"])
                ahead = int((peers["limit_streak"] > r["limit_streak"]).sum())
                me = ("本股为本行业最高板" if r["limit_streak"] and ahead == 0 else
                      f"本行业有 {ahead} 只连板高于本股" if r["limit_streak"] else "本股当日未涨停")
                last = g["last_hot"]
                line = (f"近20日有 {g['hot20']} 天出现 {HOT_BOARD} 板以上，最近一次距今 {k.dates.index(day) - k.dates.index(last)} 个交易日"
                        if last else f"近20日未出现 {HOT_BOARD} 板以上")
                text = (f"题材拓扑（D 日，所属申万二级行业 {r['grp'].removeprefix('申万:')}，共 {g['n']} 只）："
                        f"当日涨停 {g['zt']} 家（行业涨停数排名 {int(g['rank'])}/{ngrp}），近5日累计涨停 {g['zt5']} 家，"
                        f"行业平均涨跌 {g['avg']:+.2f}%，行业最高连板 {g['top']}；{me}；{line}；全市场最高连板 {mkt_top}")
            out[(r[CODE], day.isoformat())] = text
    return out


def ask_all() -> None:
    cards = [json.loads(x) for x in J.CARDS.open(encoding="utf-8")]
    topo = topology()
    done = {json.loads(x)["id"] for x in VERDICTS.open(encoding="utf-8")} if VERDICTS.exists() else set()
    todo = [x for x in cards if x["id"] not in done][: max(0, J.CAP - len(done))]
    print(f"done {len(done)}, to ask {len(todo)} with {J.MODEL}/{J.EFFORT}; topology for {len(topo)} cards", flush=True)
    lock = threading.Lock()

    def one(card: dict) -> str:
        extra = topo.get((card["code"], card["date"]), "题材拓扑（D 日）：无数据")
        text_in = f"市场环境（截至当日收盘）：{card['market']}\n\n个股卡片：\n{card['card']}\n{extra}"
        err = ""
        for attempt in range(3):
            try:
                t0 = time.perf_counter()
                text = llm.ask(text_in, model=J.MODEL, effort=J.EFFORT, system=SYSTEM)
                row = {"id": card["id"], "model": J.MODEL, "effort": J.EFFORT, "sec": round(time.perf_counter() - t0, 1),
                       "topo": extra, **J._parse(text)}
                with lock, VERDICTS.open("a", encoding="utf-8") as fh:
                    fh.write(json.dumps(row, ensure_ascii=False) + "\n")
                return "ok"
            except (ValueError, json.JSONDecodeError) as exc:
                err = f"parse: {exc}"
            except Exception as exc:  # network / rate limit: back off and retry
                err = str(exc)[:200]
                time.sleep(10 * (attempt + 1))
        return f"fail {card['id']}: {err}"

    n = 0
    with ThreadPoolExecutor(J.WORKERS) as pool:
        for f in as_completed([pool.submit(one, x) for x in todo]):
            n += 1
            r = f.result()
            if r != "ok" or n % 25 == 0:
                print(f"[{n}/{len(todo)}] {r}", flush=True)


def _pair_t(a: dict[str, float], b: dict[str, float]) -> tuple[float, float, int]:
    d = np.array([b[k] - a[k] for k in a if k in b and np.isfinite(b[k] - a[k])])
    return float(d.mean()), float(d.mean() / (d.std(ddof=1) / np.sqrt(len(d)))) if len(d) > 2 else float("nan"), len(d)


def score() -> None:
    cards = pl.read_ndjson(J.CARDS).select("id", "date", "code")
    df = cards
    for tag, path in (("anon", J.VERDICTS), ("pit", P.VERDICTS), ("topo", VERDICTS)):
        v = pl.read_ndjson(path).select("id", c("p").alias(f"p_{tag}"), c("exp").alias(f"exp_{tag}"), c("action").alias(f"act_{tag}"))
        df = df.join(v, on="id")
    df = df.join(J._labels(), on=["code", "date"], how="left")
    print(f"cards answered by all three variants: {df.height}")
    res: dict[str, object] = {"n": df.height}
    for y in ("f5_ex", "f10_ex"):
        print(f"--- {y} (percent, excess over the same-day tradable mean)")
        daily = {}
        for tag in ("anon", "pit", "topo"):
            m, t = J._ic(df, f"p_{tag}", y)
            daily[tag] = P._daily_ic(df, f"p_{tag}", y)
            g = df.group_by(f"act_{tag}").agg(c(y).mean().alias("mean"), pl.len()).sort(f"act_{tag}")
            acts = {a: (round(mm, 2), n) for a, mm, n in g.iter_rows()}
            spread = acts.get("buy", (np.nan,))[0] - acts.get("avoid", (np.nan,))[0]
            res[f"{y}:{tag}"] = {"ic": m, "t": t, "by_action": acts, "buy_minus_avoid": spread}
            print(f"  {tag:5s} rank IC(p) {m:+.3f} (t {t:+.2f}); buy−avoid {spread:+.2f}; {acts}")
        for a, b in (("anon", "topo"), ("anon", "pit"), ("pit", "topo")):
            m, t, n = _pair_t(daily[a], daily[b])
            res[f"{y}:{b}-{a}"] = {"mean": m, "t": t, "days": n}
            print(f"  per-day IC {b} − {a}: {m:+.3f} (t {t:+.2f}, {n} days)")
        for col in ("mom_20", "rev_5"):
            m, t = J._ic(df, col, y)
            res[f"{y}:{col}"] = {"ic": m, "t": t}
            print(f"  factor {col:6s} rank IC {m:+.3f} (t {t:+.2f})")
    corr = {f"{a}~{b}": float(np.corrcoef(df[f"p_{a}"].to_numpy(), df[f"p_{b}"].to_numpy())[0, 1])
            for a, b in (("anon", "topo"), ("anon", "pit"), ("pit", "topo"))}
    res["corr_p"] = corr
    print("  corr of p:", {k: round(v, 2) for k, v in corr.items()})
    json.dump(res, open(LAB / "judge_topo.json", "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    df.write_parquet(LAB / "judge_topo_scored.parquet")


if __name__ == "__main__":
    {"ask": ask_all, "score": score, "show": lambda: print(*list(topology().values())[:5], sep="\n")}[sys.argv[1] if len(sys.argv) > 1 else "score"]()
