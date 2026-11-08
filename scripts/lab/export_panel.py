"""Export the real daily panel to .work/lab/panel.parquet for the torch lab (separate venv, no backend imports).

Columns are the raw bars plus point-in-time float cap, limit flags, industry and the harness's
tradable flag. days_listed is the true listing age (harness.listed_days), not the warehouse bar count. Names are
today's snapshot, so ST is the price-based st_like flag (two exact 5% limit closes in 60 bars; blind after
the 2026-07-06 rule change that widened main-board ST limits to 10%). The benchmark (中证全指) goes to benchmark.parquet.
"""
from __future__ import annotations

import harness as H
from backend.app.playbooks.library_factor import st_like
from harness import BASE, c, ctx, float_cap_pit, listed_days, tradable


def main() -> None:
    k = ctx()
    p = (k.panel.lazy().sort("code", "date").select(
        "code", "date", "open", "high", "low", "close", "volume", "amount", "prev_close", "pct_chg",
        float_cap_pit().alias("float_cap"), "is_limit_up", "is_limit_down", "touched_limit", "is_broken",
        "open_at_limit", "open_at_floor", "limit_ratio", "limit_streak", listed_days().alias("days_listed"), "industry", "name",
        "one_word", "lhb_on", "smart_buyers", "smart_score", "trip_buyers", "inst_net",
        tradable().fill_null(False).alias("tradable"), st_like().fill_null(False).alias("st_like"),
    ).collect())
    p.write_parquet(BASE / "panel.parquet", compression="zstd")
    k.benchmark.write_parquet(BASE / "benchmark.parquet")
    print(f"panel {p.height} rows, {p['code'].n_unique()} codes, {p['date'].min()}..{p['date'].max()}; tradable {p['tradable'].sum()}")


if __name__ == "__main__":
    main()
