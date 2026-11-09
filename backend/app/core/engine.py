"""One in-memory market state. Every read is a pure projection of one frame."""
from __future__ import annotations

from collections import deque
from datetime import date, datetime, timedelta, timezone
import logging
import threading
import time

import polars as pl

from contracts.schema import (
    Breadth,
    DataProvenance,
    FactorContribution,
    FieldCoverage,
    LadderMember,
    LadderTierStat,
    MacroDashboardSummary,
    MacroPoolType,
    MarketEventsResponse,
    MarketHistoryPoint,
    ProviderStatus,
    QualityResponse,
    ScreenerFilterRequest,
    ScreenerQueryResponse,
    SealPoint,
    StockTickRecord,
    ThemeResonanceStat,
    TurnoverRegime,
)
from ..config import settings
from ..factors.engine import EXPLANATIONS, LABELS, WEIGHTS, compute as compute_factors
from ..market.live import AkShareFeed
from ..market.history import HistoryProvider
from ..market.simulation import Simulator, synthetic_highs
from .events import EventTape, diff as diff_frames
from .storage import storage

logger = logging.getLogger("pulse")
CST = timezone(timedelta(hours=8))
CURVE_POINTS = 240
UNKNOWN_SECTOR = "A股全市场"
TIER_MEMBER_CAP = 24
GROWTH_MEMBER_CAP = 12
SEAL_TIMELINE_CAP = 400
GROWTH_TIERS = (3, 4, 5, 6, 7)
SCORE_KEYS = tuple(WEIGHTS)
SCORE_COLUMNS = tuple(f"score_{key}" for key in SCORE_KEYS)
QUALITY_FIELDS = (
    ("code", "代码"),
    ("price", "现价"),
    ("pct_chg", "涨幅"),
    ("amount_today", "成交额"),
    ("turnover_today", "今日换手"),
    ("turnover_yesterday", "昨日换手"),
    ("volume_ratio", "量比"),
    ("total_mkt_cap", "总市值"),
    ("pe_ttm", "市盈率 TTM"),
    ("pb", "市净率"),
    ("seal_order_amount", "封单额"),
    ("seal_to_amount_ratio", "封成比"),
    ("high_60d", "60 日高点"),
    ("seal_success_rate_1y", "近一年封板率"),
    ("intraday_sparkline", "分时序列"),
)


def _now() -> datetime:
    return datetime.now(CST)


