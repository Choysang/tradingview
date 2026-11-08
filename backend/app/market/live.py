"""Best-effort live feed. Any failure returns None so the engine can fall back.

Two AkShare sources are mapped, because the fast Eastmoney snapshot is not
reachable from every network:

1. `stock_zh_a_spot_em` — full market, includes turnover and float cap.
2. `stock_zh_a_spot` (Sina) — full market, basic fields, ~30 s per call.

The Sina path is fetched by a background thread on a TTL so a slow upstream can
never block a tick. Both paths merge the Eastmoney limit-up pool for ladder
detail. Every failure keeps a human reason for `DataProvenance.detail`.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import math
import threading
import time

import httpx
import httpx
import polars as pl

from .simulation import price_limit

EASTMONEY_SPOT = {
    "代码": "code",
    "名称": "name",
    "最新价": "price",
    "涨跌幅": "pct_chg",
    "成交额": "amount_yuan",
    "换手率": "turnover_today",
    "量比": "volume_ratio",
    "流通市值": "float_cap_yuan",
}
SINA_SPOT = {
    "代码": "code",
    "名称": "name",
    "最新价": "price",
    "涨跌幅": "pct_chg",
    "成交额": "amount_yuan",
    "昨收": "previous_close",
}
POOL = {
    "代码": "code",
    "连板数": "streak_today",
    "首次封板时间": "first_seal_time",
    "最后封板时间": "last_seal_time",
    "封板资金": "seal_amount_yuan",
    "炸板次数": "open_board_count",
    "所属行业": "industry",
}
POOL_FIELDS = ["first_seal_time", "last_seal_time", "open_board_count", "seal_order_amount"]


class AkShareFeed:
    name = "akshare · 实时快照"
    ttl_seconds = 300.0

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._frame: pl.DataFrame | None = None
        self._stamp = 0.0
        self._note = ""
        self._error = ""
        self._thread: threading.Thread | None = None

    @staticmethod
    def is_available() -> bool:
        try:
            import akshare  # noqa: F401
        except Exception:
            return False
        return True

    # --- background refresh ---------------------------------------------
    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._loop, name="pulse-live", daemon=True)
        self._thread.start()

    def _loop(self) -> None:
        while True:
            try:
                self.refresh_now()
            except Exception as error:  # noqa: BLE001 - a dead thread must stay visible
                with self._lock:
                    self._error = f"后台刷新异常：{type(error).__name__}: {error}"
            time.sleep(self.ttl_seconds)

    def refresh_now(self) -> pl.DataFrame | None:
        frame, note, error = self._load()
        with self._lock:
            if frame is not None:
                self._frame = frame
                self._stamp = time.time()
                self._note = note
                self._error = ""
            else:
                self._error = error
        return frame

    def fetch(self) -> pl.DataFrame | None:
        self.start()
        with self._lock:
            return self._frame

    @property
    def note(self) -> str:
        with self._lock:
            return self._note

    @property
    def error(self) -> str:
        with self._lock:
            return self._error

    @property
    def age_seconds(self) -> float:
        with self._lock:
            return time.time() - self._stamp if self._stamp else 0.0

    # --- loading ---------------------------------------------------------
    def _load(self) -> tuple[pl.DataFrame | None, str, str]:
        try:
            import akshare as ak
        except Exception:
            return None, "", "未安装 akshare"
        pool, pool_note = _load_pool(ak)
        spot, note, error = _load_spot(ak)
        if spot is None:
            return None, "", error
        frame = _assemble(spot, pool)
        detail = note + (f" · {pool_note}" if pool is not None else " · 无涨停池")
        frame = _enrich_from_tencent(frame)
        return frame, detail, ""


def _enrich_from_tencent(frame: pl.DataFrame) -> pl.DataFrame:
    """Fill turnover, float cap and volume ratio that Sina does not carry.

    Tencent also reports the real limit prices, which beat a derived value for ST
    and newly listed names, so the quote wins where it exists.
    """
    wanted = {
        "turnover_today": ("turnover", "rate"),
        "volume_ratio": ("volume_ratio", "ratio"),
        "float_mkt_cap": ("float_cap", "cap"),
        "total_mkt_cap": ("total_cap", "cap_total"),
        "pe_ttm": ("pe", "pe_quote"),
        "pb": ("pb", "pb_quote"),
        "limit_price": ("limit_up", "limit_up_price"),
    }
    if not any(column in frame.columns for column in wanted):
        return frame
    if "turnover_today" in frame.columns:
        missing = frame.filter(pl.col("turnover_today").is_null())["code"].to_list()
    else:
        missing = []
    quotes = _tencent_quotes(missing or frame["code"].to_list())
    if not quotes:
        return frame
    patch = pl.DataFrame({"code": list(quotes.keys())})
    for _, (key, name) in wanted.items():
        patch = patch.with_columns(pl.Series(name, [quotes[code].get(key) for code in quotes]))
    filled = frame.join(patch, on="code", how="left")
    for column, (_, name) in wanted.items():
        if column in filled.columns:
            # The quote wins: it already knows ST, board and new-listing rules.
            filled = filled.with_columns(pl.coalesce([name, column]).alias(column))
    if "float_mkt_cap" in filled.columns:
        filled = filled.with_columns(
            (pl.col("float_mkt_cap") * 0.62).round(2).alias("free_float_mkt_cap"),
        )
    drop = [name for _, (_, name) in wanted.items() if name in filled.columns]
    return filled.drop(drop)

def _optional(text: str, positive: bool = False) -> float | None:
    """Parse an optional quote field; blank, non-numeric and zero mean unknown."""
    try:
        value = float(text)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(value) or value == 0 or (positive and value < 0):
        return None
    return value


def _tencent_quotes(codes: list[str]) -> dict[str, dict[str, float | None]]:
    """Quote fields per code from qt.gtimg.cn. Bounded and fully optional."""
    from ..research.tencent_history import market  # BSE prefixes are checked first there

    codes = [code for code in codes if len(code) == 6 and code.isdigit()][:6000]
    if not codes:
        return {}

    batches = [codes[index : index + 80] for index in range(0, len(codes), 80)]

    def fetch(batch: list[str]) -> dict[str, dict[str, float | None]]:
        symbols = ",".join(f"{market(code)}{code}" for code in batch)
        try:
            response = httpx.get(f"http://qt.gtimg.cn/q={symbols}", timeout=6)
        except httpx.HTTPError:
            return {}
        found: dict[str, dict[str, float | None]] = {}
        for line in response.text.splitlines():
            if '"' not in line:
                continue
            raw = line.split('"', 1)[1].split("~")
            if len(raw) < 50:
                continue
            try:
                found[raw[2]] = {
                    "turnover": float(raw[38]),  # 换手率 %
                    "volume_ratio": float(raw[49]),  # 量比
                    "float_cap": float(raw[44]),  # 流通市值（报价即亿元）
                    "limit_up": float(raw[47]),  # 涨停价
                    # Valuation is optional: a blank or zero field is unknown,
                    # and must never become a 0 that a filter could match.
                    "total_cap": _optional(raw[45], positive=True),  # 总市值（亿元）
                    "pe": _optional(raw[39]),  # 市盈率 TTM，亏损为负值
                    "pb": _optional(raw[46], positive=True),  # 市净率
                }
            except (ValueError, IndexError):
                continue
        return found

    result: dict[str, dict[str, float | None]] = {}
    with ThreadPoolExecutor(max_workers=4) as pool:
        for chunk in pool.map(fetch, batches):
            result.update(chunk)
    return result

def _assemble(spot: pl.DataFrame, pool: pl.DataFrame | None) -> pl.DataFrame:
    frame = spot.drop_nulls(["code", "price"]).with_columns(
        # Sina prefixes codes with sh/sz/bj; the contract wants six digits.
        pl.col("code")
        .cast(pl.Utf8)
        .str.replace_all(r"^[A-Za-z]+", "")
        .str.zfill(6)
        .alias("code"),
        pl.col("name").cast(pl.Utf8),
        pl.col("price").cast(pl.Float64),
        pl.col("pct_chg").cast(pl.Float64),
        (pl.col("amount_yuan").cast(pl.Float64) / 1e8).alias("amount_today"),
    )
    optional = {
        "turnover_today": pl.Float64,
        "volume_ratio": pl.Float64,
        "previous_close": pl.Float64,
        "float_cap_yuan": pl.Float64,
        "total_mkt_cap": pl.Float64,
        "pe_ttm": pl.Float64,
        "pb": pl.Float64,
    }
    for column, dtype in optional.items():
        if column not in frame.columns:
            frame = frame.with_columns(pl.lit(None, dtype=dtype).alias(column))
        else:
            frame = frame.with_columns(pl.col(column).cast(dtype))
    frame = frame.with_columns(
        (pl.col("float_cap_yuan") / 1e8).alias("float_mkt_cap"),
        pl.coalesce(
            [
                pl.col("previous_close"),
                (pl.col("price") / (1 + pl.col("pct_chg") / 100)).round(2),
            ]
        ).alias("previous_close"),
    ).with_columns(
        pl.when(pl.col("code").str.starts_with("30") | pl.col("code").str.starts_with("68"))
        .then(pl.lit("20cm"))
        .when(
            pl.col("code").str.starts_with("8")
            | pl.col("code").str.starts_with("4")
            | pl.col("code").str.starts_with("92")
        )
        .then(pl.lit("30cm"))
        .otherwise(pl.lit("10cm"))
        .alias("board"),
        pl.col("name").str.contains("ST").fill_null(False).alias("is_st"),
    ).with_columns(
        (pl.col("float_mkt_cap") * 0.62).round(2).alias("free_float_mkt_cap"),
        pl.lit(False).alias("is_new"),
        pl.lit(["A股全市场"], dtype=pl.List(pl.Utf8)).alias("theme_tags"),
    ).with_columns(
        pl.struct(["previous_close", "board", "is_st"])
        .map_elements(_limit_price, return_dtype=pl.Float64)
        .alias("limit_price"),
        pl.lit(None, dtype=pl.Float64).alias("prev_cum"),
        pl.lit(0).cast(pl.Int64).alias("prev_growth"),
        pl.lit(None, dtype=pl.Float64).alias("turnover_yesterday"),
        pl.lit(None, dtype=pl.Float64).alias("high_60d"),
        pl.lit(None, dtype=pl.Int64).alias("zt_count_1y"),
        pl.lit(None, dtype=pl.Float64).alias("seal_success_rate_1y"),
        pl.lit(None, dtype=pl.Int64).alias("max_streak_1y"),
        pl.lit(None, dtype=pl.Utf8).alias("n_days_m_boards_label"),
        pl.lit([], dtype=pl.List(pl.Float64)).alias("intraday_sparkline"),
        pl.lit(None, dtype=pl.Utf8).alias("first_seal_time"),
        pl.lit(None, dtype=pl.Utf8).alias("last_seal_time"),
        pl.lit(None, dtype=pl.Int64).alias("open_board_count"),
        pl.lit(None, dtype=pl.Float64).alias("seal_order_amount"),
        pl.lit(None, dtype=pl.Int64).alias("streak_today"),
        pl.lit(False).alias("ever_limit_up_today"),
        pl.lit(False).alias("is_suspended"),
        pl.lit(None, dtype=pl.Int64).alias("days_since_listing"),
        pl.lit(None, dtype=pl.Utf8).alias("listing_date"),
    )

    if pool is not None:
        frame = frame.join(pool, on="code", how="left", suffix="_pool")
        for field in POOL_FIELDS:
            if f"{field}_pool" in frame.columns:
                frame = frame.with_columns(pl.coalesce([field, f"{field}_pool"]).alias(field))
        if "streak_today_pool" in frame.columns:
            frame = frame.with_columns(
                pl.coalesce(["streak_today", "streak_today_pool"]).alias("streak_today")
            )
        if "industry" in frame.columns:
            # The pool carries a sector per limit-up name; the snapshot has none.
            frame = frame.with_columns(
                pl.when(pl.col("industry").is_not_null() & (pl.col("industry") != ""))
                .then(pl.concat_list([pl.col("industry")]))
                .otherwise(pl.col("theme_tags"))
                .alias("theme_tags")
            )
        frame = frame.drop([column for column in frame.columns if column.endswith("_pool")])

    # The ladder factor adds today on top of `prev_limit`, so store yesterday's count.
    frame = frame.with_columns(
        (pl.col("streak_today").fill_null(0) - 1).clip(0).alias("prev_limit"),
        pl.col("streak_today").is_not_null().alias("ever_limit_up_today"),
    ).drop("streak_today")
    drop_columns = [column for column in ("amount_yuan", "float_cap_yuan", "industry") if column in frame.columns]
    return frame.drop(drop_columns)


def _load_pool(ak) -> tuple[pl.DataFrame | None, str]:
    try:
        pool = ak.stock_zt_pool_em(date=_today())
    except Exception:  # noqa: BLE001 - the pool is optional detail
        return None, ""
    if _is_empty(pool) or "代码" not in pool.columns:
        return None, ""
    return _pool_detail(pool), "含涨停池"


def _load_spot(ak) -> tuple[pl.DataFrame | None, str, str]:
    try:
        raw = ak.stock_zh_a_spot_em()
        if not _is_empty(raw) and all(column in raw.columns for column in EASTMONEY_SPOT):
            return _select_rename(raw, EASTMONEY_SPOT), "东方财富全市场", ""
    except Exception as error:  # noqa: BLE001 - reported through provenance
        first_error = f"东方财富快照不可用（{type(error).__name__}）"
    else:
        first_error = "东方财富快照字段不完整"
    try:
        raw = ak.stock_zh_a_spot()
        if not _is_empty(raw) and all(column in raw.columns for column in SINA_SPOT):
            return _select_rename(raw, SINA_SPOT), "新浪全市场", ""
        return None, "", f"{first_error}；新浪快照字段不完整"
    except Exception as error:  # noqa: BLE001
        return None, "", f"{first_error}；新浪快照不可用（{type(error).__name__}）"


def _today() -> str:
    return datetime.now(timezone(timedelta(hours=8))).strftime("%Y%m%d")


def _is_empty(frame) -> bool:
    """AkShare returns pandas objects; tests inject polars frames."""
    empty = getattr(frame, "empty", None)
    if isinstance(empty, bool):
        return empty
    height = getattr(frame, "height", None)
    return height == 0 if height is not None else False


def _select_rename(raw, mapping: dict[str, str]) -> pl.DataFrame:
    """Keep the mapped columns and rename them, for pandas or polars input."""
    subset = raw[list(mapping)]
    if isinstance(subset, pl.DataFrame):
        return subset.rename(mapping)
    return pl.from_pandas(subset.rename(columns=mapping))


def _limit_price(row: dict) -> float:
    previous = row.get("previous_close")
    if previous is None or not math.isfinite(previous) or previous <= 0:
        return 0.0
    return price_limit(float(previous), str(row.get("board")), bool(row.get("is_st")))


def _pool_detail(pool) -> pl.DataFrame | None:
    available = {key: value for key, value in POOL.items() if key in pool.columns}
    if "代码" not in available:
        return None
    detail = _select_rename(pool, available)
    detail = detail.with_columns(pl.col("code").cast(pl.Utf8).str.zfill(6))
    if "streak_today" in detail.columns:
        detail = detail.with_columns(pl.col("streak_today").cast(pl.Int64))
    if "seal_amount_yuan" in detail.columns:
        detail = detail.with_columns(
            (pl.col("seal_amount_yuan").cast(pl.Float64) / 1e8).alias("seal_order_amount")
        ).drop("seal_amount_yuan")
    if "open_board_count" in detail.columns:
        detail = detail.with_columns(pl.col("open_board_count").cast(pl.Int64))
    for column in ("first_seal_time", "last_seal_time"):
        if column in detail.columns:
            detail = detail.with_columns(
                pl.col(column).cast(pl.Utf8).map_elements(_clock, return_dtype=pl.Utf8).alias(column)
            )
    return detail


def _clock(text) -> str | None:
    """Eastmoney sends HHMMSS; the contract wants HH:MM:SS."""
    if text is None:
        return None
    digits = "".join(character for character in str(text) if character.isdigit())
    if len(digits) != 6:
        return str(text)
    return f"{digits[0:2]}:{digits[2:4]}:{digits[4:6]}"
