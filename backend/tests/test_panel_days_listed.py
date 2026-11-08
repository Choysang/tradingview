from datetime import date, timedelta
from pathlib import Path

import polars as pl

from backend.app.playbooks.panel import LONG_LISTED, research_panel
from backend.app.warehouse.store import Warehouse


def test_days_listed_counts_listing_age_not_warehouse_bars(tmp_path: Path):
    days = [date(2023, 11, 1) + timedelta(days=i) for i in range(30)]
    rows = [(code, d, 10.0, 10.2, 9.8, 10.0, 1e6, 1e7) for code, start in (("600001", 0), ("600002", 20)) for d in days[start:]]
    wh = Warehouse(tmp_path, "live")
    wh.write("bars", pl.DataFrame(rows, schema=["code", "date", "open", "high", "low", "close", "volume", "amount"], orient="row"))
    panel = research_panel(wh).collect()
    old = panel.filter(pl.col("code") == "600001")["days_listed"].to_list()
    new = panel.filter(pl.col("code") == "600002")["days_listed"].to_list()
    # Already trading on the warehouse's first date: long listed from day one.
    assert old[0] == LONG_LISTED + 1 and old[-1] == LONG_LISTED + 30
    # First seen later: a new listing, counted in sessions.
    assert new == list(range(1, 11))


def test_days_listed_uses_listing_dates_when_known(tmp_path: Path):
    days = [date(2023, 11, 1) + timedelta(days=i) for i in range(30)]
    rows = [(code, d, 10.0, 10.2, 9.8, 10.0, 1e6, 1e7) for code, start in (("600001", 0), ("600002", 0), ("600003", 20)) for d in days[start:]]
    wh = Warehouse(tmp_path, "live")
    wh.write("bars", pl.DataFrame(rows, schema=["code", "date", "open", "high", "low", "close", "volume", "amount"], orient="row"))
    wh.write("listings", pl.DataFrame({"code": ["600001", "600002"], "list_date": [date(2023, 10, 30), date(2023, 11, 1)]}))
    panel = research_panel(wh).collect()
    get = lambda c: panel.filter(pl.col("code") == c)["days_listed"].to_list()
    assert get("600001")[0] == 1 + 2  # Mon 10-30 and Tue 10-31 before the first bar (Wed 11-01), plus the bar
    assert get("600002")[0] == 1  # listed on the warehouse's first date: a new listing, not long listed
    assert get("600003") == list(range(1, 11))  # no listing date, first seen later: counts its bars