class MarketEngine:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._features = storage.historical_features()
        self._simulator = Simulator(features=self._features)
        self._align_history_highs()
        requested = settings.mode if settings.mode in {"LIVE", "SIM"} else "SIM"
        self._requested_mode = requested
        self._mode = "SIM"
        self._live = AkShareFeed()
        self._sequence = 0
        self._curve: deque[MarketHistoryPoint] = deque(maxlen=CURVE_POINTS)
        self._frame = pl.DataFrame()
        self._frame_mode: str | None = None
        self._tape = EventTape()
        self._last_event_count = 0
        self._themes_cache: tuple[int, list[str]] = (-1, [])
        self._status = "ready"
        self._detail = ""
        self._source = "pulse · 高保真盘口模拟器"
        self._ticked_at = _now()
        self.refresh()

    # --- lifecycle -------------------------------------------------------
    def _align_history_highs(self) -> None:
        """Derive each 60-day high from the same series the detail chart draws.

        Without this the distance-to-high factor and the visible chart would be
        computed from two unrelated series.
        """
        for row in self._simulator.rows:
            highs = synthetic_highs(row["code"], date.today(), count=260, anchor=row["price"])
            recent = highs[-60:]
            row["high_60d"] = max(recent) if recent else row["price"]

    @property
    def history(self) -> HistoryProvider:
        return _HISTORY

    def set_mode(self, mode: str) -> DataProvenance:
        if mode not in {"LIVE", "SIM"}:
            raise ValueError("mode must be LIVE or SIM")
        with self._lock:
            self._requested_mode = mode
            self._mode = "SIM"
            self.refresh()
            return self.provenance()

    def refresh(self) -> int:
        """Advance one tick and return how many events the diff produced."""
        with self._lock:
            started = time.perf_counter()
            raw = self._acquire()
            frame = compute_factors(raw)
            previous, previous_mode = self._frame, self._frame_mode
            self._frame = frame
            self._frame_mode = self._mode
            self._sequence += 1
            self._ticked_at = _now()
            self._last_event_count = self._record_events(previous, previous_mode, frame)
            self._latency_ms = (time.perf_counter() - started) * 1000
            self._curve.append(
                MarketHistoryPoint(
                    time=self._ticked_at.strftime("%H:%M:%S"),
                    value=self._sentiment(frame),
                )
            )
            return self._last_event_count

    def _record_events(
        self, previous: pl.DataFrame, previous_mode: str | None, frame: pl.DataFrame
    ) -> int:
        """Diff against the previous frame, but only within one data source.

        Across a SIM/LIVE switch the whole market differs, so every row would
        look like an event; the tape is dropped instead.
        """
        if previous.is_empty() or previous_mode != self._mode:
            self._tape.clear()
            return 0
        try:
            found = diff_frames(
                previous, frame, self._sequence, self._ticked_at.strftime("%H:%M:%S"), self._tape.gate
            )
        except Exception:
            # A malformed provider frame must not stop the tick loop.
            logger.exception("event diff failed")
            return 0
        self._tape.extend(found)
        return len(found)

    @property
    def last_event_count(self) -> int:
        return self._last_event_count

    def events(self, since: int | None, limit: int) -> MarketEventsResponse:
        with self._lock:
            return MarketEventsResponse(
                provenance=self.provenance(), events=self._tape.latest(since, limit)
            )

    def theme_names(self) -> list[str]:
        """Distinct real theme names in the current frame (for the NL parser)."""
        with self._lock:
            if self._themes_cache[0] != self._sequence:
                names = (
                    self._frame.select(pl.col("theme_tags").explode().drop_nulls().unique())
                    .to_series()
                    .to_list()
                )
                self._themes_cache = (
                    self._sequence,
                    sorted(name for name in names if name != UNKNOWN_SECTOR),
                )
            return list(self._themes_cache[1])

    def _acquire(self) -> pl.DataFrame:
        if self._requested_mode == "LIVE":
            # The feed refreshes on its own thread: a 30 s Sina snapshot must
            # never block a tick.
            self._live.start()
            live = self._live.fetch()
            if live is not None and live.height > 0:
                age = self._live.age_seconds
                if self._mode != "LIVE":
                    # A curve that mixes SIM and LIVE points would be a lie.
                    self._curve.clear()
                self._mode = "LIVE"
                self._source = self._live.name
                self._status = "ready" if age < self._live.ttl_seconds * 2 else "stale"
                failed = f"最近一次刷新失败：{self._live.error}。" if self._status == "stale" and self._live.error else ""
                self._detail = (
                    f"{self._live.note}；快照 {int(age)} 秒前获取。{failed}"
                    "炸板数按快照口径统计，是下界；换手、量比等字段缺失处保留为空。"
                )
                return live
            self._mode = "SIM"
            self._status = "fallback"
            self._source = self._live.name
            self._detail = (
                f"实时数据尚未就绪（{self._live.error or '首次拉取进行中，通常需要 30 秒'}），"
                "当前显示模拟行情；所有数字均标记为 SIM。"
            )
        self._status = "ready" if self._mode == "SIM" and self._requested_mode == "SIM" else self._status
        self._source = "pulse · 高保真盘口模拟器"
        self._detail = (
            "由内置场景生成，用于界面与算法演示；不是真实行情。"
            if self._requested_mode == "SIM"
            else self._detail
        )
        return self._simulator.tick()

    # --- provenance ------------------------------------------------------
    def provenance(self) -> DataProvenance:
        with self._lock:
            frame = self._frame
            coverage = frame.height
            missing = [
                name
                for name in (
                    "turnover_yesterday", "seal_order_amount", "first_seal_time",
                    "zt_count_1y", "high_60d", "volume_ratio",
                )
                if name in frame.columns and frame[name].null_count() == frame.height
            ]
            return DataProvenance(
                mode=self._mode,
                source=self._source,
                as_of=self._ticked_at.isoformat(timespec="seconds"),
                trade_date=self._ticked_at.date().isoformat(),
                status=self._status,
                detail=self._detail,
                sequence=self._sequence,
                latency_ms=round(getattr(self, "_latency_ms", 0.0), 2),
                coverage=coverage,
                missing_fields=missing,
            )

    # --- projections -----------------------------------------------------
    def providers(self) -> list[ProviderStatus]:
        """Declare every data boundary instead of implying one implicit source."""
        live_installed = AkShareFeed.is_available()
        mode = self._mode
        return [
            ProviderStatus(
                key="scenario",
                name="pulse · 高保真盘口模拟器",
                kind="scenario",
                available=True,
                active=mode == "SIM",
                detail="内置场景：涨停梯队、炸板、连阳分布，每 2 秒演进一次",
            ),
            ProviderStatus(
                key="akshare",
                name=AkShareFeed.name,
                kind="live",
                available=live_installed,
                active=mode == "LIVE",
                detail=(
                    "已安装，可切换 LIVE"
                    if live_installed and not self._live.error
                    else (self._live.error or "未安装 akshare，LIVE 不可用")
                ),
            ),
            ProviderStatus(
                key="duckdb",
                name="DuckDB 历史特征（只读）",
                kind="history",
                available=settings.history_db.exists(),
                active=False,
                detail=str(settings.history_db),
            ),
            ProviderStatus(
                key="sqlite",
                name="SQLite 用户状态（自选 / 回测归档）",
                kind="store",
                available=True,
                active=True,
                detail=str(settings.store_db),
            ),
        ]

    def quality(self) -> QualityResponse:
        """Per-field coverage of the current snapshot. Missing means null, not zero."""
        with self._lock:
            frame = self._frame
            rows = frame.height
            fields: list[FieldCoverage] = []
            for name, label in QUALITY_FIELDS:
                if name not in frame.columns:
                    fields.append(FieldCoverage(field=name, label=label, coverage=0, missing=rows))
                    continue
                missing = frame[name].null_count()
                coverage = round((rows - missing) / rows * 100, 1) if rows else 0.0
                fields.append(
                    FieldCoverage(field=name, label=label, coverage=coverage, missing=missing)
                )
            return QualityResponse(provenance=self.provenance(), rows=rows, fields=fields)

    def overview(self) -> MacroDashboardSummary:
        with self._lock:
            frame = self._frame
            total = frame.height
            limit_up = frame.filter(pl.col("is_limit_up")).height
            broken = frame.filter(pl.col("ever_limit_up_today") & ~pl.col("is_limit_up")).height
            attempted = limit_up + broken
            max_height = int(frame["limit_streak"].max() or 0)
            leader = (
                frame.filter(pl.col("limit_streak") == max_height).sort(
                    "amount_today", descending=True
                )
                if max_height
                else frame.head(0)
            )
            yesterday_pool = frame.filter(pl.col("prev_limit") >= 1)
            premium = (
                float(yesterday_pool["pct_chg"].mean()) if yesterday_pool.height else None
            )
            breadth = self._breadth(frame)
            return MacroDashboardSummary(
                provenance=self.provenance(),
                total_stocks=total,
                total_limit_up=limit_up,
                total_broken_board=broken,
                broken_rate=round(broken / attempted * 100, 2) if attempted else 0.0,
                max_limit_height=max_height,
                max_height_stock=leader["name"][0] if leader.height else "—",
                yesterday_limit_up_premium=round(premium, 2) if premium is not None else None,
                total_amount=round(float(frame["amount_today"].sum() or 0.0), 2),
                sentiment_score=self._sentiment(frame),
                breadth=breadth,
                limit_ladder_tiers=self._ladder_tiers(frame),
                growth_streak_tiers=self._growth_tiers(frame),
                hot_themes=self._themes(frame),
                market_curve=list(self._curve),
                seal_timeline=self._seal_timeline(frame),
            )

    def query(self, filters: ScreenerFilterRequest) -> ScreenerQueryResponse:
        started = time.perf_counter()
        with self._lock:
            frame = self._apply(filters)
            total = frame.height
            ordered = frame.sort(
                filters.sort_by, descending=filters.sort_desc, nulls_last=True
            ).slice(filters.offset, filters.limit)
            records = _records(ordered)
            elapsed = (time.perf_counter() - started) * 1000
            return ScreenerQueryResponse(
                provenance=self.provenance(),
                total_matched=total,
                execution_ms=round(elapsed, 2),
                records=records,
            )

    def stock(self, code: str) -> StockTickRecord | None:
        with self._lock:
            row = self._frame.filter(pl.col("code") == code)
            return _records(row)[0] if row.height else None

    def price_of(self, code: str) -> float | None:
        with self._lock:
            row = self._frame.filter(pl.col("code") == code)
            return float(row["price"][0]) if row.height else None

    def observations(self, record: StockTickRecord) -> list[str]:
        notes: list[str] = []
        if record.is_limit_up and record.limit_streak:
            notes.append(
                f"当前 {record.limit_streak} 连板，首次封板 {record.first_seal_time or '未记录'}，"
                f"封单 {record.seal_order_amount if record.seal_order_amount is not None else '—'} 亿元。"
            )
        elif record.ever_limit_up_today:
            notes.append(
                f"今日曾封涨停后开板，炸板 {record.open_board_count if record.open_board_count is not None else '—'} 次。"
            )
        if record.turnover_today is not None:
            yesterday = (
                f"昨日 {record.turnover_yesterday:.2f}%，昨换比 {record.turnover_ratio_yd:.2f}"
                if record.turnover_yesterday is not None and record.turnover_ratio_yd is not None
                else "昨日换手率未知"
            )
            notes.append(f"今日换手 {record.turnover_today:.2f}%，{yesterday}。")
        if record.growth_streak and record.growth_streak >= 2:
            notes.append(
                f"连续 {record.growth_streak} 日收涨，区间累计 "
                f"{record.streak_cum_return:+.2f}%。" if record.streak_cum_return is not None
                else f"连续 {record.growth_streak} 日收涨。"
            )
        if record.zt_count_1y is not None:
            rate = (
                f"{record.seal_success_rate_1y:.1f}%"
                if record.seal_success_rate_1y is not None
                else "—"
            )
            notes.append(
                f"近一年涨停 {record.zt_count_1y} 次，封板成功率 {rate}，"
                f"最长 {record.max_streak_1y if record.max_streak_1y is not None else '—'} 连板。"
            )
        if record.dist_to_60d_high is not None:
            notes.append(f"距 60 日高点 {record.dist_to_60d_high:+.2f}%。")
        if record.theme_tags:
            notes.append(f"场景题材簇：{'、'.join(record.theme_tags)}。")
        return notes

    # --- internals -------------------------------------------------------
    def _apply(self, filters: ScreenerFilterRequest) -> pl.DataFrame:
        frame = self._frame
        conditions = [pl.col("board").is_in([board.value for board in filters.boards])]
        if filters.exclude_st:
            conditions.append(~pl.col("is_st"))
        if filters.exclude_new:
            conditions.append(~pl.col("is_new"))
        if not filters.include_suspended:
            conditions.append(~pl.col("is_suspended"))
        pool = filters.pool_type
        if pool is MacroPoolType.LIMIT_UP_LADDER:
            conditions.append(pl.col("is_limit_up"))
        elif pool is MacroPoolType.N_DAYS_M_BOARDS:
            conditions.append(pl.col("n_days_m_boards_label").is_not_null())
        elif pool is MacroPoolType.BROKEN_OR_ATTACK:
            conditions.append(pl.col("ever_limit_up_today") & ~pl.col("is_limit_up"))
        elif pool is MacroPoolType.CONTINUOUS_GROWTH:
            column = "tolerant_growth" if filters.allow_doji_tolerance else "growth_streak"
            conditions.append(pl.col(column) >= (filters.min_growth_streak or 3))
        if filters.limit_streak_exact:
            conditions.append(pl.col("limit_streak").is_in(filters.limit_streak_exact))
        if filters.min_growth_streak is not None and pool is not MacroPoolType.CONTINUOUS_GROWTH:
            column = "tolerant_growth" if filters.allow_doji_tolerance else "growth_streak"
            conditions.append(pl.col(column) >= filters.min_growth_streak)
        conditions.append(_between("free_float_mkt_cap", filters.free_cap_min, filters.free_cap_max, 20000))
        conditions.append(_between("total_mkt_cap", filters.total_cap_min, filters.total_cap_max, None))
        # Unlike the range filters, a positive-valuation cap does NOT let nulls
        # through: "PE below 20" must mean a known, profitable PE. Letting
        # loss-makers (negative PE) or names the provider cannot value pass
        # would flood a value screen with the very stocks it should exclude.
        if filters.pe_max is not None:
            conditions.append((pl.col("pe_ttm") > 0) & (pl.col("pe_ttm") <= filters.pe_max))
        if filters.pb_max is not None:
            conditions.append((pl.col("pb") > 0) & (pl.col("pb") <= filters.pb_max))
        conditions.append(_between("price", filters.price_min, filters.price_max, 10000))
        if filters.pct_chg_min is not None:
            conditions.append(pl.col("pct_chg") >= filters.pct_chg_min)
        if filters.pct_chg_max is not None:
            conditions.append(pl.col("pct_chg") <= filters.pct_chg_max)
        conditions.append(_between("turnover_today", filters.turnover_today_min, filters.turnover_today_max, 100))
        conditions.append(_between("turnover_yesterday", filters.turnover_yd_min, filters.turnover_yd_max, 100))
        if filters.turnover_regime is TurnoverRegime.SHRINK_ACCEL:
            conditions.append(pl.col("turnover_ratio_yd") < 0.75)
        elif filters.turnover_regime is TurnoverRegime.NORMAL_HAND:
            conditions.append(
                (pl.col("turnover_ratio_yd") >= 0.75) & (pl.col("turnover_ratio_yd") <= 1.35)
            )
        elif filters.turnover_regime is TurnoverRegime.EXPAND_DIV:
            conditions.append(pl.col("turnover_ratio_yd") > 1.35)
        conditions.append(_between("turnover_ratio_yd", filters.turnover_ratio_min, filters.turnover_ratio_max, None))
        conditions.append(_at_least("amount_today", filters.min_amount_today))
        if filters.max_first_seal_time:
            conditions.append(
                pl.col("first_seal_time").is_null()
                | (pl.col("first_seal_time") <= filters.max_first_seal_time)
            )
        if filters.max_open_board_count is not None:
            conditions.append(
                pl.col("open_board_count").is_null()
                | (pl.col("open_board_count") <= filters.max_open_board_count)
            )
        conditions.append(_at_least("seal_to_amount_ratio", filters.min_seal_to_amount_ratio))
        if filters.only_early_seal:
            conditions.append(pl.col("early_seal") == True)  # noqa: E712 - explicit tri-state
        if filters.min_seal_span_minutes is not None:
            conditions.append(pl.col("seal_span_minutes") >= filters.min_seal_span_minutes)
        if filters.min_streak_cum_return is not None:
            conditions.append(pl.col("streak_cum_return") >= filters.min_streak_cum_return)
        conditions.append(_at_least("zt_count_1y", filters.min_zt_count_1y))
        conditions.append(_at_least("seal_success_rate_1y", filters.min_seal_success_rate_1y))
        if filters.only_near_60d_high:
            conditions.append(pl.col("dist_to_60d_high") >= -5)
        if filters.selected_themes:
            conditions.append(
                pl.col("theme_tags")
                .list.eval(pl.element().is_in(filters.selected_themes))
                .list.any()
            )
        if filters.search.strip():
            needle = filters.search.strip()
            conditions.append(
                pl.col("name").str.contains(needle, literal=True)
                | pl.col("code").str.starts_with(needle)
            )
        if filters.codes is not None:
            conditions.append(pl.col("code").is_in(filters.codes))
        return frame.filter(pl.all_horizontal(conditions))

    def _breadth(self, frame: pl.DataFrame) -> Breadth:
        # Suspended names are not trading, so they must not enter breadth stats.
        frame = frame.filter(~pl.col("is_suspended"))
        edges = (-7.0, -5.0, -3.0, -1.0, 0.0, 1.0, 3.0, 5.0, 7.0)
        labels = ["≤-7", "-7~-5", "-5~-3", "-3~-1", "-1~0", "0~1", "1~3", "3~5", "5~7", "≥7"]
        buckets = [frame.filter(pl.col("pct_chg") < edges[0]).height]
        buckets += [
            frame.filter(
                (pl.col("pct_chg") >= edges[index]) & (pl.col("pct_chg") < edges[index + 1])
            ).height
            for index in range(len(edges) - 1)
        ]
        buckets.append(frame.filter(pl.col("pct_chg") >= edges[-1]).height)
        return Breadth(
            up=frame.filter(pl.col("pct_chg") > 0).height,
            down=frame.filter(pl.col("pct_chg") < 0).height,
            flat=frame.filter(pl.col("pct_chg") == 0).height,
            buckets=buckets,
            bucket_labels=labels,
        )

    def _ladder_tiers(self, frame: pl.DataFrame) -> list[LadderTierStat]:
        tiers: list[LadderTierStat] = []
        max_height = int(frame["limit_streak"].max() or 0)
        for level in range(1, max(max_height, 1) + 1):
            members = frame.filter(pl.col("limit_streak") == level)
            if not members.height:
                continue
            # Promotion needs yesterday's pool of the same tier; level 1 has no
            # meaningful denominator (every stock would be in "yesterday 0 board").
            pool = frame.filter(pl.col("prev_limit") == level - 1) if level > 1 else frame.head(0)
            promoted = pool.filter(pl.col("is_limit_up")).height
            top = members.sort("amount_today", descending=True).row(0, named=True)
            tiers.append(
                LadderTierStat(
                    tier_key=f"{level}板",
                    title=f"{level} 连板",
                    count=members.height,
                    promotion_rate=round(promoted / pool.height * 100, 1) if pool.height else None,
                    top_stock_name=top["name"],
                    top_theme=_first_theme(top["theme_tags"]),
                    members=_members(
                        members,
                        ["is_limit_up", "first_seal_time", "amount_today"],
                        [True, False, True],
                        TIER_MEMBER_CAP,
                    ),
                )
            )
        return tiers

    def _seal_timeline(self, frame: pl.DataFrame) -> list[SealPoint]:
        """Every stock that has touched the ceiling today, placed on the session clock."""
        sealed = (
            frame.filter(~pl.col("is_suspended") & pl.col("first_seal_time").is_not_null())
            .sort(["first_seal_time", "code"])
            .head(SEAL_TIMELINE_CAP)
        )
        return [
            SealPoint(
                code=row["code"],
                name=row["name"],
                first_seal_time=row["first_seal_time"],
                limit_streak=row["limit_streak"],
                is_limit_up=row["is_limit_up"],
                open_board_count=row["open_board_count"],
            )
            for row in sealed.select(
                "code", "name", "first_seal_time", "limit_streak", "is_limit_up", "open_board_count"
            ).to_dicts()
        ]

    def _growth_tiers(self, frame: pl.DataFrame) -> list[LadderTierStat]:
        tiers: list[LadderTierStat] = []
        for level in GROWTH_TIERS:
            members = (
                frame.filter(pl.col("growth_streak") >= 7)
                if level == 7
                else frame.filter(pl.col("growth_streak") == level)
            )
            if not members.height:
                continue
            top = members.sort("streak_cum_return", descending=True).row(0, named=True)
            tiers.append(
                LadderTierStat(
                    tier_key=f"{level}连阳" if level < 7 else "7+连阳",
                    title=f"{level} 连阳" if level < 7 else "7 连阳以上",
                    count=members.height,
                    promotion_rate=None,
                    top_stock_name=top["name"],
                    top_theme=_first_theme(top["theme_tags"]),
                    members=_members(members, ["streak_cum_return"], [True], GROWTH_MEMBER_CAP),
                )
            )
        return tiers

    def _themes(self, frame: pl.DataFrame) -> list[ThemeResonanceStat]:
        exploded = frame.explode("theme_tags").drop_nulls("theme_tags")
        # Live snapshots tag every unknown name with a placeholder; it is not a theme.
        exploded = exploded.filter(pl.col("theme_tags") != UNKNOWN_SECTOR)
        if not exploded.height:
            return []
        stats: list[ThemeResonanceStat] = []
        for (theme,), group in exploded.group_by(["theme_tags"], maintain_order=True):
            leader = group.sort(["pct_chg", "amount_today"], descending=[True, True]).row(0, named=True)
            limit_up_count = group.filter(pl.col("is_limit_up")).height
            growth_count = group.filter(pl.col("growth_streak") >= 3).height
            avg_pct = float(group["pct_chg"].mean() or 0.0)
            amount = float(group["amount_today"].sum() or 0.0)
            sparkline = _average_sparkline(group)
            stats.append(
                ThemeResonanceStat(
                    theme_name=str(theme),
                    stock_count=group.height,
                    limit_up_count=limit_up_count,
                    growth_count=growth_count,
                    avg_pct_chg=round(avg_pct, 2),
                    amount=round(amount, 2),
                    leader_code=leader["code"],
                    leader_name=leader["name"],
                    strength=round(limit_up_count * 2 + growth_count + avg_pct * 0.5, 2),
                    sparkline=sparkline,
                )
            )
        return sorted(stats, key=lambda item: item.strength, reverse=True)[:12]

    def _sentiment(self, frame: pl.DataFrame) -> float:
        total = frame.height or 1
        limit_up = frame.filter(pl.col("is_limit_up")).height
        broken = frame.filter(pl.col("ever_limit_up_today") & ~pl.col("is_limit_up")).height
        attempted = limit_up + broken
        up = frame.filter(pl.col("pct_chg") > 0).height
        down = frame.filter(pl.col("pct_chg") < 0).height
        height = int(frame["limit_streak"].max() or 0)
        limit_ratio = min(1.0, limit_up / total / 0.08)
        breadth = up / (up + down) if up + down else 0.5
        height_ratio = min(height, 7) / 7
        seal_quality = 1 - min(1.0, (broken / attempted) if attempted else 0.0)
        score = 0.40 * limit_ratio + 0.25 * breadth + 0.20 * height_ratio + 0.15 * seal_quality
        return round(score * 100, 1)


