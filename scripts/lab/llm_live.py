"""Forward-only LLM judgement with web search (lab venv). The only honest test of a model that can
read today's news: decide after the close of day D using the web, record it, score it 5 trading days
after the next open. Never run it on past days (search results would leak later information).

`decide`: candidates for the last panel day D = the top 30 names by a fusion score plus 10 random
tradable controls (seed = D). Each call gets the real code and name, the recent bars and the market
state, and is asked to search for news (company, sector, policy) published up to now. Verdicts go to
.work/lab/live/decisions.jsonl with timestamps.
`score`: for every decision whose 5-day window has closed in the current panel, the realised return
(next open -> open 5 days later) and its excess over the tradable mean.
Env: LIVE_SCORE (fusion_static40.pt), LIVE_TOP (30), LIVE_CONTROLS (10), LIVE_MODEL, LIVE_EFFORT.
"""
from __future__ import annotations

import json
import os
import random
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

import numpy as np
import polars as pl
import torch

import llm
import tengine as E

LIVE = E.LAB / "live"
LIVE.mkdir(exist_ok=True)
DECISIONS = LIVE / "decisions.jsonl"
MODEL, EFFORT = os.environ.get("LIVE_MODEL", "gpt-6.1-terra"), os.environ.get("LIVE_EFFORT", "high")

SYSTEM = """你是一名经验丰富的 A 股交易员，可以联网搜索。今天是 {today}。你要判断一只股票在下一个交易日开盘买入、
持有 5 个交易日后开盘卖出，能否跑赢全市场可交易股票的平均收益（往返成本约 0.2%）。
请先搜索：该公司最近两周的公告和新闻、所属行业或题材的最新动态、相关政策，以及当前 A 股整体情绪。
只使用发布日期不晚于今天的信息，并结合下面给出的行情数据作判断。
只输出一个 JSON 对象，不要输出其他文字：
{{"action": "buy" | "watch" | "avoid", "p_outperform": 0-100 的整数, "expected_excess_pct": 数字,
 "reason": "不超过 80 个汉字", "news": [{{"date": "YYYY-MM-DD", "title": "...", "url": "..."}}]}}"""


def _bars(rows: pl.DataFrame) -> str:
    out = []
    for r in rows.tail(20).iter_rows(named=True):
        turn = r["amount"] / r["float_cap"] * 100 if r["float_cap"] else float("nan")
        flag = "涨停" if r["is_limit_up"] else "跌停" if r["is_limit_down"] else ""
        out.append(f"{r['date']} 收{r['close']:.2f} {r['pct_chg']:+.2f}% 换手{turn:.2f}% {flag}".rstrip())
    return "\n".join(out)


def decide() -> None:
    d = E.Data()
    t = d.T - 1
    day = d.dates[t]
    score = torch.load(E.LAB / os.environ.get("LIVE_SCORE", "fusion_static40.pt"))
    s = torch.where(d.tradable[t], score[t], -torch.inf)
    top = torch.topk(s, int(os.environ.get("LIVE_TOP", "30"))).indices.tolist()
    rng = random.Random(day.toordinal())
    pool = [j for j in torch.nonzero(d.tradable[t]).flatten().tolist() if j not in set(top)]
    controls = rng.sample(pool, int(os.environ.get("LIVE_CONTROLS", "10")))
    p = pl.read_parquet(E.LAB / "panel.parquet").filter(pl.col("date") > d.dates[max(0, t - 30)]).sort("code", "date")
    idx = d.index_close.numpy()
    market = f"中证全指 {day} 收盘 {idx[t]:.2f}，近5日 {(idx[t] / idx[t - 5] - 1) * 100:+.1f}%，近20日 {(idx[t] / idx[t - 20] - 1) * 100:+.1f}%，近60日 {(idx[t] / idx[t - 60] - 1) * 100:+.1f}%"
    done = {json.loads(x)["id"] for x in DECISIONS.open(encoding="utf-8")} if DECISIONS.exists() else set()
    jobs = []
    for j, group in [(j, "top") for j in top] + [(j, "control") for j in controls]:
        code = d.codes[j]
        rid = f"{day.isoformat()}_{code}"
        if rid in done:
            continue
        rows = p.filter(pl.col("code") == code)
        last = rows.tail(1).row(0, named=True)
        prompt = (f"市场：{market}\n股票：{code} {last['name']}，行业 {last['industry']}，流通市值约 {last['float_cap']:.0f} 亿元\n"
                  f"近20个交易日：\n{_bars(rows)}")
        jobs.append((rid, code, group, prompt, float(score[t, j])))
    system = SYSTEM.format(today=datetime.now().strftime("%Y-%m-%d"))
    print(f"decision day {day}: {len(jobs)} calls ({MODEL}/{EFFORT}, web search)", flush=True)

    def one(job):
        rid, code, group, prompt, sc = job
        for attempt in range(3):
            try:
                text = llm.ask(prompt, model=MODEL, effort=EFFORT, system=system, extra={"tools": [{"type": "web_search"}]})
                obj = json.loads(text[text.find("{"): text.rfind("}") + 1])
                return {"id": rid, "decision_date": day.isoformat(), "code": code, "group": group, "score": sc,
                        "asked_at": datetime.now().isoformat(timespec="seconds"), "model": MODEL, **obj}
            except Exception as exc:
                err = str(exc)[:200]
                time.sleep(5 * (attempt + 1))
        return {"id": rid, "error": err}

    with ThreadPoolExecutor(4) as ex:
        for row in ex.map(one, jobs):
            if "error" in row:
                print("fail", row, flush=True)
                continue
            with DECISIONS.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    print("done", flush=True)


def score() -> None:
    if not DECISIONS.exists():
        print("no decisions yet")
        return
    d = E.Data()
    y = d.label(5)
    raw = (E._shift(d.f["open"], -6) / E._shift(d.f["open"], -1) - 1) * 100
    rows = []
    for x in (json.loads(v) for v in DECISIONS.open(encoding="utf-8")):
        t = d.dates.index(datetime.fromisoformat(x["decision_date"]).date()) if datetime.fromisoformat(x["decision_date"]).date() in d.date_index else None
        if t is None or t + 6 >= d.T:
            continue
        j = d.codes.index(x["code"])
        rows.append({**{k: x.get(k) for k in ("decision_date", "code", "group", "action", "p_outperform", "score")},
                     "ret5": float(raw[t, j]), "ex5": float(y[t, j])})
    if not rows:
        print("no decision has a complete 5-day window yet")
        return
    df = pl.DataFrame(rows)
    print(df.group_by("group", "action").agg(pl.len(), pl.col("ex5").mean(), pl.col("ret5").mean()).sort("group", "action"))
    print("spearman(p, ex5):", df.select(pl.corr("p_outperform", "ex5", method="spearman")).item())


if __name__ == "__main__":
    {"decide": decide, "score": score}[sys.argv[1] if len(sys.argv) > 1 else "score"]()
