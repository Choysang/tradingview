"""Check the user's account of 2026 themes against the data (lab venv, polars only).

The account: Jan-Jun 2026 a tech herding rally (抱团), Jul-Oct a tech selloff, an innovative-drug rally in
August. Equal-weight industry averages hide herding, which lives in a few large names, so each theme is
measured two ways per month: equal weight and float-cap weight (previous day's float cap), daily
close-to-close returns compounded, minus the same-weighted whole market. Themes come from Sina concept
lists (sectors.parquet): today's membership, so a stock that joined a concept after its run flatters that
concept's past (look-ahead; the read here is descriptive). Index months come from the warehouse
(CSI 300, CSI 1000, ChiNext). Output .work/lab/theme_check.json and a printed table.
"""
from __future__ import annotations

import glob
import json
import os

import polars as pl

LAB = os.path.join(os.path.dirname(__file__), "..", "..", ".work", "lab")
THEMES = {
    "科技芯片": ("华为海思", "科创50", "华为概念", "国产软件", "信息安全", "华为鸿蒙", "鸿蒙概念", "汽车电子", "5G概念"),
    "创新药": ("创新药", "CXO概念", "CRO概念", "免疫治疗", "抗癌", "生物疫苗", "基因概念"),
    "机器人": ("机器人概念", "智能机器"),
    "新能源车电池": ("锂电池", "固态电池", "高压快充", "钠电池", "华为汽车", "特斯拉"),
    "光伏风电": ("光伏", "光伏概念", "风电", "TOPCon", "BC电池", "HIT电池", "钙钛矿"),
    "军工": ("国防军工", "军工航天", "军民融合", "大飞机", "卫星导航"),
    "黄金资源": ("黄金概念", "锂矿", "稀缺资源", "涉矿概念"),
    "白酒消费": ("白酒概念", "奢侈品", "婴童概念"),
}
INDEX = {"000300": "沪深300", "000852": "中证1000", "399006": "创业板指"}


def main() -> None:
    p = pl.read_parquet(os.path.join(LAB, "panel.parquet"), columns=["code", "date", "close", "prev_close", "float_cap", "tradable"])
    p = p.sort("code", "date").with_columns(
        (pl.col("close") / pl.col("prev_close") - 1).alias("r"), pl.col("float_cap").shift(1).over("code").alias("w"),
        pl.col("date").dt.strftime("%Y-%m").alias("month"))
    p = p.filter(pl.col("r").is_finite() & (pl.col("r").abs() < 0.35) & pl.col("tradable"))
    sec = pl.read_parquet(os.path.join(LAB, "sectors.parquet")).filter(pl.col("kind") == "concept")
    mkt = p.group_by("date").agg(pl.col("r").mean().alias("m_ew"), ((pl.col("r") * pl.col("w")).sum() / pl.col("w").sum()).alias("m_cw"))
    rows = {}
    for theme, concepts in THEMES.items():
        codes = sec.filter(pl.col("sector").is_in(concepts))["code"].unique()
        x = p.filter(pl.col("code").is_in(codes)).group_by("date").agg(
            pl.col("r").mean().alias("ew"), ((pl.col("r") * pl.col("w")).sum() / pl.col("w").sum()).alias("cw"), pl.len().alias("n"))
        x = x.join(mkt, on="date").with_columns(pl.col("date").dt.strftime("%Y-%m").alias("month"))
        m = x.group_by("month").agg(
            (((1 + pl.col("ew")).product() / (1 + pl.col("m_ew")).product() - 1) * 100).alias("ew_ex"),
            (((1 + pl.col("cw")).product() / (1 + pl.col("m_cw")).product() - 1) * 100).alias("cw_ex"),
            (((1 + pl.col("cw")).product() - 1) * 100).alias("cw_abs"), pl.col("n").mean().alias("n")).sort("month")
        rows[theme] = {r["month"]: r for r in m.to_dicts()}
        rows[theme]["_members"] = len(codes)
    files = glob.glob(os.path.join(os.environ.get("PULSE_DATA_DIR", ""), "warehouse", "live", "index_bars", "**", "*.parquet"), recursive=True)
    idx = {}
    if files:
        ib = pl.concat([pl.read_parquet(f).select("symbol", "date", "close") for f in files]).unique(["symbol", "date"]).sort("symbol", "date")
        ib = ib.with_columns(pl.col("date").dt.strftime("%Y-%m").alias("month")).group_by("symbol", "month").agg(pl.col("close").last()).sort("symbol", "month")
        ib = ib.with_columns(((pl.col("close") / pl.col("close").shift(1).over("symbol") - 1) * 100).alias("ret"))
        idx = {s: {r["month"]: r["ret"] for r in ib.filter(pl.col("symbol") == s).to_dicts()} for s in INDEX}
    months = [m for m in sorted(next(iter(rows.values())).keys()) if not m.startswith("_") and m >= "2025-07"]
    print("monthly theme excess vs the same-weighted market, %: cap-weighted (equal-weight); members = today's Sina concept lists")
    print("month    " + " ".join(f"{INDEX[s]:>7s}" for s in INDEX) + "  " + "  ".join(f"{t:>13s}" for t in THEMES))
    for m in months:
        ix = " ".join(f"{idx.get(s, {}).get(m, float('nan')):+7.1f}" for s in INDEX)
        th = "  ".join(f"{rows[t][m]['cw_ex']:+6.1f}({rows[t][m]['ew_ex']:+5.1f})" if m in rows[t] else " " * 13 for t in THEMES)
        print(f"{m}  {ix}  {th}")
    print("members:", {t: rows[t]["_members"] for t in THEMES})
    json.dump({"themes": {t: {k: v for k, v in r.items()} for t, r in rows.items()}, "index": idx},
              open(os.path.join(LAB, "theme_check.json"), "w", encoding="utf-8"), ensure_ascii=False, indent=1, default=str)


if __name__ == "__main__":
    main()
