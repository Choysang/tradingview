"""M5-E05: snapshot of candidate strategy states, the current baseline and data health.

Why: the package's M5-E05 asks for a record of unfinished gates, the current baseline, candidate states and the
input health versions, so a later cutover can show what changed. Read-only.

Usage (from pulse3/)::

    python tools/m5_candidates.py --out docs/m5
"""
from __future__ import annotations

import argparse
import json
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from pulse import config
from pulse.adapters.postgres import db
from pulse.adapters.postgres.release import PgAnalysisRelease

PERMISSIONED = ("shadow", "active")       # lifecycles that may influence anything beyond research records


def snapshot(conn) -> dict:
    inst = conn.execute(
        "SELECT d.display_name, d.role, d.method_family, d.horizon_id, i.instance_id, i.lifecycle, i.implementation_status,"
        " i.reproduction_status, i.health FROM strategy.instance i JOIN strategy.definition d USING (strategy_id, strategy_version)"
        " ORDER BY d.display_name").fetchall()
    events = conn.execute("SELECT to_lifecycle, count(*) n FROM strategy.lifecycle_event GROUP BY 1 ORDER BY 1").fetchall()
    cur = PgAnalysisRelease(conn).current("main")
    health = conn.execute(
        "SELECT DISTINCT ON (source_id, endpoint_id) source_id, endpoint_id, runtime_state, freshness_state, consecutive_failures,"
        " last_error_code, updated_at FROM ingest.source_health ORDER BY source_id, endpoint_id, updated_at DESC").fetchall()
    return {
        "taken_at": datetime.now(timezone.utc).isoformat(),
        "baseline_release": None if cur is None else {"release_id": str(cur.release_id), "decision_as_of": cur.decision_as_of.isoformat(),
                                                      "degraded": list(cur.degraded_components)},
        "instances": len(inst),
        "by_lifecycle": dict(Counter(r["lifecycle"] for r in inst)),
        "by_implementation": dict(Counter(r["implementation_status"] for r in inst)),
        "by_reproduction": dict(Counter(r["reproduction_status"] for r in inst)),
        "by_health": dict(Counter(r["health"] for r in inst)),
        "with_production_influence": [{"name": r["display_name"], "lifecycle": r["lifecycle"]} for r in inst if r["lifecycle"] in PERMISSIONED],
        "lifecycle_events": {r["to_lifecycle"]: r["n"] for r in events},
        "source_health": [{k: (v.isoformat() if hasattr(v, "isoformat") else v) for k, v in r.items()} for r in health],
        "open_gates": ["M5-E03 10-session continuity (needs sessions after 2026-11-13)",
                       "M5-E04 forward record nodes N1-N3 (docs/m5/E04_preregistration.md)",
                       "M6 cutover (needs M5 and the user's decision)"],
    }


def render(s: dict) -> str:
    b = s["baseline_release"] or {}
    lines = [f"# M5-E05 candidate and baseline snapshot ({s['taken_at']})", "",
             f"- baseline release `{b.get('release_id')}`, decision_as_of {b.get('decision_as_of')}, degraded {b.get('degraded')}",
             f"- strategy instances {s['instances']}: lifecycle {s['by_lifecycle']}",
             f"- implementation {s['by_implementation']}; reproduction {s['by_reproduction']}; health {s['by_health']}",
             f"- instances with production influence (shadow/active): {s['with_production_influence'] or 'none'}",
             f"- lifecycle events so far: {s['lifecycle_events']}", "", "## Source health (latest per endpoint)"]
    lines += [f"- {h['source_id']}/{h['endpoint_id']}: {h['runtime_state']}/{h['freshness_state']}, failures {h['consecutive_failures']},"
              f" last error {h['last_error_code']}, at {h['updated_at']}" for h in s["source_health"]]
    lines += ["", "## Gates still open"] + [f"- {g}" for g in s["open_gates"]] + [""]
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(prog="m5_candidates")
    ap.add_argument("--out", required=True, type=Path)
    a = ap.parse_args()
    conn = db.connect(config.load().dsn, autocommit=True)
    try:
        s = snapshot(conn)
    finally:
        conn.close()
    a.out.mkdir(parents=True, exist_ok=True)
    day = s["taken_at"][:10]
    (a.out / f"m5_e05_{day}.json").write_text(json.dumps(s, ensure_ascii=False, indent=1), encoding="utf-8")
    (a.out / f"m5_e05_{day}.md").write_text(render(s), encoding="utf-8")
    print(render(s))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
