"""Stock page backfill (docs/LEGACY_TRIAGE.md rows 41, 159, 43): chart indicator series and the stock statistics card.

Why: the browser only draws. MA/EMA/BOLL/MACD/KDJ/RSI are computed here, from the very bars the chart shows (the payload of
``getStockBars``), through the registered feature kernels (``pulse.domain.features``), so chart, ranking, plans and
backtests share one implementation. The statistics card reuses the same kernels on the daily bars visible at the release's
``decision_as_of``.

Invariants
* Input is a ``getStockBars`` window (wire form). Nothing is read from the database here except the listing date and the
  circulating-cap fact; the bars were already filtered by ``available_at <= decision_as_of``.
* Output series have exactly one value per bar; a value is ``None`` in the warm-up and wherever an input in its window is
  missing (kernel null policy); never 0, never forward-filled.
* A statistic that pulse3 does not hold point-in-time is ``unavailable`` with the reason; an approximation is ``estimated``
  with the reason. Seal facts DO exist now, but only as estimates from the imported 60 s recordings (``bf_live_service``, policy
  seal-facts-v2, one dataset per recorded session): they are served by ``/v1/x/bf/live/seal-facts`` and, as the seal success rate over
  the covered sessions, by ``bf_fillm_stock``. The four intraday fields of THIS card stay unavailable because its field type has no
  time value and no estimate flag for them; the card never infers them from daily bars.
Not covered: intraday statistics, per-session ST price-limit classes (the board class percent is used), Decimal-exact kernels
(numeric policy ``float64-v1``, values are rounded to 6 decimals on the wire).
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any, Mapping, Sequence

from pulse.contracts import decimal_str
from pulse.domain.features import indicators as ind
from pulse.domain.features.definitions import NUMERIC_POLICY, Series, definition
from pulse.domain.features.definitions import compute as compute_feature
from pulse.domain.playbooks.view import limit_class_pct

#: Look-back of the chart window per timeframe, days before ``decision_as_of``. Mirrors ``LOOKBACK_MS`` in
#: ``apps/web/src/lib/barsParams.ts``; the response carries ``window_hash`` so a page can prove both sides read one window.
LOOKBACK_DAYS: Mapping[str, int] = {"1m": 1, "5m": 5, "15m": 10, "1h": 30, "1d": 400, "1w": 800, "1mo": 1800, "1y": 4000}
INDICATOR_SETS = ("ma", "ema", "boll", "macd", "kdj", "rsi")
STATS_LOOKBACK_DAYS = 400            # about 275 sessions: enough for the 250-session limit-up count
HIGH_WINDOW = 60
LIMIT_COUNT_SESSIONS = 250
WIRE_SCALE = 6


# ---- snapshot choice -------------------------------------------------------------------------------------------
def _is_intraday(tf: str) -> bool:
    return tf[-1:] in ("s", "m", "h") and tf[:-1].isdigit() and not tf.endswith("mo")


def pick_snapshot(chart_refs: Sequence[Mapping[str, Any]], snapshot_refs: Sequence[Mapping[str, Any]], instrument_id: str,
                  timeframe: str, mode: str) -> str | None:
    """Bars snapshot for (instrument, timeframe, mode), the same choice the stock page makes (``deriveBarsParams``).

    The workspace names the exact snapshot per instrument in ``chart_window_refs`` (``bars:<snapshot>:<instrument>:<tf>:<mode>``);
    otherwise the release's declared inputs are matched by dataset name; otherwise the first bars dataset (the bars endpoint
    then refuses with a structured 409 when it cannot answer)."""
    want = timeframe if _is_intraday(timeframe) else "1d"
    for ref in chart_refs:
        p = str(ref.get("ref", "")).split(":")
        if len(p) >= 5 and p[0] == "bars" and p[2] == instrument_id and p[3] == want and p[4] == mode and p[1]:
            return p[1]
    bars = [s for s in snapshot_refs if "bar" in str(s.get("dataset_id", "")).lower()]

    def match(s: Mapping[str, Any]) -> bool:
        did = str(s["dataset_id"]).lower()
        if mode != "raw":
            return "adjust" in did or "fwd" in did
        if _is_intraday(timeframe):
            return "raw" in did and (f"_{timeframe}_" in did or did.endswith(f"_{timeframe}"))
        return "raw" in did and "adjust" not in did and ("_1d_" in did or did.endswith("_1d"))

    hit = next((s for s in bars if match(s)), None) or (bars[0] if bars else None)
    return str(hit["snapshot_id"]) if hit else None


def window_start(decision_as_of: datetime, timeframe: str) -> datetime:
    return decision_as_of - timedelta(days=LOOKBACK_DAYS[timeframe])


# ---- indicators ------------------------------------------------------------------------------------------------
@dataclass(frozen=True)
class SeriesSpec:
    key: str
    indicator: str
    output: str
    pane: str
    label: str


@dataclass(frozen=True)
class FeatureRun:
    feature_id: str
    params: Mapping[str, Any]
    specs: tuple[SeriesSpec, ...]


def _runs() -> dict[str, tuple[FeatureRun, ...]]:
    price, sub = "price", "sub"
    return {
        "ma": tuple(FeatureRun("ma", {"n": n}, (SeriesSpec(f"ma{n}", "ma", "ma", price, f"MA{n}"),)) for n in (5, 10, 20, 60)),
        "ema": tuple(FeatureRun("ema", {"n": n}, (SeriesSpec(f"ema{n}", "ema", "ema", price, f"EMA{n}"),)) for n in (12, 26)),
        "boll": (FeatureRun("boll", {"n": 20, "k": 2.0}, (
            SeriesSpec("boll_mid", "boll", "mid", price, "BOLL中轨"), SeriesSpec("boll_upper", "boll", "upper", price, "BOLL上轨"),
            SeriesSpec("boll_lower", "boll", "lower", price, "BOLL下轨"))),),
        "macd": (FeatureRun("macd", {"fast": 12, "slow": 26, "signal": 9}, (
            SeriesSpec("macd", "macd", "macd", sub, "DIF"), SeriesSpec("macd_signal", "macd", "signal", sub, "DEA"),
            SeriesSpec("macd_hist", "macd", "hist", sub, "MACD柱"))),),
        "kdj": (FeatureRun("kdj", {"n": 9}, (
            SeriesSpec("kdj_k", "kdj", "k", sub, "K"), SeriesSpec("kdj_d", "kdj", "d", sub, "D"), SeriesSpec("kdj_j", "kdj", "j", sub, "J"))),),
        "rsi": tuple(FeatureRun("rsi", {"n": n}, (SeriesSpec(f"rsi{n}", "rsi", "rsi", sub, f"RSI{n}"),)) for n in (6, 14)),
    }


RUNS = _runs()


def parse_set(raw: str | None) -> list[str]:
    """``ma,ema`` -> ordered unique names; unknown names raise ``ValueError`` listing them."""
    names = [p.strip().lower() for p in (raw or ",".join(INDICATOR_SETS)).split(",") if p.strip()]
    bad = [n for n in names if n not in INDICATOR_SETS]
    if bad or not names:
        raise ValueError(", ".join(bad) if bad else "empty")
    return [n for n in INDICATOR_SETS if n in names]


def _px(bar: Mapping[str, Any], name: str) -> Decimal | None:
    v = bar.get(f"{name}_price_raw", bar.get(f"{name}_price"))
    return None if v is None else Decimal(v)


def _f(v: Decimal | None) -> float | None:
    return None if v is None else float(v)


def series_of(window: Mapping[str, Any]) -> Series:
    """Aligned float series of a ``getStockBars`` window (raw ``*_price_raw`` or adjusted ``*_price`` names)."""
    bars = window["bars"]
    return Series(
        dates=tuple(b["trade_date"] for b in bars), open=tuple(_f(_px(b, "open")) for b in bars),
        high=tuple(_f(_px(b, "high")) for b in bars), low=tuple(_f(_px(b, "low")) for b in bars),
        close=tuple(_f(_px(b, "close")) for b in bars),
        volume=tuple(None if b.get("volume_shares") is None else float(b["volume_shares"]) for b in bars))


def wire_value(v: float | None) -> str | None:
    if v is None or not math.isfinite(v):
        return None
    return decimal_str(Decimal(f"{v:.{WIRE_SCALE}f}"))


def compute_indicators(window: Mapping[str, Any], names: Sequence[str]) -> list[dict]:
    """Series for the requested indicator sets over exactly the bars of ``window`` (same order, same length)."""
    s = series_of(window)
    out = []
    for name in names:
        for run in RUNS[name]:
            fd = definition(run.feature_id)
            params = fd.resolve_params(run.params)
            res = compute_feature(run.feature_id, s, params)
            numeric = {k: v for k, v in params.items() if isinstance(v, (int, float)) and not isinstance(v, bool)}
            for spec in run.specs:
                values = [wire_value(v) for v in res[spec.output]]
                valid = [i for i, v in enumerate(values) if v is not None]
                warm = max(int(fd.warmup(params)), 0)
                if spec.key == "macd":                       # DIF needs only the slow EMA; the registered warm-up is that of the signal line
                    warm = params["slow"] - 1
                out.append({
                    "key": spec.key, "indicator": spec.indicator, "output": spec.output, "pane": spec.pane, "label": spec.label,
                    "params": numeric, "definition_version": fd.definition_version, "implementation_digest": fd.implementation_digest,
                    "warmup_bars": warm, "first_valid_index": valid[0] if valid else None,
                    "valid_count": len(valid), "values": values})
    return out


def indicator_bars(window: Mapping[str, Any]) -> list[dict]:
    return [{"bar_open_time": b["bar_open_time"], "trade_date": b["trade_date"], "is_final": b["is_final"]} for b in window["bars"]]


# ---- statistics card -------------------------------------------------------------------------------------------
INTRADAY_KEYS = ("first_seal_time", "last_seal_time", "seal_amount", "open_board_count")
NO_INTRADAY = "这个数值只能由分时快照估算（见封板事实接口 /v1/x/bf/live/seal-facts）；日线无法还原，本卡片不推断、不填充"


@dataclass(frozen=True)
class CapFact:
    float_cap_cny: Decimal
    as_of: str
    available_at: datetime


@dataclass(frozen=True)
class StatsInput:
    price_basis: str                       # adjustment mode of ``price_window``
    price_window: Mapping[str, Any] | None  # daily window visible at the cutoff (None: no daily bars could be read)
    price_missing_reason: str | None
    raw_window: Mapping[str, Any] | None   # raw daily window for the newest session's amount / volume
    raw_missing_reason: str | None
    cap: CapFact | None
    cap_missing_reason: str | None
    listed_on: date | None
    board_code: str
    decision_as_of: datetime


def _field(key: str, label: str, *, value: str | None = None, kind: str | None = None, unit: str | None = None, as_of: str | None = None,
           basis: str | None = None, reason: str | None = None, estimated: bool = False) -> dict:
    status = "unavailable" if value is None else ("estimated" if estimated else "ok")
    if status != "ok" and not reason:
        raise ValueError(f"{key}: a missing or estimated statistic needs a reason")
    return {"key": key, "label": label, "status": status, "value": value, "value_kind": kind if value is not None else None,
            "unit": unit, "as_of": as_of, "basis": basis, "reason": reason}


def _unavailable(key: str, label: str, reason: str, unit: str | None = None) -> dict:
    return _field(key, label, unit=unit, reason=reason)


def _last_bar(window: Mapping[str, Any] | None) -> Mapping[str, Any] | None:
    return window["bars"][-1] if window and window["bars"] else None


def _turnover(inp: StatsInput) -> tuple[dict, dict]:
    bar = _last_bar(inp.raw_window)
    if bar is None:
        why = inp.raw_missing_reason or "截点前没有可见的原始价日线"
        return (_unavailable("turnover_amount", "成交额", why, "元"), _unavailable("turnover_rate", "换手率", why, "%"))
    day = bar["trade_date"]
    amount, estimated, how = None, False, "日线 turnover_amount_cny"
    if bar.get("turnover_amount_cny") is not None:
        amount = Decimal(bar["turnover_amount_cny"])
    elif bar.get("volume_shares") is not None and _px(bar, "close") is not None:
        amount = Decimal(bar["volume_shares"]) * _px(bar, "close")  # type: ignore[operator]
        estimated, how = True, "成交量 × 收盘价（原始价）"
    if amount is None:
        why = "该日线没有成交额，也没有成交量或收盘价可用于估算"
        return (_unavailable("turnover_amount", "成交额", why, "元"), _unavailable("turnover_rate", "换手率", why, "%"))
    amt_reason = "数据源没有成交额，按 成交量 × 收盘价 估算，会偏离真实成交额" if estimated else None
    amt = _field("turnover_amount", "成交额", value=decimal_str(amount.quantize(Decimal("0.01"))), kind="decimal", unit="元", as_of=day,
                 basis=how, reason=amt_reason, estimated=estimated)
    if inp.cap is None:
        return amt, _unavailable("turnover_rate", "换手率", inp.cap_missing_reason or "截点前没有可见的流通市值事实", "%")
    if inp.cap.float_cap_cny is None or inp.cap.float_cap_cny <= 0:      # a zero/negative cap in the legacy file is not a cap
        return amt, _unavailable("turnover_rate", "换手率", f"{inp.cap.as_of} 的流通市值不为正，无法计算换手率", "%")
    rate = (amount / inp.cap.float_cap_cny * 100).quantize(Decimal("0.0001"))
    why = (f"换手率按 成交额 ÷ 流通市值 估算（不是 成交量 ÷ 流通股本）：流通市值取自 {inp.cap.as_of} 的历史快照，单位未经数据源声明"
           "（按亿元解释），不是当日值。" + ("成交额本身也是估算值。" if estimated else ""))
    return amt, _field("turnover_rate", "换手率", value=decimal_str(rate), kind="decimal", unit="%", as_of=day,
                       basis=f"{how} ÷ 流通市值({inp.cap.as_of})", reason=why, estimated=True)


def _price_stats(inp: StatsInput) -> list[dict]:
    w = inp.price_window
    labels = {"dist_to_60d_high": ("距60日高点", "%"), "limit_up_count_1y": ("近1年涨停次数", "次"), "limit_streak": ("当前连板", "板"),
              "up_streak": ("当前连阳", "天")}
    if w is None or not w["bars"]:
        why = inp.price_missing_reason or "截点前没有可见的日线"
        return [_unavailable(k, lab, why, u) for k, (lab, u) in labels.items()]
    bars = w["bars"]
    last = bars[-1]
    day = last["trade_date"]
    basis_name = {"raw": "原始价", "forward_adjusted": "前复权价", "backward_adjusted": "后复权价"}[inp.price_basis]
    series = series_of(w)
    out = []
    # distance to the 60-session high (close of the newest bar vs the highest high of the last 60 bars, same price basis)
    recent = bars[-HIGH_WINDOW:]
    highs = [_px(b, "high") for b in recent]
    close = _px(last, "close")
    if len(bars) < HIGH_WINDOW:
        out.append(_unavailable("dist_to_60d_high", "距60日高点", f"窗口内只有 {len(bars)} 根日线，不足 {HIGH_WINDOW} 根，不拿短窗口冒充60日", "%"))
    elif close is None or any(h is None for h in highs):
        out.append(_unavailable("dist_to_60d_high", "距60日高点", "最近60根日线里有缺失的最高价或收盘价，缺口不补", "%"))
    else:
        top = max(h for h in highs if h is not None)
        pct = ((close - top) / top * 100).quantize(Decimal("0.0001")) if top > 0 else None
        out.append(_field("dist_to_60d_high", "距60日高点", value=None if pct is None else decimal_str(pct), kind="decimal", unit="%", as_of=day,
                          basis=f"最新收盘 相对 最近{HIGH_WINDOW}根日线最高价（{basis_name}）；≥0 表示已创新高",
                          reason="最高价不为正，无法计算" if pct is None else None))
    # limit-up statistics (close-to-close rise >= class limit - 0.5pp; ratios are valid on adjusted series)
    pct_limit = limit_class_pct(inp.board_code)
    note = f"按板块涨跌幅 {pct_limit:g}% 的收盘涨幅口径（容差0.5个百分点，{basis_name}），未区分 ST 时期的5%限制"
    count = ind.limit_count(series.close, pct_limit, LIMIT_COUNT_SESSIONS)[-1]
    if count is None:
        out.append(_unavailable("limit_up_count_1y", "近1年涨停次数", f"窗口内日线不足 {LIMIT_COUNT_SESSIONS + 1} 根或有缺口，无法数满近{LIMIT_COUNT_SESSIONS}个交易日", "次"))
    else:
        out.append(_field("limit_up_count_1y", "近1年涨停次数", value=str(int(count)), kind="integer", unit="次", as_of=day,
                          basis=f"最近{LIMIT_COUNT_SESSIONS}个交易日；{note}"))
    streak = ind.limit_streak(series.close, pct_limit)[-1]
    out.append(_unavailable("limit_streak", "当前连板", "最新两根日线有缺失，无法判断", "板") if streak is None else
               _field("limit_streak", "当前连板", value=str(int(streak)), kind="integer", unit="板", as_of=day, basis=note))
    up = ind.up_streak(series.close)[-1]
    out.append(_unavailable("up_streak", "当前连阳", "最新两根日线有缺失，无法判断", "天") if up is None else
               _field("up_streak", "当前连阳", value=str(int(up)), kind="integer", unit="天", as_of=day,
                      basis=f"连续收盘高于前一日收盘的天数（{basis_name}）"))
    return out


def build_stats(inp: StatsInput) -> list[dict]:
    """The card's fields in display order. Every field is present; a missing one says why."""
    amount, rate = _turnover(inp)
    price = {f["key"]: f for f in _price_stats(inp)}
    cutoff_day = inp.decision_as_of.date()
    if inp.listed_on is None:
        listing = _unavailable("listing_date", "上市日期", "证券主数据没有上市日期")
    elif inp.listed_on > cutoff_day:
        listing = _unavailable("listing_date", "上市日期", f"主数据上市日期 {inp.listed_on.isoformat()} 晚于数据截点，截点时点不可见")
    else:
        listing = _field("listing_date", "上市日期", value=inp.listed_on.isoformat(), kind="date", basis="证券主数据")
    intraday = [_unavailable(k, label, NO_INTRADAY, unit) for k, label, unit in (
        ("first_seal_time", "首次封板时间", None), ("last_seal_time", "最后封板时间", None), ("seal_amount", "封单金额", "元"),
        ("open_board_count", "炸板次数", "次"))]
    return [amount, rate, price["dist_to_60d_high"], price["limit_up_count_1y"], price["limit_streak"], price["up_streak"], listing, *intraday]


