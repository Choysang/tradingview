"""Background scheduler: keeps the daily report fresh without anyone clicking.

One daemon thread, two jobs:
- on start, make sure a report exists for the warehouse's latest trading day;
- every minute, on a trading day after 15:35 CST (once per day): sync the live
  warehouse's daily bars (when it has any), regenerate the report, push it.

Every step is wrapped: a failing sync must not stop the report, a failing push
must not stop the next day. Failures are logged and exposed through
`scheduler_state()` so the notify status can show them. Disabled with
`PULSE_SCHEDULER=0` (the test suite sets it).
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
import logging
import os
import threading
from typing import Callable

from .market.calendar import Calendar, load_calendar

logger = logging.getLogger("pulse")
CST = timezone(timedelta(hours=8))
RUN_AFTER = time(15, 35)
POLL_SECONDS = 60.0
MAX_ATTEMPTS = 3  # per day, when a step fails
RETRY_AFTER = timedelta(minutes=10)


def _now() -> datetime:
    return datetime.now(CST)


class Scheduler:
    def __init__(
        self,
        clock: Callable[[], datetime] = _now,
        calendar: Callable[[], Calendar] = load_calendar,
    ) -> None:
        self._clock = clock
        self._calendar = calendar
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.done_date: date | None = None  # the day whose after-close run has completed
        self.attempts: dict[date, int] = {}
        self.last_attempt: datetime | None = None
        self.last_run: str | None = None
        self.last_error: str | None = None

    # --- decisions (pure, testable with an injected clock) -------------------
    def due(self, now: datetime) -> bool:
        """Should the after-close job run at `now`?"""
        today = now.date()
        if self.done_date == today:
            return False
        if not self._calendar().is_trading_day(today) or now.time() < RUN_AFTER:
            return False
        if self.attempts.get(today, 0) >= MAX_ATTEMPTS:
            return False
        return (
            self.last_attempt is None
            or self.last_attempt.date() != today
            or now - self.last_attempt >= RETRY_AFTER
        )

    def tick(self, now: datetime | None = None) -> bool:
        """One poll. Returns True when the after-close job ran."""
        now = now or self._clock()
        if not self.due(now):
            return False
        today = now.date()
        self.attempts[today] = self.attempts.get(today, 0) + 1
        self.last_attempt = now
        if self.run_daily():
            self.done_date = today
        return True

    # --- jobs --------------------------------------------------------------------
    def _step(self, name: str, action: Callable[[], object]) -> bool:
        try:
            action()
            return True
        except Exception as error:  # noqa: BLE001 - a failed step must not end the loop
            logger.exception("scheduler step %s failed", name)
            with self._lock:
                self.last_error = f"{name}：{type(error).__name__}: {error}"[:300]
            return False

    def run_daily(self) -> bool:
        """Sync (live bars only), regenerate the report, push. True when all steps worked."""
        results = [self._step("sync", self._sync), self._step("report", self._report)]
        results.append(self._step("push", self._push))
        with self._lock:
            self.last_run = self._clock().isoformat(timespec="seconds")
            if all(results):
                self.last_error = None
        return all(results)

    def _sync(self) -> None:
        from contracts.schema import SyncRequest
        from .warehouse.store import warehouse

        if not warehouse("live").has_bars():
            logger.info("scheduler: live warehouse empty, daily sync skipped")
            return
        from .warehouse import sync

        job = sync.run_sync(SyncRequest(kind="daily"))
        if getattr(job, "status", "done") == "failed":
            raise RuntimeError(f"日更同步失败：{getattr(job, 'message', '')}")

    def _report(self) -> None:
        from .playbooks import picks

        picks.generate_report(force=True)

    def _push(self) -> None:
        from .notify import push

        if not push.status().configured:
            return
        from .playbooks import picks

        report = picks.latest_report()
        if report is not None:
            push.push_report(report)

    def startup(self) -> None:
        """Make sure the latest trading day has a report; warm evaluation caches."""

        def ensure() -> None:
            from .playbooks import evaluate, picks
            from .warehouse.store import active

            latest = active().coverage()[2]
            report = picks.latest_report()
            if report is not None and (latest is None or report.trade_date >= latest):
                return
            picks.generate_report()
            evaluate.prewarm()

        if self._step("startup", ensure):
            with self._lock:
                self.last_run = self._clock().isoformat(timespec="seconds")
        self._mark_done_if_report_is_fresh()

    def _mark_done_if_report_is_fresh(self) -> None:
        """A restart after the close must not repeat (and re-push) today's run."""
        try:
            from .playbooks import picks

            report = picks.latest_report()
            now = self._clock()
            if report is None:
                return
            generated = datetime.fromisoformat(report.generated_at)
            if generated.tzinfo is None:
                generated = generated.replace(tzinfo=CST)
            local = generated.astimezone(CST)
            if local.date() == now.date() and local.time() >= RUN_AFTER:
                self.done_date = now.date()
        except Exception:  # noqa: BLE001 - best effort only
            logger.debug("scheduler: could not read the latest report", exc_info=True)

    # --- thread -------------------------------------------------------------------
    def _loop(self) -> None:
        self.startup()
        while not self._stop.wait(POLL_SECONDS):
            try:
                self.tick()
            except Exception:  # noqa: BLE001
                logger.exception("scheduler tick failed")

    def start(self) -> bool:
        if os.environ.get("PULSE_SCHEDULER", "1") == "0" or self._thread is not None:
            return False
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="pulse-scheduler", daemon=True)
        self._thread.start()
        return True

    def stop(self) -> None:
        self._stop.set()
        self._thread = None

    def state(self) -> dict[str, object]:
        with self._lock:
            return {
                "enabled": self._thread is not None,
                "last_run": self.last_run,
                "last_error": self.last_error,
                "done_date": self.done_date.isoformat() if self.done_date else None,
            }


_SCHEDULER = Scheduler()


def start() -> bool:
    return _SCHEDULER.start()


def stop() -> None:
    _SCHEDULER.stop()


def scheduler_state() -> dict[str, object]:
    return _SCHEDULER.state()
