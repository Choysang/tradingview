"""BF fwdlog lane: the daily step of the three forward experiments, their append-only datasets and the stored-row reads.

Why: the old server runs E077 / E078 (tail-of-day picks, index half hours), E089 (market-state log with recorder health) and the intraday paper replay (triggers
v2.1, E053-E059) every session. pulse3 imports the old recordings as the declared source ``legacy_recordings`` (live lane) and computes the SAME rows from them with
the pre-registered rules unchanged (``domain.fwdlog``). Rows are appended to lake datasets through ``bindings.publish_derived``; a decided session is NEVER
rewritten (a second run adds only sessions without a row).
Datasets: ``fwdlog_e077_tail``, ``fwdlog_e078_halfhour``, ``fwdlog_e089_state``, ``fwdlog_paper_days`` / ``fwdlog_paper_buys`` / ``fwdlog_paper_sells``. One snapshot per
publish holds only new rows; its coverage document lists the session dates it decided, so reading the decided set never touches the lake.
Invariants: point in time (recordings and bars are read as visible at ``as_of``; a session is decided only when its recording is complete, E077 also needs the next
session's); each experiment is registered in the research registry as ``shadow`` BEFORE its first row; a failed day is retried (E089 records an error row like the old log,
so a gap shows); the paper replay is an EXPERIMENT RECORD (nothing is booked, no ledger).
Not covered: scheduling (deploy/windows), closing the registry runs, the rules (``domain.fwdlog``), the HTTP reads (``api.routes_bf_fwdlog``).
"""
from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from typing import Any, Callable, Iterable, Mapping

import polars as pl
import psycopg

from pulse.adapters.lake import LakeStore
from pulse.adapters.sources.legacy_recordings import LegacyRecordings
from pulse.application import bf_fwdlog_inputs as IN
from pulse.application import bf_fwdlog_paper as PA
from pulse.application import bf_fwdlog_registry as REG
from pulse.application import bindings as B
from pulse.application.bf_live_service import DATASET_EXT, DATASET_QUOTES, LiveService
from pulse.contracts import to_wire
from pulse.domain.fwdlog import paper as PP
from pulse.domain.fwdlog import state as ST
from pulse.domain.fwdlog import stats as SS
from pulse.domain.fwdlog import tail as TL
from pulse.domain.fwdlog import triggers as TR
from pulse.domain.fwdlog.common import CST, D, UTC
from pulse.domain.portfolio.forward_book import HybridCalendar

CONTRACT = "pulse.analysis_artifact"
DS = {"E077": "fwdlog_e077_tail", "E078": "fwdlog_e078_halfhour", "E089": "fwdlog_e089_state", "PAPER_DAYS": "fwdlog_paper_days", "PAPER_BUYS": "fwdlog_paper_buys",
      "PAPER_SELLS": "fwdlog_paper_sells"}
FIRST_SESSION = TL.START                                 # the old logs start on 2026-11-02
E089_READY_AT = time(17, 0)                              # both old recorders have ended by then
INDEX_SYMBOL = "sh000001"
NOTE = ("rows reproduce the old server's forward logs from the imported 60 s recordings with the pre-registered rules unchanged; "
        "a price of 0 in a recording is a missing price (pulse3 rule), not a return of -100 percent")


def _z(t: datetime) -> str:
    return t.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _dec(v) -> Decimal | None:
    return None if v is None else v if isinstance(v, Decimal) else Decimal(str(v))


@dataclass
class RunReport:
    through: str
    sessions: list[str] = field(default_factory=list)
    added: dict[str, list[str]] = field(default_factory=dict)
    pending: dict[str, list[dict]] = field(default_factory=dict)
    errors: list[dict] = field(default_factory=list)
    registered: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def add(self, key: str, day: date) -> None:
        self.added.setdefault(key, []).append(day.isoformat())

    def wait(self, key: str, day: date, reason: str) -> None:
        self.pending.setdefault(key, []).append({"session_date": day.isoformat(), "reason": reason})

    def wire(self) -> dict:
        return {"through": self.through, "sessions": self.sessions, "added": self.added, "pending": self.pending, "errors": self.errors,
                "registered": self.registered, "notes": self.notes}


