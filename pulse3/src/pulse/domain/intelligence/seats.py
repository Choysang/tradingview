"""Longhubang seat reports: names, units, windows, net-in-report, PIT use and style hypotheses (spec FINAL §5.2, SS-A11,14-18).

Why: a seat is a DISCLOSURE UNIT (a broker branch or a report-local label), not a person or an account.
Names are resolved only on evidence in the disclosed name itself (broker legal name + a city from a
versioned gazetteer + a branch suffix); everything else stays ``unresolved`` / ``ambiguous`` /
``report_local_label`` and is never forced onto a stable id. Amounts keep their raw unit and convert exactly.

Invariants: ``机构专用`` numbering is report-local (two days = two labels, never one institution);
nicknames such as "拉萨天团" are ``ambiguous`` groups without members; "前东路" alone is ``unresolved``;
overlapping disclosure windows are never summed; a report is usable only from its actual ``available_at``
(an after-close report cannot enter a same-day intraday decision); style statistics use only reports whose
entry session starts after ``available_at`` and outcomes known by the evaluation ``as_of``. Legacy float 亿
amounts are converted through their shortest decimal representation (``repr``) into CNY_10000, never by
float arithmetic. Not covered: price data access, the cost-aware execution of any seat strategy.
"""
from __future__ import annotations

import hashlib
import math
import re
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from typing import Callable, Iterable, Sequence

GAZETTEER_VERSION = "cn-city-gazetteer-v1"
RESOLVER_VERSION = "seat-name-rules-v1"
SEAT_NS = uuid.UUID("5a3c7e19-0d44-4b8e-9f10-2c6a1d7b3e55")
HYP_NS = uuid.UUID("9e7d1b34-62aa-4f0c-8d15-71c3b0a4e2f9")

_CITIES = """北京 上海 天津 重庆 石家庄 唐山 秦皇岛 邯郸 邢台 保定 张家口 承德 沧州 廊坊 衡水 太原 大同 阳泉 长治 晋城 朔州 晋中 运城 忻州 临汾 吕梁
呼和浩特 包头 乌海 赤峰 通辽 鄂尔多斯 呼伦贝尔 巴彦淖尔 乌兰察布 沈阳 大连 鞍山 抚顺 本溪 丹东 锦州 营口 阜新 辽阳 盘锦 铁岭 朝阳 葫芦岛
长春 吉林 四平 辽源 通化 白山 松原 白城 延吉 哈尔滨 齐齐哈尔 鸡西 鹤岗 双鸭山 大庆 伊春 佳木斯 七台河 牡丹江 黑河 绥化
南京 无锡 徐州 常州 苏州 南通 连云港 淮安 盐城 扬州 镇江 泰州 宿迁 昆山 常熟 张家港 江阴 宜兴 太仓 杭州 宁波 温州 嘉兴 湖州 绍兴 金华 衢州 舟山 台州 丽水
义乌 慈溪 余姚 诸暨 瑞安 乐清 温岭 临安 合肥 芜湖 蚌埠 淮南 马鞍山 淮北 铜陵 安庆 黄山 滁州 阜阳 宿州 六安 亳州 池州 宣城
福州 厦门 莆田 三明 泉州 漳州 南平 龙岩 宁德 晋江 石狮 南昌 景德镇 萍乡 九江 新余 鹰潭 赣州 吉安 宜春 抚州 上饶
济南 青岛 淄博 枣庄 东营 烟台 潍坊 济宁 泰安 威海 日照 临沂 德州 聊城 滨州 菏泽 郑州 开封 洛阳 平顶山 安阳 鹤壁 新乡 焦作 濮阳 许昌 漯河 三门峡 南阳 商丘 信阳 周口 驻马店 济源
武汉 黄石 十堰 宜昌 襄阳 鄂州 荆门 孝感 荆州 黄冈 咸宁 随州 恩施 长沙 株洲 湘潭 衡阳 邵阳 岳阳 常德 张家界 益阳 郴州 永州 怀化 娄底
广州 韶关 深圳 珠海 汕头 佛山 江门 湛江 茂名 肇庆 惠州 梅州 汕尾 河源 阳江 清远 东莞 中山 潮州 揭阳 云浮 顺德 南宁 柳州 桂林 梧州 北海 防城港 钦州 贵港 玉林 百色 贺州 河池 来宾 崇左
海口 三亚 三沙 儋州 成都 自贡 攀枝花 泸州 德阳 绵阳 广元 遂宁 内江 乐山 南充 眉山 宜宾 广安 达州 雅安 巴中 资阳 西昌 贵阳 六盘水 遵义 安顺 毕节 铜仁 凯里
昆明 曲靖 玉溪 保山 昭通 丽江 普洱 临沧 大理 楚雄 红河 文山 西双版纳 拉萨 日喀则 昌都 林芝 山南 那曲 阿里 西安 铜川 宝鸡 咸阳 渭南 延安 汉中 榆林 安康 商洛
兰州 嘉峪关 金昌 白银 天水 武威 张掖 平凉 酒泉 庆阳 定西 陇南 西宁 海东 银川 石嘴山 吴忠 固原 中卫 乌鲁木齐 克拉玛依 吐鲁番 哈密 昌吉 库尔勒 喀什 伊犁""".split()
CITIES: tuple[str, ...] = tuple(sorted(set(_CITIES), key=lambda c: (-len(c), c)))

