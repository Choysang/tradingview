"""Today's crowd snapshot: industries moving as a pack, name clusters, Dragon-Tiger seats.

Built from the research panel's latest day (the crowd columns of crowd.py) so the
page shows exactly what the crowd playbooks see.
"""
from __future__ import annotations

from datetime import date

import polars as pl

from contracts.schema import CrowdSnapshot, CrowdStock, IndustryHeat, LhbStock, NameCluster, SeatRecord
from . import crowd
from .evaluate import Context, context, dataset_provenance
from ..warehouse.store import Warehouse, active

TOP_INDUSTRIES = 8
TOP_NAMES = 6
TOP_LHB = 20
TOP_SEATS = 15
FOLLOWERS = 4


def _stock(row: dict) -> CrowdStock:
    return CrowdStock(
        code=row["code"], name=row.get("name") or row["code"],
        pct_chg=None if row.get("pct_chg") is None else round(float(row["pct_chg"]), 2),
        limit_streak=int(row.get("limit_streak") or 0), is_limit_up=bool(row.get("is_limit_up")),
    )


def industries(day: pl.DataFrame) -> list[IndustryHeat]:
    tagged = day.filter(pl.col("industry").is_not_null())
    if tagged.is_empty():
        return []
    stats = (
        tagged.group_by("industry").agg(
            pl.col("is_limit_up").sum().alias("limit_ups"), pl.len().alias("members"), pl.col("pct_chg").mean().alias("avg_pct"),
        ).filter(pl.col("limit_ups") >= 2).sort(["limit_ups", "avg_pct"], descending=True).head(TOP_INDUSTRIES)
    )
    out = []
    for row in stats.iter_rows(named=True):
        members = tagged.filter(pl.col("industry") == row["industry"])
        leader = members.filter(pl.col("is_limit_up")).sort(["limit_streak", "amount"], descending=True).head(1).to_dicts()
        followers = members.filter(~pl.col("is_limit_up") & (pl.col("pct_chg") >= 3)).sort("pct_chg", descending=True).head(FOLLOWERS)
        out.append(IndustryHeat(
            industry=row["industry"], limit_ups=int(row["limit_ups"]), members=int(row["members"]),
            avg_pct=None if row["avg_pct"] is None else round(float(row["avg_pct"]), 2),
            leader=_stock(leader[0]) if leader else None, followers=[_stock(r) for r in followers.to_dicts()],
        ))
    return out


def names(day: pl.DataFrame) -> list[NameCluster]:
    chars = crowd.rare_chars(day.select("code", "name").drop_nulls().unique("code"))
    if chars.is_empty():
        return []
    hits = day.filter(pl.col("is_limit_up")).select("code").join(chars, on="code", how="inner")
    counts = hits.group_by("char").agg(pl.len().alias("n")).filter(pl.col("n") >= 2).sort(["n", "char"], descending=[True, False])
    out = []
    for char in counts["char"].head(TOP_NAMES).to_list():
        members = day.join(chars.filter(pl.col("char") == char), on="code", how="inner")
        ups = members.filter(pl.col("is_limit_up")).sort(["limit_streak", "amount"], descending=True)
        followers = members.filter(~pl.col("is_limit_up") & (pl.col("pct_chg") >= 3)).sort("pct_chg", descending=True).head(FOLLOWERS)
        out.append(NameCluster(char=char, limit_ups=[_stock(r) for r in ups.to_dicts()], followers=[_stock(r) for r in followers.to_dicts()]))
    return out


def lhb(day: pl.DataFrame) -> list[LhbStock]:
    listed = day.filter(pl.col("lhb_on").fill_null(False))
    if listed.is_empty():
        return []
    ranked = listed.sort(["smart_buyers", "inst_net"], descending=True, nulls_last=True).head(TOP_LHB)
    return [
        LhbStock(
            code=r["code"], name=r.get("name") or r["code"],
            pct_chg=None if r.get("pct_chg") is None else round(float(r["pct_chg"]), 2),
            smart_buyers=int(r["smart_buyers"] or 0), trip_buyers=int(r["trip_buyers"] or 0),
            inst_net=None if r.get("inst_net") is None else round(float(r["inst_net"]), 3),
        )
        for r in ranked.to_dicts()
    ]


