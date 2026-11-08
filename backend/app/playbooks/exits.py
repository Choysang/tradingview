"""Exit lab: for each playbook, which way of buying and selling works, decided without hindsight.

Picking a stock is half the trade; when to buy and sell decides what is kept.
Each playbook is re-run with its default rules plus one practitioner rule at a
time (and one combination): a sell rule, or a buy rule that refuses to chase a
gap or rests an order below the signal close. Only the in-sample period is
used to choose. The variant with the best in-sample Sharpe is chosen; its
out-of-sample result against the default's is the honest test of whether that
rule helps. A playbook only switches from its default when the in-sample gain
is clear (MIN_GAIN), so noise does not pick the rule.
"""
from __future__ import annotations

import threading
import time

from contracts.schema import (
    EntryRules, ExitLabReport, ExitRules, ExitVariantResult, PerformanceStats, PlaybookExitStudy,
)
from .backtest import simulate
from .base import Playbook, catalog, resolve
from .evaluate import (
    SPLIT_FRACTION, Context, _requires_history_missing, context, dataset_provenance, ebb_days, run_signals, stats,
)

MIN_GAIN = 0.2  # in-sample Sharpe a variant must add over the default to be chosen
MIN_OOS_GAIN = 0.1  # out-of-sample Sharpe it must add to count as improved (a coin flip passes "> 0" half the time)
MIN_OOS_TRADES = 20

# (key, label, rule changes); "default" first. Short plays get open/limit/fade rules,
# swing and long plays get trend-following ones; both may leave on a sentiment ebb.
# An "entry" key changes the buy instead of the sell.
ENTRY_VARIANTS: tuple[tuple[str, str, dict], ...] = (
    ("no_chase", "不追高（高开 > 3% 放弃）", {"entry": {"max_gap_pct": 3.0}}),
    ("pullback", "回踩 2% 挂单买", {"entry": {"pullback_pct": 2.0}}),
)
# Holding-period trading on the base position (做T): a resting order on one side, the close on the other.
T_VARIANTS: tuple[tuple[str, str, dict], ...] = (
    ("t_high", "倒T：冲高 3% 卖一半、收盘买回", {"t_high_pct": 3.0}),
    ("t_low", "正T：下探 2% 买一半、收盘卖出", {"t_low_pct": 2.0}),
    ("t_both", "双向T：±3% 高抛低吸", {"t_high_pct": 3.0, "t_low_pct": 3.0}),
)
# Stop width (Lo & Remorov 2017: tight stops lose mostly to costs).
STOP_VARIANTS: tuple[tuple[str, str, dict], ...] = (
    ("stop_tight", "紧止损 5%", {"stop_loss_pct": 5.0}),
    ("stop_wide", "宽止损 15%", {"stop_loss_pct": 15.0}),
)
SHORT_VARIANTS: tuple[tuple[str, str, dict], ...] = (
    ("default", "默认卖法", {}),
    ("weak_open", "次日不及预期（低开 2%）", {"weak_open_pct": 2.0}),
    ("not_limit", "断板卖", {"sell_if_not_limit": True}),
    ("fade", "冲高回落卖", {"sell_on_fade": True}),
    ("ebb", "情绪退潮卖", {"sell_on_ebb": True}),
    ("combo_short", "低开 2% + 冲高回落 + 退潮", {"weak_open_pct": 2.0, "sell_on_fade": True, "sell_on_ebb": True}),
    ("broken", "炸板卖", {"sell_on_broken": True}),
    ("shadow", "高位放量长上影卖", {"sell_on_shadow": True}),
    ("scale_half", "涨 5% 先卖一半", {"scale_out_pct": 5.0}),
    *STOP_VARIANTS,
    *T_VARIANTS,
    *ENTRY_VARIANTS,
)
TREND_VARIANTS: tuple[tuple[str, str, dict], ...] = (
    ("default", "默认卖法", {}),
    ("below_ma5", "跌破 5 日线卖", {"sell_below_ma5": True}),
    ("stall", "放量滞涨卖", {"sell_on_stall": True}),
    ("chandelier", "吊灯止损（3×ATR）", {"chandelier_atr": 3.0}),
    ("ebb", "情绪退潮卖", {"sell_on_ebb": True}),
    ("combo_trend", "吊灯止损 + 放量滞涨", {"chandelier_atr": 3.0, "sell_on_stall": True}),
    ("chandelier_tight", "紧吊灯止损（2×ATR）", {"chandelier_atr": 2.0}),
    ("chandelier_wide", "宽吊灯止损（5×ATR）", {"chandelier_atr": 5.0}),
    ("bias", "乖离过大卖（高于 20 日线 15%）", {"bias_exit_pct": 15.0}),
    ("shadow", "高位放量长上影卖", {"sell_on_shadow": True}),
    ("scale_half", "涨 10% 先卖一半", {"scale_out_pct": 10.0}),
    *STOP_VARIANTS,
    *T_VARIANTS,
    *ENTRY_VARIANTS,
)

_LOCK = threading.RLock()
_STUDIES: dict[tuple[str, str], PlaybookExitStudy] = {}
_REPORT: tuple[str, ExitLabReport] | None = None


