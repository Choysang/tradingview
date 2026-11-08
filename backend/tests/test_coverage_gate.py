from backend.app.domain.coverage_gate import decide

EXP = [f"{600000 + i}" for i in range(100)]


def test_full_coverage_is_ok_without_retry() -> None:
    calls = []
    d = decide(EXP, EXP, retry=lambda m: calls.append(m) or [])
    assert d.status == "ok" and not d.retried and not calls and d.may_generate


def test_retry_fills_gap_to_ok() -> None:
    d = decide(EXP, EXP[:96], retry=lambda m: list(m))
    assert d.retried and d.status == "ok" and d.coverage == 1.0


def test_degraded_between_thresholds() -> None:
    d = decide(EXP, EXP[:96], retry=lambda m: [])
    assert d.retried and d.status == "degraded" and d.may_generate and len(d.missing) == 4


def test_refuse_below_95() -> None:
    d = decide(EXP, EXP[:90], retry=lambda m: list(m)[:2])
    assert d.status == "refuse" and not d.may_generate and d.coverage == 0.92


def test_empty_expected_refuses() -> None:
    assert decide([], []).status == "refuse"
