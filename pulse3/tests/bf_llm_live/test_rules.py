"""E092 pure rules: the frozen document hash, the deadline, the universe pick, the prompt, the verdict parser and the cost estimate."""
from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from pulse.domain.llm_live import rules as R

DOC = Path(__file__).resolve().parents[3] / R.SPEC_PATH


@pytest.mark.skipif(not DOC.exists(), reason="the pre-registration is not next to the code")
def test_frozen_hash_matches_the_preregistration_document():
    raw = DOC.read_bytes()
    assert R.frozen_hash(raw) == R.SPEC_SHA256
    assert R.frozen_hash(raw.replace(b"\n", b"\r\n")) == R.SPEC_SHA256                 # line endings do not change it


def test_a_result_section_is_outside_the_frozen_prefix():
    base = b"# t\n\n## 3. x\nbody\n"
    assert R.frozen_hash(base + b"\n## 4. result\nverdict\n") == R.frozen_hash(base + b"\n## 4. other\n")
    assert R.frozen_hash(base) != R.frozen_hash(base.replace(b"body", b"changed"))


def test_deadline_is_the_next_session_0925_china_time():
    assert R.deadline_after(date(2026, 11, 23)).isoformat() == "2026-11-24T09:25:00+08:00"
    assert R.deadline_after(date(2026, 11, 27)).isoformat() == "2026-11-30T09:25:00+08:00"       # Friday -> Monday
    assert R.sessions_after(date(2026, 11, 23), 6) == date(2026, 12, 1)


def test_universe_is_top_30_by_score_then_code_plus_seeded_controls():
    scores = {f"60{i:04d}": float(i % 7) for i in range(200)}           # many ties
    top, ctl = R.pick_universe(scores, date(2026, 11, 23))
    assert len(top) == 30 and len(ctl) == 10 and not set(top) & set(ctl)
    assert all(scores[top[i]] >= scores[top[i + 1]] for i in range(29))
    assert [c for c in top if scores[c] == 6.0] == sorted(c for c in top if scores[c] == 6.0)     # ties by code
    assert R.pick_universe(scores, date(2026, 11, 23)) == (top, ctl)                                # deterministic
    assert R.pick_universe(scores, date(2026, 11, 24))[1] != ctl                                    # the seed is the decision date
    assert all(scores[c] <= min(scores[t] for t in top) for c in ctl)


def test_universe_refuses_too_few_names_and_ignores_non_finite_scores():
    with pytest.raises(ValueError):
        R.pick_universe({f"{i:06d}": 1.0 for i in range(39)}, date(2026, 11, 23))
    scores = {f"{i:06d}": float(i) for i in range(45)} | {"999999": float("nan"), "999998": None}
    top, ctl = R.pick_universe(scores, date(2026, 11, 23))
    assert "999999" not in top + ctl and "999998" not in top + ctl


def test_prompt_marks_unknowns_instead_of_inventing_them():
    bars = [{"date": date(2026, 11, 20), "close": 10.0, "pct_chg": 10.0, "amount": 2.0, "float_cap": 50.0, "is_limit_up": True, "is_limit_down": False},
            {"date": date(2026, 11, 23), "close": 9.0, "pct_chg": None, "amount": None, "float_cap": None, "is_limit_up": False, "is_limit_down": False}]
    text = R.build_prompt(market="m", code="600000", name="N", industry=None, float_cap=None, bars=bars)
    assert "流通市值未知" in text and "行业" not in text
    assert "2026-11-20 收10.00 +10.00% 换手4.00% 涨停" in text and "收9.00 n/a 换手n/a" in text


def test_market_line_needs_a_complete_index_series():
    closes = [3000.0 + i for i in range(61)]
    assert R.market_line(date(2026, 11, 23), closes).startswith("上证指数 2026-11-23 收盘 3060.00")
    with pytest.raises(ValueError):
        R.market_line(date(2026, 11, 23), closes[:60])
    with pytest.raises(ValueError):
        R.market_line(date(2026, 11, 23), [float("nan")] + closes[1:])


@pytest.mark.parametrize("text", ['{"action":"buy","p_outperform":0}', 'noise {"action":"avoid","p_outperform":100.0,"expected_excess_pct":-1} tail'])
def test_verdict_accepts_the_documented_range(text):
    assert R.parse_verdict(text)["action"] in R.ACTIONS


