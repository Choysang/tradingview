"""Versioned calendar events: lock-up expiries, suspensions, meetings, macro releases, exchange rule dates (PEX07, D09).

Why: the old site kept a calendar store (research seeds + Eastmoney 财经日历 + rule-computed expiries) whose rows were
revised, never overwritten. This module keeps those semantics for pulse3: an event is identified by a stable key, every
observation with different content becomes the next revision, and a revision is usable only from its real
``available_at`` (we cannot know a schedule before we saw it).
Invariants:
* identity ``calendar_event_id = uuid5(source_id, source_event_key)``; a changed schedule/status/name is a new revision
  (``postponed`` when the planned time moves, ``corrected`` when the name/content changes, ``cancelled`` only on explicit
  evidence); a row that disappears from a source is NOT a cancellation (absence is not evidence);
* a date-only event never carries a clock time (``time_precision`` date/date_range <-> ``scheduled_at`` NULL);
* expected / actual are separate ``Measurement`` objects (initial value and later revisions are different revisions);
* rule-computed expiry dates stay ``rule_computed`` until a notice confirms them.
Not covered: fetching (adapters), stock resolution (application layer, point in time from stored memberships).
"""
from __future__ import annotations

import hashlib
import json
import re
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Callable
from zoneinfo import ZoneInfo

CST = ZoneInfo("Asia/Shanghai")
CALENDAR_NS = uuid.UUID("5e0a3c52-14d7-4b8a-9d36-2c7f0b1e4a90")
LINK_RULE_VERSION = "event-links-v2"
PRECISIONS = ("minute", "approx_minute", "date", "date_range")
STATUSES = ("scheduled", "postponed", "cancelled", "occurred", "corrected", "rule_computed")
CATEGORIES = ("macro_release", "policy_meeting", "conference", "market_rule", "company_lockup", "company_suspension",
              "company_meeting", "overseas", "index_adjustment", "other")


@dataclass(frozen=True)
class Observation:
    """One reading of an event from one source."""
    source_id: str
    source_event_key: str
    name: str
    category: str
    region: str
    scheduled_at: datetime | None
    scheduled_date: date | None
    time_precision: str
    status: str = "scheduled"
    expected: dict | None = None
    actual: dict | None = None
    instrument_code: str | None = None
    notes: dict = field(default_factory=dict)

    def __post_init__(self):
        if self.time_precision not in PRECISIONS:
            raise ValueError(f"unknown time precision {self.time_precision!r}")
        if self.status not in STATUSES:
            raise ValueError(f"unknown status {self.status!r}")
        if (self.time_precision in ("minute", "approx_minute")) != (self.scheduled_at is not None):
            raise ValueError("a minute-precision event needs scheduled_at; a date-only event must not carry a clock time")
        if self.scheduled_at is not None and self.scheduled_at.tzinfo is None:
            raise ValueError("scheduled_at must be timezone-aware")
        if self.scheduled_at is None and self.scheduled_date is None:
            raise ValueError("an event needs a time or a date")
        if self.category not in CATEGORIES:
            raise ValueError(f"unknown category {self.category!r}")

    @property
    def event_id(self) -> uuid.UUID:
        return event_id(self.source_id, self.source_event_key)

    @property
    def day(self) -> date:
        return self.scheduled_at.astimezone(CST).date() if self.scheduled_at else self.scheduled_date  # type: ignore[return-value]


def event_id(source_id: str, key: str) -> uuid.UUID:
    return uuid.uuid5(CALENDAR_NS, f"{source_id}|{key}")