_BROKER = re.compile(r"^(?P<broker>[一-龥A-Za-z（）()·]{2,30}?(?:股份有限公司|有限责任公司|有限公司))(?P<rest>.+)$")
_BRANCH_SUFFIX = re.compile(r"(证券营业部|营业部|分公司)$")
_LOCAL_LABEL = re.compile(r"^机构专用\s*\d*$")
_STOCK_CONNECT = re.compile(r"^(沪股通|深股通|港股通)专用$")
_NICKNAME = re.compile(r"(天团|帮|系|游资|敢死队|一哥|大军)$")


@dataclass(frozen=True)
class SeatResolution:
    status: str  # resolved | ambiguous | unresolved | report_local_label
    reason: str
    broker_legal_name: str | None = None
    city: str | None = None
    canonical_name: str | None = None
    seat_id: uuid.UUID | None = None

    def evidence(self, raw_name: str, **extra) -> dict:
        d = {"resolver": RESOLVER_VERSION, "gazetteer": GAZETTEER_VERSION, "reason": self.reason, "raw_seat_name": raw_name}
        if self.status == "resolved":
            d.update({"broker_legal_name": self.broker_legal_name, "city": self.city, "city_basis": "name_prefix_in_gazetteer"})
        d.update(extra)
        return d


def seat_uuid(broker_legal_name: str, canonical_name: str, city: str) -> uuid.UUID:
    return uuid.uuid5(SEAT_NS, f"{broker_legal_name}|{canonical_name}|{city}")


def resolve_seat_name(raw: str) -> SeatResolution:
    """Resolve a DISCLOSED name. The raw string is always kept by the caller; nothing here corrects it."""
    name = re.sub(r"\s+", "", raw or "")
    if not name:
        return SeatResolution("unresolved", "empty_name")
    if _LOCAL_LABEL.match(name):
        return SeatResolution("report_local_label", "generic_institution_label_numbering_is_report_local")
    if _STOCK_CONNECT.match(name):
        return SeatResolution("unresolved", "stock_connect_special_unit_has_no_broker_legal_name_in_this_model")
    if _NICKNAME.search(name) and not _BROKER.match(name):
        return SeatResolution("ambiguous", "group_nickname_without_versioned_membership")
    m = _BROKER.match(name)
    if not m:
        return SeatResolution("unresolved", "no_broker_legal_name_in_text")
    broker, rest = m["broker"], m["rest"]
    if not _BRANCH_SUFFIX.search(rest):
        return SeatResolution("unresolved", "not_a_branch_name_suffix")
    city = next((c for c in CITIES if rest.startswith(c)), None)
    if city is None:
        return SeatResolution("unresolved", "city_not_determinable_from_name")
    return SeatResolution("resolved", "broker_legal_name_city_prefix_branch_suffix", broker, city, name, seat_uuid(broker, name, city))


