"""战法库 v3: two factor composites from the 2026-10 factor study (docs/research/2026-10-31-factor-mining.md).

Same contract as library.py: signals are pure `(panel, params) -> [code, date, score]`, use
completed bars only (trailing windows, per-date ranks) and never `shift(-k)`.

Both trade a daily rolling book instead of a calendar rebalance. Every day the best `offer`
names by composite score are signalled; the engine fills free slots with the best names it
does not hold yet (max_new_per_day = positions / holding days) and keeps each one a fixed
number of days. A weekly or monthly rebalance with a fixed holding period drifts out of step
after holidays: positions still held on the next rebalance day make that whole cycle skip.

Composite score = mean of the per-factor percentile ranks (higher = better) among the names
that are eligible and have every factor; each factor is oriented so that a higher value is
expected to predict a higher return.

Two exclusions the study showed matter:
- ST/*ST names, recognised from prices only (`name` is today's snapshot, so filtering history
  by it would let the future decide). Until 2026-07-05 a main-board ST stock moved within 5% a
  day: at least two closes on the exact 5% limit price (at the day's high or low) in 60 bars,
  with no move above 5.25%, mark it. A low-volatility blue chip can close at +5% once, so one
  hit is not enough. From 2026-07-06 the exchanges widened that limit to 10% (visible in the
  bars: closes at +5% from ST names vanish that week); about 60 bars later no ST name can be
  told apart any more and none is excluded.
- Prices below `min_price`: sub-1-yuan names head for delisting, stop trading and lock the slot
  until the data ends. The check uses the qfq close, which sits below the price paid back then
  for names that later paid dividends, so the floor is approximate in early years.
"""
from __future__ import annotations

from datetime import date

import polars as pl

from contracts.schema import PlaybookSpec
from .library import CODE, add, c, emit, equal, liquid, param, short_exits
from .library_sentiment import float_cap_pit

ST_LIMIT_WIDENED = date(2026, 7, 6)  # main-board ST/*ST daily limit 5% -> 10% from this session
ST_WINDOW = 60  # bars in which 5%-limit closes mark a name as ST
ST_MIN_HITS = 2
PRICE_TICK = 0.011  # a close within one cent of the limit price (qfq rounding)
MIN_DAYS = 120

PCT = c("pct_chg")

# Every factor is oriented so that a higher value is expected to predict a higher return.
FACTORS: dict[str, pl.Expr] = {
    # Low-risk / low-attention (MAX effect, low volatility, low turnover).
    "low_turn_5": -(c("amount") / float_cap_pit()).rolling_mean(5).over(CODE),
    "max_20": -PCT.rolling_max(20).over(CODE),
    "lowvol_60": -PCT.rolling_std(60).over(CODE),
    # Volume dry-up after a fall (缩量超跌).
    "turn_shrink_5_60": -(c("amount").rolling_mean(5) / c("amount").rolling_mean(60)).over(CODE),
    "bias_60": -(c("close") / c("ma60") - 1),
    "rsi_low": -c("rsi_14"),
}


def st_like() -> pl.Expr:
    """10cm-board name with at least ST_MIN_HITS closes on the exact 5% limit price (at the day's
    high for up, low for down) in the trailing ST_WINDOW bars and no move above 5.25% in them."""
    up = (c("prev_close") * 1.05 * 100).round(0) / 100
    down = (c("prev_close") * 0.95 * 100).round(0) / 100
    at_limit = (((c("close") - up).abs() <= PRICE_TICK) & (c("close") >= c("high"))) | (
        ((c("close") - down).abs() <= PRICE_TICK) & (c("close") <= c("low"))
    )
    hits = at_limit.fill_null(False).cast(pl.Int32).rolling_sum(ST_WINDOW, min_samples=1).over(CODE)
    wide = (PCT.abs() > 5.25).fill_null(False).cast(pl.Int8).rolling_max(ST_WINDOW, min_samples=1).over(CODE) == 1
    return (c("board") == "10cm") & (hits >= ST_MIN_HITS) & ~wide


def not_st() -> pl.Expr:
    return ~st_like().fill_null(False)


def tradable(p: dict[str, float]) -> pl.Expr:
    return liquid(p["min_amount"]) & (c("days_listed") >= MIN_DAYS) & (c("close") >= p["min_price"]) & not_st()