def variants_for(pb: Playbook) -> tuple[tuple[str, str, dict], ...]:
    if pb.spec.horizon == "short":
        # 断板卖 and 炸板卖 only make sense for plays that bought a limit-up streak.
        return tuple(v for v in SHORT_VARIANTS if v[0] not in ("not_limit", "broken") or pb.spec.style == "sentiment")
    return TREND_VARIANTS


def entry_text(entry: EntryRules, close: float | None = None) -> str:
    """The buy instruction in plain Chinese; with the signal-day close it names the price."""
    def price(pct: float) -> str:
        return f"（{close * (1 + pct / 100):.2f} 元）" if close else ""

    if entry.pullback_pct:
        return (
            f"次日在信号日收盘价下方 {entry.pullback_pct:g}%{price(-entry.pullback_pct)}挂单，"
            "盘中跌到才买；没跌到就放弃。"
        )
    if entry.max_gap_pct is not None:
        return f"次日开盘买；高开超过 {entry.max_gap_pct:g}%{price(entry.max_gap_pct)}就放弃，不追高。"
    return "次日开盘买；一字涨停买不到就放弃。"


def plan_text(exits: ExitRules) -> list[str]:
    """The sell plan in plain Chinese, in the order a trader checks it."""
    lines = []
    if exits.weak_open_pct:
        lines.append(f"买入次日开盘若低于买入价 {exits.weak_open_pct:g}%，开盘即卖（不及预期）。")
    if exits.stop_loss_pct:
        lines.append(f"盘中跌破买入价 {exits.stop_loss_pct:g}% 止损。")
    if exits.take_profit_pct:
        lines.append(f"盘中涨到买入价 +{exits.take_profit_pct:g}% 止盈。")
    if exits.scale_out_pct:
        lines.append(f"盘中涨到买入价 +{exits.scale_out_pct:g}% 先卖一半，余下的继续按下面的规则卖（分批止盈）。")
    if exits.t_high_pct or exits.t_low_pct:
        share = f"{exits.t_share * 10:g} 成"
        legs = []
        if exits.t_high_pct:
            legs.append(f"冲高到昨收 +{exits.t_high_pct:g}% 卖出 {share}底仓、收盘买回")
        if exits.t_low_pct:
            legs.append(f"下探到昨收 −{exits.t_low_pct:g}% 买入 {share}、收盘卖出同样数量")
        lines.append("持有期间做T：" + "；".join(legs) + "。")
    if exits.sell_if_not_limit:
        lines.append("收盘没有封住涨停，次日开盘卖（断板就走）。")
    if exits.sell_on_broken:
        lines.append("盘中触及涨停但收盘没封住，次日开盘卖（炸板就走）。")
    if exits.sell_on_fade:
        lines.append("盘中冲高 7% 以上、收盘涨幅不足 3%，次日开盘卖（冲高回落）。")
    if exits.sell_below_ma5:
        lines.append("收盘跌破 5 日线，次日开盘卖。")
    if exits.sell_on_stall:
        lines.append("成交量超过 5 日均量 2 倍而涨幅不足 2%，次日开盘卖（放量滞涨）。")
    if exits.sell_on_shadow:
        lines.append("持仓盈利时出现放量长上影（上影线超过当日振幅一半、量超过 5 日均量 2 倍），次日开盘卖。")
    if exits.bias_exit_pct:
        lines.append(f"收盘高于 20 日线 {exits.bias_exit_pct:g}% 以上（乖离过大），次日开盘卖。")
    if exits.chandelier_atr:
        lines.append(f"收盘低于持仓以来最高收盘 − {exits.chandelier_atr:g}×ATR14，次日开盘卖（吊灯止损）。")
    if exits.trailing_stop_pct:
        lines.append(f"收盘从持仓最高收盘回撤 {exits.trailing_stop_pct:g}%，次日开盘卖（移动止损）。")
    if exits.sell_on_ebb:
        lines.append("市场情绪转入退潮，次日开盘卖。")
    lines.append(f"以上都没触发，持有 {exits.holding_days} 个交易日后收盘卖。")
    return lines


def _sharpe(s: PerformanceStats) -> float:
    return s.sharpe if s.sharpe is not None else float("-inf")


