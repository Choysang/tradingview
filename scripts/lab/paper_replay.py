"""Paper replay of one recorded day: B1 / B3 buys and S1 / S3 / M3 sells, nothing traded (backend venv).

    python scripts/lab/paper_replay.py 2026-11-02|today [.work/intraday] [calendar events.jsonl]

`today` is the CST date; a day without a finished recording (no summary.json ending "done") is skipped with
exit 0, so the server can run it on a weekday schedule.

Inputs: the recorded day (`<root>/<day>/quotes.parquet`, `hot.parquet`, through `intraday_view`, so every
decision reads only rows with known_time <= its clock), the local warehouse's daily bars (the previous session's
limit-ups, and volume x close as its amount, since the bars carry no amount), the 申万 map (`.work/lab/main/sw_industry.parquet`, peers and sector moves for M3).

Buys (signal -> order -> three queue scenarios, every fired intent kept as an OpportunityRecord):
* B1 on the previous session's main-board limit-ups at the 09:25 final auction; the order meets the first
  continuous snapshot: below the limit it fills at that snapshot's ask1, at the limit it queues.
* B3 on every main-board name of the day that sealed, opened >= 30 s and sealed again before 14:30; the order
  queues at the limit from the reseal's known time.
Sells, on a hypothetical book: a block (about 10,000 yuan) of each previous-session limit-up, bought at that
close, so sellable today under T+1 (`inventory`). Each holding runs every sell rule side by side:
S0 open (sell at the first continuous snapshot), S1 seal decay, S3 under VWAP 90 s, M3 exit, Sbreak (开板即卖) in
three versions: at the first break, or at the first break M3-Tape / M3-Strict does not call benign, and HOLD. A sell is paper-filled at the bid of the row that fired it (at the limit for S1).
Today's B1 / B3 buys sit in acquired_today, so a sell rule firing on them is booked NOT_SELLABLE.
M3 inputs: peers = other names of the same 申万 level-2 industry sealed in the latest full snapshot (any
position, not only 低位: positions are not modelled yet); sector dump = the industry's median move since the
previous full snapshot <= SECTOR_DUMP_PCT; catalyst = calendar events linked by `event_links` (None when none).
Outputs: `.work/lab/main/paper/<day>/opportunities.parquet`, `sells.parquet`, `summary.md`.
Returns are D0 only (fill -> close); the overnight and next-day legs need the next recorded day.
"""
from __future__ import annotations

import json
import sys
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import polars as pl

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from backend.app.domain import board_rules as R  # noqa: E402
from backend.app.domain import event_links as L  # noqa: E402
from backend.app.domain import inventory as INV  # noqa: E402
from backend.app.domain import market_state as M  # noqa: E402
from backend.app.domain import queue_fill as Q  # noqa: E402
from backend.app.domain import triggers as T  # noqa: E402
from backend.app.warehouse import calendar_events as C  # noqa: E402
from backend.app.warehouse import intraday_view as V  # noqa: E402
from backend.app.warehouse.store import warehouse  # noqa: E402
from contracts.core import (FillScenario, OpportunityRecord, OpportunityStatus, PortfolioState, Position,  # noqa: E402
                            UnfilledReason)
from contracts.news import BreakState  # noqa: E402

CST = timezone(timedelta(hours=8))
MAIN = ("600", "601", "603", "605", "000", "001", "002", "003")
BLOCK_YUAN, LOT = 10_000.0, 100
SECTOR_DUMP_PCT = -1.0
SW = ROOT / ".work/lab/main/sw_industry.parquet"
CALENDAR = Path(sys.argv[3]) if len(sys.argv) > 3 else ROOT / "backend/data/calendar/events.jsonl"


def iid(code: str) -> str:
    return f"CN.{'SH' if code.startswith('6') else 'SZ'}.{code}"


def aware(t: datetime) -> datetime:
    return t.replace(tzinfo=CST)


def prev_session(day: date) -> tuple[date, pl.DataFrame]:
    bars = (warehouse("live").scan("bars")  # skips retired parts
            .filter((pl.col("date") < day) & (pl.col("date") >= day - timedelta(days=20))).select("code", "date", "close", "volume").collect()
            # the warehouse bars carry no amount (Tencent kline): volume (shares) x close stands in for it
            .with_columns((pl.col("volume") * pl.col("close")).alias("amount")))
    last = bars["date"].max()
    two = bars.filter(pl.col("date") >= last - timedelta(days=10)).sort("code", "date")
    two = two.with_columns(pl.col("close").shift(1).over("code").alias("pc")).filter(pl.col("date") == last)
    return last, two


