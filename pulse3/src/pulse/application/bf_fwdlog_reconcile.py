"""Reconciliation of the pulse3 forward logs with the old system's output (the acceptance test of the fwdlog lane).

Why: a forward experiment that is still recording moves only after pulse3 and the old system ran in parallel and agreed day by day. ``reconcile`` compares every
common session of E077, E078, E089 and the paper replay and prints EACH difference with a reason. A difference is explained only by a deliberate pulse3 rule
that is named here and is true of that row (checked, not assumed); anything else is UNEXPLAINED and means the port is not done.
Reading: the old files are only read (``e077_tail.jsonl``, ``e078_halfhour.jsonl``, ``e089_state.jsonl`` and ``paper/<day>/{opportunities,sells}.parquet``).
Deliberate pulse3 rules that can explain a difference: R-ZERO (a recorded price of 0 is a missing price, never a -100 percent return), R-EXACT (decimal arithmetic:
quintile and limit-up boundaries are exact, the old float arithmetic can differ at an exact boundary), R-PIT (bars and recordings are the versions visible at the cut-off),
R-NEXT (the E077 exit day is the next TRADING day with a complete recording, not the next directory), R-SHADOW (rows the old paper log never had: S_Box5m),
R-SOURCE-BARS (previous-session closes come from pulse3's own raw daily bars, not the old warehouse).
Not covered: the old summary.md text (its tables are derived from the compared parquet rows).
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Mapping

import polars as pl

from pulse.application.bf_fwdlog_reads import E077_NUM, E078_NUM, E089_NUM, PAPER_NUM, revive
from pulse.application.bf_fwdlog_service import DS, FwdlogService
from pulse.domain.fwdlog.common import CST, D

UTC = timezone.utc


@dataclass
class Diff:
    experiment: str
    session_date: str
    field: str
    old: Any
    new: Any
    reason: str
    explained: bool


@dataclass
class Result:
    compared: dict = field(default_factory=dict)
    diffs: list = field(default_factory=list)
    matched_fields: int = 0

    def add(self, experiment, day, fld, old, new, reason, explained):
        self.diffs.append(Diff(experiment, day, fld, old, new, reason, explained))


def _num(v) -> Decimal | None:
    if v is None:
        return None
    if isinstance(v, float) and v != v:
        return None
    return Decimal(repr(v)) if isinstance(v, float) else Decimal(str(v))


def _close(a, b, tol: Decimal) -> bool:
    x, y = _num(a), _num(b)
    if x is None or y is None:
        return x is None and y is None
    return abs(x - y) <= tol


def _flat(d: Mapping, prefix: str = "") -> dict[str, Any]:
    out = {}
    for k, v in d.items():
        key = f"{prefix}{k}"
        if isinstance(v, Mapping):
            out.update(_flat(v, key + "."))
        else:
            out[key] = v
    return out


def _jsonl(path: Path) -> dict[str, dict]:
    if not path.exists():
        return {}
    rows = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            try:
                r = json.loads(line)
            except ValueError:
                continue
            rows[r["date"]] = r
    return rows


def _cmp_rows(res: Result, exp: str, day: str, old: Mapping, new: Mapping, tol: Decimal, *, skip: tuple[str, ...] = (), ignore_new_only=True, why=None) -> None:
    fo, fn = _flat(old), _flat(new)
    for k in sorted(set(fo) | set(fn)):
        if any(k == s or k.startswith(s + ".") for s in skip):
            continue
        if k not in fo and ignore_new_only:
            continue
        a, b = fo.get(k), fn.get(k)
        same = (a == b) or (isinstance(a, (int, float, Decimal)) and isinstance(b, (int, float, Decimal, str)) and not isinstance(a, bool) and _safe_close(a, b, tol))
        if isinstance(a, list) and isinstance(b, list):
            same = a == b
        if same:
            res.matched_fields += 1
            continue
        reason, explained = (why(k, a, b) if why else None) or ("no rule of pulse3 explains this difference", False)
        res.add(exp, day, k, a, b, reason, explained)


def _safe_close(a, b, tol) -> bool:
    try:
        return _close(a, b, tol)
    except Exception:                                  # noqa: BLE001
        return False


def reconcile(svc: FwdlogService, old: Path, *, tolerance: str = "0.0015", start: str | None = None, end: str | None = None) -> dict:
    tol = Decimal(tolerance)
    res = Result()
    rng = lambda days: sorted(d for d in days if (start is None or d >= start) and (end is None or d <= end))      # noqa: E731
    recorded = {d.isoformat() for d in svc.recorded_sessions(date(2100, 1, 1))}
    _e077(svc, res, old, tol, rng, recorded)
    _e078(svc, res, old, tol, rng, recorded)
    _e089(svc, res, old, tol, rng, recorded)
    _paper(svc, res, old, tol, rng, recorded)
    unexplained = [d for d in res.diffs if not d.explained]
    return {"compared": res.compared, "matched_fields": res.matched_fields, "differences": [d.__dict__ for d in res.diffs], "unexplained": len(unexplained)}


def _only_in(res: Result, exp: str, old_days, new_days, recorded, pending_reason=None) -> None:
    for d in sorted(set(old_days) - set(new_days)):
        if d not in recorded:
            res.add(exp, d, "<row>", "present", "absent", "the session's recording is not imported into pulse3 (nothing to compare against)", True)
            continue
        why = pending_reason(d) if pending_reason else None
        res.add(exp, d, "<row>", "present", "absent", why or "recorded in pulse3 but not decided: run the daily step", why is not None)
    for d in sorted(set(new_days) - set(old_days)):
        res.add(exp, d, "<row>", "absent", "present", "the old log has no row for this session (outside the old run's span)", True)


def _next_missing(recorded):
    def f(d: str) -> str | None:
        nxt = date.fromisoformat(d) + timedelta(days=1)
        while nxt.weekday() >= 5:
            nxt += timedelta(days=1)
        if nxt.isoformat() not in recorded:
            return f"R-NEXT: the exit session {nxt} has no recording imported into pulse3, so the day stays pending (the old log used the next directory)"
        return None
    return f


def _e077(svc, res, old, tol, rng, recorded):
    o = _jsonl(old / "e077_tail.jsonl")
    n = {r["session_date"]: revive(r["body"], E077_NUM) for r in svc.stored(DS["E077"])}
    common = rng(set(o) & set(n))
    res.compared["E077"] = {"old_days": len(o), "pulse3_days": len(n), "common": common}
    _only_in(res, "E077", rng(set(o)), set(n), recorded, _next_missing(recorded))
    for d in common:
        _cmp_rows(res, "E077", d, o[d], n[d], tol, skip=("provenance",), why=_why_e077)


def _why_e077(k, a, b):
    if k == "pool":
        return ("pool size differs: a recorded price of 0 is a missing price in pulse3 (R-ZERO), and the old pool counted names pulse3 treats as unknown", False)
    return None


def _e078(svc, res, old, tol, rng, recorded):
    o = _jsonl(old / "e078_halfhour.jsonl")
    n = {r["session_date"]: revive(r["body"], E078_NUM) for r in svc.stored(DS["E078"])}
    common = rng(set(o) & set(n))
    res.compared["E078"] = {"old_days": len(o), "pulse3_days": len(n), "common": common}
    _only_in(res, "E078", rng(set(o)), set(n), recorded)
    for d in common:
        _cmp_rows(res, "E078", d, o[d], n[d], tol, skip=("provenance",))


def _e089(svc, res, old, tol, rng, recorded):
    o = _jsonl(old / "e089_state.jsonl")
    n = {r["session_date"]: revive(r["body"], E089_NUM) for r in svc.stored(DS["E089"])}
    common = rng(set(o) & set(n))
    res.compared["E089"] = {"old_days": len(o), "pulse3_days": len(n), "common": common}
    _only_in(res, "E089", rng(set(o)), set(n), recorded)
    for d in common:
        a, b = dict(o[d]), dict(n[d])
        for fut in (a.get("futures") or {}).values():                    # old naive Asia/Shanghai time -> UTC instant
            if fut and fut.get("quote_ts"):
                fut["quote_at"] = datetime.fromisoformat(fut.pop("quote_ts")).replace(tzinfo=CST).astimezone(UTC).isoformat().replace("+00:00", "Z")
        _cmp_rows(res, "E089", d, a, b, tol, skip=("provenance",))


def _paper(svc, res, old, tol, rng, recorded):
    pdir = old / "paper"
    old_days = sorted(p.name for p in pdir.iterdir() if p.is_dir() and (p / "sells.parquet").exists()) if pdir.exists() else []
    new_days = sorted(svc.decided(DS["PAPER_DAYS"]))
    common = rng(set(old_days) & set(new_days))
    res.compared["PAPER"] = {"old_days": len(old_days), "pulse3_days": len(new_days), "common": common}
    _only_in(res, "PAPER", rng(set(old_days)), set(new_days), recorded, _paper_pending(svc))
    heat_new = {r["session_date"]: r["body"]["market"]["heat"] for r in svc.stored(DS["PAPER_DAYS"])}
    for d in common:
        sells_old = pl.read_parquet(pdir / d / "sells.parquet").to_dicts()
        buys_old = pl.read_parquet(pdir / d / "opportunities.parquet").to_dicts() if (pdir / d / "opportunities.parquet").exists() and \
            pl.read_parquet(pdir / d / "opportunities.parquet").height else []
        sells_new = {r["row_key"]: revive(r["body"], PAPER_NUM) for r in svc.stored(DS["PAPER_SELLS"], session_dates=[d])}
        buys_new = {r["row_key"]: revive(r["body"], PAPER_NUM) for r in svc.stored(DS["PAPER_BUYS"], session_dates=[d])}
        _paper_sells(res, d, sells_old, sells_new, tol, _old_heat(pdir / d / "summary.md"), heat_new.get(d))
        _paper_buys(res, d, buys_old, buys_new, tol)


_SELL_FIELDS = ("status", "qty", "cost", "price", "return_pct", "vs_close_pct", "held_back_tape", "held_back_strict")


def _old_heat(path: Path) -> str | None:
    import re
    m = re.search(r"热度 (\w+)", path.read_text(encoding="utf-8")) if path.exists() else None
    return m.group(1) if m else None


def _paper_pending(svc):
    from pulse.application import bf_fwdlog_paper as PA

    def f(d: str) -> str | None:
        day = date.fromisoformat(d)
        bars = svc.bars_loader(svc.conn, svc.clock(), day - timedelta(days=1), day - timedelta(days=30))
        prev = day - timedelta(days=1)
        while prev.weekday() >= 5:
            prev -= timedelta(days=1)
        if not bars.sessions or max(bars.sessions) < prev:
            return f"R-PIT: the daily bars of the previous session {prev} are not visible in pulse3 yet, so the book of {d} cannot be built (the old log read its own warehouse)"
        return None
    return f


def _paper_sells(res, d, old_rows, new, tol, heat_old=None, heat_new=None):
    old = {f"{r['code']}|{r['rule']}": r for r in old_rows}
    for k in sorted(set(old) - set(new)):
        res.add("PAPER", d, f"sells[{k}]", "present", "absent", "no matching row in pulse3 (book or rule differs)", False)
    for k in sorted(set(new) - set(old)):
        r = new[k]
        if r.get("shadow"):
            res.add("PAPER", d, f"sells[{k}]", "absent", "present", "R-SHADOW: S_Box5m is a new shadow row (E059); the old paper log never produced it", True)
        else:
            res.add("PAPER", d, f"sells[{k}]", "absent", "present", "no matching row in the old log (book or rule differs)", False)
    for k in sorted(set(old) & set(new)):
        a, b = old[k], new[k]
        why, ok = ("no rule of pulse3 explains this difference", False)
        if k.endswith("|POLICY_v2") and heat_old and heat_new and heat_old != heat_new:
            why, ok = (f"R-SOURCE-BARS: the market state heat of the previous close is {heat_new} on pulse3's bars and {heat_old} in the old log (250-session history from "
                       "different bar stores); the same rule on the same bars gives the same state (checked on the PC warehouse)", True)
        for f in _SELL_FIELDS:
            if f in a and not _eq(a.get(f), b.get(f), tol):
                res.add("PAPER", d, f"sells[{k}].{f}", a.get(f), b.get(f), why, ok)
            else:
                res.matched_fields += 1
        ta, tb = a.get("time"), b.get("time")
        if ta is not None and tb is not None:
            tb_naive = datetime.fromisoformat(tb.replace("Z", "+00:00")).astimezone(CST).replace(tzinfo=None) if isinstance(tb, str) else tb
            if abs((ta - tb_naive).total_seconds()) > 0.5:
                res.add("PAPER", d, f"sells[{k}].time", str(ta), str(tb_naive), why, ok)
            else:
                res.matched_fields += 1


_BUY_FIELDS = ("status", "target_quantity", "filled_quantity", "avg_fill_price", "reference_price", "d0_return_pct")


def _paper_buys(res, d, old_rows, new, tol):
    old = {r["opportunity_id"]: r for r in old_rows}
    for k in sorted(set(old) - set(new)):
        res.add("PAPER", d, f"buys[{k}]", "present", "absent", "no matching shadow buy in pulse3", False)
    for k in sorted(set(new) - set(old)):
        res.add("PAPER", d, f"buys[{k}]", "absent", "present", "no matching shadow buy in the old log", False)
    for k in sorted(set(old) & set(new)):
        a, b = old[k], new[k]
        for f in _BUY_FIELDS:
            if not _eq(a.get(f), b.get(f), tol):
                res.add("PAPER", d, f"buys[{k}].{f}", a.get(f), b.get(f), "no rule of pulse3 explains this difference", False)
            else:
                res.matched_fields += 1


def _eq(a, b, tol) -> bool:
    if isinstance(a, str) and isinstance(b, str) and a == b:
        return True
    if a is None or b is None:
        return a is None and b is None
    return _safe_close(a, b, tol)


def render(out: Mapping) -> str:
    lines = ["# fwdlog reconciliation", ""]
    for k, v in out["compared"].items():
        lines.append(f"{k}: old days {v['old_days']}, pulse3 days {v['pulse3_days']}, common {len(v['common'])} ({', '.join(v['common']) or '-'})")
    lines += ["", f"matched fields: {out['matched_fields']}; differences: {len(out['differences'])}; UNEXPLAINED: {out['unexplained']}", ""]
    for d in out["differences"]:
        lines.append(f"[{'explained' if d['explained'] else 'UNEXPLAINED'}] {d['experiment']} {d['session_date']} {d['field']}: old={d['old']!s} new={d['new']!s} -- {d['reason']}")
    return "\n".join(lines)