def content_hash(o: Observation) -> str:
    """Hash of everything that makes a revision different (notes excluded: provenance chatter is not a schedule change)."""
    doc = {"name": o.name, "category": o.category, "region": o.region,
           "at": o.scheduled_at.astimezone(timezone.utc).isoformat() if o.scheduled_at else None,
           "date": o.scheduled_date.isoformat() if o.scheduled_date else None, "precision": o.time_precision,
           "status": o.status, "expected": o.expected, "actual": o.actual, "code": o.instrument_code}
    return hashlib.sha256(json.dumps(doc, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()


_CANCEL = re.compile(r"取消|撤销举行|不再召开|终止召开")
_POSTPONE = re.compile(r"延期|推迟|顺延|改期")
_CORRECT = re.compile(r"更正|修订|补充")


def classify_change(prev: dict | None, o: Observation) -> str:
    """Status of the new revision relative to the previous stored one (``prev`` = stored row as dict)."""
    if o.status in ("occurred", "cancelled"):
        return o.status
    if _CANCEL.search(o.name):
        return "cancelled"
    if o.actual is not None:
        return "occurred"
    if prev is None:
        return o.status
    moved = (prev["scheduled_at"], prev["scheduled_date"]) != (o.scheduled_at, o.scheduled_date)
    if not moved and prev["name"] == o.name and prev["expected"] == o.expected and o.status == "scheduled":
        return prev["status"]            # a re-read of an unchanged schedule keeps what we already concluded
    if moved or _POSTPONE.search(o.name):
        return "postponed"
    if prev["name"] != o.name or _CORRECT.search(o.name) or prev["expected"] != o.expected:
        return "corrected"
    return o.status


def next_revision(prev: dict | None, o: Observation) -> tuple[str, int, str]:
    """('new'|'revised'|'same', revision number, resulting status). Same content never adds a revision."""
    status = classify_change(prev, o)
    final = Observation(**{**o.__dict__, "status": status})
    if prev is None:
        return "new", 1, status
    if prev["content_hash"] == content_hash(final):
        return "same", prev["revision"], prev["status"]
    return "revised", prev["revision"] + 1, status


def finalize(o: Observation, status: str) -> Observation:
    return Observation(**{**o.__dict__, "status": status})


def visible(rows: list[dict], decision_as_of: datetime) -> list[dict]:
    """Latest revision of each event that was known at ``decision_as_of`` (point in time)."""
    best: dict[uuid.UUID, dict] = {}
    for r in rows:
        if r["available_at"] <= decision_as_of and (r["calendar_event_id"] not in best or r["revision"] > best[r["calendar_event_id"]]["revision"]):
            best[r["calendar_event_id"]] = r
    return sorted(best.values(), key=lambda r: (r["scheduled_at"] or datetime.combine(r["scheduled_date"], datetime.min.time(), CST), str(r["calendar_event_id"])))


# ---- rule-computed exchange dates (index futures/options: third Friday; ETF options: fourth Wednesday) -----------------------
def _nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    d = date(year, month, 1)
    d += timedelta(days=(weekday - d.weekday()) % 7)
    return d + timedelta(weeks=n - 1)


def expiries(year: int, month: int, is_trading_day: Callable[[date], bool]) -> dict[str, date]:
    """Expiry days of one month, rolled forward to the next trading day. Rule-computed: not a notice."""
    out: dict[str, date] = {}
    for name, weekday, n in (("index_futures_options", 4, 3), ("etf_options", 2, 4)):
        d = _nth_weekday(year, month, weekday, n)
        for _ in range(15):
            if is_trading_day(d):
                break
            d += timedelta(days=1)
        else:
            raise LookupError(f"no trading day within 15 days after {d}")
        out[name] = d
    return out


def expiry_observations(year: int, month: int, is_trading_day: Callable[[date], bool]) -> list[Observation]:
    labels = {"index_futures_options": "股指期货/期权交割日", "etf_options": "ETF期权到期日"}
    return [Observation("pulse_rules", f"expiry:{name}:{d.isoformat()[:7]}", f"{labels[name]}（规则推算）", "market_rule", "CN",
                        None, d, "date", "rule_computed", notes={"rule": "third-friday/fourth-wednesday rolled to next trading day",
                                                                  "confirmed_by_notice": False})
            for name, d in expiries(year, month, is_trading_day).items()]


# ---- event -> industry links (board display names of legacy_boards_industry; stocks resolved point in time elsewhere) --------
# (name pattern, board names, theme)
LINK_RULES: tuple[tuple[str, tuple[str, ...], str], ...] = (
    (r"NVIDIA|英伟达|AMD|算力", ("电子器件", "电子信息"), "AI算力"),
    (r"进口博览会|广交会|外贸", ("物资外贸", "交通运输"), "外贸"),
    (r"互联网大会|互联网之光|百度世界|人工智能|AI", ("电子信息", "传媒娱乐"), "AI应用"),
    (r"半导体|芯片|集成电路", ("电子器件",), "半导体"),
    (r"航空|航天|飞机", ("飞机制造",), "航空"),
    (r"城市更新|房地产|楼市", ("房地产", "建筑建材"), "城市更新"),
    (r"农产品|农业|种业", ("农林牧渔",), "农业"),
    (r"光伏|储能|新能源", ("电力行业", "发电设备"), "光伏储能"),
    (r"汽车|自动驾驶", ("汽车制造",), "智能汽车"),
    (r"机器人|具身智能", ("机械行业", "仪器仪表"), "机器人"),
    (r"美博会|美丽产业|化妆品", ("化工行业", "其它行业"), "美妆"),
    (r"双十一|电商", ("商业百货", "交通运输"), "电商"),
    (r"医药|药品|医疗", ("生物制药", "医疗器械"), "医药"),
    (r"白酒|酒业", ("酿酒行业",), "白酒"),
    (r"煤炭|能源大会", ("煤炭行业", "石油行业"), "能源"),
)
SKIP_CATEGORIES = ("macro_release", "market_rule")      # too broad to point at one industry (deliberately not linked)


def links_for(o: Observation) -> list[tuple[str, str]]:
    """(board display name, theme) pairs for an event; empty for macro releases and market rules."""
    if o.category in SKIP_CATEGORIES:
        return []
    out: list[tuple[str, str]] = []
    for pattern, boards, theme in LINK_RULES:
        if re.search(pattern, o.name):
            out.extend((b, theme) for b in boards)
    return list(dict.fromkeys(out))
