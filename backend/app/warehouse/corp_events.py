"""Stock-level calendar from the Eastmoney data center: 限售股解禁 (unlocks) and 停复牌 (suspensions), main board.

* unlocks: report RPT_LIFT_STAGE (data.eastmoney.com/dxf), every unlock dated from today to `days` ahead.
  Shares are in 万股; `free_ratio` / `total_ratio` are the unlocked shares over the current float / total shares
  (above 1 when a post-IPO lock-up is larger than the float: 603092 6.2 on 2026-11-09; checked on
  2026-11-02: 46.0012 万股 x 6.66 元 = 306.37 万元 = LIFT_MARKET_CAP). The source's market value uses the
  price at fetch time, so it is kept only as `value_at_fetch_wan`; decisions should price the shares
  themselves at the decision time.
* suspensions: report RPT_CUSTOM_SUSPEND_DATA_INTERFACE (data.eastmoney.com/tfpxx), the list the site shows for
  one date (DATETIME), which already includes suspensions announced for the next session.
Each sync writes a snapshot `<data_dir>/corp_events/<kind>/<fetch date>.parquet` with `fetched_at` (UTC), and
the raw answers next to it, so what was known on a day is the snapshot of that day, never a later one.
Requests go 1 s apart; a refusal (403 / 429 / 501, non-JSON) stops the sync with an error.
Usage: python -m backend.app.warehouse.corp_events sync [days]
"""
from __future__ import annotations

import gzip
import json
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx
import polars as pl

from ..config import settings

URL = "https://datacenter-web.eastmoney.com/api/data/v1/get"
HEADERS = {"User-Agent": "Mozilla/5.0", "Referer": "https://data.eastmoney.com/"}
PACE, PAGE_SIZE, MAX_PAGES = 1.0, 500, 20
CST = timezone(timedelta(hours=8))
MAIN_PREFIX = ("600", "601", "603", "605", "000", "001", "002", "003")
UNLOCK_COLUMNS = ("SECURITY_CODE,SECURITY_NAME_ABBR,FREE_DATE,CURRENT_FREE_SHARES,ABLE_FREE_SHARES,LIFT_MARKET_CAP,"
                  "FREE_RATIO,FREE_SHARES_TYPE,TOTAL_RATIO,NON_FREE_SHARES,BATCH_HOLDER_NUM")
UNLOCK_SCHEMA: dict[str, Any] = {"code": pl.Utf8, "name": pl.Utf8, "unlock_date": pl.Date, "shares_wan": pl.Float64,
                                 "tradable_shares_wan": pl.Float64, "free_ratio": pl.Float64, "total_ratio": pl.Float64,
                                 "share_type": pl.Utf8, "holders": pl.Int64, "value_at_fetch_wan": pl.Float64,
                                 "fetched_at": pl.Datetime("us", "UTC")}
SUSPEND_SCHEMA: dict[str, Any] = {"code": pl.Utf8, "name": pl.Utf8, "suspend_start": pl.Datetime("us"),
                                  "suspend_end": pl.Datetime("us"), "expected_resume": pl.Date, "span": pl.Utf8,
                                  "reason": pl.Utf8, "fetched_at": pl.Datetime("us", "UTC")}


def root() -> Path:
    return settings.data_dir / "corp_events"


def _get(client: httpx.Client, params: dict[str, str]) -> list[dict]:
    rows: list[dict] = []
    for page in range(1, MAX_PAGES + 1):
        response = client.get(URL, params={**params, "pageNumber": str(page), "pageSize": str(PAGE_SIZE),
                                            "source": "WEB", "client": "WEB"})
        if response.status_code in (403, 429, 501):
            raise RuntimeError(f"Eastmoney {params['reportName']} refused: HTTP {response.status_code}")
        try:
            payload = response.json()
        except ValueError as exc:
            raise RuntimeError(f"Eastmoney {params['reportName']} answered non-JSON (HTTP {response.status_code})") from exc
        result = (payload or {}).get("result") or {}  # an empty day answers result null
        rows += [r for r in (result.get("data") or []) if isinstance(r, dict)]
        if page >= int(result.get("pages") or 1):
            break
        time.sleep(PACE)
    return rows


def _day(text: str | None) -> date | None:
    return datetime.fromisoformat(text).date() if text else None


def _when(text: str | None) -> datetime | None:
    return datetime.fromisoformat(text) if text else None