def resolve_mention(text: str) -> SeatResolution:
    """A user's oral mention (e.g. "前东路"): resolvable only if it is a full disclosed name; a fragment never is."""
    return resolve_seat_name(text)


# ---- amounts ----------------------------------------------------------------------------------------------
UNIT_MULTIPLIER = {"CNY": Decimal(1), "CNY_10000": Decimal(10000)}


def to_cny(raw_amount: Decimal, raw_unit: str) -> Decimal:
    try:
        return raw_amount * UNIT_MULTIPLIER[raw_unit]
    except KeyError:
        raise ValueError(f"unknown seat amount unit {raw_unit!r}") from None


def norm_amount(d: Decimal) -> str:
    """Scale-independent text of an amount (26941100.000000 and 26941100.000000000000 hash alike)."""
    return format(d.normalize(), "f") if d != 0 else "0"


def legacy_yi_to_wan(value: float | None) -> Decimal | None:
    """Legacy float 亿元 -> exact 万元 Decimal via the float's shortest repr (0.269411 -> 2694.11); missing stays None."""
    if value is None:
        return None
    # digits below 0.01 CNY (1e-10 亿) in a float repr are binary noise (0.06119884179999999), not information: quantize to cents
    return Decimal(repr(float(value))).quantize(Decimal("1E-10")) * Decimal(10000)


def _cny_text(wan: Decimal | None) -> str | None:
    return None if wan is None else str(wan * 10000)


# ---- reports ----------------------------------------------------------------------------------------------
@dataclass(frozen=True)
class SeatRowRec:
    side: str
    rank: int
    raw_seat_name: str
    raw_amount: Decimal
    raw_unit: str
    resolution: SeatResolution
    source_amounts: dict
    source_seat_code: str | None = None

    @property
    def canonical_amount_cny(self) -> Decimal:
        return to_cny(self.raw_amount, self.raw_unit)


@dataclass(frozen=True)
class SeatReportRec:
    source_id: str
    source_report_id: str
    exchange_code: str
    code: str
    window_start: date
    window_end: date
    disclosure_reason: str
    rule_version: str
    trade_scope: str
    rows: tuple[SeatRowRec, ...]
    quality_status: str
    notes: tuple[str, ...] = ()
    available_at: datetime | None = None
    extra: dict = field(default_factory=dict)

    def content_hash(self) -> str:
        parts = [self.window_start.isoformat(), self.window_end.isoformat(), self.disclosure_reason]
        for r in self.rows:
            parts.append(f"{r.side}|{r.rank}|{r.raw_seat_name}|{norm_amount(r.canonical_amount_cny)}")
        return hashlib.sha256("\n".join(parts).encode()).hexdigest()


_NUMWORD = {"三": 3, "五": 5, "十": 10, "二": 2, "四": 4}
_WINDOW = re.compile(r"连续(\d+|[二三四五十])个(?:交易日|工作日)")


def window_sessions(reason: str) -> int:
    m = _WINDOW.search(reason or "")
    if not m:
        return 1
    g = m.group(1)
    return int(g) if g.isdigit() else _NUMWORD[g]


def window_start(reason: str, window_end: date, sessions: Sequence[date]) -> tuple[date, bool]:
    """(start, complete). ``sessions`` = ascending observed trading dates; incomplete when history is too short."""
    n = window_sessions(reason)
    if n == 1:
        return window_end, True
    earlier = [d for d in sessions if d <= window_end]
    if len(earlier) >= n and earlier[-1] == window_end:
        return earlier[-n], True
    return window_end, False