def limit_price(prev_close: float) -> float:
    return float(Decimal(str(prev_close * 1.1)).quantize(Decimal("0.01")))


def context(frame: pl.DataFrame, sw2: dict[str, str]) -> dict[datetime, tuple[dict[str, int], dict[str, float], float | None]]:
    """Per full-snapshot known time: sealed names per industry, each industry's median move since the last one, and
    the live failed-board rate (touched the limit today but not sealed now / touched), main board."""
    full = frame.filter(pl.col("kind") == "full").with_columns(pl.col("code").replace_strict(sw2, default=None).alias("sw2"))
    out, prev = {}, None
    for kt, snap in full.sort("known_time").group_by("known_time", maintain_order=True):
        sealed = snap.filter((pl.col("price") >= pl.col("limit_up") - T.EPS) & ((pl.col("ask1_px").fill_null(0)) == 0)
                             & pl.col("sw2").is_not_null())
        counts = dict(sealed.group_by("sw2").len().iter_rows())
        moves: dict[str, float] = {}
        if prev is not None:
            j = snap.join(prev.select("code", pl.col("price").alias("p0")), on="code").filter(pl.col("sw2").is_not_null())
            moves = dict(j.group_by("sw2").agg(((pl.col("price") / pl.col("p0") - 1) * 100).median()).iter_rows())
        main = snap.filter(pl.col("code").str.slice(0, 3).is_in(MAIN) & pl.col("limit_up").is_not_null())
        touched = main.filter(pl.col("high") >= pl.col("limit_up") - T.EPS)
        now_sealed = touched.filter((pl.col("price") >= pl.col("limit_up") - T.EPS) & (pl.col("ask1_px").fill_null(0) == 0))
        fail = (touched.height - now_sealed.height) / touched.height if touched.height else None
        out[kt[0]] = (counts, moves, fail)
        prev = snap
    return out


def ctx_at(ctx: dict, times: list[datetime], t: datetime):
    import bisect
    i = bisect.bisect_right(times, t) - 1
    return ctx[times[i]] if i >= 0 else ({}, {}, None)


def buy_fill(rows: pl.DataFrame, arrive: datetime, lots: float, close_t: datetime, limit: float) -> dict[FillScenario, tuple[float, float | None]]:
    """(filled lots, price) per scenario: below the limit at arrival -> ask1 of the first row after; else the queue."""
    after = rows.filter(pl.col("known_time") >= arrive)
    first = after.row(0, named=True) if after.height else None
    if first and first["price"] is not None and (first.get("ask1_px") or 0) and first["ask1_px"] < limit - T.EPS:
        return {sc: (lots, first["ask1_px"]) for sc in FillScenario}
    snaps = rows.select(*Q.NEEDED)
    sim = Q.simulate(snaps, arrive, lots, close_t)
    return {sc: (f.filled_lots, limit if f.filled_lots else None) for sc, f in sim.items()}


