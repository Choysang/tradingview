"""Experiment ledger: every method tried, kept whether it worked or not, with the market it was tried in and
what would make it worth trying again. A method that fails in one regime may work in the next, so nothing
is deleted; a later run of the same method is a new entry that names the earlier one in `supersedes`.

    python ledger.py add '<json object>'   # append one entry (id and date filled in when missing)
    python ledger.py render                # rewrite docs/research/ledger/README.md from the JSONL

Entry fields: id, date, round, track (llm | factor | mining | fusion | rl | traditional | rotation | timing |
regime | theme | style | hedge | data | hot_money | pattern), name, hypothesis, config, results ({period: text}), verdict
(works | fails | mixed | inconclusive), why, regime (the market it was tested in), reactivate_when,
artifacts (paths), supersedes (ids).
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DIR = ROOT / "docs" / "research" / "ledger"
FILE = DIR / "experiments.jsonl"
VERDICTS = ("works", "fails", "mixed", "inconclusive")


def load() -> list[dict]:
    return [json.loads(x) for x in FILE.open(encoding="utf-8")] if FILE.exists() else []


def add(entry: dict) -> dict:
    rows = load()
    if entry.get("verdict") not in VERDICTS:
        raise SystemExit(f"verdict must be one of {VERDICTS}")
    entry.setdefault("id", f"E{len(rows) + 1:03d}")
    entry.setdefault("date", time.strftime("%Y-%m-%d"))
    if any(r["id"] == entry["id"] for r in rows):
        raise SystemExit(f"id {entry['id']} exists; entries are append-only, use a new id with supersedes")
    DIR.mkdir(parents=True, exist_ok=True)
    with FILE.open("a", encoding="utf-8", newline="\n") as fh:
        fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
    return entry


def render() -> None:
    rows = load()
    out = ["# 实验台账", "", "> 由 `scripts/lab/ledger.py render` 生成，不要手改；条目只追加（`experiments.jsonl`）。",
           "> 失败的方法也记录：它失败时的市场环境，以及什么情况下值得再试。",
           "> 期间口径（2026-11-02 起）：2026-07-01~10-30 统称 DEV（开发集；旧条目里的 SELECT = DEV_A，VERIFY = DEV_B），"
           "已被反复查看，不是测试集。真正的样本外是 OOS：2026-11-02 起由冻结版本前向记录的决策。", ""]
    by_track: dict[str, list[dict]] = {}
    for r in rows:
        by_track.setdefault(r["track"], []).append(r)
    counts = {v: sum(r["verdict"] == v for r in rows) for v in VERDICTS}
    out += [f"共 {len(rows)} 条：有效 {counts['works']}，无效 {counts['fails']}，分时期有效 {counts['mixed']}，不确定 {counts['inconclusive']}。", ""]
    for track, items in by_track.items():
        out += [f"## {track}", "", "| id | 日期 | 方法 | 结果 | 结论 | 原因 | 何时再试 |", "|---|---|---|---|---|---|---|"]
        for r in items:
            res = "；".join(f"{k} {v}" for k, v in r.get("results", {}).items())
            cell = lambda s: str(s or "").replace("|", "/").replace("\n", " ")  # noqa: E731
            out.append(f"| {r['id']} | {r['date']} | {cell(r['name'])} | {cell(res)} | {r['verdict']} | {cell(r.get('why'))} | {cell(r.get('reactivate_when'))} |")
        out.append("")
    (DIR / "README.md").write_text("\n".join(out), encoding="utf-8", newline="\n")
    print(f"rendered {len(rows)} entries")


if __name__ == "__main__":
    if sys.argv[1:2] == ["add"]:
        print(add(json.loads(sys.argv[2]))["id"])
    elif sys.argv[1:2] == ["render"]:
        render()
    else:
        raise SystemExit(__doc__)
