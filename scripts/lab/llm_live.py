"""Forward-only LLM judgement with web search (lab venv). The only honest test of a model that can
read today's news: decide after the close of day D using the web, record it, score it 5 trading days
after the next open. Never run it on past days (search results would leak later information).

`prepare` (LAB_UNIVERSE=main): candidates for the last panel day D = the top 30 names by the fusion score plus 10
random tradable controls (seed = D). Each job holds the real code and name, the recent bars and the market state.
The jobs file goes to the server, where `llm_ask.py` calls the model with the server's key (E092). No key and no
network here. The decisions come back to E.LAB/live/decisions.jsonl, so the main-board log stays apart from the E002
batch in .work/lab/live/decisions.jsonl, which holds other boards.
`score` (LAB_UNIVERSE=main for the E092 log; `score --e002` without it for the older batch): for every decision made in time whose 5-day
window has closed in the current panel, the realised return (next open -> open 5 days later), its excess over the
tradable mean, and the pre-registered verdict from llm_eval.py.
Env: LIVE_SCORE (fusion_static40_safe.pt), LIVE_TOP (30), LIVE_CONTROLS (10).
"""
from __future__ import annotations

import json
import math
import os
import random
import sys
from datetime import datetime

import polars as pl
import torch

from llm_ask import LIVE_EFFORT, LIVE_MODEL, deadline_after, read_rows
from llm_eval import evaluate, scorable
import tengine as E

LIVE = E.LAB / "live"
LIVE.mkdir(exist_ok=True)
DECISIONS = LIVE / "decisions.jsonl"
JOBS = LIVE / "jobs"

SYSTEM = """你是一名经验丰富的 A 股交易员，可以联网搜索。今天是 {today}。你要判断一只股票在下一个交易日开盘买入、
持有 5 个交易日后开盘卖出，能否跑赢沪深主板可交易股票的平均收益（往返成本约 0.25%）。
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


def prepare() -> None:
    if E.UNIVERSE != "main":
        raise SystemExit("prepare is main-board only: set LAB_UNIVERSE=main")
    d = E.Data()
    t = d.T - 1
    day = d.dates[t]
    score = torch.load(E.LAB / os.environ.get("LIVE_SCORE", "fusion_static40_safe.pt"))
    if tuple(score.shape) != (d.T, d.N):
        raise SystemExit(f"score shape {tuple(score.shape)} does not match the panel {(d.T, d.N)}")
    n_top = int(os.environ.get("LIVE_TOP", "30"))
    ok = d.tradable[t] & torch.isfinite(score[t])
    if int(ok.sum()) < n_top:
        raise SystemExit(f"{day}: only {int(ok.sum())} tradable names have a score")
    top = torch.topk(torch.where(ok, score[t], -torch.inf), n_top).indices.tolist()
    rng = random.Random(day.toordinal())
    pool = [j for j in torch.nonzero(d.tradable[t]).flatten().tolist() if j not in set(top)]
    controls = rng.sample(pool, int(os.environ.get("LIVE_CONTROLS", "10")))
    p = pl.read_parquet(E.BASE / "panel.parquet").filter(pl.col("date") > d.dates[max(0, t - 30)]).sort("code", "date")
    idx = d.index_close.numpy()  # benchmark_sh under LAB_UNIVERSE=main
    if not all(map(math.isfinite, (idx[t], idx[t - 5], idx[t - 20], idx[t - 60]))):
        raise SystemExit(f"{day}: the benchmark has no close for the market line of the prompt")
    market = f"上证指数 {day} 收盘 {idx[t]:.2f}，近5日 {(idx[t] / idx[t - 5] - 1) * 100:+.1f}%，近20日 {(idx[t] / idx[t - 20] - 1) * 100:+.1f}%，近60日 {(idx[t] / idx[t - 60] - 1) * 100:+.1f}%"
    jobs = []
    for j, group in [(j, "top") for j in top] + [(j, "control") for j in controls]:
        code = d.codes[j]
        rows = p.filter(pl.col("code") == code)
        last = rows.tail(1).row(0, named=True)
        industry = f"行业 {last['industry']}，" if last["industry"] else ""  # the panel has no industry for some names
        prompt = (f"市场：{market}\n股票：{code} {last['name']}，{industry}流通市值约 {last['float_cap']:.0f} 亿元\n"
                  f"近20个交易日：\n{_bars(rows)}")
        jobs.append({"id": f"{day.isoformat()}_{code}", "code": code, "group": group, "score": float(score[t, j]), "prompt": prompt})
    payload = {"decision_date": day.isoformat(), "not_after": deadline_after(day).isoformat(), "model": LIVE_MODEL, "effort": LIVE_EFFORT,
               "system": SYSTEM.format(today=datetime.now().strftime("%Y-%m-%d")), "jobs": jobs}
    JOBS.mkdir(exist_ok=True)
    path = JOBS / f"jobs_{day.isoformat()}.json"
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)
    print(path, flush=True)


def score() -> None:
    e002 = "--e002" in sys.argv
    if (E.UNIVERSE == "main") == e002:
        raise SystemExit("use LAB_UNIVERSE=main for the E092 log, or --e002 without it for the older all-board batch")
    logged = read_rows(DECISIONS)
    if not logged:
        print("no decisions yet")
        return
    d = E.Data()
    y = d.label(5)
    raw = (E._shift(d.f["open"], -6) / E._shift(d.f["open"], -1) - 1) * 100
    rows = []
    for x in scorable(logged):
        t = d.dates.index(datetime.fromisoformat(x["decision_date"]).date()) if datetime.fromisoformat(x["decision_date"]).date() in d.date_index else None
        if t is None or t + 6 >= d.T or x.get("p_outperform") is None:
            continue
        j = d.codes.index(x["code"])
        rows.append({**{k: x.get(k) for k in ("decision_date", "code", "group", "action", "score")},
                     "p_outperform": float(x["p_outperform"]), "ret5": float(raw[t, j]), "ex5": float(y[t, j])})
    if not rows:
        print("no decision has a complete 5-day window yet")
        return
    df = pl.DataFrame(rows).filter(pl.col("ex5").is_finite())
    print(df.group_by("group", "action").agg(pl.len(), pl.col("ex5").mean(), pl.col("ret5").mean()).sort("group", "action"))
    print("spearman(p, ex5):", df.select(pl.corr("p_outperform", "ex5", method="spearman")).item())
    print("E002 under the E092 rules:" if e002 else "E092 verdict:", json.dumps(evaluate(rows), ensure_ascii=False))


if __name__ == "__main__":
    {"prepare": prepare, "score": score}[sys.argv[1] if len(sys.argv) > 1 else "score"]()
