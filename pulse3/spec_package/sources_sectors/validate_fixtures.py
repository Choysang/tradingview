"""Validate bounded synthetic contract examples; this is not a production connector."""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime
from decimal import Decimal
from pathlib import Path

BASE = Path(__file__).resolve().parent


def main() -> None:
    checks: list[dict] = []

    def check(name: str, condition: bool) -> None:
        assert condition, name
        checks.append({"check": name, "passed": True})

    fixtures = json.loads((BASE / "fixtures.json").read_text())
    evidence = json.loads((BASE / "evidence.json").read_text())
    catalog = json.loads((BASE / "source_catalog_v3.json").read_text())
    taxonomy = json.loads((BASE / "taxonomy.json").read_text())
    fields = json.loads((BASE / "sources_sectors_fields.json").read_text())

    revisions: dict[tuple, str] = {}
    for row in fixtures["document_deliveries"]:
        key = (row["source_id"], row["source_record_id"], row["revision"])
        if key in revisions:
            check("duplicate delivery preserves exact version payload", revisions[key] == row["payload_hash"])
        revisions[key] = row["payload_hash"]
    check("push/poll dedupe and late correction preserved", len(revisions) == fixtures["expected_unique_document_revisions"])

    origins = {r["origin_group"] for r in fixtures["origin_evidence"] if r["status"] in {"confirmed_original", "confirmed_independent_report"} and r["origin_group"]}
    check("republication count does not inflate independent origins", len(origins) == fixtures["expected_independent_origins"])

    unit_multiplier = {"CNY": Decimal(1), "CNY_10000": Decimal(10000)}
    amounts = [Decimal(row["raw_value"]) * unit_multiplier[row["raw_unit"]] for row in fixtures["amount_observations"]]
    check("yuan and ten-thousand yuan reconcile exactly", len(set(amounts)) == 1 and all(amount == Decimal(row["expected_cny"]) for amount, row in zip(amounts, fixtures["amount_observations"])))
    series = {(r["provider"], r["metric"], r["methodology"]) for r in fixtures["funding_metrics"]}
    check("same display name does not merge funding methodologies", len(series) == 2)

    timestamp = datetime.fromisoformat
    lhb = fixtures["lhb"]
    check("after-close disclosure unavailable to earlier intraday decision", timestamp(lhb["available_at"]) > timestamp(lhb["intraday_decision"]))
    check("next-day decision may use already available disclosure", timestamp(lhb["available_at"]) <= timestamp(lhb["next_day_decision"]))
    net = sum((Decimal(row["amount_cny"]) * (1 if row["side"] == "buy" else -1) for row in lhb["same_report_rows"]), Decimal(0))
    check("same seat buy and sell rows both contribute within one report", net == Decimal(lhb["expected_net_cny"]))

    community = fixtures["community"]
    check("future engagement excluded despite old post creation date", timestamp(community["post_created_at"]) <= timestamp(community["early_decision"]) < timestamp(community["available_at"]))
    member = fixtures["membership"]
    check("historical membership requires knowledge time as well as effective time", timestamp(member["effective_from"]) <= timestamp(member["historical_decision"]) < timestamp(member["available_at"]))

    policy = fixtures["candidate_hysteresis"]
    state = "observing"
    consecutive = 0
    states = []
    for value in policy["scores"]:
        score = Decimal(value)
        if state in {"observing", "cooling"}:
            consecutive = consecutive + 1 if score >= Decimal(policy["entry_threshold"]) else 0
            if consecutive >= policy["required_entry_windows"]:
                state, consecutive = "qualified", 0
        elif state == "qualified" and score < Decimal(policy["exit_threshold"]):
            state, consecutive = "cooling", 0
        states.append(state)
    check("separate entry and exit thresholds prevent boundary churning", states == policy["expected_states"])

    seat = next(row for row in taxonomy["seat_resolution_examples"] if row["raw_mention"] == "前东路")
    check("unknown seat is not silently corrected", seat["resolution_status"] == "unresolved" and seat["canonical_seat_id"] is None and not seat["automatic_correction_allowed"])
    check("community defaults to read-only", not taxonomy["community_policy"]["external_posting_adapter_enabled"] and not catalog["community_policy"]["external_posting_enabled"])
    popularity = next(row for row in catalog["sources"] if row["source_id"] == "eastmoney_popularity")
    check("conflicting official refresh claims remain disputed", popularity["cadence"]["cadence_status"] == "disputed" and popularity["cadence"]["declared_refresh_seconds"] is None)
    check("research catalog does not fabricate active connections", all(not row["enabled"] and row["integration_status"] == "unconfigured" and not row["rights"]["credential_configured"] for row in catalog["sources"]))
    evidence_ids = {row["evidence_id"] for row in evidence["items"]}
    check("all catalog evidence references resolve", all(ref["evidence_id"] in evidence_ids for row in catalog["sources"] for ref in row["primary_sources"]))
    check("source health and freshness include independent calendar semantics", "not_expected" in catalog["runtime_contract"]["freshness_state"] and "paused" in catalog["runtime_contract"]["runtime_state"])

    sql = (BASE / "sources_sectors_schema.sql").read_text()
    import pglast

    statements = pglast.parse_sql(sql)
    check("PostgreSQL migration parses", len(statements) > 0)
    functions = re.findall(r"CREATE FUNCTION\b.*?\$\$;", sql, flags=re.S)
    for function in functions:
        pglast.parse_plpgsql(function)
    check("sector rank guard PLpgSQL bodies parse", len(functions) == 2)
    check("12 domain tables have complete field descriptions", len(fields["tables"]) == 12 and all(field["meaning"] for table in fields["tables"] for field in table["fields"]))
    check("sector overview uses sector objects, not instrument ranking rows", taxonomy["ranking_scope_contract"]["sector_overview_row_table"] == "decision.sector_rank_entry")

    result = {
        "checked_at": "2026-11-10",
        "data_kind": "synthetic_only",
        "passed_checks": len(checks),
        "checks": checks,
        "sql_parser": f"pglast {getattr(pglast, '__version__', 'unknown')}",
        "sql_statements": len(statements),
        "plpgsql_function_bodies_parsed": len(functions),
        "sql_sha256": hashlib.sha256(sql.encode()).hexdigest(),
        "postgres_migration_executed": False,
        "trigger_bodies_executed": False,
        "live_source_connection_tested": False,
        "real_return_backtest_executed": False,
        "external_posts_sent": False,
    }
    (BASE / "validation_result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    print(f"{len(checks)} synthetic contract checks passed; {len(statements)} SQL statements parsed; no production integration claim.")


if __name__ == "__main__":
    main()
