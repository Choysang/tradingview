"""Writes the synthetic fixtures of the fusion page (apps/web/src/pages/bf/fusion/fixtures). Run once; the test checks them against the contract."""
from __future__ import annotations

import json
from pathlib import Path

OUT = Path(__file__).resolve().parents[2] / "apps" / "web" / "src" / "pages" / "bf" / "fusion" / "fixtures"
SHA = "a" * 64
PRE = [{"key": k, "ledger_id": i, "document": d, "sha256": SHA} for k, i, d in (
    ("main", "E036/E040", "docs/research/2026-11-01-round7-plan.md"), ("h10", "E065", "docs/research/2026-11-07-batch1-preregistration.md"),
    ("monitor", "E068", "docs/research/2026-11-07-batch2-preregistration.md"), ("x20", "E085", "docs/research/2026-11-08-batch4-preregistration.md"),
    ("os20", "E081", "docs/research/2026-11-08-os20-shock-rebound-preregistration.md"))]

status = {
    "score_name": "fusion_static40_safe", "implementation": "fusion-static40-safe-v1",
    "current": {"rows": 400, "dates": 4, "first_date": "2026-11-03", "last_date": "2026-11-06"},
    "asof": {"rows": 200, "dates": 2, "first_date": "2026-11-05", "last_date": "2026-11-06"},
    "last_run": {"run_id": "abc123def456", "written_on": "2026-11-06", "status": "done", "code": None, "detail": None, "session": "2026-11-06",
                 "seconds": "612.4", "peak_memory_mb": "1750.0"},
    "books": [{"key": "main", "last_date": "2026-11-06", "rows": 2}, {"key": "h10", "last_date": "2026-11-06", "rows": 2}],
    "frozen_data": [{"file": "factor_expressions.json", "sha256": SHA}, {"file": "level_safety.json", "sha256": SHA}], "preregistrations": PRE}

scores = {"date": "2026-11-06", "kind": "asof", "available_dates": ["2026-11-05", "2026-11-06"], "rule": "static40, level-safe, main board",
          "rows": [{"rank": i + 1, "code": f"60{i:04d}", "display_name": None if i == 2 else f"Name {i}", "score": f"{0.3 - i * 0.01:.6f}"} for i in range(5)]}


def book(key: str, hold: int, w: str) -> dict:
    rows = [{"date": d, "gate_w": w, "new_buys_allowed": 1, "try_buy": ["600513"] if i else [], "offer_top10": ["600513", "603937"],
             "holdings": ["002632", "603937"], "nav": nav, "ungated_nav": "156.8422", "nav_since_freeze_pct": None if i == 0 else "0.458",
             "scores_sha256_16": "2f4e9a18f9645597"} for i, (d, nav) in enumerate((("2026-11-05", "168.1000"), ("2026-11-06", "168.3577")))]
    return {"key": key, "label": f"Book {key}", "rule": f"hold {hold}, 20 slots, top 40", "ledger_id": "E036/E040" if key == "main" else "E065", "rows": rows}


books = {"books": [book("main", 5, "0.2500"), book("h10", 10, "1.0000")]}


def head(key: str, ledger: str, doc: str, summary: dict) -> dict:
    return {"key": key, "label": f"Experiment {key}", "ledger_id": ledger, "document": doc, "document_sha256": SHA, "summary": summary}


experiments = {"experiments": [
    {**head("monitor", "E068", "docs/research/2026-11-07-batch2-preregistration.md", {"decision_dates": 2, "unflagged": 1, "mean_ic": "0.0392", "nw_t": None, "remaining": 99}),
     "rows": [{"date": "2026-11-02", "n": 2477, "ic": "0.0222", "decile_spread": "-0.184", "top_ex": "-0.002", "top_sv": "0.411", "flagged": False, "coverage_fail_days": []},
              {"date": "2026-11-03", "n": 2148, "ic": "0.0423", "decile_spread": "0.325", "top_ex": "0.001", "top_sv": "0.181", "flagged": True, "coverage_fail_days": ["2026-11-05"]}]},
    {**head("x20", "E085", "docs/research/2026-11-08-batch4-preregistration.md", {}),
     "rows": [{"date": "2026-11-03", "x20": "0.999059", "ew": "0.999297", "excess_pct": "-0.024", "n_x20": 1982, "n_ew": 2477, "trade_day": True, "turnover_x20": "0.5", "turnover_ew": "0.5"},
              {"date": "2026-11-04", "x20": "1.019548", "ew": "1.019013", "excess_pct": "0.052", "n_x20": 1982, "n_ew": 2477, "trade_day": False, "turnover_x20": None, "turnover_ew": None}]},
    {**head("os20", "E081", "docs/research/2026-11-08-os20-shock-rebound-preregistration.md", {"dates": 1, "ex_gap": "-3.129", "p_gap": "1.0"}),
     "rows": [{"date": "2026-11-02", "mkt20": "-7.563", "flagged": False, "coverage_fail_days": [], "groups": [
         {"group": "OS20_GAP", "n": 128, "ex": "-3.129", "raw": "-4.117"}, {"group": "OS20_NOGAP", "n": 119, "ex": "-1.428", "raw": "-2.247"},
         {"group": "OS5_GAP", "n": 106, "ex": None, "raw": None}]}]}]}

empty = {"status": {**status, "current": {"rows": 0, "dates": 0, "first_date": None, "last_date": None},
                    "asof": {"rows": 0, "dates": 0, "first_date": None, "last_date": None}, "last_run": None,
                    "books": [{"key": "main", "last_date": None, "rows": 0}, {"key": "h10", "last_date": None, "rows": 0}]},
         "scores": {"date": None, "kind": None, "available_dates": [], "rule": "static40, level-safe, main board", "rows": []},
         "books": {"books": [{**book("main", 5, "1"), "rows": []}, {**book("h10", 10, "1"), "rows": []}]}}

FILES = {"status": (status, "FusionStatus"), "scores": (scores, "FusionScores"), "books": (books, "FusionBooks"), "experiments": (experiments, "FusionExperiments"),
         "status-empty": (empty["status"], "FusionStatus"), "scores-empty": (empty["scores"], "FusionScores"), "books-empty": (empty["books"], "FusionBooks")}

if __name__ == "__main__":
    OUT.mkdir(parents=True, exist_ok=True)
    for name, (doc, _) in FILES.items():
        (OUT / f"{name}.json").write_text(json.dumps(doc, ensure_ascii=False, indent=1) + "\n", encoding="utf-8", newline="\n")
    (OUT / "index.json").write_text(json.dumps({n: d for n, (_, d) in FILES.items()}, indent=1) + "\n", encoding="utf-8", newline="\n")
    print("written", len(FILES))
