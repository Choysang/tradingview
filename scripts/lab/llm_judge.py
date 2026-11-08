"""LLM direct-judgement pilot: can a frontier model pick A-share stocks from an anonymized chart card?

Design (fixed before any call is made)
- 20 trading days spread over 2026-07-01 .. the last day with a full 5-day forward window.
- Each day 30 tradable stocks drawn at random, 10 from each float-cap tercile (seed 20261101).
- The card has no code, name, date or absolute price: prices are rebased to 100, caps are buckets.
  The contamination probe (llm_probe.py) found neither model claims any knowledge after mid 2024.
- One call per card, gpt-6.1-terra, reasoning effort high. Hard cap 600 calls (env JUDGE_CAP).
- Label: buy at the next open, sell at the open 5 trading days later; excess over the same-day mean of
  all tradable stocks. A 10-day label is kept too. Rows whose next open is a sealed limit-up are flagged.

Stages: `cards` builds .work/lab/judge_cards.jsonl, `ask` fills judge_verdicts.jsonl (resumable),
`score` prints rank IC, buy/avoid spreads and the same numbers for plain factors on the same rows.
"""
from __future__ import annotations

import json
import os
import random
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date

import numpy as np
import polars as pl

import harness as H
import llm
from harness import CODE, LAB, c, ctx, tradable

CARDS = LAB / "judge_cards.jsonl"
VERDICTS = LAB / "judge_verdicts.jsonl"
START = date(2026, 7, 1)
DAYS, PER_DAY, SEED = 20, 30, 20261101
CAP = int(os.environ.get("JUDGE_CAP", "600"))
MODEL, EFFORT = os.environ.get("JUDGE_MODEL", "gpt-6.1-terra"), os.environ.get("JUDGE_EFFORT", "high")
WORKERS = int(os.environ.get("JUDGE_WORKERS", "6"))

SYSTEM = """你是一名经验丰富的 A 股交易员。你会收到一张匿名的个股卡片：没有代码、名称和日期，价格已重定基为 100。
只依据卡片内容判断，不要猜测是哪只股票或哪一天。交易规则：T+1，次日开盘买入，持有 5 个交易日后在开盘卖出，
往返成本约 0.2%。你的目标是跑赢同一天全市场可交易股票的平均收益。
只输出一个 JSON 对象，不要输出其他文字：
{"action": "buy" | "watch" | "avoid", "p_outperform": 0-100 的整数（跑赢全市场平均的概率）,
 "expected_excess_pct": 数字（预期 5 日超额收益，百分比）, "reason": "不超过 60 个汉字的理由"}"""


def _pick_days() -> list[date]:
    dates = ctx().dates
    last = len(dates) - 1 - 6  # entry t+1, exit open t+6
    pool = [d for d in dates[: last + 1] if d >= START]
    step = (len(pool) - 1) / (DAYS - 1)
    return [pool[round(i * step)] for i in range(DAYS)]


def _market(day: date) -> str:
    k = ctx()
    b = k.benchmark.filter(c("date") <= day).sort("date")
    cl = b["close"].to_numpy()
    r = lambda n: (cl[-1] / cl[-1 - n] - 1) * 100  # noqa: E731
    ma60 = cl[-60:].mean()
    p = k.panel.filter(c("date").is_in(k.dates[max(0, k.dates.index(day) - 4): k.dates.index(day) + 1]))
    br = p.group_by("date").agg((c("pct_chg") > 0).mean().alias("up"), c("is_limit_up").sum().alias("zt"),
                                c("is_limit_down").sum().alias("dt"), c("pct_chg").mean().alias("avg")).sort("date")
    return (f"指数近5日 {r(5):+.1f}%，近20日 {r(20):+.1f}%，近60日 {r(60):+.1f}%，相对60日均线 {(cl[-1] / ma60 - 1) * 100:+.1f}%；"
            f"近5日平均上涨家数占比 {br['up'].mean() * 100:.0f}%，日均涨停 {br['zt'].mean():.0f} 家、跌停 {br['dt'].mean():.0f} 家；"
            f"当日全市场平均涨跌 {br['avg'][-1]:+.2f}%")


