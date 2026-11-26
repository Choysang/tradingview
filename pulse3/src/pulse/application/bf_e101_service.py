"""E101 service: registration, the forward step, the plan reports, the judgement, the status and the one-off historical robustness run.

Why: one place for the commands of ``pulse.workers.e101``; it adds no rule (rules: ``domain.e101``; steps: ``bf_e101_forward`` / ``bf_e101_plan`` / ``bf_e101_core``).
Invariants: the frozen prefix of the pre-registration is checked against the packaged copy on every command (fail closed); E101 is registered before its first row; nothing is
recorded for an event whose D0 is before 2026-11-26; the history run is report only (section 4: H1 and H3, run once, never decides) and publishes once (a second publish is refused).
History events come from the E073 calendar history file (the server's calendar starts 2026-11-09); its path is an argument.
Not covered: scheduling (``deploy/windows``), HTTP.
"""
from __future__ import annotations

import dataclasses
import json
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from hashlib import sha256
from pathlib import Path
from typing import Any

import polars as pl
import psycopg

from pulse.adapters.lake import LakeStore
from pulse.application import bf_batch7_inputs as IN7
from pulse.application import bf_batch8_core as CORE8
from pulse.application import bf_e101_core as C
from pulse.application import bf_e101_forward as FW
from pulse.application import bf_e101_plan as PL
from pulse.application import bf_e101_registry as REG
from pulse.application import bindings as B
from pulse.application import frozen_specs as FS
from pulse.application.data_service import DataService
from pulse.contracts import to_wire
from pulse.domain.e101 import rules as R
from pulse.domain.e101 import stats as ST

SPEC_FILE = Path(R.SPEC_PATH).name
HISTORY_DATASET = "e101_history"
WARMUP_DAYS = 130


def check_document(specs_dir: Path | None = None, repo_root: Path | None = None) -> str:
    return FS.verify(SPEC_FILE, R.SPEC_SHA256, R.frozen_hash, repo_path=R.SPEC_PATH, specs_dir=specs_dir, repo_root=repo_root)


def _mean(xs: list[float]) -> float | None:
    return sum(xs) / len(xs) if xs else None


def history(conn: psycopg.Connection, lake: LakeStore, *, as_of: datetime, calendar_file: Path, since: date = R.HISTORY_START, until: date = R.HISTORY_END) -> dict[str, Any]:
    """H1 and H3 over the history window, plus per-class conditional statistics for the plan reports. Report only."""
    code_of = CORE8.code_of(conn, as_of)
    win = IN7.pick_window(conn, lake, as_of, since - timedelta(days=WARMUP_DAYS), code_of, need_day=until)
    if win is None:
        return {"status": "no_data", "reason": f"no raw bars cover {until}", "decides": False}
    inp, info = IN7.prepare(conn, lake, win, as_of=as_of, with_seats=False)
    panel = C.with_helpers(inp.panel)
    inp = dataclasses.replace(inp, panel=panel)
    events = [e for e in C.events_of(C.history_rows(calendar_file), inp.sessions) if since <= e.d0 <= until]
    mk = C.market_days(panel)
    slots = [d.isoformat() for d in inp.sessions if since <= d <= until and d in mk]
    market_r = {d.isoformat(): v["r"] for d, v in mk.items()}
    event_days = {e.d0.isoformat() for e in events} | {e.r.isoformat() for e in events if e.r is not None}
    e1 = ST.h1({s: market_r[s] for s in slots}, events, slots, event_days)
    legs = C.legs_for(inp, R.H3_HOLD)
    recs, skipped, bodies = [], 0, {}
    for e in events:
        p = C.previous_session(inp.sessions, e.d0)
        if p is None or e.d0 not in inp.sessions:
            skipped += 1
            continue
        b = C.leg_bodies(inp, legs, p, C.groups_at(panel, p), R.H3_HOLD)
        if b is None:
            skipped += 1
            continue
        bodies[e.key] = b
        recs += C.group_records(e.key, b)
    h3_slots = sorted(set(slots) | {b["decision_day"] for b in bodies.values()})
    groups, p3 = ST.two_groups(recs, h3_slots)
    ctl = (e1.extra or {}).get("controls_by_weekday", {})
    by: dict[str, dict] = {}
    acc: dict[str, dict[str, list[float]]] = defaultdict(lambda: defaultdict(list))
    for e in events:
        a = acc[e.cls]
        a["n"].append(1.0)
        d0 = mk.get(e.d0)
        if d0:
            a["d0_r"].append(d0["r"])
            if e.d0.isoweekday() in ctl:
                a["d0_control"].append(ctl[e.d0.isoweekday()])
        rr = mk.get(e.r) if e.r else None
        if rr:
            a["r_gap"].append(rr["gap"])
            a["r_intra"].append(rr["intra"])
            a["r_r"].append(rr["r"])
        for g in R.GROUPS:
            for x in (bodies.get(e.key) or {}).get("groups", {}).get(g, {}).get("names", []):
                if x.get("ex_sv") is not None:
                    a[f"d0_{g}_ex"].append(x["ex_sv"])
    for cls, a in acc.items():
        by[cls] = {"n": len(a["n"]), **{k: _mean(v) for k, v in a.items() if k != "n"}, "d0_leader_n": len(a["d0_leader_ex"]), "d0_core_n": len(a["d0_core_ex"])}
    dirs = {"H1": None if e1.effect is None else ("negative (pre-registered)" if e1.effect < 0 else "not negative"),
            **{f"H3_{g}": None if v.effect is None else ("positive (pre-registered)" if v.effect > 0 else "not positive") for g, v in groups.items()}}
    return {"status": "ok" if events else "no_sample", "source": win.source, "window": [since.isoformat(), until.isoformat()], "sessions": len(slots),
            "events": len(events), "events_by_class": {c: sum(1 for e in events if e.cls == c) for c, _ in R.CLASSES}, "h3_skipped": skipped,
            "H1": e1.wire(), "H3": {g: v.wire() for g, v in groups.items()}, "H3_p": p3, "direction_vs_preregistered": dirs, "by_class": by,
            "calendar_file_sha256": sha256(calendar_file.read_bytes()).hexdigest(), "flag_info": {k: v for k, v in info.items() if k != "notes"}, "decides": False,
            "note": "history was partly seen (E073 looked at the day before CPI / payrolls / FOMC); robustness only, never a judgement"}