def report_id(code: str, window_end: date, reason: str, prefix: str) -> str:
    return f"{prefix}:{code}:{window_end.isoformat()}:{hashlib.sha1(reason.encode()).hexdigest()[:12]}"


def windows_overlap(a: SeatReportRec, b: SeatReportRec) -> bool:
    return a.code == b.code and a.window_start <= b.window_end and b.window_start <= a.window_end


class OverlappingWindows(ValueError):
    """SS-A15/G09: overlapping one-day and multi-day reports are not additive."""


def aggregate_seat_buy(reports: Sequence[SeatReportRec], seat_id: uuid.UUID) -> Decimal:
    """Sum of a resolved seat's buy-side amounts across reports, only if the windows are provably disjoint."""
    for i, a in enumerate(reports):
        for b in reports[i + 1:]:
            if windows_overlap(a, b):
                raise OverlappingWindows(f"{a.source_report_id} overlaps {b.source_report_id}")
    return sum((r.canonical_amount_cny for rep in reports for r in rep.rows if r.side == "buy" and r.resolution.seat_id == seat_id), Decimal(0))


def net_in_report(rep: SeatReportRec, seat_id: uuid.UUID) -> Decimal | None:
    """Net of one resolved seat INSIDE one report: buy-side minus sell-side amounts; falls back to the source row's own pair."""
    rows = [r for r in rep.rows if r.resolution.seat_id == seat_id]
    if not rows:
        return None
    buy = [r for r in rows if r.side == "buy"]
    sell = [r for r in rows if r.side == "sell"]
    if buy and sell:
        return sum((r.canonical_amount_cny for r in buy), Decimal(0)) - sum((r.canonical_amount_cny for r in sell), Decimal(0))
    sa = rows[0].source_amounts
    if sa.get("buy_cny") is not None and sa.get("sell_cny") is not None:
        return Decimal(sa["buy_cny"]) - Decimal(sa["sell_cny"])
    return None


def usable_reports(reports: Iterable[SeatReportRec], decision_as_of: datetime) -> list[SeatReportRec]:
    """SS-A14: only reports whose actual availability is not after the decision instant."""
    return [r for r in reports if r.available_at is not None and r.available_at <= decision_as_of]


# ---- builders -----------------------------------------------------------------------------------------------
_VENUE = {"6": "XSHG", "0": "XSHE", "3": "XSHE", "4": "XBSE", "8": "XBSE", "9": "XBSE"}


def exchange_for(code: str) -> str:
    return _VENUE[code[0]]


def is_equity_code(code: str) -> bool:
    """A-share equity numbering only; convertible bonds (11xxxx/12xxxx) and funds also appear in vendor lists and are not seat-report subjects here."""
    return bool(re.fullmatch(r"\d{6}", code)) and code[0] in _VENUE and not code.startswith(("12", "11"))


def _s(d: Decimal | None) -> str | None:
    return None if d is None else str(d)


def _dec(v) -> Decimal | None:
    return None if v is None else Decimal(str(v))