def _card(rows: pl.DataFrame) -> str:
    """rows: one stock's bars up to and including the day, oldest first (>= 120 bars)."""
    t = rows.tail(1).row(0, named=True)
    base = rows["close"][-61] if rows.height > 61 else rows["close"][0]
    k = 100 / base
    board = {0.1: "主板（涨跌停 10%）", 0.2: "创业板/科创板（20%）", 0.3: "北交所（30%）"}.get(round(t["limit_ratio"] or 0.1, 1), "主板")
    cap = t["float_cap"] or 0
    cap_b = "小盘（流通市值<50亿）" if cap < 50 else "中盘（50-200亿）" if cap < 200 else "大盘（>200亿）"
    lines = []
    for r in rows.tail(30).iter_rows(named=True):
        turn = (r["amount"] / cap * 100) if cap else float("nan")
        vr = r["volume"] / r["vol_ma5"] if r["vol_ma5"] else float("nan")
        flag = "涨停" if r["is_limit_up"] else "跌停" if r["is_limit_down"] else ("炸板" if r["is_broken"] else "")
        lines.append(f"{r['open'] * k:.2f} {r['high'] * k:.2f} {r['low'] * k:.2f} {r['close'] * k:.2f} {r['pct_chg']:+.2f}% "
                     f"换手{turn:.2f}% 量比{vr:.2f} {flag}".rstrip())
    close = t["close"]
    pos = lambda v: f"{(close / v - 1) * 100:+.1f}%" if v else "无"  # noqa: E731
    last250 = rows.tail(250)
    hi, lo = last250["high"].max(), last250["low"].min()
    zt20 = int(rows.tail(20)["is_limit_up"].sum())
    zt60 = int(rows.tail(60)["is_limit_up"].sum())
    turn20 = float((rows.tail(20)["amount"] / cap * 100).mean()) if cap else float("nan")
    ind = t["industry"] or "未知"
    return "\n".join([
        f"制度：{board}；行业：{ind}；{cap_b}；上市交易日约 {t['days_listed']} 天",
        "近30日日线（价格按60个交易日前收盘=100重定基；每行：开 高 低 收 涨跌幅 换手 量比 标记），最早在上：",
        *lines,
        f"长周期：近60日 {t['ret_60']:+.1f}%，近120日 {t['ret_120']:+.1f}%，近250日 {(t['ret_250'] if t['ret_250'] is not None else float('nan')):+.1f}%；"
        f"距250日最高 {(close / hi - 1) * 100:+.1f}%，距250日最低 {(close / lo - 1) * 100:+.1f}%",
        f"20日波动率 {t['volatility_20']:.2f}，20日平均换手 {turn20:.2f}%，近20日涨停 {zt20} 次，近60日涨停 {zt60} 次，当前连板 {t['limit_streak'] or 0}",
        f"收盘相对均线：MA5 {pos(t['ma5'])}，MA10 {pos(t['ma10'])}，MA20 {pos(t['ma20'])}，MA60 {pos(t['ma60'])}，MA120 {pos(t['ma120'])}，MA250 {pos(t['ma250'])}",
        f"MACD（占价格%）：DIF {t['macd_dif'] / close * 100:+.2f}，DEA {t['macd_dea'] / close * 100:+.2f}，柱 {t['macd_hist'] / close * 100:+.2f}；"
        f"KDJ：K {t['kdj_k']:.0f} D {t['kdj_d']:.0f} J {t['kdj_j']:.0f}；RSI6 {t['rsi_6']:.0f}，RSI14 {t['rsi_14']:.0f}；"
        f"布林位置 {((close - t['boll_lower']) / (t['boll_upper'] - t['boll_lower']) * 100) if t['boll_upper'] and t['boll_upper'] > t['boll_lower'] else float('nan'):.0f}%（0=下轨，100=上轨）",
    ])


