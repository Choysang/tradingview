"""Scheduler decisions with an injected clock; steps are faked, nothing sleeps or hits the network."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
import sys
import types

import pytest

from backend.app import scheduler as sched
from backend.app.market.calendar import Calendar
from backend.app.scheduler import CST, Scheduler

FRIDAY = date(2026, 10, 30)
SATURDAY = date(2026, 10, 31)
HOLIDAY = date(2026, 10, 29)  # a Thursday the calendar marks closed


def at(day: date, hour: int, minute: int = 0) -> datetime:
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=CST)


@pytest.fixture
def machine():
    calendar = Calendar(holidays=frozenset({HOLIDAY.isoformat()}))
    job = Scheduler(calendar=lambda: calendar)
    runs: list[datetime] = []

    def fake_daily() -> bool:
        runs.append(job.last_attempt)
        return True

    job.run_daily = fake_daily  # type: ignore[method-assign]
    return job, runs


def test_the_test_dates_are_what_they_claim() -> None:
    assert FRIDAY.weekday() == 4 and SATURDAY.weekday() == 5


def test_non_trading_days_never_run(machine) -> None:
    job, runs = machine
    assert not job.tick(at(SATURDAY, 16))
    assert not job.tick(at(HOLIDAY, 16))
    assert runs == []


def test_before_1535_does_not_run(machine) -> None:
    job, runs = machine
    assert not job.tick(at(FRIDAY, 9))
    assert not job.tick(at(FRIDAY, 15, 34))
    assert runs == []


def test_runs_at_1535_and_only_once_per_day(machine) -> None:
    job, runs = machine
    assert job.tick(at(FRIDAY, 15, 35))
    assert not job.tick(at(FRIDAY, 15, 36))
    assert not job.tick(at(FRIDAY, 23, 59))
    assert len(runs) == 1 and job.done_date == FRIDAY


def test_already_ran_today_blocks_even_after_a_restart_marker(machine) -> None:
    job, runs = machine
    job.done_date = FRIDAY  # what startup sets when today's post-close report exists
    assert not job.tick(at(FRIDAY, 16))
    assert runs == []


def test_runs_again_the_next_trading_day(machine) -> None:
    job, runs = machine
    assert job.tick(at(FRIDAY, 16))
    monday = date(2026, 11, 2)
    assert not job.tick(at(monday, 10))
    assert job.tick(at(monday, 15, 40))
    assert len(runs) == 2


def test_failed_run_retries_with_spacing_and_a_cap(machine) -> None:
    job, runs = machine
    outcomes = iter([False, False, False, True])

    def flaky() -> bool:
        runs.append(job.last_attempt)
        return next(outcomes)

    job.run_daily = flaky  # type: ignore[method-assign]
    assert job.tick(at(FRIDAY, 15, 35))
    assert not job.tick(at(FRIDAY, 15, 40))  # too soon after the failure
    assert job.tick(at(FRIDAY, 15, 46))
    assert job.tick(at(FRIDAY, 16, 0))
    assert not job.tick(at(FRIDAY, 17, 0))  # three attempts used up
    assert len(runs) == 3 and job.done_date is None


# --- run_daily: each step survives the others failing -----------------------------------------


def _install(monkeypatch, name: str, **functions) -> types.ModuleType:
    module = types.ModuleType(f"backend.app.{name}")
    for key, function in functions.items():
        setattr(module, key, function)
    monkeypatch.setitem(sys.modules, module.__name__, module)
    package_name, _, leaf = module.__name__.rpartition(".")
    package = sys.modules.get(package_name)
    if package is not None:
        monkeypatch.setattr(package, leaf, module, raising=False)
    return module


def test_failing_sync_does_not_stop_report_and_push(monkeypatch) -> None:
    from backend.app.notify import push
    from backend.app.warehouse import store

    calls: list[str] = []
    job = Scheduler()
    live = types.SimpleNamespace(has_bars=lambda: True)
    monkeypatch.setattr(store, "warehouse", lambda dataset: live)

    def broken_sync(request):
        calls.append("sync")
        raise RuntimeError("network down")

    _install(monkeypatch, "warehouse.sync", run_sync=broken_sync)
    report = object()
    _install(
        monkeypatch, "playbooks.picks",
        generate_report=lambda force=False: calls.append(f"report force={force}") or report,
        latest_report=lambda: report,
    )
    monkeypatch.setattr(push, "status", lambda: types.SimpleNamespace(configured=True))
    monkeypatch.setattr(push, "push_report", lambda r: calls.append("push") or None)

    assert job.run_daily() is False
    assert calls == ["sync", "report force=True", "push"]
    assert job.last_error and "sync" in job.last_error and "network down" in job.last_error
    assert job.last_run is not None


def test_failing_push_is_recorded_and_the_loop_survives(monkeypatch) -> None:
    from backend.app.notify import push
    from backend.app.warehouse import store

    job = Scheduler()
    monkeypatch.setattr(store, "warehouse", lambda dataset: types.SimpleNamespace(has_bars=lambda: False))
    report = object()
    _install(monkeypatch, "playbooks.picks", generate_report=lambda force=False: report, latest_report=lambda: report)
    monkeypatch.setattr(push, "status", lambda: types.SimpleNamespace(configured=True))

    def refuse(_):
        raise push.PushError("推送失败：HTTP 500")

    monkeypatch.setattr(push, "push_report", refuse)
    assert job.run_daily() is False
    assert "推送失败" in (job.last_error or "")
    # tick() keeps working afterwards
    assert job.tick(at(FRIDAY, 7)) is False


def test_successful_run_clears_the_error_and_skips_push_when_unconfigured(monkeypatch) -> None:
    from backend.app.notify import push
    from backend.app.warehouse import store

    job = Scheduler()
    job.last_error = "old"
    monkeypatch.setattr(store, "warehouse", lambda dataset: types.SimpleNamespace(has_bars=lambda: False))
    sent: list[object] = []
    report = object()
    _install(monkeypatch, "playbooks.picks", generate_report=lambda force=False: report, latest_report=lambda: report)
    monkeypatch.setattr(push, "status", lambda: types.SimpleNamespace(configured=False))
    monkeypatch.setattr(push, "push_report", lambda r: sent.append(r))
    assert job.run_daily() is True
    assert job.last_error is None and sent == []


def test_failed_sync_job_status_counts_as_a_failure(monkeypatch) -> None:
    from backend.app.warehouse import store

    job = Scheduler()
    monkeypatch.setattr(store, "warehouse", lambda dataset: types.SimpleNamespace(has_bars=lambda: True))
    _install(monkeypatch, "warehouse.sync", run_sync=lambda request: types.SimpleNamespace(status="failed", message="bad"))
    with pytest.raises(RuntimeError, match="bad"):
        job._sync()


# --- startup & state --------------------------------------------------------------------------


def test_startup_generates_only_when_the_latest_day_has_no_report(monkeypatch) -> None:
    from backend.app.warehouse import store

    calls: list[str] = []
    monkeypatch.setattr(store, "active", lambda: types.SimpleNamespace(coverage=lambda: (10, "2026-01-01", "2026-10-30")))
    stale = types.SimpleNamespace(trade_date="2026-10-29", generated_at="2026-10-29T15:40:00+08:00")
    state = {"report": stale}
    _install(
        monkeypatch, "playbooks.picks",
        latest_report=lambda: state["report"],
        generate_report=lambda force=False: calls.append("generate") or None,
    )
    _install(monkeypatch, "playbooks.evaluate", prewarm=lambda: calls.append("prewarm"))
    Scheduler().startup()
    assert calls == ["generate", "prewarm"]

    calls.clear()
    state["report"] = types.SimpleNamespace(trade_date="2026-10-30", generated_at="2026-10-30T15:50:00+08:00")
    Scheduler().startup()
    assert calls == []


def test_startup_marks_today_done_when_a_post_close_report_exists(monkeypatch) -> None:
    from backend.app.warehouse import store

    monkeypatch.setattr(store, "active", lambda: types.SimpleNamespace(coverage=lambda: (10, None, "2026-10-30")))
    fresh = types.SimpleNamespace(trade_date="2026-10-30", generated_at="2026-10-30T15:50:00+08:00")
    _install(monkeypatch, "playbooks.picks", latest_report=lambda: fresh, generate_report=lambda force=False: None)
    job = Scheduler(clock=lambda: at(FRIDAY, 18))
    job.startup()
    assert job.done_date == FRIDAY


def test_startup_failure_is_recorded_not_raised(monkeypatch) -> None:
    from backend.app.warehouse import store

    def boom():
        raise OSError("disk")

    monkeypatch.setattr(store, "active", boom)
    job = Scheduler()
    job.startup()
    assert job.last_error and "startup" in job.last_error


def test_scheduler_is_disabled_by_the_environment(monkeypatch) -> None:
    monkeypatch.setenv("PULSE_SCHEDULER", "0")
    job = Scheduler()
    assert job.start() is False and job.state()["enabled"] is False
    assert sched.scheduler_state()["enabled"] is False


def test_state_shape() -> None:
    state = Scheduler().state()
    assert set(state) == {"enabled", "last_run", "last_error", "done_date"}