_HISTORY = HistoryProvider()
_ENGINE: MarketEngine | None = None
_ENGINE_LOCK = threading.Lock()


def engine() -> MarketEngine:
    global _ENGINE
    with _ENGINE_LOCK:
        if _ENGINE is None:
            _ENGINE = MarketEngine()
        return _ENGINE


def _records(frame: pl.DataFrame) -> list[StockTickRecord]:
    fields = [
        name
        for name in StockTickRecord.model_fields
        if name != "factors" and name in frame.columns
    ]
    columns = fields + [column for column in SCORE_COLUMNS if column in frame.columns]
    rows = frame.select(columns).to_dicts()
    for row in rows:
        row["factors"] = [
            FactorContribution(
                key=key,
                label=LABELS[key],
                score=row.pop(f"score_{key}", None),
                weight=weight,
                explanation=EXPLANATIONS[key],
            )
            for key, weight in WEIGHTS.items()
        ]
    return [StockTickRecord.model_validate(row) for row in rows]


def _first_theme(tags: list[str] | None) -> str | None:
    """First real theme; the live placeholder says nothing about the stock."""
    return next((tag for tag in tags or [] if tag != UNKNOWN_SECTOR), None)


def _members(
    group: pl.DataFrame, by: list[str], descending: list[bool], cap: int
) -> list[LadderMember]:
    """Tier members in display order; nulls last so unknown seal times never lead."""
    ordered = group.sort(by, descending=descending, nulls_last=True).head(cap)
    return [
        LadderMember(
            code=row["code"],
            name=row["name"],
            pct_chg=row["pct_chg"],
            is_limit_up=row["is_limit_up"],
            first_seal_time=row["first_seal_time"],
            open_board_count=row["open_board_count"],
            seal_order_amount=row["seal_order_amount"],
            theme=_first_theme(row["theme_tags"]),
        )
        for row in ordered.select(
            "code", "name", "pct_chg", "is_limit_up", "first_seal_time",
            "open_board_count", "seal_order_amount", "theme_tags",
        ).to_dicts()
    ]


def _between(column: str, low: float | None, high: float | None, default_high: float | None):
    """Unknown values pass the condition instead of being excluded.

    The UI states that missing fields do not participate in a filter; excluding
    them would silently hide every name whose provider lacks the metric.
    """
    expression = pl.lit(True)
    if low:
        expression = expression & (pl.col(column).fill_null(low) >= low)
    if high is not None and (default_high is None or high < default_high):
        expression = expression & (pl.col(column).fill_null(high) <= high)
    return expression


def _at_least(column: str, value: float | None):
    if value is None or value <= 0:
        return pl.lit(True)
    return pl.col(column) >= value


def _average_sparkline(group: pl.DataFrame) -> list[float]:
    series = [row for row in group["intraday_sparkline"].to_list() if row]
    if not series:
        return []
    length = min(len(row) for row in series)
    return [
        round(sum(row[-length + index] for row in series) / len(series), 3)
        for index in range(length)
    ]
