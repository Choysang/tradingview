"""Exit lab and crowd snapshot: plans read like a trader's checklist, choices are made in-sample only."""
from __future__ import annotations

from fastapi.testclient import TestClient

from backend.app.main import app
from backend.app.playbooks import exits
from contracts.schema import ExitRules


def test_plan_lists_every_active_rule_in_checking_order():
    plan = exits.plan_text(ExitRules(
        holding_days=3, stop_loss_pct=7, weak_open_pct=2, sell_if_not_limit=True, sell_on_ebb=True,
    ))
    assert plan[0].startswith("买入次日开盘若低于买入价 2%") and "止损" in plan[1]
    assert any("断板" in line for line in plan) and any("退潮" in line for line in plan)
    assert plan[-1] == "以上都没触发，持有 3 个交易日后收盘卖。"
    assert exits.plan_text(ExitRules(holding_days=5, stop_loss_pct=None)) == ["以上都没触发，持有 5 个交易日后收盘卖。"]


def test_variants_fit_the_playbook():
    from backend.app.playbooks.base import resolve

    short = {key for key, _, _ in exits.variants_for(resolve("first_board_relay"))}
    reversal = {key for key, _, _ in exits.variants_for(resolve("rsi_oversold"))}
    trend = {key for key, _, _ in exits.variants_for(resolve("turtle_55"))}
    assert "not_limit" in short and "not_limit" not in reversal  # 断板卖 only for limit-up plays
    assert {"below_ma5", "chandelier", "stall"} <= trend and "weak_open" not in trend


def test_exit_study_endpoint_chooses_in_sample_and_reports_out_of_sample():
    with TestClient(app) as client:
        body = client.get("/api/v1/playbooks/turtle_55/exits").json()
        missing = client.get("/api/v1/playbooks/nope/exits")
        untestable = client.get("/api/v1/playbooks/value_pe_pb/exits")
    keys = [variant["key"] for variant in body["variants"]]
    assert keys[0] == "default" and body["chosen_key"] in keys
    default = body["variants"][0]
    chosen = next(v for v in body["variants"] if v["key"] == body["chosen_key"])
    if body["chosen_key"] != "default":
        assert chosen["in_sample"]["sharpe"] >= default["in_sample"]["sharpe"] + exits.MIN_GAIN
        gain = (chosen["out_of_sample"]["sharpe"] or float("-inf")) - (default["out_of_sample"]["sharpe"] or float("-inf"))
        assert body["improved_out_of_sample"] == (
            chosen["out_of_sample"]["trades"] >= exits.MIN_OOS_TRADES and gain >= exits.MIN_OOS_GAIN
        )
    else:
        assert body["improved_out_of_sample"] is None
    assert body["plan"][-1].startswith("以上都没触发")
    assert missing.status_code == 404 and untestable.status_code == 409


def test_crowd_snapshot_says_which_sources_are_missing():
    with TestClient(app) as client:
        body = client.get("/api/v1/crowd").json()
    assert body["provenance"]["mode"] == "SIM"
    assert body["lhb"] == [] and body["seats"] == [] and body["industries"] == []
    assert any("boards" in note for note in body["notes"]) and any("lhb" in note for note in body["notes"])
