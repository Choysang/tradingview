"""E101 plan reports (pre-registration section 5): a D0 report before the close (14:30) and an R report before the open (09:00), recorded only, never deciding.

Why: the user asked for a closing plan on the day before a US release and an opening plan on the day after. The report holds only rule-computed lists and statistics: the event,
its release time, the consensus (none: no source, see ``bf_e101_forward.NO_CONSENSUS``), public rate probabilities ("无": none is recorded), the historical statistics of the
class (marked as already seen, for reference only), and the two groups of section 3 at the previous close. It carries no trading instruction; the top line says the test has not passed.
Rows ``plan_d0`` / ``plan_r`` (session = report day, code = class) are appended to ``e101_forward`` once; the same text is written to ``<out_dir>/<day>-<kind>-<class>.md``.
Not covered: scheduling (``deploy/windows/run_e101_plan.ps1``), delivery to a phone.
"""
from __future__ import annotations

import json
import os
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import psycopg

from pulse.adapters.lake import LakeStore
from pulse.application import bf_batch8_core as CORE8
from pulse.application import bf_e101_core as C
from pulse.application import bf_e101_forward as FW
from pulse.domain.e101 import rules as R

BANNER = "E101 预案：未通过检验，仅记录，不是交易建议"
KINDS = {"d0": "plan_d0", "r": "plan_r"}
TITLES = {"d0": "公布前收盘预案（D0 14:30）", "r": "公布后开盘预案（R 09:00）"}
CLASS_NAMES = {"US_CORE_PCE": "美国核心 PCE", "US_CPI": "美国 CPI", "US_NFP": "美国新增非农就业", "FOMC": "美联储利率决议"}


def latest_history(conn: psycopg.Connection, lake: LakeStore) -> dict | None:
    m = conn.execute("SELECT * FROM market.dataset_manifest WHERE dataset_id=%s AND manifest_state='READY' ORDER BY manifest_version DESC LIMIT 1", ("e101_history",)).fetchone()
    if m is None:
        return None
    rows = lake.read_snapshot_frames(m["content_hash"]).to_dicts()
    return json.loads(rows[0]["body"]) if rows else None


def _body(kind: str, day: date, e: R.Event, prev: date, groups: dict[str, list[str]], names: dict[str, str], hist: dict | None) -> dict[str, Any]:
    by = (hist or {}).get("by_class", {}).get(e.cls)
    body: dict[str, Any] = {"banner": BANNER, "kind": kind, "day": day.isoformat(), "event": FW._event_body(e), "consensus": None, "consensus_note": FW.NO_CONSENSUS,
                            "rate_probabilities": "无", "groups_at": prev.isoformat(), "groups": {g: [{"code": c, "name": names.get(c)} for c in v] for g, v in groups.items()},
                            "history": by, "history_note": "历史部分已看过、仅供参考（2023-11 ~ 2026-11 复核，不作判定依据）" if by else "历史复核尚未运行"}
    if kind == "r":
        body.update({"actual": None, "surprise": None, "direction": None,
                     "scenarios": "没有历史一致预期，无法按偏鹰 / 符合 / 偏鸽分情景；下面只有该类事件 R 日的无条件统计"})
    return body


def _fmt(x: float | None) -> str:
    return "无" if x is None else f"{x:+.2%}"