def reports_from_eastmoney(list_rows: Sequence[dict], buy_rows: Sequence[dict], sell_rows: Sequence[dict], sessions: Sequence[date],
                           available_at: datetime) -> list[SeatReportRec]:
    """Eastmoney data-center rows (amounts CNY yuan). Reports are keyed by the vendor TRADE_ID."""
    seats: dict[str, list[tuple[str, dict]]] = {}
    for side, rows in (("buy", buy_rows), ("sell", sell_rows)):
        for r in rows:
            seats.setdefault(str(r["TRADE_ID"]), []).append((side, r))
    out = []
    for l in list_rows:
        code = str(l["SECURITY_CODE"])
        if not is_equity_code(code):
            continue
        wend = date.fromisoformat(str(l["TRADE_DATE"])[:10])
        reason = str(l["EXPLANATION"])
        wstart, complete = window_start(reason, wend, sessions)
        rows = []
        per_side_rank: dict[str, int] = {"buy": 0, "sell": 0}
        # the vendor tables carry no rank column: rank = position by disclosed amount (desc), then name, within the side
        ordered = sorted(seats.get(str(l["TRADE_ID"]), []),
                         key=lambda sr: (sr[0], -(_dec(sr[1]["BUY" if sr[0] == "buy" else "SELL"]) or Decimal(0)), str(sr[1]["OPERATEDEPT_NAME"])))
        for side, r in ordered:
            per_side_rank[side] += 1
            amt = _dec(r["BUY"] if side == "buy" else r["SELL"])
            if amt is None:
                continue
            raw = str(r["OPERATEDEPT_NAME"])
            res = resolve_seat_name(raw)
            rows.append(SeatRowRec(side, per_side_rank[side], raw, amt, "CNY", res,
                                   {"buy_cny": _s(_dec(r["BUY"])), "sell_cny": _s(_dec(r["SELL"])), "net_cny": _s(_dec(r.get("NET")))},
                                   str(r.get("OPERATEDEPT_CODE")) if r.get("OPERATEDEPT_CODE") is not None else None))
        notes = ["top-N table only: trades outside the published seats are unknown"]
        if not complete:
            notes.append("multi-session window start unknown: calendar too short")
        out.append(SeatReportRec("eastmoney_lhb_disclosure", f"em:{l['TRADE_ID']}", exchange_for(code), code, wstart, wend, reason, "unknown", "unknown",
                                 tuple(rows), "valid" if complete and rows else "partial", tuple(notes), available_at,
                                 {"trade_id": str(l["TRADE_ID"]), "vendor_explanation_code": l.get("CHANGE_TYPE")}))
    return out


def reports_from_legacy(stock_rows: Sequence[dict], seat_rows: Sequence[dict], sessions: Sequence[date]) -> list[SeatReportRec]:
    """Legacy parquet rows. Amounts are float 亿 -> exact CNY_10000. ``available_at`` = the legacy ``ingested_at`` (no back-dating)."""
    by_key: dict[tuple, list[dict]] = {}
    for r in seat_rows:
        by_key.setdefault((r["code"], r["date"], r["reason"]), []).append(r)
    out = []
    for s in stock_rows:
        code, wend, reason = s["code"], s["date"], s["reason"]
        if not is_equity_code(code):
            continue
        rows_src = by_key.get((code, wend, reason), [])
        wstart, complete = window_start(reason, wend, sessions)
        rows, skipped = [], 0
        for side in ("buy", "sell"):
            part = sorted((r for r in rows_src if r["side"] == side), key=lambda r: r["rank"])
            for pos, r in enumerate(part, start=1):  # legacy ranks are 0- or 1-based; the table needs rank > 0, the source rank is kept in evidence
                raw = str(r["seat"])
                amt = legacy_yi_to_wan(r["buy"] if side == "buy" else r["sell"])
                if amt is None:  # the table needs an amount; a row without one is not invented as 0
                    skipped += 1
                    continue
                res = resolve_seat_name(raw)
                rows.append(SeatRowRec(side, pos, raw, amt, "CNY_10000", res,
                                       {"source_rank": int(r["rank"]), "buy_cny": _cny_text(legacy_yi_to_wan(r["buy"])), "sell_cny": _cny_text(legacy_yi_to_wan(r["sell"])),
                                        "net_cny": _cny_text(legacy_yi_to_wan(r["net"]))},
                                       str(r["seat_code"]) if r.get("seat_code") is not None else None))
        avail = max([r["ingested_at"] for r in rows_src] + [s["ingested_at"]])
        notes = ["legacy float 亿元 amounts (6 decimals) converted through repr and quantized to cents; finer precision not available",
                 "top-N table only: trades outside the published seats are unknown"]
        if not complete:
            notes.append("multi-session window start unknown: calendar too short")
        if skipped:
            notes.append(f"{skipped} legacy seat row(s) without an amount were not stored (never invented as 0)")
        out.append(SeatReportRec("legacy_warehouse", report_id(code, wend, reason, "legacy"), exchange_for(code), code, wstart, wend, reason, "unknown",
                                 "unknown", tuple(rows), "partial", tuple(notes), avail, {"rows_skipped_no_amount": skipped}))
    return out