def composite(names: tuple[str, ...]):
    def signal(panel: pl.LazyFrame, p: dict[str, float]) -> pl.LazyFrame:
        ok = tradable(p)
        for name in names:
            ok = ok & FACTORS[name].is_not_null() & FACTORS[name].is_finite()
        frame = panel.with_columns(ok.alias("_ok")).with_columns(
            *(pl.when(c("_ok")).then(FACTORS[name]).alias(f"_f{i}") for i, name in enumerate(names)),
            c("_ok").sum().over("date").alias("_n"),
        ).with_columns(
            pl.mean_horizontal(*(c(f"_f{i}").rank().over("date") / c("_n") for i in range(len(names)))).alias("_s"),
        ).with_columns(
            c("_s").rank("ordinal", descending=True).over("date").alias("_r"),
        )
        return emit(frame, c("_ok") & (c("_r") <= p["offer"]), c("_s"))
    return signal


def common_params(offer: float) -> list:
    return [
        param("min_amount", "最低日均成交额(亿)", 0.5, 0.1, 5, 0.1),
        param("min_price", "最低股价(元)", 2, 0, 10, 0.5),
        param("offer", "每日候选数", offer, 5, 100, 1),
    ]


EXCLUDE = (
    "剔除：20 日均成交额 < min_amount；days_listed < 120；"
    "收盘价 < min_price（按前复权价比较，早年分红多的股票当时的实际价格更高，门槛是近似）；"
    "ST：只按价格识别，不按今天的名称回溯。60 根 K 线内至少两次收在 5% 涨停价（当日最高）或 5% 跌停价（当日最低），"
    "且没有超过 5.25% 的涨跌，视为 ST。2026-07-06 起主板 ST 涨跌幅改为 10%，"
    "约 60 个交易日后价格特征消失，此后无法识别、也不剔除 ST。"
)

add(PlaybookSpec(
    id="low_risk_combo", name="低风险复合", style="volatility", horizon="long",
    summary="近 5 日换手低、近 20 日最大单日涨幅小、近 60 日波动小的股票，每天补仓，持有 20 日。",
    rule=(
        "三个因子在可交易股票中逐日排百分位：近 5 日换手率均值（成交额 / 当日流通市值）越低越好；"
        "近 20 日最大单日涨幅越小越好；近 60 日日涨跌幅标准差越小越好。得分 = 三个百分位的平均。"
        f"每天发出得分前 offer 名，空出的仓位按得分补进还没持有的股票，每天最多新开 1 只，最多 20 只；"
        f"次日开盘买入，持有 20 个交易日收盘卖出，不设价格止损（实验室里止损和移动止损都让结果变差）。{EXCLUDE}"
    ),
    source=(
        "Bali, Cakici & Whitelaw (2011) MAX 效应；Ang, Hodrick, Xing & Zhang (2006) 低波动；"
        "Liu, Stambaugh & Yuan (2019) 中国市场换手率因子。三个因子来自文献，"
        "但是在看过 2025-12 以后的样本外表现之后才决定组合检验，样本外不是完全独立的检验。"
    ),
    params=common_params(40),
    exits=short_exits(20, None), sizing=equal(20, 1)), composite(("low_turn_5", "max_20", "lowvol_60")))

add(PlaybookSpec(
    id="shrink_oversold", name="缩量超跌", style="reversal", horizon="swing",
    summary="近 5 日成交额相对 60 日萎缩、价格低于 60 日线较多、RSI 低的股票，每天补仓，持有 10 日。",
    rule=(
        "三个因子在可交易股票中逐日排百分位：近 5 日均成交额 / 近 60 日均成交额越低越好；"
        "收盘价相对 60 日均线的偏离越负越好；RSI(14) 越低越好。得分 = 三个百分位的平均。"
        f"每天发出得分前 offer 名，空出的仓位按得分补进还没持有的股票，每天最多新开 2 只，最多 20 只；"
        f"次日开盘买入，持有 10 个交易日收盘卖出，不设价格止损。{EXCLUDE}"
    ),
    source=(
        "短期反转（Jegadeesh, 1990）；缩量止跌为社区经验（未经学术验证）。三个因子都满足样本内规则"
        "（IC > 0、Newey-West t ≥ 2、前 20% 超额 ≥ 0.05%/5 日），但是从 12 个达标因子里挑选时已看过样本外结果。"
    ),
    params=common_params(40),
    exits=short_exits(10, None), sizing=equal(20, 2)), composite(("turn_shrink_5_60", "bias_60", "rsi_low")))