class FwdlogService:
    def __init__(self, conn: psycopg.Connection, lake: LakeStore, recordings: LegacyRecordings | None = None, *, clock: Callable[[], datetime] | None = None,
                 frozen: IN.Frozen | None = None, events_path=None, bars_loader=None, hist_loader=None):
        if not conn.autocommit:
            raise ValueError("FwdlogService needs an autocommit connection; it opens explicit short transactions")
        self.conn, self.lake = conn, lake
        self.rec = recordings or LegacyRecordings()
        self.clock = clock or (lambda: datetime.now(UTC))
        self.live = LiveService(conn, lake, self.rec, clock=self.clock)
        self.frozen = frozen or IN.load_frozen()
        self.events_path = events_path
        self._recorded: list[date] = []
        self.hist_loader = hist_loader or (lambda c, a, u, since: PA.load_hist_cents(c, a, u, since))
        self.bars_loader = bars_loader or (lambda c, a, u, since: PA.load_bars(c, a, u, since))   # tests / local reconciliation read bars elsewhere

    def _events(self) -> tuple[list, dict]:
        """M3 catalyst events: pulse3's imported calendar dataset (step 2), else the old store file until that dataset exists.

        An explicit ``events_path`` (tests, local reconciliation) reads that file. The dataset holds the same events as the file
        (``fillm import-calendar`` runs daily; parity is tested in ``tests/bf_fillm``), and ``sha256`` stays the source file's hash.
        ``PULSE_CATALYST_SOURCE=native`` reads pulse3's own calendar instead (``bf_fwdlog_catalyst``); an empty native calendar is NOT replaced by the legacy one."""
        if self.events_path is None:
            from pulse.application import bf_fwdlog_catalyst as CAT
            if CAT.source_mode() == "native":
                return CAT.read_native_events(self.conn)
            from pulse.application import bf_fillm_calendar as CAL
            events, info = CAL.read_calendar_events(self.conn, self.lake)
            if info["present"]:
                ready = CAL.latest_ready(self.conn, None)["ready_at"]
                stale_h = (self.clock() - ready).total_seconds() / 3600
                return events, {"source": "pulse3_calendar" + (f", STALE: last import {stale_h:.0f} h ago" if stale_h > 20 else ""), "path_present": True, "events": info["events"], "sha256": info["source_sha256"],
                                "latest_first_seen_at": info["latest_first_seen_at"], "snapshot_id": str(info["snapshot_id"])}
        events, note = IN.read_events(self.events_path)
        return events, {**note, "source": "file"}

    # ---- stored rows (append-only datasets) ------------------------------------------------------------------------
    def _manifests(self, dataset: str) -> list[dict]:
        return self.conn.execute("SELECT * FROM market.dataset_manifest WHERE dataset_id=%s AND manifest_state='READY' ORDER BY manifest_version", (dataset,)).fetchall()

    def decided(self, dataset: str) -> set[str]:
        out: set[str] = set()
        for m in self._manifests(dataset):
            out |= set((m["coverage_document"] or {}).get("session_dates", ()))
        return out

    def stored(self, dataset: str, *, session_dates: Iterable[str] | None = None) -> list[dict]:
        """Stored rows oldest first: ``{session_date, row_key, decided_at, body}`` (body = the row as the wire form; a repeated key keeps its first row)."""
        want = None if session_dates is None else set(session_dates)
        seen: set[tuple[str, str]] = set()
        out: list[dict] = []
        for m in self._manifests(dataset):
            cov = m["coverage_document"] or {}
            if want is not None and not (want & set(cov.get("session_dates", ()))):
                continue
            df = self.lake.read_snapshot_frames(m["content_hash"])
            for r in df.iter_rows(named=True):
                k = (r["session_date"], r["row_key"])
                if k in seen or (want is not None and r["session_date"] not in want):
                    continue
                seen.add(k)
                out.append({"session_date": r["session_date"], "row_key": r["row_key"], "decided_at": r["decided_at"], "body": json.loads(r["body"]),
                            "snapshot_id": str(m["snapshot_id"])})
        out.sort(key=lambda r: (r["session_date"], r["row_key"]))
        return out

    def publish(self, dataset: str, rows: list[dict], *, deps: Iterable[uuid.UUID], extra: dict | None = None) -> dict | None:
        """Append rows ``{session_date, row_key, body}`` as one immutable snapshot; refuses a session that is already decided in this dataset."""
        if not rows:
            return None
        already = self.decided(dataset)
        clash = sorted({r["session_date"] for r in rows} & already)
        if clash:
            raise RuntimeError(f"{dataset}: sessions {clash} are already decided; a decided session is never rewritten")
        now = self.clock()
        texts = [json.dumps(to_wire(r["body"]), sort_keys=True, separators=(",", ":"), ensure_ascii=False) for r in rows]
        frame = pl.DataFrame({"session_date": [r["session_date"] for r in rows], "row_key": [r["row_key"] for r in rows], "decided_at": [_z(now)] * len(rows),
                              "body": texts, "body_sha256": [hashlib.sha256(t.encode("utf-8")).hexdigest() for t in texts]}).sort(["session_date", "row_key"])
        cov = {"dataset": dataset, "rules_version": TR.TRIGGERS_VERSION if dataset.startswith("fwdlog_paper") else REG.C.RULES_VERSION,
               "session_dates": sorted({r["session_date"] for r in rows}), "rows": frame.height, "decided_at": _z(now), "note": NOTE,
               "frozen_manifest_sha256": self.frozen.manifest_sha256, "frame_sha256": hashlib.sha256(frame.write_csv().encode("utf-8")).hexdigest(), **(extra or {})}
        return B.publish_derived(self.live.data, dataset, frame, cov, contract_id=CONTRACT,
                                 dependencies=[(i, "quote_snapshots") for i in dict.fromkeys(deps)], watermark=now)

    # ---- sessions ------------------------------------------------------------------------------------------------------
    def recorded_sessions(self, through: date) -> list[date]:
        """Sessions with a READY quote dataset, oldest first (the old logs walk the recorded day directories the same way)."""
        rows = self.conn.execute("SELECT DISTINCT coverage_document->>'session_date' AS d FROM market.dataset_manifest WHERE dataset_id=%s AND manifest_state='READY'",
                                 (DATASET_QUOTES,)).fetchall()
        return sorted(d for d in (date.fromisoformat(r["d"]) for r in rows if r["d"]) if FIRST_SESSION <= d <= through)

    def _calendar(self) -> HybridCalendar:
        days = [r["trade_date"] for r in self.conn.execute("SELECT DISTINCT trade_date FROM market.bar_projection WHERE timeframe='1d'")]
        return HybridCalendar(days or [date(2020, 1, 2)])

    # ---- the daily step ---------------------------------------------------------------------------------------------------
    def run(self, through: date, *, as_of: datetime | None = None, only: Iterable[str] | None = None) -> RunReport:
        """Decide every session up to ``through`` that has no row yet. ``as_of`` is the point-in-time cutoff of everything read (default: now)."""
        as_of = as_of or self.clock()
        rep = RunReport(through.isoformat())
        sessions = self.recorded_sessions(through)
        rep.sessions = [s.isoformat() for s in sessions]
        self._recorded = sessions
        keys = set(only) if only else {"E077", "E078", "E089", "PAPER"}
        first = self.conn.execute("SELECT * FROM market.dataset_manifest WHERE dataset_id=%s AND manifest_state='READY' ORDER BY coverage_document->>'session_date', manifest_version LIMIT 1",
                                  (DATASET_QUOTES,)).fetchone()
        if first is None:
            rep.notes.append("no READY quote_snapshots_60s dataset: nothing to do (import the recordings first)")
            return rep
        for spec in REG.EXPERIMENTS:
            if spec.key in keys and REG.registered(self.conn, spec) is None:
                REG.ensure_registered(self.conn, spec, self.frozen, first, now=self.clock())
                rep.registered.append(spec.key)
        events, ev_note = self._events()
        rep.notes.append(f"calendar store ({ev_note['source']}): {ev_note['events']} events" + ("" if ev_note["path_present"] else " (absent: M3 catalyst unknown)"))
        done = {k: self.decided(DS[k]) for k in ("E077", "E078", "E089", "PAPER_DAYS")}
        rows: dict[str, list[dict]] = {k: [] for k in DS}
        deps: dict[str, list[uuid.UUID]] = {k: [] for k in DS}
        cal = self._calendar()
        by_day = {d: self.live.latest_ready(DATASET_QUOTES, d, as_of) for d in sessions}

        def complete(day: date) -> dict | None:
            """The visible dataset version of a session when the old recorder finished it (done marker), else None; no frame is read."""
            man = by_day.get(day)
            return man if man is not None and man["coverage_document"].get("source_complete") else None

        for s in sessions:
            need = {"E078": "E078" in keys and s.isoformat() not in done["E078"], "PAPER": "PAPER" in keys and s.isoformat() not in done["PAPER_DAYS"],
                    "E077": "E077" in keys and s.isoformat() not in done["E077"]}
            if not any(need.values()):
                continue
            man = complete(s)
            if man is None:
                for k, v in need.items():
                    if v:
                        rep.wait(k, s, "the session's recording is not complete in the imported dataset (no done marker yet)")
                continue
            prov = {"quote_snapshot_id": str(man["snapshot_id"]), "quote_content_hash": man["content_hash"], "quote_manifest_version": man["manifest_version"]}
            if need["E078"] or need["PAPER"]:
                df = IN.day_frames(self.live, DATASET_QUOTES, s, as_of)             # one session's frame in memory at a time (about 0.5 GB)
                if need["E078"]:
                    self._try(rep, "E078", s, lambda: self._e078(s, IN.snapshots(df.frame), as_of, prov, rows, deps, df))
                if need["PAPER"]:
                    self._try(rep, "PAPER", s, lambda: self._paper(s, df, as_of, events, ev_note, prov, rows, deps, rep))
                del df
            if need["E077"]:
                nxt = cal.next_trading_day(s)
                nman = complete(nxt) if nxt in by_day else None
                if nman is None:
                    rep.wait("E077", s, f"the next trading day {nxt} has no complete recording in the imported dataset yet (the exit is at its 10:00 snapshot)")
                else:
                    xprov = {**prov, "exit_quote_snapshot_id": str(nman["snapshot_id"]), "exit_quote_content_hash": nman["content_hash"]}
                    self._try(rep, "E077", s, lambda: self._e077(s, nxt, as_of, xprov, rows, deps, man, nman))
        if "E089" in keys:
            self._e089_all(through, as_of, rep, rows, deps, done["E089"])
        for dataset_key, extra_keys in (("E077", "E077"), ("E078", "E078"), ("E089", "E089"), ("PAPER_BUYS", "PAPER"), ("PAPER_SELLS", "PAPER"), ("PAPER_DAYS", "PAPER")):
            if rows[dataset_key]:
                self.publish(DS[dataset_key], rows[dataset_key], deps=deps[dataset_key])
        return rep

    def _try(self, rep: RunReport, key: str, day: date, fn: Callable[[], str | None]) -> None:
        try:
            why = fn()
        except Exception as exc:                          # noqa: BLE001 - one broken session must not stop the others
            rep.errors.append({"experiment": key, "session_date": day.isoformat(), "error": f"{type(exc).__name__}: {exc}"[:300]})
            return
        if why:
            rep.wait(key, day, why)
        else:
            rep.add(key, day)

    # ---- E078 --------------------------------------------------------------------------------------------------------------
    def _e078(self, s: date, snaps: pl.DataFrame, as_of, prov, rows, deps, df) -> str | None:
        ew = TL.halfhour(IN.first_in_window(snaps, s, time(10, 0)), IN.first_in_window(snaps, s, time(14, 30)), IN.closing_quotes(snaps, s))
        ext = IN.day_frames(self.live, DATASET_EXT, s, as_of, code_col="symbol", main_only=False)
        index = None
        if ext is not None:
            ix = IN.index_quotes(IN.ext_rows(ext.frame), INDEX_SYMBOL, s)
            index = TL.halfhour(*ix) if ix[0] and ix[1] and ix[2] else None
            prov = {**prov, "ext_snapshot_id": str(ext.snapshot_id)}
        row = {**TL.e078_row(s, ew, index, index_recorded=ext is not None), "provenance": prov}
        rows["E078"].append({"session_date": s.isoformat(), "row_key": s.isoformat(), "body": row})
        deps["E078"].append(df.snapshot_id)
        return None

    # ---- E077 --------------------------------------------------------------------------------------------------------------
    def _e077(self, s: date, nxt: date, as_of, prov, rows, deps, man, nman) -> str | None:
        bars = self.bars_loader(self.conn, as_of, s - timedelta(days=1), s - timedelta(days=45))
        if not bars.sessions:
            return "no visible daily bars before the session (the 20-session limit-up history is unknown)"
        hist = TL.limit_history({c: [(d, cl) for d, _, cl, _ in v] for c, v in bars.by_code.items()}, s)
        df = IN.day_frames(self.live, DATASET_QUOTES, s, as_of)                    # the two sessions are read one after the other, never together
        snaps = IN.snapshots(df.frame)
        q1430, q1450 = IN.first_in_window(snaps, s, time(14, 30)), IN.first_in_window(snaps, s, time(14, 50))
        del df, snaps
        nf = IN.day_frames(self.live, DATASET_QUOTES, nxt, as_of)
        qexit = IN.first_in_window(IN.snapshots(nf.frame), nxt, time(10, 0))
        del nf
        row = TL.e077_row(s, nxt, q1430, q1450, qexit, hist)
        row["provenance"] = {**prov, "bars_sessions_visible": len(bars.sessions), "history_codes": len(hist)}
        rows["E077"].append({"session_date": s.isoformat(), "row_key": s.isoformat(), "body": row})
        deps["E077"] += [man["snapshot_id"], nman["snapshot_id"]]
        return None

    # ---- paper replay -------------------------------------------------------------------------------------------------------
    def _paper(self, s: date, df, as_of, events, ev_note, prov, rows, deps, rep) -> str | None:
        inp, why, notes = PA.build(self.conn, df, self.frozen, events, as_of=as_of, bars_loader=self.bars_loader, hist_loader=self.hist_loader, recorded=self._recorded)
        if inp is None:
            return why
        res = PP.replay_day(inp)
        summary = {**res["summary"], "provenance": {**prov, "calendar_events_sha256": ev_note.get("sha256"), "calendar_events": ev_note.get("events"), "calendar_source": ev_note.get("source"), **notes}}
        key = s.isoformat()
        rows["PAPER_DAYS"].append({"session_date": key, "row_key": key, "body": summary})
        for b in res["buys"]:
            rows["PAPER_BUYS"].append({"session_date": key, "row_key": b["opportunity_id"], "body": b})
        for x in res["sells"]:
            rows["PAPER_SELLS"].append({"session_date": key, "row_key": f"{x['code']}|{x['rule']}", "body": x})
        for k in ("PAPER_DAYS", "PAPER_BUYS", "PAPER_SELLS"):
            deps[k].append(df.snapshot_id)
        return None

    # ---- E089 ---------------------------------------------------------------------------------------------------------------
    def _e089_all(self, through: date, as_of: datetime, rep: RunReport, rows, deps, done: set[str]) -> None:
        cal = self._calendar()
        last = through if as_of.astimezone(CST) >= datetime.combine(through, E089_READY_AT, CST) or as_of.astimezone(CST).date() > through else through - timedelta(days=1)
        sums = dict(self.frozen.seed_sums)
        for r in self.stored(DS["E089"]):
            amount = _dec(r["body"].get("etf_amount"))
            if amount:
                sums[r["session_date"]] = amount
        day = ST.START
        while day <= last:
            name = day.isoformat()
            if cal.is_trading_day(day) and name not in done:
                if any(ST.NOT_TRADING in str(IN.read_json(self.rec.day_dir(kind, day) / "summary.json").get("end")) for kind in ("intraday", "intraday_ext")):
                    pass                                                      # an unlisted holiday: no row (the old log skips it too)
                else:
                    try:
                        row, snap_ids, wait = self._e089_row(day, as_of, sums)
                    except Exception as exc:                                   # noqa: BLE001 - a broken file costs that day's row, not the days after it
                        row, snap_ids, wait = {"date": name, "error": f"{type(exc).__name__}: {exc}"[:200], **ST.NO_ETF_FLAGS}, [], None
                        rep.errors.append({"experiment": "E089", "session_date": name, "error": row["error"]})
                    if wait:
                        rep.wait("E089", day, wait)                              # later rows need this day's ETF sum for their baseline: stop here
                        return
                    rows["E089"].append({"session_date": name, "row_key": name, "body": row})
                    deps["E089"] += snap_ids
                    rep.add("E089", day)
                    if row.get("etf_amount"):
                        sums[name] = row["etf_amount"]
            day += timedelta(days=1)

    def _on_disk(self, kind: str, day: date, stream_dir: str, compact: str) -> bool:
        d = self.rec.day_dir(kind, day)
        return (d / compact).is_file() or any((d / stream_dir).glob("*.parquet")) if (d / stream_dir).exists() else (d / compact).is_file()

    def _e089_row(self, day: date, as_of: datetime, sums: Mapping[str, Decimal]) -> tuple[dict | None, list, str | None]:
        q = IN.day_frames(self.live, DATASET_QUOTES, day, as_of)
        if self._on_disk("intraday", day, "quotes", "quotes.parquet") and (q is None or not q.complete):
            return None, [], "the main-board recording exists but its import is not complete yet"
        ext = IN.day_frames(self.live, DATASET_EXT, day, as_of, code_col="symbol", main_only=False)
        if ext is None and self._on_disk("intraday_ext", day, "parts", "quotes_ext.parquet"):
            return None, [], "the extended recording exists but is not imported yet"
        closing = None
        ids = []
        if q is not None:
            ids.append(q.snapshot_id)
            closing = {c: (v.price, v.prev_close) for c, v in IN.closing_quotes(IN.snapshots(q.frame), day).items()}
        ext_dir = self.rec.day_dir("intraday_ext", day)
        summary, merged, health_prov = IN.recorder_health(ext, ext_dir)
        rows = IN.ext_rows(ext.frame) if ext is not None else None
        if ext is not None:
            ids.append(ext.snapshot_id)
        row = ST.state_row(day, closing=closing, ext=rows, summary=summary, merged=merged, sums=sums)
        row["provenance"] = {"quote_snapshot_id": None if q is None else str(q.snapshot_id), "ext_snapshot_id": None if ext is None else str(ext.snapshot_id), **health_prov}
        return row, ids, None
