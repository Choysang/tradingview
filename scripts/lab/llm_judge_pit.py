"""LLM judgement with point-in-time news (backend venv): the round-5 pilot removed news to avoid leakage;
here the same 600 cards get the stock's identity and only what was public by the decision day D.

The OpenAI web_search tool cannot be limited to dates before D, so it is not used. Instead each card gets:
- code, name and the date D (the contamination probe found no model knowledge of 2026-07..10 markets);
- titles of the company's cninfo announcements dated D-30..D (newest first, at most 25), from pit_news.py;
- the titles of CCTV 新闻联播 on D-1 and D (policy backdrop; broadcast at 19:00, before the next open).
Same model, effort, label and rules as llm_judge.py; verdicts go to judge_verdicts_pit.jsonl (resumable).
`score` compares with the anonymous run card by card: rank IC of each, and the per-day IC difference with
its t over the 20 days.
Usage: python llm_judge_pit.py ask | score
"""
from __future__ import annotations

import json
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, timedelta

import numpy as np
import polars as pl

import harness as H
import llm
import llm_judge as J
import pit_news as N
from harness import LAB, c

VERDICTS = LAB / "judge_verdicts_pit.jsonl"
SYSTEM = """你是一名经验丰富的 A 股交易员。你会收到一张个股卡片（含代码、名称和决策日 D）、该公司截至 D 日的公告标题，
以及 D 日与前一日的新闻联播标题。只依据这些材料和 D 日之前已有的常识判断，不要使用 D 日之后发生的任何事情。
交易规则：D 日收盘后决策，T+1，次日开盘买入，持有 5 个交易日后在开盘卖出，往返成本约 0.2%。
你的目标是跑赢同一天全市场可交易股票的平均收益。
只输出一个 JSON 对象，不要输出其他文字：
{"action": "buy" | "watch" | "avoid", "p_outperform": 0-100 的整数（跑赢全市场平均的概率）,
 "expected_excess_pct": 数字（预期 5 日超额收益，百分比）, "reason": "不超过 60 个汉字的理由"}"""
MAX_ANN = 25


def names() -> dict[tuple[str, str], str]:
    p = pl.read_parquet(LAB / "panel.parquet", columns=["code", "date", "name"])  # the lab export, point in time
    p = p.filter(c("date") >= J.START).select("code", c("date").dt.strftime("%Y-%m-%d").alias("date"), "name")
    return {(a, b): n for a, b, n in p.iter_rows()}


def prompt(card: dict, name: str) -> str:
    day = date.fromisoformat(card["date"])
    ann = sorted(N.announcements(card["code"], day), key=lambda r: r["date"], reverse=True)
    ann = [r for r in ann if r["date"] <= card["date"]][:MAX_ANN]
    tv = N.cctv(day - timedelta(days=1)) + N.cctv(day)
    lines = [f"决策日 D：{card['date']}（收盘后）", f"股票：{card['code']} {name}", "",
             f"市场环境（截至 D 日收盘）：{card['market']}", "", "个股卡片：", card["card"], "",
             f"近 30 日公告标题（{len(ann)} 条，新的在上）："]
    lines += [f"- {r['date']} {r['title']}" for r in ann] or ["- 无"]
    lines += ["", "新闻联播标题（D 日与前一日）："] + ([f"- {t}" for t in tv[:40]] or ["- 无"])
    return "\n".join(lines)


def ask_all() -> None:
    cards = [json.loads(x) for x in J.CARDS.open(encoding="utf-8")]
    nm = names()
    done = {json.loads(x)["id"] for x in VERDICTS.open(encoding="utf-8")} if VERDICTS.exists() else set()
    todo = [x for x in cards if x["id"] not in done][: max(0, J.CAP - len(done))]
    print(f"done {len(done)}, to ask {len(todo)} with {J.MODEL}/{J.EFFORT}", flush=True)
    lock = threading.Lock()

    def one(card: dict) -> str:
        text_in = prompt(card, nm.get((card["code"], card["date"]), ""))
        err = ""
        for attempt in range(3):
            try:
                t0 = time.perf_counter()
                text = llm.ask(text_in, model=J.MODEL, effort=J.EFFORT, system=SYSTEM)
                row = {"id": card["id"], "model": J.MODEL, "effort": J.EFFORT, "sec": round(time.perf_counter() - t0, 1),
                       "prompt_chars": len(text_in), **J._parse(text)}
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


def _daily_ic(df: pl.DataFrame, col: str, y: str) -> dict[str, float]:
    out = {}
    for (d,), g in df.group_by(["date"]):
        g = g.drop_nulls([col, y])
        if g.height >= 10:
            out[d] = float(np.corrcoef(g[col].rank().to_numpy(), g[y].rank().to_numpy())[0, 1])
    return out


def score() -> None:
    cards = pl.read_ndjson(J.CARDS).select("id", "date", "code")
    a = pl.read_ndjson(J.VERDICTS).select("id", c("p").alias("p_anon"), c("exp").alias("exp_anon"), c("action").alias("act_anon"))
    b = pl.read_ndjson(VERDICTS).select("id", c("p").alias("p_pit"), c("exp").alias("exp_pit"), c("action").alias("act_pit"))
    df = cards.join(a, on="id").join(b, on="id").join(J._labels(), on=["code", "date"], how="left")
    print(f"paired cards {df.height}; actions anon {dict(df.group_by('act_anon').len().iter_rows())}, pit {dict(df.group_by('act_pit').len().iter_rows())}")
    res = {}
    for y in ("f5_ex", "f10_ex"):
        print(f"--- {y}")
        for col in ("p_anon", "p_pit", "exp_anon", "exp_pit", "mom_20", "rev_5"):
            m, t = J._ic(df, col, y)
            res[f"{y}:{col}"] = (m, t)
            print(f"  rank IC {col:9s} {m:+.3f} (t {t:+.2f})")
        da, dp = _daily_ic(df, "p_anon", y), _daily_ic(df, "p_pit", y)
        diff = np.array([dp[k] - da[k] for k in da if k in dp and np.isfinite(dp[k] - da[k])])
        t = float(diff.mean() / (diff.std(ddof=1) / np.sqrt(len(diff)))) if len(diff) > 2 else float("nan")
        res[f"{y}:diff"] = (float(diff.mean()), t)
        print(f"  pit − anon per-day IC {diff.mean():+.3f} (t {t:+.2f}, {len(diff)} days)")
        g = df.group_by("act_pit").agg(c(y).mean().alias("mean"), pl.len()).sort("act_pit")
        print("  pit by action:", [(x, round(m, 2), n) for x, m, n in g.iter_rows()])
    print(f"  corr(p_pit, p_anon) {np.corrcoef(df['p_pit'].to_numpy(), df['p_anon'].to_numpy())[0, 1]:+.2f}; "
          f"corr(p_pit, mom_20) {df.select(pl.corr('p_pit', 'mom_20', method='spearman')).item():+.2f}")
    json.dump(res, open(LAB / "judge_pit.json", "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    df.write_parquet(LAB / "judge_pit_scored.parquet")


if __name__ == "__main__":
    {"ask": ask_all, "score": score}[sys.argv[1] if len(sys.argv) > 1 else "score"]()