@pytest.mark.parametrize("text", ["no json", '{"action":"hold","p_outperform":50}', '{"action":"buy","p_outperform":101}', '{"action":"buy","p_outperform":true}',
                                  '{"action":"buy","p_outperform":"60"}', '{"action":"buy","p_outperform":50,"expected_excess_pct":"x"}', '{"action":"buy"}'])
def test_verdict_rejects_anything_else(text):
    with pytest.raises(ValueError):
        R.parse_verdict(text)


def test_cost_is_estimated_from_usage_or_unknown():
    p = R.Prices(in_per_m=10.0, out_per_m=40.0, search_per_call=0.5, reserve_per_call=1.0)
    assert R.estimate_cost({"input_tokens": 1_000_000, "output_tokens": 500_000}, 2, p) == pytest.approx(10 + 20 + 1)
    assert R.estimate_cost({}, 1, p) is None and R.estimate_cost({"input_tokens": 1}, 1, p) is None and R.estimate_cost(None, 1, p) is None


def test_prices_and_cap_come_from_the_environment_and_refuse_garbage():
    assert R.daily_cap_from_env({}) == 5.0 and R.daily_cap_from_env({"PULSE_LLM_DAILY_USD_CAP": "2.5"}) == 2.5
    for bad in ("x", "-1", "nan", "inf"):
        with pytest.raises(ValueError):
            R.daily_cap_from_env({"PULSE_LLM_DAILY_USD_CAP": bad})
    assert R.prices_from_env({"PULSE_LLM_PRICE_IN_PER_M": "1"}).in_per_m == 1.0
    with pytest.raises(ValueError):
        R.prices_from_env({"PULSE_LLM_PRICE_OUT_PER_M": "abc"})


def _row(kind, n, cost=None, session="d"):
    body = {"n": n} if kind == "call" else {"call_n": n, "cost_usd": cost}
    return {"session": session, "kind": kind, "key": str(n), "body": body}


def test_spend_counts_unresolved_calls_at_the_larger_of_the_reserve_and_the_mean_known_cost():
    p = R.Prices(reserve_per_call=0.5)
    rows = [_row("call", 1), _row("answer", 1, 1.0), _row("call", 2), _row("call", 3), _row("error", 3, None), _row("call", 1, session="other")]
    spent, unresolved = R.day_spend(rows, "d", p)
    assert unresolved == 2 and spent == pytest.approx(1.0 + 2 * 1.0)               # call 2 never resolved, call 3 failed with unknown cost: each at the mean known cost 1.0, not the 0.5 reserve
    assert R.allows_next_call(rows, "d", p, cap_usd=4.0) is True                    # 3.0 + one more call at 1.0
    assert R.allows_next_call(rows, "d", p, cap_usd=3.9) is False
    cheap = [_row("call", 1), _row("answer", 1, 0.1), _row("call", 2)]
    assert R.day_spend(cheap, "d", p)[0] == pytest.approx(0.1 + 0.5)                # a cheap day never drops below the reserve


def test_a_zero_cost_refusal_does_not_lower_the_unit_cost():
    p = R.Prices(reserve_per_call=0.5)
    rows = [_row("call", 1), _row("answer", 1, 2.0), _row("call", 2), _row("error", 2, 0.0), _row("call", 3)]
    assert R.unit_cost(rows, "d", p) == 2.0
    assert R.day_spend(rows, "d", p)[0] == pytest.approx(2.0 + 2.0)


def test_failure_cost_rules():
    assert R.failure_cost("http_401", None) == 0.0 and R.failure_cost("http_429", None) == 0.0 and R.failure_cost("refusal", None) == 0.0
    assert R.failure_cost("refusal", 0.07) == 0.07                                     # a reported usage is a cost
    for label in ("http_500", "ReadError", "RemoteProtocolError", "timeout", "bad_verdict", "malformed_reply", "status_incomplete"):
        assert R.failure_cost(label, None) is None                                      # counted at the unit cost


def test_max_output_tokens_default_and_environment():
    assert R.max_output_tokens_from_env({}) == 12000 and R.max_output_tokens_from_env({"PULSE_LLM_MAX_OUTPUT_TOKENS": "4000"}) == 4000
    for bad in ("x", "10", "-5"):
        with pytest.raises(ValueError):
            R.max_output_tokens_from_env({"PULSE_LLM_MAX_OUTPUT_TOKENS": bad})