def _main(code: Any) -> bool:
    return str(code or "")[:3] in MAIN_PREFIX


def unlocks_frame(rows: list[dict], fetched: datetime) -> pl.DataFrame:
    out = [{"code": r["SECURITY_CODE"], "name": r.get("SECURITY_NAME_ABBR"), "unlock_date": _day(r.get("FREE_DATE")),
            "shares_wan": r.get("CURRENT_FREE_SHARES"), "tradable_shares_wan": r.get("ABLE_FREE_SHARES"),
            "free_ratio": r.get("FREE_RATIO"), "total_ratio": r.get("TOTAL_RATIO"), "share_type": r.get("FREE_SHARES_TYPE"),
            "holders": r.get("BATCH_HOLDER_NUM"), "value_at_fetch_wan": r.get("LIFT_MARKET_CAP"), "fetched_at": fetched}
           for r in rows if _main(r.get("SECURITY_CODE")) and r.get("FREE_DATE")]
    return pl.DataFrame(out, schema=UNLOCK_SCHEMA)


def suspensions_frame(rows: list[dict], fetched: datetime) -> pl.DataFrame:
    out = [{"code": r["SECURITY_CODE"], "name": r.get("SECURITY_NAME_ABBR"), "suspend_start": _when(r.get("SUSPEND_START_TIME")),
            "suspend_end": _when(r.get("SUSPEND_END_TIME")), "expected_resume": _day(r.get("PREDICT_RESUME_DATE")),
            "span": r.get("SUSPEND_EXPIRE"), "reason": r.get("SUSPEND_REASON"), "fetched_at": fetched}
           for r in rows if _main(r.get("SECURITY_CODE"))]
    return pl.DataFrame(out, schema=SUSPEND_SCHEMA)


def _save(kind: str, day: date, frame: pl.DataFrame, raw: list[dict], base: Path) -> Path:
    folder = base / kind
    folder.mkdir(parents=True, exist_ok=True)
    with gzip.open(folder / f"{day}.raw.json.gz", "wt", encoding="utf-8") as fh:
        json.dump(raw, fh, ensure_ascii=False)
    target = folder / f"{day}.parquet"
    frame.write_parquet(target)
    return target


def sync(days: int = 90, now: datetime | None = None, client: httpx.Client | None = None, base: Path | None = None) -> dict[str, int]:
    now = now or datetime.now(timezone.utc)
    base = base or root()
    today = now.astimezone(CST).date()
    own = client is None
    client = client or httpx.Client(timeout=10, headers=HEADERS)
    try:
        lift = _get(client, {"reportName": "RPT_LIFT_STAGE", "columns": UNLOCK_COLUMNS, "sortColumns": "FREE_DATE", "sortTypes": "1",
                             "filter": f"(FREE_DATE>='{today}')(FREE_DATE<='{today + timedelta(days=days)}')"})
        time.sleep(PACE)
        stop = _get(client, {"reportName": "RPT_CUSTOM_SUSPEND_DATA_INTERFACE", "columns": "ALL", "sortColumns": "SUSPEND_START_DATE",
                             "sortTypes": "-1", "filter": f"(MARKET=\"全部\")(DATETIME='{today}')"})
    finally:
        if own:
            client.close()
    unlocks, suspensions = unlocks_frame(lift, now), suspensions_frame(stop, now)
    _save("unlocks", today, unlocks, lift, base)
    _save("suspensions", today, suspensions, stop, base)
    return {"unlocks": unlocks.height, "unlocks_all_boards": len(lift), "suspensions": suspensions.height}


def unlock_pressure(unlocks: pl.DataFrame, as_of: date, horizon_days: int = 10) -> pl.DataFrame:
    """Per code: share of the float unlocking from `as_of` through `as_of + horizon_days`, and the first such date."""
    window = unlocks.filter(pl.col("unlock_date").is_between(as_of, as_of + timedelta(days=horizon_days)))
    return (window.group_by("code").agg(pl.col("free_ratio").sum().alias("unlock_free_ratio"),
                                        pl.col("unlock_date").min().alias("next_unlock"))
            .sort("unlock_free_ratio", descending=True))


def main(argv: list[str]) -> int:
    if argv[:1] == ["sync"] and len(argv) <= 2:
        print(f"corp events: {sync(int(argv[1]) if len(argv) == 2 else 90)} -> {root()}")
        return 0
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