def markdown(b: dict) -> str:
    e = b["event"]
    lines = [f"# {b['banner']}", "", f"## {TITLES[b['kind']]}：{b['day']}", "",
             f"- 事件：{CLASS_NAMES.get(e['cls'], e['cls'])}（{'; '.join(e['names'])}）", f"- 公布时刻（北京时间）：{e['release_at']}",
             f"- D0：{e['d0']}；R：{e['r'] or '无'}", f"- 一致预期：无（{b['consensus_note']}）", f"- 公开利率概率：{b['rate_probabilities']}"]
    if b["kind"] == "r":
        lines += ["- 公布值：未记录", "- 超预期 s 与方向：无", f"- 情景：{b['scenarios']}"]
    lines += ["", f"### 历史条件统计（{b['history_note']}）", ""]
    h = b.get("history")
    if h:
        lines += [f"- 事件数 {h['n']}；D0 主板等权收益均值 {_fmt(h.get('d0_r'))}（非事件同星期几对照 {_fmt(h.get('d0_control'))}）",
                  f"- R 日开盘跳空均值 {_fmt(h.get('r_gap'))}；R 日开盘到收盘均值 {_fmt(h.get('r_intra'))}",
                  f"- D0 情绪龙头组超额均值 {_fmt(h.get('d0_leader_ex'))}（{h.get('d0_leader_n', 0)} 只次）；趋势核心组 {_fmt(h.get('d0_core_ex'))}（{h.get('d0_core_n', 0)} 只次）"]
    else:
        lines.append("- 无")
    lines += ["", f"### 规则名单（按第 3 节规则在 {b['groups_at']} 收盘算出，不是人工挑选）", ""]
    for g, title in (("leader", "情绪龙头组（连板数最高且 ≥ 3）"), ("core", "趋势核心组（流通市值前 10% 且 20 日涨幅前 20%）")):
        names = b["groups"][g]
        lines.append(f"- {title}：" + ("、".join(f"{x['code']} {x['name'] or ''}".strip() for x in names) if names else "无"))
    lines.append("")
    return "\n".join(lines)


def _names(conn: psycopg.Connection, codes: list[str]) -> dict[str, str]:
    if not codes:
        return {}
    rows = conn.execute("SELECT DISTINCT ON (a.source_symbol) a.source_symbol code, s.display_name FROM core.instrument_alias a"
                        " JOIN LATERAL (SELECT display_name FROM core.instrument_state x WHERE x.instrument_id=a.instrument_id ORDER BY state_version DESC LIMIT 1) s ON true"
                        " WHERE a.source_symbol = ANY(%s) ORDER BY a.source_symbol, a.effective_from DESC", (codes,)).fetchall()
    return {r["code"]: r["display_name"] for r in rows}


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def run(conn: psycopg.Connection, lake: LakeStore, svc, kind: str, day: date, *, as_of: datetime, out_dir: Path, loader=None, events: list[R.Event] | None = None) -> dict:
    if kind not in KINDS:
        raise ValueError(f"kind must be one of {sorted(KINDS)}")
    evs = events if events is not None else C.events_of(C.calendar_rows(conn, as_of))
    targets = [e for e in evs if (e.d0 if kind == "d0" else e.r) == day]
    if not targets:
        return {"day": day.isoformat(), "kind": kind, "appended": False, "reason": "no US release with this report day"}
    stored = FW.stored_rows(conn, lake)
    ix = FW.index(stored)
    todo = [e for e in targets if f"{day.isoformat()}|{e.cls}" not in ix[KINDS[kind]]]
    if not todo:
        return {"day": day.isoformat(), "kind": kind, "appended": False, "reason": "already recorded"}
    ws = CORE8.Windows(conn, lake, as_of, day - timedelta(days=FW.WINDOW_DAYS), loader=loader)
    win = ws.best(None)
    if win is None:
        return {"day": day.isoformat(), "kind": kind, "appended": False, "not_ready": True, "reason": "no raw daily bars window"}
    inp, _ = ws.prepared(win)
    panel = C.with_helpers(inp.panel)
    prev = C.previous_session(inp.sessions, day)
    want = C.previous_session(C.open_sessions(day - timedelta(days=15), day), day)
    if prev is None or prev != want:
        return {"day": day.isoformat(), "kind": kind, "appended": False, "not_ready": True,
                "reason": f"bars of the previous open day {want} are not ready (latest {prev}); nothing recorded"}
    groups = C.groups_at(panel, prev)
    names = _names(conn, sorted({c for v in groups.values() for c in v}))
    hist = latest_history(conn, lake)
    rows, files = [], []
    for e in todo:
        b = _body(kind, day, e, prev, groups, names, hist)
        rows.append(FW._row(day, KINDS[kind], e.cls, b))
        path = out_dir / f"{day.isoformat()}-{kind}-{e.cls}.md"
        _write(path, markdown(b))
        files.append(str(path))
    FW.publish(svc, rows, as_of=as_of, stored_keys=ix["keys"])
    return {"day": day.isoformat(), "kind": kind, "appended": True, "events": [e.key for e in todo], "groups_at": prev.isoformat(),
            "groups": {g: len(v) for g, v in groups.items()}, "files": files}
