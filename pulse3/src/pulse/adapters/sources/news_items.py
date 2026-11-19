"""Shared pieces of the native 7x24 news recorder's feed parsers (Tonghuashun, Eastmoney, CLS).

Why: the three feeds answer in three shapes; the recorder (and the legacy recorder files it must stay compatible with) wants one
item shape. Each feed adapter has a ``recorder_items(body)`` that returns plain dicts with exactly these keys: ``item_id, title,
body, url, source_published_at, source_modified_at, codes, comments, level`` (times are aware UTC datetimes or None).
Invariants: pure functions, no IO and no clock; a body that is not the expected JSON shape raises ``BadAnswer`` (never an empty
list: an empty list is a real answer), and an HTML body raises ``BlockedPage`` (a block, not a bad answer); source times are kept as the source states them (epoch seconds, or Asia/Shanghai text
converted to UTC); a missing value stays None.
Not covered: HTTP (the adapters), persistence and revision logic (``application.native_news_recorder``).
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo

from pulse.domain.intelligence.feeds import stock_code_from_ref

UTC = timezone.utc
CST = ZoneInfo("Asia/Shanghai")
_BARE = re.compile(r"(?:[a-zA-Z]{2}|\d\.)?(\d{6})")


class BadAnswer(RuntimeError):
    """HTTP 200 but not the data the feed promises (vendor error code, missing list, text that is not JSON): counts toward the day breaker."""


class BlockedPage(BadAnswer):
    """HTTP 200 whose body is an HTML page on a JSON data endpoint: a WAF / captcha / login shell, i.e. a block in disguise.

    Never a retry candidate: the recorder stops that host for the day on the first one."""


def load_json(body: bytes) -> Any:
    text = body.decode("utf-8", errors="replace").lstrip("﻿").strip()
    if text[:1] == "<":
        raise BlockedPage(f"html page on a data endpoint: {text[:60]!r}")
    if text[:1] not in ("{", "["):
        raise BadAnswer(f"not JSON: {text[:60]!r}")
    try:
        return json.loads(text)
    except ValueError as exc:
        raise BadAnswer(f"bad JSON: {exc}") from exc


def epoch_utc(value: Any) -> datetime | None:
    try:
        return datetime.fromtimestamp(float(value), UTC) if value not in (None, "", 0, "0") else None
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def cst_text_utc(text: str | None) -> datetime | None:
    try:
        return datetime.strptime(text, "%Y-%m-%d %H:%M:%S").replace(tzinfo=CST).astimezone(UTC) if text else None
    except (TypeError, ValueError):
        return None


def bare_codes(values: list[Any]) -> list[str]:
    """6-digit codes in order, de-duplicated (Tonghuashun gives bare codes in a list that holds stocks only)."""
    out: list[str] = []
    for v in values:
        m = _BARE.fullmatch(str(v).strip())
        if m and m.group(1) not in out:
            out.append(m.group(1))
    return out


def stock_codes(refs: list[Any]) -> list[str]:
    """A-share equity codes from vendor refs (``1.600519``, ``sz002695``); sectors, indexes and foreign tickers are dropped.

    Stricter than the old recorder, which kept ``1.000001`` (the SSE index) as the stock 000001."""
    out: list[str] = []
    for r in refs:
        c = stock_code_from_ref(str(r).strip())
        if c and c not in out:
            out.append(c)
    return out
