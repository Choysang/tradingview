"""战法库 v4: technical analysis from the 2026-11 study (docs/research/2026-11-01-technical-bear.md).

K 线 (candlestick) bottom patterns, MACD divergence and below-zero crosses, KDJ / Bollinger
oversold turns, weekly and monthly MACD / moving-average crosses, and daily-weekly-monthly
resonance. Same contract as library.py: pure `(panel, params) -> [code, date, score]`, completed
bars only, no `shift(-k)`.

Weekly and monthly bars are rebuilt from daily closes. A week's (month's) values are used from the
first trading day of the next week (month): on its own last day nobody can be sure it is the last
day without a calendar, and a panel cut mid-period must give the same answer as the full one.
Scores are the strength of each pattern (how long the shadow, how deep the fall, how liquid the
name), so same-day candidates rank the same way every run.

The study found these bottom signals lost money through the 2026-09/10 decline; they stay in the
library so the board, the lab and the bear-market rule can keep measuring them.
"""
from __future__ import annotations

import polars as pl

from contracts.schema import PlaybookSpec
from .library import CODE, COMMUNITY, add, c, emit, equal, param, short_exits
from .library_factor import tradable

PARAMS = [
    param("min_amount", "最低日均成交额(亿)", 0.5, 0.1, 5, 0.1),
    param("min_price", "最低股价(元)", 2, 0, 10, 0.5),
]
EXCLUDE = "股票池：20 日均成交额 ≥ min_amount、上市 ≥ 120 根 K 线、股价 ≥ min_price，按价格特征剔除 ST（同低风险复合）。"
STUDY = "2026-11 检验见 docs/research/2026-11-01-technical-bear.md：2026-09、10 两个下跌月都亏钱。"


# --- building blocks -------------------------------------------------------------------


def _helpers(panel: pl.LazyFrame) -> pl.LazyFrame:
    """Per-stock helper columns, computed first so no window expression nests inside another."""
    cl, o, dif = c("close"), c("open"), c("macd_dif")
    return panel.with_columns(
        ((cl / cl.shift(5).over(CODE) - 1) <= -0.05).alias("_down5"),
        ((cl / cl.shift(10).over(CODE) - 1) <= -0.08).alias("_down10"),
        (cl <= cl.rolling_min(20).over(CODE) * 1.03).alias("_near_low"),
        o.shift(1).over(CODE).alias("_o1"), cl.shift(1).over(CODE).alias("_c1"),
        o.shift(2).over(CODE).alias("_o2"), cl.shift(2).over(CODE).alias("_c2"),
        cl.shift(3).over(CODE).alias("_c3"), c("prev_close").shift(2).over(CODE).alias("_pc2"),
        ((dif > c("macd_dea")) & (dif.shift(1).over(CODE) <= c("macd_dea").shift(1).over(CODE))).alias("_golden"),
        c("macd_hist").shift(1).over(CODE).alias("_hist1"), c("kdj_j").shift(1).over(CODE).alias("_j1"),
        cl.shift(10).rolling_min(30).over(CODE).alias("_cmin"), dif.shift(10).rolling_min(30).over(CODE).alias("_dmin"),
        c("amount").rolling_mean(20).over(CODE).alias("_amt20"),
    ).with_columns(
        c("_down5").shift(1).over(CODE).alias("_down5_1"), c("_down10").shift(3).over(CODE).alias("_down10_3"),
    )


def _candle() -> dict[str, pl.Expr]:
    o, h, l, cl = c("open"), c("high"), c("low"), c("close")
    return {
        "body": (cl - o).abs(), "range": h - l, "upper": h - pl.max_horizontal("open", "close"),
        "lower": pl.min_horizontal("open", "close") - l, "green": cl > o,
    }