def build_cards() -> None:
    k = ctx()
    rng = random.Random(SEED)
    days = _pick_days()
    dates = k.dates
    out = []
    for day in days:
        u = k.panel.filter((c("date") == day) & tradable().fill_null(False) & c("float_cap").is_not_null()).select(CODE, "float_cap")
        q = u["float_cap"].quantile(1 / 3), u["float_cap"].quantile(2 / 3)
        picked = []
        for lo, hi in ((0, q[0]), (q[0], q[1]), (q[1], float("inf"))):
            group = sorted(u.filter((c("float_cap") >= lo) & (c("float_cap") < hi))[CODE].to_list())
            picked += rng.sample(group, PER_DAY // 3)
        market = _market(day)
        i = dates.index(day)
        hist = k.panel.filter(c(CODE).is_in(picked) & (c("date") <= day) & (c("date") >= dates[max(0, i - 260)])).sort(CODE, "date")
        for code in picked:
            rows = hist.filter(c(CODE) == code)
            out.append({"id": f"{day.isoformat()}_{code}", "date": day.isoformat(), "code": code,
                        "market": market, "card": _card(rows)})
    with CARDS.open("w", encoding="utf-8") as fh:
        for row in out:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    print(f"cards {len(out)} over {len(days)} days: {days[0]}..{days[-1]}")


def _parse(text: str) -> dict:
    s, e = text.find("{"), text.rfind("}")
    obj = json.loads(text[s: e + 1])
    action = str(obj.get("action", "")).lower()
    if action not in ("buy", "watch", "avoid"):
        raise ValueError(f"bad action {action!r}")
    return {"action": action, "p": float(obj.get("p_outperform")), "exp": float(obj.get("expected_excess_pct")),
            "reason": str(obj.get("reason", ""))[:120]}


def ask_all() -> None:
    cards = [json.loads(x) for x in CARDS.open(encoding="utf-8")]
    done = {json.loads(x)["id"] for x in VERDICTS.open(encoding="utf-8")} if VERDICTS.exists() else set()
    todo = [x for x in cards if x["id"] not in done][: max(0, CAP - len(done))]
    print(f"done {len(done)}, to ask {len(todo)} (cap {CAP}) with {MODEL}/{EFFORT}", flush=True)
    lock = threading.Lock()

    def one(card: dict) -> str:
        prompt = f"市场环境（截至当日收盘）：{card['market']}\n\n个股卡片：\n{card['card']}"
        for attempt in range(3):
            try:
                t0 = time.perf_counter()
                text = llm.ask(prompt, model=MODEL, effort=EFFORT, system=SYSTEM)
                row = {"id": card["id"], "model": MODEL, "effort": EFFORT, "sec": round(time.perf_counter() - t0, 1), **_parse(text)}
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
    with ThreadPoolExecutor(WORKERS) as pool:
        for f in as_completed([pool.submit(one, x) for x in todo]):
            n += 1
            r = f.result()
            if r != "ok" or n % 25 == 0:
                print(f"[{n}/{len(todo)}] {r}", flush=True)


def _labels() -> pl.DataFrame:
    k = ctx()
    nxt = c("open").shift(-1).over(CODE)
    p = k.panel.lazy().sort(CODE, "date").with_columns(
        tradable().fill_null(False).alias("_ok"),
        ((c("open").shift(-6).over(CODE) / nxt - 1) * 100).alias("f5"),
        ((c("open").shift(-11).over(CODE) / nxt - 1) * 100).alias("f10"),
        c("open_at_limit").shift(-1).over(CODE).alias("blocked"),
    ).filter(c("_ok") & (c("date") >= START))
    p = p.with_columns(*(pl.when(c(x).is_finite()).then(c(x)).alias(x) for x in ("f5", "f10")))
    p = p.with_columns(*((c(x) - c(x).mean().over("date")).alias(f"{x}_ex") for x in ("f5", "f10")))
    return p.select(CODE, c("date").dt.strftime("%Y-%m-%d").alias("date"), "f5_ex", "f10_ex", "blocked",
                    (-c("ret_5")).alias("rev_5"), c("ret_20").alias("mom_20"), (-c("volatility_20")).alias("lowvol_20"),
                    (-c("float_cap").log()).alias("small")).collect()


def _ic(df: pl.DataFrame, col: str, y: str) -> tuple[float, float]:
    ics = []
    for _, g in df.group_by("date"):
        g = g.drop_nulls([col, y])
        if g.height >= 10:
            ics.append(np.corrcoef(g[col].rank().to_numpy(), g[y].rank().to_numpy())[0, 1])
    a = np.array([x for x in ics if np.isfinite(x)])
    return float(a.mean()), float(a.mean() / (a.std(ddof=1) / np.sqrt(len(a)))) if len(a) > 2 else float("nan")


def score() -> None:
    v = pl.read_ndjson(VERDICTS)
    cards = pl.read_ndjson(CARDS).select("id", "date", "code")
    df = v.join(cards, on="id").join(_labels(), on=["code", "date"], how="left")
    df = df.with_columns(pl.when(c("action") == "buy").then(1).when(c("action") == "avoid").then(-1).otherwise(0).alias("act"))
    print(f"verdicts {v.height}; with labels {df.filter(c('f5_ex').is_not_null()).height}; blocked next open {df['blocked'].sum()}")
    print("action counts", dict(df.group_by("action").len().iter_rows()))
    for y in ("f5_ex", "f10_ex"):
        print(f"--- {y} (percent, excess over same-day tradable mean)")
        for col in ("p", "exp", "act", "rev_5", "mom_20", "lowvol_20", "small"):
            m, t = _ic(df, col, y)
            print(f"  rank IC {col:10s} {m:+.3f} (t {t:+.2f})")
        g = df.group_by("action").agg(c(y).mean().alias("mean"), c(y).median().alias("median"), pl.len()).sort("action")
        print("  by action:", [(a, round(m, 2), round(md, 2), n) for a, m, md, n in g.iter_rows()])
    tokens = [json.loads(x) for x in (LAB / "llm_usage.jsonl").open(encoding="utf-8")] if (LAB / "llm_usage.jsonl").exists() else []
    mine = [x for x in tokens if x["model"] == MODEL]
    print(f"usage rows for {MODEL}: {len(mine)}; input {sum(x['usage'].get('input_tokens', 0) for x in mine)}, "
          f"output {sum(x['usage'].get('output_tokens', 0) for x in mine)} tokens")
    df.write_parquet(LAB / "judge_scored.parquet")


if __name__ == "__main__":
    stage = sys.argv[1] if len(sys.argv) > 1 else "score"
    {"cards": build_cards, "ask": ask_all, "score": score}[stage]()