# ---- database-backed inputs ------------------------------------------------------------------------------------
def load_listing(conn, instrument_id) -> date | None:
    row = conn.execute("SELECT listed_on FROM core.instrument WHERE instrument_id=%s", (instrument_id,)).fetchone()
    return None if row is None else row["listed_on"]


def load_board_code(conn, instrument_id, as_of: datetime, policy: str) -> str:
    """Board class of the newest instrument state visible at the cutoff (``main`` when none is visible).

    Reconstructed policy: states imported later are used by ``effective_from`` only (as ``bindings.load_instrument_meta``)."""
    from pulse.application import bindings as B

    avail = "" if B.reconstructed(policy) else " AND available_at <= %(as_of)s"
    row = conn.execute(
        "SELECT board_code FROM core.instrument_state WHERE instrument_id=%(iid)s AND effective_from <= %(as_of)s" + avail
        + " ORDER BY effective_from DESC, state_version DESC LIMIT 1", {"iid": instrument_id, "as_of": as_of}).fetchone()
    return row["board_code"] if row else "main"


def load_cap_fact(conn, lake_root, instrument_id, as_of: datetime, policy: str) -> tuple[CapFact | None, str | None, dict | None]:
    """(cap fact, reason when missing, manifest row) of the newest circulating-cap version visible at ``as_of``.

    Visibility follows the release policy exactly as ``bindings.visible_caps``: the observed policy needs
    ``available_at <= as_of``; the reconstructed policy uses the version's own as-of date."""
    import polars as pl

    from pulse.adapters.lake import LakeStore
    from pulse.application import bindings as B

    man = B._ready_manifest(conn, B.CAP_DATASET, None if B.reconstructed(policy) else as_of)
    if man is None:
        return None, "截点前没有已发布的流通市值数据集", None
    try:
        df = LakeStore(lake_root).read_snapshot_frames(man["content_hash"])
    except (FileNotFoundError, ValueError, OSError) as e:
        return None, f"流通市值数据集读取失败（{type(e).__name__}）", man
    if df is None:
        return None, "流通市值数据集为空", man
    df = df.filter(pl.col("instrument_id") == str(instrument_id))
    if B.reconstructed(policy):
        df = df.filter(pl.col("as_of") <= as_of.astimezone(B.CST).date().isoformat())
    else:
        df = df.filter(pl.col("available_at") <= as_of)
    df = df.filter(pl.col("float_cap_cny").is_not_null()).sort(["as_of", "available_at"])
    if df.height == 0:
        return None, "截点前没有这只股票的流通市值事实", man
    r = df.row(-1, named=True)
    return CapFact(Decimal(r["float_cap_cny"]), r["as_of"], r["available_at"]), None, man
