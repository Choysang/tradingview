"""封板质量补充因子：首末封板间隔与早封标记。

放在 `plugins/` 下的文件会被 `registry.discover_plugins()` 自动加载，无需改动核心管线。
因子对缺列快照保持幂等：没有封板时间就输出空值，不让整条管线失败。
"""
from __future__ import annotations

import polars as pl

try:  # Works whether the package root is the repo or backend/.
    from backend.app.factors.registry import register_factor
except ImportError:  # pragma: no cover - uvicorn started from backend/
    from app.factors.registry import register_factor

EARLY_SEAL = pl.lit("09:35:00").str.to_time(format="%H:%M:%S")


@register_factor("seal_span", ["seal"], ["seal_span_minutes", "early_seal"])
def seal_span(frame: pl.LazyFrame) -> pl.LazyFrame:
    """How long the board stayed closed, and whether it closed before 09:35."""
    if not {"first_seal_time", "last_seal_time"} <= set(frame.collect_schema().names()):
        return frame.with_columns(
            pl.lit(None, dtype=pl.Float64).alias("seal_span_minutes"),
            pl.lit(False).alias("early_seal"),
        )
    first = pl.col("first_seal_time").str.to_time(format="%H:%M:%S", strict=False)
    last = pl.col("last_seal_time").str.to_time(format="%H:%M:%S", strict=False)
    return frame.with_columns(
        pl.when(first.is_not_null() & last.is_not_null())
        .then((last - first).dt.total_minutes().round(1))
        .otherwise(None)
        .alias("seal_span_minutes"),
        pl.when(first.is_not_null()).then(first <= EARLY_SEAL).otherwise(False).alias("early_seal"),
    )
