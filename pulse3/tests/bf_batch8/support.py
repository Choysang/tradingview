"""Helpers of the batch8 tests: overlay rows and a synthetic world builder shared with batch7's raw-bar helpers."""
from __future__ import annotations

import json


def overlay_row(rule: str, scenario: str, flag: bool, *, cf: float | None = None, actual: dict | None = None) -> dict:
    body = {"rule": rule, "scenario": scenario, "flags_known": True, "flags": {"flag": flag, "l1": flag, "l3": False, "t1": False}}
    if flag and cf is not None:
        body["counterfactual_d1_open"] = {"ret_gross": cf, "ret_net": cf - 0.00252, "price": "1", "source": "paper_S0_open"}
        body["actual_exits"] = {r: {"ret_gross": v, "ret_net": v - 0.00252, "price": "1"} for r, v in (actual or {}).items()}
    return {"kind": "overlay", "body": json.dumps(body), "code": f"{rule}|x|{scenario}", "session": "2026-11-24"}
