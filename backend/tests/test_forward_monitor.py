"""E068 forward monitor log rules (scripts/lab/forward_monitor.py): append-only, latest row per date wins."""
from __future__ import annotations

import json
import sys
from pathlib import Path

LAB = Path(__file__).resolve().parents[2] / "scripts" / "lab"
sys.path.insert(0, str(LAB))

import forward_monitor as M  # noqa: E402


def _line(day: str, flagged: bool, ic: float) -> str:
    return json.dumps({"date": day, "ic": ic, "flagged": flagged})


def test_latest_row_per_date_wins() -> None:
    lines = [_line("2026-10-30", True, 0.042), "", _line("2026-11-02", False, 0.01), _line("2026-10-30", False, 0.05)]
    latest = M.latest_by_date(lines)
    assert set(latest) == {"2026-10-30", "2026-11-02"}
    assert latest["2026-10-30"] == {"date": "2026-10-30", "ic": 0.05, "flagged": False}


def test_flagged_date_gets_a_new_row_only_once_its_window_passes() -> None:
    flagged = {"date": "2026-10-30", "flagged": True}
    clean = {"date": "2026-10-30", "flagged": False}
    assert M.needs_row(None, flagged_now=True)  # a new date is always written, flagged or not
    assert M.needs_row(None, flagged_now=False)
    assert M.needs_row(flagged, flagged_now=False)  # backfilled: re-evaluate
    assert not M.needs_row(flagged, flagged_now=True)  # still failing: no duplicate row
    assert not M.needs_row(clean, flagged_now=False)  # an unflagged row is final
    assert not M.needs_row(clean, flagged_now=True)


def test_holdings_book_for_news_alerts() -> None:
    """daily_forward step 9: holdings and the watch list (next buys + offer top 10, minus holdings) for E075."""
    import json
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts" / "lab"))
    import daily_forward as D
    rows = [json.dumps({"date": "2026-11-05", "holdings": ["000001"], "try_buy": [], "offer_top10": []}),
            json.dumps({"date": "2026-11-06", "holdings": ["600513", "002632"], "try_buy": ["600513", "002451"],
                        "offer_top10": ["002451", "000912"]}), ""]
    book = D.holdings_book(rows)
    assert book == {"date": "2026-11-06", "holdings": ["002632", "600513"], "watch": ["000912", "002451"], "source": "gated_book"}
    assert D.holdings_book([]) is None