def seats(ctx: Context, day: pl.DataFrame) -> list[SeatRecord]:
    history = crowd.seat_history(ctx.wh, ctx.panel.select("code", "date", "open", "close"))
    if history is None:
        return []
    _, _, buys, settled = history
    latest = ctx.dates[-1]
    names_by_code = dict(day.select("code", "name").iter_rows())
    today = buys.filter(pl.col("date") == latest).group_by("seat").agg(pl.col("code"))
    records = crowd.seat_records(settled, latest).join(today, on="seat", how="inner")
    records = records.sort(["smart", "score"], descending=True).head(TOP_SEATS)
    return [
        SeatRecord(
            seat=r["seat"], events=int(r["n"]), avg_return=round(float(r["score"]), 2),
            trip_share=round(float(r["trip_share"]) * 100, 1), smart=bool(r["smart"]), trip=bool(r["trip"]),
            bought_today=[names_by_code.get(code, code) for code in r["code"]],
        )
        for r in records.to_dicts()
    ]


def lhb_notes(known: set[date] | None, latest: date) -> list[str]:
    """Why the Dragon-Tiger columns may be empty today: no list at all, or the latest day not fetched yet."""
    if known is None:
        return ["仓库里没有龙虎榜：运行 python -m backend.app.warehouse.lhb --years 3 同步。"]
    if latest not in known:
        return [
            f"龙虎榜 {latest.isoformat()} 的榜单尚未同步（通常 18:00 后才发布，也可能是该日同步失败）："
            "席位与机构净买入暂按未知处理，不当作无人上榜；收盘后运行 python -m backend.app.warehouse.lhb 补齐。"
        ]
    return []


def _empty(ctx: Context) -> CrowdSnapshot:
    return CrowdSnapshot(
        provenance=dataset_provenance(ctx), trade_date="", industries=[], names=[], lhb=[], seats=[],
        notes=["仓库里还没有日线数据，抱团快照为空：先同步行情。"],
    )


def snapshot(wh: Warehouse | None = None) -> CrowdSnapshot:
    try:
        ctx = context(wh)
    except ValueError:  # the research panel is empty: nothing to show, not a server error
        no_data = Context(wh=wh or active(), version="", panel=pl.DataFrame(), dates=[], regimes={}, benchmark=None, names={})
        return _empty(no_data)
    if not ctx.dates:
        return _empty(ctx)
    latest = ctx.dates[-1]
    day = ctx.panel.filter(pl.col("date") == latest)
    notes = [
        "行业用新浪当前成分快照；概念板块成分常在题材火了之后才建立，只作展示，不参与回测。",
        "名称联动：名称里全市场不超过 20 个名称共有的少见字，当天至少 2 只涨停。",
        f"席位战绩：上榜买入后次日开盘买、第 {crowd.FORWARD_BARS} 根 K 线收盘卖，只算已到期的交易，"
        f"收缩平均 ≥ {crowd.SMART_MIN_RETURN:g}% 且 ≥ {crowd.SMART_MIN_EVENTS} 次为强势；"
        f"≥ {crowd.TRIP_MIN_SHARE:.0%} 次日即卖为一日游。机构专用是匿名席位池，单独计净买入。",
    ]
    if day["industry"].null_count() == day.height:
        notes.append("仓库里没有板块成分：运行 python -m backend.app.warehouse.boards 同步。")
    notes += lhb_notes(crowd.lhb_known_days(ctx.wh), latest)
    return CrowdSnapshot(
        provenance=dataset_provenance(ctx), trade_date=latest.isoformat(),
        industries=industries(day), names=names(day), lhb=lhb(day), seats=seats(ctx, day), notes=notes,
    )