def period_lines(panel: pl.LazyFrame, every: str, prefix: str, min_periods: int) -> pl.LazyFrame:
    """(code, date) -> the latest completed week ("1w") or month ("1mo") on that day.

    Columns `<prefix>_up` (DIF > DEA), `<prefix>_golden` (DIF crossed above DEA in that period),
    `<prefix>_ma_golden` (MA5 crossed above MA10) and `<prefix>_fresh` (the stock's first bar on which
    that period is known: the next period's first trading day, or its next bar after a suspension).
    MACD is 12/26/9 on period closes, seeded with the first close in the warehouse; a period counts
    once the stock has at least `min_periods` of them."""
    daily = panel.select(CODE, "date", "close")
    starts = (daily.select("date").unique().with_columns(c("date").dt.truncate(every).alias("p"))
              .group_by("p").agg(c("date").min().alias("first")).sort("p")
              .with_columns(c("p").shift(1).alias("done")))  # on `first`, the period `done` is complete
    known = starts.select(c("done").alias("p"), c("first").alias("avail")).drop_nulls()
    per = (daily.sort(CODE, "date").with_columns(c("date").dt.truncate(every).alias("p"))
           .group_by(CODE, "p", maintain_order=True).agg(c("close").last().alias("pc")).sort(CODE, "p")
           .with_columns(
               (c("pc").ewm_mean(span=12, adjust=False) - c("pc").ewm_mean(span=26, adjust=False)).over(CODE).alias("dif"),
               c("pc").rolling_mean(5).over(CODE).alias("ma5"), c("pc").rolling_mean(10).over(CODE).alias("ma10"),
               pl.int_range(pl.len()).over(CODE).alias("n"),
           ).with_columns(c("dif").ewm_mean(span=9, adjust=False).over(CODE).alias("dea"))
           .with_columns(
               (c("dif") > c("dea")).alias(f"{prefix}_up"),
               ((c("dif") > c("dea")) & (c("dif").shift(1).over(CODE) <= c("dea").shift(1).over(CODE))).alias(f"{prefix}_golden"),
               ((c("ma5") > c("ma10")) & (c("ma5").shift(1).over(CODE) <= c("ma10").shift(1).over(CODE))).alias(f"{prefix}_ma_golden"),
           ))
    right = (per.join(known, on="p").filter(c("n") >= min_periods - 1)  # n counts from 0
             .select(CODE, c("avail").alias("date"), c("avail").alias("_avail"), f"{prefix}_up", f"{prefix}_golden",
                     f"{prefix}_ma_golden").sort("date"))
    # Both sides are sorted by date just above; Polars cannot verify that itself when `by` is given.
    out = (daily.select(CODE, "date").sort("date").join_asof(right, on="date", by=CODE, strategy="backward",
                                                             check_sortedness=False).sort(CODE, "date"))
    first_seen = c("_avail").is_not_null() & (c("_avail") != c("_avail").shift(1).over(CODE)).fill_null(True)
    return out.with_columns(first_seen.alias(f"{prefix}_fresh")).drop("_avail")


def _signal(hit, score):
    def signal(panel: pl.LazyFrame, p: dict[str, float]) -> pl.LazyFrame:
        frame = _helpers(panel)
        return emit(frame, tradable(p) & hit(), score())
    return signal


def _timeframe_signal(hit, needs: tuple[str, ...]):
    def signal(panel: pl.LazyFrame, p: dict[str, float]) -> pl.LazyFrame:
        frame = _helpers(panel)
        if "w" in needs:
            frame = frame.join(period_lines(panel, "1w", "w", 35), on=[CODE, "date"], how="left")
        if "m" in needs:
            frame = frame.join(period_lines(panel, "1mo", "m", 12), on=[CODE, "date"], how="left")
        # A join does not keep row order, and tradable() has rolling windows: restore (code, date) order first.
        return emit(frame.sort(CODE, "date"), tradable(p) & hit(), c("_amt20"))
    return signal


def _tech(spec_id: str, name: str, style: str, horizon: str, summary: str, rule: str, hold: int, per_day: int, fn,
          source: str = COMMUNITY) -> None:
    add(PlaybookSpec(
        id=spec_id, name=name, style=style, horizon=horizon, summary=summary,  # type: ignore[arg-type]
        rule=f"{rule}次日开盘买入，持有 {hold} 个交易日收盘卖出，不设价格止损；最多 20 只，每天最多新开 {per_day} 只。{EXCLUDE}{STUDY}",
        source=source, params=PARAMS, exits=short_exits(hold, None), sizing=equal(20, per_day)), fn)


K = _candle()
NISON = "K 线形态按 Steve Nison《日本蜡烛图技术》（1991）的常见定义量化为日线规则；阈值为社区经验，未经学术验证"
APPEL = "MACD 由 Gerald Appel 提出（1970 年代）；背离、零轴、多周期用法为社区经验，未经学术验证"

# --- K 线形态 -----------------------------------------------------------------------------

