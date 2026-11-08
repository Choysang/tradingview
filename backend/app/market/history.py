"""Daily bars for detail charts, backtests and cross-sectional research.

Every series carries its own provenance: LIVE bars come from a real provider,
SIM bars from the deterministic synthetic generator and say so.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
import threading

import polars as pl

from contracts.schema import DataProvenance
from .live import AkShareFeed
from .simulation import _scale, _series, synthetic_history

CST = timezone(timedelta(hours=8))


def _now() -> datetime:
    return datetime.now(CST)


class HistoryProvider:
    """Daily bars for detail charts and backtests, with explicit provenance."""

    def __init__(self) -> None:
        self._cache: dict[str, tuple[list, DataProvenance]] = {}
        self._lock = threading.RLock()
        self._live = AkShareFeed.is_available()
        self._panels: dict[tuple, tuple[pl.DataFrame, DataProvenance]] = {}
        self._warmed: set[str] = set()

    def bars(self, code: str, anchor: float | None = None) -> tuple[list, DataProvenance]:
        with self._lock:
            cached = self._cache.get(code)
        if cached:
            return cached
        # The network fetch runs unlocked so one slow code cannot stall the panel or warm-up.
        result = self._live_bars(code) or self._synthetic_bars(code, anchor)
        with self._lock:
            return self._cache.setdefault(code, result)

    # --- cross-section for pattern scans and factor research ---------------------

    def panel(
        self, codes: list[tuple[str, float | None]], mode: str, warm: int = 300
    ) -> tuple[pl.DataFrame, DataProvenance]:
        """Long (code, date, OHLCV) frame for many stocks.

        SIM: synthetic bars for every code, never the network. LIVE: every code
        whose real bars are cached (not just today's top turnover, which would
        select winners after the fact); a once-per-day background warm-up fetches
        the first `warm` of `codes` so later calls cover more of the market.
        """
        today = _now().date().isoformat() if mode == "LIVE" else date.today().isoformat()
        if mode == "SIM":
            key = ("SIM", today, hash(tuple(sorted(code for code, _ in codes))))
            return self._cached_panel(key, lambda: self._sim_panel(codes))
        cached = self._cached_live()
        self._start_warmup(codes[:warm])
        key = ("LIVE", today, hash(tuple(sorted(cached))))
        return self._cached_panel(key, lambda: self._live_panel(cached, warm))

    def _cached_panel(self, key: tuple, build) -> tuple[pl.DataFrame, DataProvenance]:
        with self._lock:
            hit = self._panels.get(key)
        if hit is not None:
            return hit
        built = build()  # outside the lock: building a SIM panel takes about a second
        with self._lock:
            self._panels = dict(list(self._panels.items())[-3:])
            return self._panels.setdefault(key, built)

    @staticmethod
    def _frame(series: dict[str, list[tuple]]) -> pl.DataFrame:
        """Rows are (date, open, high, low, close, volume) tuples per code."""
        columns: dict[str, list] = {k: [] for k in ("code", "date", "open", "high", "low", "close", "volume")}
        for code, rows in series.items():
            columns["code"].extend([code] * len(rows))
            for position, name in enumerate(("date", "open", "high", "low", "close", "volume")):
                columns[name].extend(row[position] for row in rows)
        return pl.DataFrame(
            columns,
            schema={"code": pl.String, "date": pl.String, "open": pl.Float64, "high": pl.Float64,
                    "low": pl.Float64, "close": pl.Float64, "volume": pl.Float64},
        )

    def _sim_panel(self, codes: list[tuple[str, float | None]]) -> tuple[pl.DataFrame, DataProvenance]:
        end = date.today()
        series = {code: _scale(_series(code, end, 260), anchor)[0] for code, anchor in codes}
        frame = self._frame(series)
        provenance = DataProvenance(
            mode="SIM",
            source="pulse · 合成日线生成器",
            as_of=_now().isoformat(timespec="seconds"),
            trade_date=frame["date"].max() if frame.height else end.isoformat(),
            status="fallback",
            detail=f"合成日线面板：{len(series)} 只 × 260 根，仅用于演示与代码路径验证，不含真实行情。",
            coverage=len(series),
        )
        return frame, provenance

    def _cached_live(self) -> dict[str, list]:
        with self._lock:
            return {code: bars for code, (bars, info) in self._cache.items() if info.mode == "LIVE"}

    def _live_panel(self, cached: dict[str, list], warm: int) -> tuple[pl.DataFrame, DataProvenance]:
        series = {
            code: [(b.date, b.open, b.high, b.low, b.close, b.volume) for b in bars]
            for code, bars in cached.items()
        }
        frame = self._frame(series)
        provenance = DataProvenance(
            mode="LIVE",
            source="东方财富/腾讯 · 前复权日线（已缓存）",
            as_of=_now().isoformat(timespec="seconds"),
            trade_date=frame["date"].max() if frame.height else _now().date().isoformat(),
            status="ready" if series else "fallback",
            detail=f"仅含已缓存真实日线的 {len(series)} 只；预热按今日成交额前 {warm} 只，存在选择偏差，覆盖会随预热增长。",
            coverage=len(series),
        )
        return frame, provenance

    def _start_warmup(self, codes: list[tuple[str, float | None]]) -> None:
        """At most one daemon warm-up per trade date; it never blocks a request."""
        today = _now().date().isoformat()
        with self._lock:
            if today in self._warmed or not self._live:
                return
            self._warmed.add(today)
        codes_only = [code for code, _ in codes]
        threading.Thread(target=self._warm, args=(codes_only,), daemon=True, name="history-warmup").start()

    def _warm(self, codes: list[str]) -> None:
        for code in codes:
            with self._lock:
                if code in self._cache and self._cache[code][1].mode == "LIVE":
                    continue
            result = self._live_bars(code)  # network call, deliberately outside the lock
            if result:
                with self._lock:
                    self._cache[code] = result

    def _live_bars(self, code: str) -> tuple[list, DataProvenance] | None:
        if not self._live:
            return None
        from ..research.tencent_history import fetch_daily

        try:
            frame = self._akshare_bars(code)
        except Exception:
            frame = None
        if frame is not None:
            return frame
        bars = fetch_daily(code, count=320)
        if bars:
            return bars, self._bars_provenance(bars, "腾讯行情 · 前复权日线", "qt.gtimg.cn 前复权日线，交易日历由数据源提供。")
        return None

    def _akshare_bars(self, code: str):
        try:
            import akshare as ak

            start = (_now() - timedelta(days=420)).strftime("%Y%m%d")
            frame = ak.stock_zh_a_hist(
                symbol=code, period="daily", start_date=start, end_date=_now().strftime("%Y%m%d"),
                adjust="qfq",
            )
        except Exception:
            return None
        if frame is None or frame.empty:
            return None
        from contracts.schema import OHLCBar

        bars = [
            OHLCBar(
                date=str(row["日期"])[:10],
                open=float(row["开盘"]),
                high=float(row["最高"]),
                low=float(row["最低"]),
                close=float(row["收盘"]),
                volume=float(row["成交量"]),
            )
            for _, row in frame.iterrows()
        ]
        return bars, self._bars_provenance(bars, "akshare · stock_zh_a_hist（前复权日线）", "东方财富前复权日线，交易日历由数据源提供。")

    @staticmethod
    def _bars_provenance(bars: list, source: str, detail: str) -> DataProvenance:
        return DataProvenance(
            mode="LIVE",
            source=source,
            as_of=_now().isoformat(timespec="seconds"),
            trade_date=bars[-1].date if bars else _now().date().isoformat(),
            detail=detail,
            coverage=len(bars),
        )

    def synthetic(self, code: str, anchor: float | None = None) -> tuple[list, DataProvenance]:
        """Synthetic bars anchored to a SIM price, so a SIM sheet never pairs a
        made-up quote with somebody else's real history."""
        return self._synthetic_bars(code, anchor)

    def _synthetic_bars(self, code: str, anchor: float | None = None) -> tuple[list, DataProvenance]:
        bars = synthetic_history(code, date.today(), anchor=anchor)
        provenance = DataProvenance(
            mode="SIM",
            source="pulse · 合成日线生成器",
            as_of=_now().isoformat(timespec="seconds"),
            trade_date=bars[-1].date if bars else date.today().isoformat(),
            status="fallback",
            detail="合成日线仅用于界面与算法演示，不含真实历史成交，不可作为研究依据。",
            coverage=len(bars),
        )
        return bars, provenance