def publish_history(svc, conn: psycopg.Connection, out: dict, *, as_of: datetime) -> dict:
    if conn.execute("SELECT 1 FROM market.dataset_manifest WHERE dataset_id=%s AND manifest_state='READY' LIMIT 1", (HISTORY_DATASET,)).fetchone():
        raise RuntimeError(f"{HISTORY_DATASET} is already published: section 4 runs the history once")
    body = json.dumps(to_wire(out), sort_keys=True, ensure_ascii=False, default=str)
    rid = sha256(f"{as_of.isoformat()}{out.get('source')}".encode()).hexdigest()[:16]
    frame = pl.DataFrame({"run_id": [rid], "kind": ["report"], "body": [body]})
    cov = {"dataset": HISTORY_DATASET, "spec_sha256": R.SPEC_SHA256, "run_id": rid, "as_of": as_of.isoformat(), "frame_sha256": sha256(frame.write_csv().encode()).hexdigest()}
    return B.publish_derived(svc, HISTORY_DATASET, frame, cov, contract_id=FW.CONTRACT, watermark=as_of)


class E101Service:
    def __init__(self, conn: psycopg.Connection, lake: LakeStore, *, clock=None, loader=None):
        self.conn, self.lake, self.loader = conn, lake, loader
        self.svc = DataService(conn, lake, clock=clock)

    def _anchor(self, as_of: datetime) -> dict:
        row = self.conn.execute("SELECT * FROM market.dataset_manifest WHERE dataset_id IN ('bars_native_raw_1d','bars_raw_1d') AND manifest_state='READY' AND ready_at <= %s "
                                "ORDER BY (dataset_id='bars_native_raw_1d') DESC, manifest_version DESC LIMIT 1", (as_of,)).fetchone()
        if row is None:
            raise RuntimeError("no READY raw daily bars manifest: nothing to anchor the registration to")
        return row

    def register(self, as_of: datetime) -> None:
        REG.ensure_registered(self.conn, self._anchor(as_of), now=as_of)

    def forward(self, day: date, *, as_of: datetime | None = None, events: list[R.Event] | None = None) -> dict:
        as_of = as_of or datetime.now(timezone.utc)
        check_document()
        if day < R.FORWARD_START:
            return {"through": day.isoformat(), "appended": False, "reason": f"before the first D0 {R.FORWARD_START}"}
        self.register(as_of)
        return FW.step(self.conn, self.lake, self.svc, day, as_of=as_of, loader=self.loader, events=events)

    def plan(self, kind: str, day: date, *, out_dir: Path, as_of: datetime | None = None, events: list[R.Event] | None = None) -> dict:
        as_of = as_of or datetime.now(timezone.utc)
        check_document()
        self.register(as_of)
        return PL.run(self.conn, self.lake, self.svc, kind, day, as_of=as_of, out_dir=out_dir, loader=self.loader, events=events)

    def judge(self, today: date, *, as_of: datetime | None = None) -> dict:
        check_document()
        return FW.judge_and_store(self.svc, self.conn, self.lake, today, as_of or datetime.now(timezone.utc))

    def status(self, today: date) -> dict:
        return FW.overview(self.conn, self.lake, today)

    def history(self, *, calendar_file: Path, as_of: datetime | None = None, publish: bool = True) -> dict:
        as_of = as_of or datetime.now(timezone.utc)
        check_document()
        out = history(self.conn, self.lake, as_of=as_of, calendar_file=calendar_file)
        if publish and out.get("status") == "ok":
            self.register(as_of)
            out["snapshot_id"] = str(publish_history(self.svc, self.conn, out, as_of=as_of)["snapshot_id"])
        return out