# ---- style hypotheses ---------------------------------------------------------------------------------------
@dataclass(frozen=True)
class SeatStyleHypothesis:
    """A TESTABLE, versioned statement; never an instruction. Key is a resolved seat id or an exact literal name (no entity claim)."""
    key_kind: str  # seat_id | raw_name_literal
    key: str
    side: str
    conditions: dict
    horizon_sessions: int
    outcome: str  # next_open_gap | next_close_return | max_adverse_excursion
    claim: str
    min_sample: int = 30
    version: int = 1

    def hypothesis_id(self) -> uuid.UUID:
        return uuid.uuid5(HYP_NS, "|".join([self.key_kind, self.key, self.side, repr(sorted(self.conditions.items())), str(self.horizon_sessions), self.outcome]))

    def to_input_requirements(self) -> dict:
        return {"key_kind": self.key_kind, "key": self.key, "side": self.side, "conditions": self.conditions,
                "needs": ["seat_report.available_at", "entry session open after available_at", "adjusted bars with known availability"]}


@dataclass(frozen=True)
class StyleObservation:
    report_source_id: str
    report_available_at: datetime
    entry_at: datetime  # first tradable instant AFTER the disclosure becomes available
    outcome_available_at: datetime
    value: Decimal
    context: dict


class LookaheadError(ValueError):
    """An observation whose entry instant is before the disclosure was available (backfilled as if known intraday)."""


def evaluate_style(h: SeatStyleHypothesis, observations: Sequence[StyleObservation], as_of: datetime, *, shrinkage_k: int = 20,
                   trials_in_family: int = 1) -> dict:
    """Conditional outcome statistics using only information available at ``as_of``."""
    used: list[Decimal] = []
    excluded_late = 0
    for o in observations:
        if o.entry_at < o.report_available_at:
            raise LookaheadError(f"{o.report_source_id}: entry before disclosure availability")
        if o.report_available_at > as_of or o.outcome_available_at > as_of:
            excluded_late += 1
            continue
        if any(o.context.get(k) != v for k, v in h.conditions.items()):
            continue
        used.append(o.value)
    n = len(used)
    out: dict = {"hypothesis_key": f"{h.key_kind}:{h.key}:{h.outcome}", "as_of": as_of.isoformat(), "conditions": h.conditions, "n": n,
                 "excluded_not_yet_available": excluded_late, "trials_in_family": trials_in_family}
    if n < h.min_sample:
        out["outcome"] = {"status": "insufficient_sample", "min_sample": h.min_sample}
        return out
    mean = sum(used) / n
    var = sum((x - mean) ** 2 for x in used) / (n - 1)
    se = Decimal(str(math.sqrt(float(var) / n)))
    z = Decimal("1.96") if trials_in_family <= 1 else Decimal(str(round(_z_two_sided(0.05 / trials_in_family), 4)))
    out["outcome"] = {"status": "estimated", "mean": str(mean), "standard_error": str(se), "ci_low": str(mean - z * se), "ci_high": str(mean + z * se),
                      "ci_z": str(z), "shrunk_mean": str(mean * Decimal(n) / Decimal(n + shrinkage_k)), "shrinkage_k": shrinkage_k,
                      "hit_rate": str(Decimal(sum(1 for x in used if x > 0)) / n), "multiple_comparison": "bonferroni_z" if trials_in_family > 1 else "none",
                      "note": "conditional description after disclosure; not an instruction and not a cost-aware executable result"}
    return out


def _z_two_sided(alpha: float) -> float:
    lo, hi = 0.0, 10.0
    for _ in range(80):
        mid = (lo + hi) / 2
        if math.erfc(mid / math.sqrt(2)) > alpha:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2