_tech("k_hammer", "锤子线", "reversal", "short", "下跌后出现长下影、小实体的 K 线，视为止跌。",
      "近 5 日跌幅 ≥ 5%，收盘在 20 日最低收盘价的 3% 以内；下影线 ≥ 2 倍实体，上影线不超过实体（容差为收盘价的 0.1%，十字锤也算）。得分 = 下影线 / max(实体, 收盘价的 0.1%)。",
      5, 4, _signal(lambda: (K["range"] > 0) & (K["lower"] >= 2 * K["body"]) & (K["upper"] <= K["body"] + 0.001 * c("close"))
                    & c("_down5") & c("_near_low"),
                    lambda: K["lower"] / pl.max_horizontal(K["body"], 0.001 * c("close"))), NISON)
_tech("k_engulfing", "阳包阴", "reversal", "short", "下跌后一根阳线完全包住前一天的阴线（看涨吞没）。",
      "昨日阴线，之前 5 日跌幅 ≥ 5%；今日阳线，开盘 ≤ 昨日收盘、收盘 ≥ 昨日开盘。得分 = 今日实体 / 昨日实体。",
      5, 4, _signal(lambda: (c("_c1") < c("_o1")) & K["green"] & (c("open") <= c("_c1")) & (c("close") >= c("_o1")) & c("_down5_1"),
                    lambda: K["body"] / pl.max_horizontal(c("_o1") - c("_c1"), 0.001 * c("close"))), NISON)
_tech("k_morning_star", "早晨之星", "reversal", "short", "大跌、小实体、阳线收复一半，三根 K 线组成的底部形态。",
      "近 10 日跌幅 ≥ 8%；前天跌 ≥ 3%，昨天实体不超过前天实体的 30%，今天阳线收盘高于前天实体中点。得分 = 收盘高出中点的幅度（%）+ 1。",
      5, 4, _signal(lambda: (c("_c2") / c("_pc2") - 1 <= -0.03) & ((c("_c1") - c("_o1")).abs() <= (c("_c2") - c("_o2")).abs() * 0.3)
                    & K["green"] & (c("close") >= (c("_o2") + c("_c2")) / 2) & c("_down10"),
                    lambda: (c("close") / ((c("_o2") + c("_c2")) / 2) - 1) * 100 + 1), NISON)
_tech("k_three_soldiers", "红三兵", "reversal", "short", "下跌后连续三根逐步抬高的阳线。",
      "三天前之前的 10 日跌幅 ≥ 8%；连续三天阳线，收盘逐日抬高，开盘逐日不低于前一天开盘，今日涨幅 < 6%。得分 = 三日涨幅（%）。",
      5, 4, _signal(lambda: K["green"] & (c("_c1") > c("_o1")) & (c("_c2") > c("_o2")) & (c("close") > c("_c1")) & (c("_c1") > c("_c2"))
                    & (c("open") >= c("_o1")) & (c("_o1") >= c("_o2")) & (c("pct_chg").abs() < 6) & c("_down10_3"),
                    lambda: (c("close") / c("_c3") - 1) * 100), NISON)
_tech("k_doji_low", "低位十字星", "reversal", "short", "下跌到低位后出现实体很小的十字星。",
      "近 5 日跌幅 ≥ 5%，收盘在 20 日最低收盘价的 3% 以内；振幅 > 3%，实体 ≤ 振幅的 10%。得分 = 近 5 日跌幅（%）。",
      5, 4, _signal(lambda: (K["range"] > 0.03 * c("prev_close")) & (K["body"] <= 0.1 * K["range"]) & c("_near_low") & c("_down5"),
                    lambda: -(c("close") / c("close").shift(5).over(CODE) - 1) * 100), NISON)
_tech("k_lower_shadow_volume", "低位放量长下影", "reversal", "short", "低位放量、下影线占振幅六成以上，视为承接。",
      "收盘在 20 日最低收盘价的 3% 以内；下影线 ≥ 振幅的 60%，振幅 > 4%，成交量 ≥ 5 日均量的 1.5 倍。得分 = 下影占比 × 量比。",
      5, 4, _signal(lambda: (K["lower"] >= 0.6 * K["range"]) & (K["range"] > 0.04 * c("prev_close"))
                    & (c("volume") >= 1.5 * c("vol_ma5")) & c("_near_low"),
                    lambda: K["lower"] / K["range"] * c("volume") / c("vol_ma5")), NISON)

# --- MACD / KDJ / 布林 ----------------------------------------------------------------------