def study(pb: Playbook, ctx: Context) -> PlaybookExitStudy:
    key = (ctx.version, pb.spec.id)
    with _LOCK:
        cached = _STUDIES.get(key)
    if cached is not None:
        return cached
    signals = run_signals(pb, ctx)
    ebb = ebb_days(ctx)
    split_idx = max(1, int(len(ctx.dates) * SPLIT_FRACTION))
    split_date = ctx.dates[split_idx - 1]
    oos_start = ctx.dates[split_idx] if split_idx < len(ctx.dates) else None
    results = []
    for vkey, label, changes in variants_for(pb):
        exits = pb.spec.exits.model_copy(update={k: v for k, v in changes.items() if k != "entry"})
        entry = pb.spec.entry.model_copy(update=changes.get("entry", {}))
        in_sample = stats(*simulate(
            ctx.panel, signals, exits, pb.spec.sizing, ctx.benchmark, end=split_date, ebb_days=ebb, entry=entry,
        ))
        out_of_sample = (
            stats(*simulate(
                ctx.panel, signals, exits, pb.spec.sizing, ctx.benchmark, start=oos_start, ebb_days=ebb, entry=entry,
            ))
            if oos_start is not None else PerformanceStats()
        )
        results.append(ExitVariantResult(
            key=vkey, label=label, exits=exits, entry=entry, in_sample=in_sample, out_of_sample=out_of_sample,
        ))
    default = results[0]
    best = max(results[1:], key=lambda r: _sharpe(r.in_sample), default=default)
    chosen = (
        best if best.in_sample.sharpe is not None and _sharpe(best.in_sample) >= _sharpe(default.in_sample) + MIN_GAIN
        else default
    )
    improved = None if chosen is default else (
        chosen.out_of_sample.trades >= MIN_OOS_TRADES
        and _sharpe(chosen.out_of_sample) >= _sharpe(default.out_of_sample) + MIN_OOS_GAIN
    )
    result = PlaybookExitStudy(
        playbook_id=pb.spec.id, name=pb.spec.name, horizon=pb.spec.horizon, variants=results,
        chosen_key=chosen.key, improved_out_of_sample=improved, plan=plan_text(chosen.exits),
        entry_note=entry_text(chosen.entry),
    )
    with _LOCK:
        _STUDIES[key] = result
    return result


def testable(pb: Playbook, ctx: Context) -> bool:
    return pb.spec.available and not (pb.requires_history and _requires_history_missing(ctx.panel, pb.requires_history))


def playbook_study(playbook_id: str) -> PlaybookExitStudy:
    pb = resolve(playbook_id)
    if pb is None:
        raise KeyError(playbook_id)
    ctx = context()
    if not testable(pb, ctx):
        raise ValueError(f"{pb.spec.name} 无法回测，没有卖法对比")
    return study(pb, ctx)


def report(force: bool = False) -> ExitLabReport:
    global _REPORT
    started = time.perf_counter()
    ctx = context()
    with _LOCK:
        if not force and _REPORT is not None and _REPORT[0] == ctx.version:
            return _REPORT[1]
    studies = [study(pb, ctx) for pb in catalog() if testable(pb, ctx)]
    switched = [s for s in studies if s.chosen_key != "default"]
    improved = sum(bool(s.improved_out_of_sample) for s in switched)
    summary = (
        f"{len(studies)} 套战法里，{len(switched)} 套在样本内找到比默认更好的买卖法（夏普至少多 {MIN_GAIN:g}），"
        f"其中 {improved} 套在样本外也更好（至少 {MIN_OOS_TRADES} 笔、夏普多 {MIN_OOS_GAIN:g}）。"
        if switched else f"{len(studies)} 套战法在样本内都没有找到明显好于默认的买卖法。"
    )
    result = ExitLabReport(
        provenance=dataset_provenance(ctx), studies=studies, compared=len(switched), improved=improved,
        summary=summary,
        notes=[
            f"只用前 {SPLIT_FRACTION:.0%} 的日期选买卖法，后 {1 - SPLIT_FRACTION:.0%} 检验；样本外的结果不参与选择。"
            f"样本外至少 {MIN_OOS_TRADES} 笔、夏普至少多 {MIN_OOS_GAIN:g} 才算更好。",
            "每次只在默认规则上改一条（另有一个卖法组合），信号、仓位和成本都不变。",
            "买法：不追高（次日高开超过 3% 放弃）、回踩买（信号日收盘下方 2% 挂单，触及才成交）；放弃的名额留给下一只。",
            "收盘判定的规则次日开盘卖；“次日不及预期”在买入次日开盘直接卖；跌停封死时顺延。",
            "短线战法比较开盘、断板、冲高回落、退潮类卖法；波段与长线比较均线、滞涨、吊灯止损、退潮类卖法。",
        ],
        execution_ms=round((time.perf_counter() - started) * 1000, 2),
    )
    with _LOCK:
        _REPORT = (ctx.version, result)
    return result


def live_rules(pb: Playbook, ctx: Context) -> tuple[ExitRules, EntryRules]:
    """The lab's choice when it also held out of sample, else the playbook's defaults."""
    result = study(pb, ctx)
    if result.chosen_key == "default" or not result.improved_out_of_sample:
        return pb.spec.exits, pb.spec.entry
    chosen = next(v for v in result.variants if v.key == result.chosen_key)
    return chosen.exits, chosen.entry


def chosen_exits(playbook_id: str, ctx: Context) -> ExitRules | None:
    """The chosen exits when a study is already cached and it switched away from the default."""
    with _LOCK:
        cached = _STUDIES.get((ctx.version, playbook_id))
    if cached is None or cached.chosen_key == "default":
        return None
    return next(v.exits for v in cached.variants if v.key == cached.chosen_key)


def reset_caches() -> None:
    global _REPORT
    with _LOCK:
        _STUDIES.clear()
        _REPORT = None
