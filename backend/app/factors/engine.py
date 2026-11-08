import polars as pl
from .registry import register_factor, execute_factors

VERSION = "2.0.0"
WEIGHTS = {"momentum": .30, "liquidity": .20, "quality": .25, "trend": .25}
LABELS = {"momentum": "趋势动量", "liquidity": "量能活跃", "quality": "封板质量", "trend": "位置强度"}
EXPLANATIONS = {
    "momentum": "当日涨幅与连续上涨天数的固定权重得分",
    "liquidity": "成交额与实时量比的有界标准化得分",
    "quality": "封成比、炸板次数与历史封板率的组合",
    "trend": "当前价格相对近60日高点的位置得分",
}

@register_factor("turnover", [], ["turnover_ratio_yd"])
def turnover(frame):
    return frame.with_columns((pl.col("turnover_today") / pl.col("turnover_yesterday").clip(lower_bound=.05)).alias("turnover_ratio_yd"))

@register_factor("ladder", [], ["limit_streak", "is_limit_up"])
def ladder(frame):
    return frame.with_columns((pl.col("price") >= pl.col("limit_price")).fill_null(False).alias("is_limit_up")).with_columns(
        pl.when(pl.col("is_limit_up")).then(pl.col("prev_limit") + 1).otherwise(0).alias("limit_streak"))

@register_factor("growth", ["turnover"], ["growth_streak", "streak_cum_return"])
def growth(frame):
    compound = ((1 + pl.col("prev_cum") / 100) * (1 + pl.col("pct_chg") / 100) - 1) * 100
    tolerant = (pl.col("pct_chg") >= -.4) & (pl.col("turnover_ratio_yd") < .9)
    positive = pl.col("pct_chg") > 0
    return frame.with_columns(
        pl.when(positive).then(pl.col("prev_growth") + 1).otherwise(0).alias("growth_streak"),
        pl.when(positive).then(compound).otherwise(0.).alias("streak_cum_return"),
        pl.when(positive | tolerant).then(pl.col("prev_growth") + 1).otherwise(0).alias("tolerant_growth"),
        pl.when(positive | tolerant).then(compound).otherwise(0.).alias("tolerant_return"))

@register_factor("seal", [], ["seal_to_amount_ratio", "seal_to_cap_ratio"])
def seal(frame):
    # A sealed board has a real seal order; anything else has no meaningful ratio.
    sealed = pl.col("seal_order_amount") > 0
    return frame.with_columns(
        pl.when(sealed)
        .then(pl.col("seal_order_amount") / pl.col("amount_today").clip(lower_bound=.01) * 100)
        .otherwise(None)
        .alias("seal_to_amount_ratio"),
        pl.when(sealed)
        .then(pl.col("seal_order_amount") / pl.col("free_float_mkt_cap").clip(lower_bound=1) * 100)
        .otherwise(None)
        .alias("seal_to_cap_ratio"))

@register_factor("history", [], ["dist_to_60d_high"])
def history(frame):
    return frame.with_columns(((pl.col("price") / pl.col("high_60d") - 1) * 100).alias("dist_to_60d_high"))

@register_factor("scores", ["growth", "seal", "history"], ["composite_score"])
def scores(frame):
    frame = frame.with_columns(
        (pl.col("pct_chg") * 3 + pl.col("growth_streak") * 6 + 20).clip(0, 100).alias("score_momentum"),
        # Live snapshots rarely carry volume ratio; a missing value must not void
        # the whole composite score.
        (pl.col("amount_today").sqrt() * 10 + pl.col("volume_ratio").fill_null(0) * 15)
        .clip(0, 100)
        .alias("score_liquidity"),
        (
            pl.col("seal_to_amount_ratio").fill_null(0) * 1.5
            + pl.col("seal_success_rate_1y").fill_null(0) * .5
            - pl.col("open_board_count").fill_null(0) * 8
        ).clip(0, 100).alias("score_quality"),
        (100 + pl.col("dist_to_60d_high") * 3).clip(0, 100).alias("score_trend"))
    # A dimension without data drops out and the rest is renormalised: live
    # snapshots lack the 60-day high, so voiding every score would make ranking
    # impossible; silently treating it as 0 would be a fabricated rank.
    weighted = pl.sum_horizontal(
        [pl.col(f"score_{key}") * weight for key, weight in WEIGHTS.items()]
    )
    available = pl.sum_horizontal(
        [pl.when(pl.col(f"score_{key}").is_not_null()).then(pl.lit(weight)).otherwise(0.0)
         for key, weight in WEIGHTS.items()]
    )
    return frame.with_columns(
        pl.when(available > 0)
        .then(weighted / available)
        .otherwise(None)
        .round(2)
        .alias("composite_score")
    )

def compute(frame: pl.DataFrame) -> pl.DataFrame:
    return execute_factors(frame.lazy()).collect()