_tech("macd_bottom_divergence", "MACD 底背离", "reversal", "swing", "价格创新低而 DIF 没有创新低，且 MACD 柱回升。",
      "收盘低于 10–40 日前这段时间的最低收盘；DIF 高于同一段时间的最低 DIF，且 DIF < 0；今日 MACD 柱高于昨日。得分 = DIF 相对那段最低 DIF 的回升比例。",
      10, 2, _signal(lambda: (c("close") < c("_cmin")) & (c("macd_dif") > c("_dmin")) & (c("macd_dif") < 0) & (c("macd_hist") > c("_hist1")),
                     lambda: (c("macd_dif") - c("_dmin")) / c("_dmin").abs()), APPEL)
_tech("macd_golden_below_zero", "MACD 零轴下金叉", "reversal", "swing", "DIF 在零轴下方上穿 DEA。",
      "今日 DIF 上穿 DEA，且 DIF < 0。得分 = −DIF / 收盘 × 100（离零轴越远越高）。",
      10, 2, _signal(lambda: c("_golden") & (c("macd_dif") < 0), lambda: -c("macd_dif") / c("close") * 100), APPEL)
_tech("kdj_j_oversold_turn", "KDJ J 值超卖拐头", "reversal", "short", "J 值跌到 0 以下后拐头向上。",
      "J < 0 且高于昨日 J；收盘在 20 日最低收盘价的 3% 以内。得分 = −J。",
      5, 4, _signal(lambda: (c("kdj_j") < 0) & (c("kdj_j") > c("_j1")) & c("_near_low"), lambda: -c("kdj_j")))
_tech("boll_lower_rsi6", "下轨超卖下影", "reversal", "short", "跌破布林下轨、RSI6 低于 20，并收出下影线。",
      "收盘 < 布林下轨，RSI(6) < 20，下影线长于实体。得分 = 收盘低于下轨的幅度（%）。",
      5, 4, _signal(lambda: (c("close") < c("boll_lower")) & (c("rsi_6") < 20) & (K["lower"] > K["body"]),
                    lambda: (c("boll_lower") / c("close") - 1) * 100))

# --- 周线、月线与多周期 -----------------------------------------------------------------------

_tech("w_macd_golden", "周线 MACD 金叉", "trend", "swing", "周线 DIF 上穿 DEA，且日线站上 20 日线。",
      "上一根已完成的周线上 DIF 上穿 DEA（周线至少 35 根），在下一周的第一个交易日、收盘高于 20 日均线时发出。得分 = 20 日均成交额。",
      10, 2, _timeframe_signal(lambda: c("w_fresh") & c("w_golden").fill_null(False) & (c("close") > c("ma20")), ("w",)), APPEL)
_tech("m_macd_golden", "月线 MACD 金叉", "trend", "long", "月线 DIF 上穿 DEA。",
      "上一根已完成的月线上 DIF 上穿 DEA（月线至少 12 根），在下个月第一个交易日发出。月线 MACD 以仓库里第一根月线收盘为种子，仓库只有约 3 年，数值仍明显受起点影响，与看盘软件不完全一致，补入更早的历史后结果会变。得分 = 20 日均成交额。",
      20, 1, _timeframe_signal(lambda: c("m_fresh") & c("m_golden").fill_null(False), ("m",)), APPEL)
_tech("m_ma_golden", "月线均线金叉", "trend", "long", "月线 MA5 上穿 MA10。",
      "上一根已完成的月线上 MA5 上穿 MA10（月线至少 12 根），在下个月第一个交易日发出。得分 = 20 日均成交额。",
      20, 1, _timeframe_signal(lambda: c("m_fresh") & c("m_ma_golden").fill_null(False), ("m",)))
_tech("mtf_resonance", "日周月共振", "trend", "swing", "日线 MACD 金叉，同时周线、月线 MACD 都在多头。",
      "今日日线 DIF 上穿 DEA；最近一根已完成的周线和月线都是 DIF > DEA。得分 = 20 日均成交额。",
      10, 2, _timeframe_signal(lambda: c("_golden") & c("w_up").fill_null(False) & c("m_up").fill_null(False), ("w", "m")), APPEL)

TECH_IDS = (
    "k_hammer", "k_engulfing", "k_morning_star", "k_three_soldiers", "k_doji_low", "k_lower_shadow_volume",
    "macd_bottom_divergence", "macd_golden_below_zero", "kdj_j_oversold_turn", "boll_lower_rsi6",
    "w_macd_golden", "m_macd_golden", "m_ma_golden", "mtf_resonance",
)
