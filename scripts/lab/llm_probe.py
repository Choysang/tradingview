"""Contamination probe: does the model already know how the A-share market moved after 2026-06?

If it can name the index path or events of 2026-07..2026-10, its judgements on cards from that
period could lean on memory instead of the card, so the pilot would need extra masking.
Answers go to .work/lab/llm_probe.json together with the true monthly index returns.
"""
from __future__ import annotations

import json
import sys
from datetime import date

import harness as H  # noqa: F401  (sets sys.path for backend imports)
import llm
from harness import LAB, c, ctx

QUESTION = """Answer from your own knowledge only, without browsing. Be honest about uncertainty.
1. What is your training data cutoff (month and year)?
2. For the China A-share market, give your best estimate of the monthly percentage change of the
   Shanghai Composite index in July, August, September and October 2026. If you do not know, say "unknown"
   for that month instead of guessing.
3. List any specific A-share market events you know of from June 2026 onward (policy, crashes, rallies),
   or say "none known".
Reply as JSON: {"cutoff": str, "months": {"2026-07": str, "2026-08": str, "2026-09": str, "2026-10": str},
"events": [str], "confidence": "none|low|medium|high"}"""


def monthly_index() -> dict[str, float]:
    b = ctx().benchmark.filter(c("date") >= date(2026, 6, 1)).sort("date")
    out, prev = {}, None
    for (month,), g in b.group_by(c("date").dt.strftime("%Y-%m"), maintain_order=True):
        last = float(g["close"][-1])
        if prev is not None:
            out[month] = round((last / prev - 1) * 100, 2)
        prev = last
    return out


def main() -> None:
    truth = monthly_index()
    answers = {}
    for model, effort, extra in (("gpt-6.1-terra", "high", None), ("gpt-6-luna", "max", {"service_tier": "priority"})):
        try:
            answers[model] = llm.ask(QUESTION, model=model, effort=effort, extra=extra)
        except Exception as exc:  # report and keep the other model's answer
            answers[model] = f"error: {exc}"
        print(model, answers[model][:1500], flush=True)
    print("truth (中证全指 monthly %):", truth)
    (LAB / "llm_probe.json").write_text(json.dumps({"truth": truth, "answers": answers}, ensure_ascii=False, indent=1), encoding="utf-8")


if __name__ == "__main__":
    sys.exit(main())