def main() -> int:
    day = datetime.now(CST).date() if sys.argv[1] == "today" else date.fromisoformat(sys.argv[1])
    root = Path(sys.argv[2]) if len(sys.argv) > 2 else ROOT / ".work/intraday"
    summary = root / day.isoformat() / "summary.json"
    if not summary.exists() or json.loads(summary.read_text(encoding="utf-8")).get("end") != "done":
        print(f"paper replay: {day} has no finished recording under {root}, skipped")
        return 0
    out_dir = ROOT / ".work/lab/main/paper" / day.isoformat()
    out_dir.mkdir(parents=True, exist_ok=True)
    frame = V.load_day(root / day.isoformat())
    names = dict(frame.select("code", "name").unique("code").iter_rows())
    d0 = datetime.combine(day, datetime.min.time())
    t_open, t_close = V.at(d0, "09:30:00"), V.at(d0, "15:00:00")
    last, prev = prev_session(day)
    hist = (warehouse("live").scan("bars").filter((pl.col("date") <= last) & (pl.col("date") >= last - timedelta(days=420))
                                                  & pl.col("code").str.slice(0, 3).is_in(MAIN))
            .select("code", "date", "high", "close").collect())
    mstate = M.state_at(M.daily_counts(hist), last)
    prev = prev.filter(pl.col("code").str.slice(0, 3).is_in(MAIN))
    prev_ups = prev.filter(pl.col("pc").is_not_null() & (pl.col("close") >= pl.col("pc").map_elements(limit_price, return_dtype=pl.Float64) - T.EPS))
    prev_ups = prev_ups.filter(~pl.col("code").replace_strict(names, default="").str.contains("ST"))
    prev_amount = dict(prev.select("code", "amount").iter_rows())
    sw = pl.read_parquet(SW, columns=["code", "sw2", "joined"])
    sw2 = dict(sw.select("code", "sw2").iter_rows())
    linked = L.link(C.load(CALENDAR).values(), L.members(sw, day)) if CALENDAR.exists() else []
    ctx = context(frame, sw2)
    ctx_times = sorted(ctx)
    session = frame.filter(pl.col("kind").is_in(("full", "hot", "close")) & pl.col("known_time").is_between(t_open, t_close + timedelta(seconds=10)))
    by_code = {k[0]: g for k, g in session.group_by("code")}
    final = {r["code"]: r for r in frame.filter(pl.col("kind") == "auction_final").iter_rows(named=True)}
    t_final = max((r["known_time"] for r in final.values()), default=V.at(d0, "09:25:15"))

    # the book: previous-session limit-ups, sellable today
    positions, book = [], {}
    for code, close in prev_ups.select("code", "close").iter_rows():
        qty = max(LOT, int(BLOCK_YUAN / close / LOT) * LOT)
        positions.append(Position(instrument_id=iid(code), total_qty=qty, sellable_qty=qty, acquired_today=0,
                                  cost_basis=Decimal(str(close)) * qty))
        book[code] = (qty, close)
    state = PortfolioState(account_id="paper", ledger_version=0, as_of=aware(t_open), session_date=day.isoformat(),
                           cash_available=Decimal("1000000"), positions=tuple(positions))

    opps: list[dict] = []
    close_px = {r["code"]: r["price"] for r in frame.filter(pl.col("kind") == "close").iter_rows(named=True)}

    def record(rule: str, code: str, t: datetime, limit: float, fills: dict, extra: dict | None = None) -> None:
        nonlocal state
        qty = max(LOT, int(BLOCK_YUAN / limit / LOT) * LOT)
        for sc, (lots, px) in fills.items():
            filled = int(min(lots * LOT, qty) // LOT * LOT)
            status = OpportunityStatus.FILLED if filled == qty else OpportunityStatus.PARTIALLY_FILLED if filled else OpportunityStatus.UNFILLED
            rec = OpportunityRecord(opportunity_id=f"{day}-{rule}-{code}-{sc.value}", intent_id=f"{day}-{rule}-{code}", account_id="paper",
                                    instrument_id=iid(code), strategy_ref=f"{rule}/{T.TRIGGERS_VERSION}", decision_time=aware(t), status=status,
                                    scenario=sc, target_quantity=qty, filled_quantity=filled,
                                    avg_fill_price=Decimal(str(px)) if filled and px else None, reference_price=Decimal(str(limit)),
                                    unfilled_reason=UnfilledReason.LIMIT_UP_QUEUE if not filled else None)
            cp = close_px.get(code)
            opps.append({**rec.model_dump(mode="json"), "code": code, "name": names.get(code), "rule": rule,
                         "d0_return_pct": (cp / px - 1) * 100 if filled and px and cp else None, **(extra or {})})
            if sc is FillScenario.BASE and filled and T.ACTIVE.get(rule, True):  # base scenario of booked rules only
                state = INV.reserve_buy(state, iid(code), filled, Decimal(str(px)) * filled, aware(t))
                state = INV.buy_fill(state, iid(code), filled, Decimal(str(px)), Decimal(0), Decimal(str(px)) * filled, aware(t))

    # B1
    for code in prev_ups["code"]:
        f = final.get(code)
        if f and T.b1(f, prev_amount.get(code)) and code in by_code:
            rows = by_code[code]
            record("B1", code, t_final, f["limit_up"], buy_fill(rows, t_final, max(1, int(BLOCK_YUAN / f["limit_up"] / LOT)), t_close, f["limit_up"]))
    # B3
    for code, rows in by_code.items():
        if not code.startswith(MAIN) or "ST" in (names.get(code) or ""):
            continue
        trig = T.B3()
        for r in rows.iter_rows(named=True):
            if trig.feed(r):
                record("B3", code, r["known_time"], r["limit_up"], buy_fill(rows, r["known_time"], max(1, int(BLOCK_YUAN / r["limit_up"] / LOT)), t_close, r["limit_up"]),
                       {"live_fail_rate": ctx_at(ctx, ctx_times, r["known_time"])[2]})
                break

    # sells
    sells = []
    for code, (qty, cost) in book.items():
        rows = by_code.get(code)
        if rows is None:
            sells.append({"code": code, "name": names.get(code), "rule": "ALL", "status": "SUSPENDED", "qty": qty, "cost": cost})
            continue
        rs = rows.rows(named=True)
        first = rs[0]
        exits = {"S0_open": (first["known_time"], first.get("bid1_px") or first["price"], "open")}
        s1, s3 = T.S1(), T.S3()
        held = {"tape": 0, "strict": 0}
        m3 = {"tape": BreakState.SEALED, "strict": BreakState.SEALED}
        was_sealed = False
        for r in rs:
            t = r["known_time"]
            bid = r.get("bid1_px") or r["price"]
            if "S1_decay" not in exits and s1.feed(r):
                exits["S1_decay"] = (t, r["limit_up"], f"bid at limit {r.get('bid1_lots')} lots, peak {s1.peak:.0f}")
            below = s3.below_s(r)
            if "S3_vwap" not in exits and below >= R.VWAP_GRACE_S:
                exits["S3_vwap"] = (t, bid, f"under VWAP {below:.0f} s")
            sealed = T.sealed(r)
            was_sealed |= sealed
            if was_sealed:
                counts, moves, _ = ctx_at(ctx, ctx_times, t)
                ind = sw2.get(code)
                peers = None if ind is None else counts.get(ind, 0) - (1 if sealed else 0)
                dump = None if ind is None or ind not in moves else moves[ind] <= SECTOR_DUMP_PCT
                cat = R.catalyst_pending(linked, code, aware(t)) if linked else None
                for variant in m3:
                    if m3[variant] is BreakState.EXIT:
                        continue
                    chk = R.break_step(m3[variant], aware(t), sealed, None if not r.get("vwap") else r["price"] >= r["vwap"], below,
                                       peers, cat, dump, iid(code), variant=variant)
                    m3[variant] = chk.state
                    if chk.state is BreakState.EXIT:  # the exit rule does not depend on the variant
                        exits.setdefault("M3_exit", (t, bid, chk.reason))
            # 开板即卖 and its M3 versions: sell at a break unless that variant calls it benign (BENIGN needs the price
            # above VWAP, S3 needs it below and S1 fires while sealed, so M3 can only change this rule, not S1 / S3)
            if was_sealed and not sealed:
                exits.setdefault("Sbreak_open", (t, bid, "first break"))
                for variant, state_now in m3.items():
                    key = f"Sbreak_{variant}"
                    if key in exits:
                        continue
                    if state_now is BreakState.BENIGN:
                        held[variant] += 1
                    else:
                        exits[key] = (t, bid, f"break, M3 {state_now.value}")
        cp = close_px.get(code) or rs[-1]["price"]
        exits["HOLD_close"] = (t_close, cp, "close")
        intraday = sorted((exits[k] for k in ("S1_decay", "M3_exit") if k in exits), key=lambda x: x[0])
        exits["POLICY_v2"] = T.sell_policy(True, mstate.heat, intraday[0] if intraday else None,
                                           exits["S0_open"][:2], exits["HOLD_close"][:2])
        exits["POLICY_v3_shadow"] = T.sell_policy_v3(T.sealed(first), intraday[0] if intraday else None,
                                                     exits["S0_open"][:2], exits["HOLD_close"][:2])
        for rule, (t, px, why) in exits.items():
            row = {"code": code, "name": names.get(code), "rule": rule, "status": "FILLED", "qty": qty, "cost": cost, "time": t,
                   "price": px, "why": why, "return_pct": (px / cost - 1) * 100, "vs_close_pct": (px / cp - 1) * 100}
            p = state.position(iid(code))
            try:  # a dry check of the T+1 ledger on a copy: every rule sells the same shares
                INV.reserve_sell(state, iid(code), qty, aware(t))
            except INV.LedgerError:
                row["status"] = "NOT_SELLABLE"
            sells.append(row | {"sellable": p.free_to_sell if p else 0, "held_back_tape": held["tape"], "held_back_strict": held["strict"]})
    # T+1 check on today's buys: they sit in acquired_today and never add to what may be sold today
    t1 = []
    for o in opps:
        if o["scenario"] == "BASE" and o["filled_quantity"] and T.ACTIVE.get(o["rule"], True):
            p = state.position(o["instrument_id"])
            held_before = book.get(o["code"], (0, 0))[0]
            ok = p is not None and p.acquired_today >= o["filled_quantity"] and p.free_to_sell == held_before
            t1.append((o["code"], "NOT_SELLABLE" if ok else "SELLABLE (bug)"))

    op = pl.DataFrame(opps, infer_schema_length=None) if opps else pl.DataFrame()
    se = pl.DataFrame(sells, infer_schema_length=None)
    op.write_parquet(out_dir / "opportunities.parquet")
    se.write_parquet(out_dir / "sells.parquet")
    write_summary(out_dir, day, last, prev_ups.height, op, se, t1, len(linked), mstate)
    print((out_dir / "summary.md").read_text(encoding="utf-8"))
    return 0


def write_summary(out_dir: Path, day: date, last: date, n_book: int, op: pl.DataFrame, se: pl.DataFrame, t1, n_events: int,
                  mstate: M.MarketState) -> None:
    lines = [f"# 纸面回放 {day}（{T.TRIGGERS_VERSION}，{L.RULES_VERSION}）", "",
             f"- 持仓簿：{last} 主板非 ST 涨停 {n_book} 只，每只约 1 万元，按 {last} 收盘价买入，今日可卖。",
             f"- 已关联的日历事件：{n_events} 条（M3 催化输入）。",
             f"- {mstate.day} 收盘市场状态：主板涨停 {mstate.limit_ups} 家，炸板率 "
             f"{'未知' if mstate.failed_rate is None else f'{mstate.failed_rate:.0%}'}，热度 {mstate.heat}，炸板 {mstate.failed}。",
             f"- 买点记账：{'、'.join(k + ('记账' if v else '影子') for k, v in T.ACTIVE.items())}（B1 见 E054，B3 见 E056）；POLICY_v2 = 本版卖出规则；POLICY_v3_shadow = 开盘封板则留到 S1/M3 出场（否则收盘），不封板则开盘卖，只做对照。", ""]
    if op.height:
        lines += ["## 买点（三档撮合，D0 收益 = 成交价到今日收盘，未扣成本）", "",
                  "| 规则 | 档位 | 触发 | 全部成交 | 部分 | 未成交 | D0 均值% | D0 中位% |", "|---|---|---|---|---|---|---|---|"]
        for (rule, sc), g in op.group_by("rule", "scenario", maintain_order=True):
            r = g["d0_return_pct"].drop_nulls()
            lines.append(f"| {rule} | {sc} | {g.height} | {(g['status'] == 'FILLED').sum()} | {(g['status'] == 'PARTIALLY_FILLED').sum()} | "
                         f"{(g['status'] == 'UNFILLED').sum()} | {r.mean() if r.len() else float('nan'):+.2f} | {r.median() if r.len() else float('nan'):+.2f} |")
        lines += ["", f"- T+1 检查：今日 BASE 档买入 {len(t1)} 笔，{sum(1 for _, s in t1 if s == 'NOT_SELLABLE')} 笔的股份留在当日买入、不计入今日可卖（应全部如此）。", ""]
    sold = se.filter(pl.col("status") == "FILLED")
    lines += ["## 卖点（同一批持仓，各规则并排）", "", "| 规则 | 触发只数 | 相对成本均值% | 相对收盘均值% | 中位% | 卖得比收盘好的占比 |",
              "|---|---|---|---|---|---|"]
    for rule, g in sold.group_by("rule", maintain_order=True):
        v = g["vs_close_pct"]
        lines.append(f"| {rule[0]} | {g.height} | {g['return_pct'].mean():+.2f} | {v.mean():+.2f} | {v.median():+.2f} | {(v > 0).mean():.0%} |")
    hb = se.filter(pl.col("rule") == "HOLD_close")
    lines += ["", f"- M3 判良性、压住开板卖出的快照数：Tape {hb['held_back_tape'].sum()}，Strict {hb['held_back_strict'].sum()}。",
              "- Sbreak_open = 开板即卖；Sbreak_tape / Sbreak_strict = 开板时该版本 M3 判良性就继续持有，否则卖出；M3_exit = 跌破 VWAP 90 秒或板块跳水。",
              "- M3 只能改变开板卖出：良性要求价格在 VWAP 之上，S3 要求在之下，S1 只在封板时触发，所以 S1/S3 不受 M3 影响。",
              "- 每条规则只在触发的持仓上有记录；没触发的持仓等于持有到收盘。只有 1 天，不能下结论。"]
    (out_dir / "summary.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


if __name__ == "__main__":
    sys.exit(main())
