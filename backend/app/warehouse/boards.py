"""Sina industry and concept board memberships for the historical warehouse.

Table `boards`: code, kind ("industry" | "concept"), board_id, board_name, as_of.

This is a CURRENT snapshot of who belongs to a board today, not a history of
memberships. Using it on past dates carries look-ahead bias: concept boards are
usually created or filled after a theme got hot, so a stock tagged "华为汽车" today
may not have been in it when it rallied. Industry boards change slowly, so the
bias is small there. Research may use the snapshot to describe the present, or
to group stocks by industry; it must not back-test a concept as if it had been
known at the time. `as_of` is the snapshot date; every full refresh replaces the
previous snapshot, so a stock that left a board drops out of it.

Sources: the board list from `Market_Center.getHQNodes` (nested lists: "新浪行业"
holds ids like `new_blhy`, "概念板块" ids like `gn_hwqc`) and the members of each
board from `Market_Center.getHQNodeData`, paged by 100. Politeness matches the
other providers: ~2 requests/s per host; HTTP 403 / 429 / 456 / 501, a redirect
or an HTML page raises `BoardsBlocked` at once and the job stops.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import date
import logging
import sys
import time
from typing import Any, Callable
from urllib.parse import urlsplit

import httpx
import polars as pl

from .lhb import is_a_share
from .store import Warehouse, warehouse
from .tencent import _Pacer, new_client

LOG = logging.getLogger("pulse.warehouse.boards")

NODES_URL = "https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/Market_Center.getHQNodes"
MEMBERS_URL = "https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/Market_Center.getHQNodeData"
KINDS = {"新浪行业": "industry", "概念板块": "concept"}
PAGE_SIZE = 100
MAX_PAGES = 40
RETRIES = 2
BACKOFF = 0.5
MAX_CONSECUTIVE_ERRORS = 10
_PACERS = {
    "money.finance.sina.com.cn": _Pacer(2.0),
    "vip.stock.finance.sina.com.cn": _Pacer(2.0),
}

BOARD_SCHEMA: dict[str, Any] = {
    "code": pl.Utf8,
    "kind": pl.Utf8,
    "board_id": pl.Utf8,
    "board_name": pl.Utf8,
    "as_of": pl.Date,
}

ProgressHook = Callable[[str], None]


class BoardsError(RuntimeError):
    """A request kept failing after its retries."""


class BoardsBlocked(BoardsError):
    """The host refuses us; retrying only extends the block."""


@dataclass(frozen=True)
class Board:
    kind: str
    board_id: str
    name: str


@dataclass(frozen=True)
class BoardsSummary:
    boards_total: int
    boards_done: int
    rows: int
    errors: int
    blocked: bool
    complete: bool  # every board of the list was fetched: the snapshot replaced the old one
    message: str

    @property
    def ok(self) -> bool:
        return self.complete and not self.blocked


# --- parsing ---------------------------------------------------------------------

def _find(node: Any, name: str) -> list | None:
    """The first nested `[name, [children...]]` entry called `name`."""
    if not isinstance(node, list):
        return None
    if len(node) >= 2 and node[0] == name and isinstance(node[1], list):
        return node
    for child in node:
        found = _find(child, name)
        if found is not None:
            return found
    return None


def parse_nodes(payload: Any) -> list[Board]:
    """Industry and concept boards from the nested `getHQNodes` answer. Leaves are
    `[name, "", id]`; duplicates (an id listed twice) are kept once."""
    boards: dict[tuple[str, str], Board] = {}
    for title, kind in KINDS.items():
        group = _find(payload, title)
        for leaf in group[1] if group else []:
            if isinstance(leaf, list) and len(leaf) >= 3 and isinstance(leaf[2], str) and leaf[2].strip():
                board_id = leaf[2].strip()
                boards.setdefault((kind, board_id), Board(kind, board_id, str(leaf[0]).strip()))
    return list(boards.values())


def parse_members(rows: Any) -> list[str]:
    """A-share codes of one member page, in order, without repeats."""
    codes: list[str] = []
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict):
            continue
        code = str(row.get("code") or str(row.get("symbol") or "")[-6:]).strip()
        if is_a_share(code) and code not in codes:
            codes.append(code)
    return codes


# --- http ------------------------------------------------------------------------------

def _blocked(response: httpx.Response) -> bool:
    return (
        response.status_code in (403, 429, 456, 501)
        or 300 <= response.status_code < 400
        or response.content[:200].lstrip().lower().startswith((b"<!doctype", b"<html"))
    )


def _get(client: httpx.Client, url: str, params: dict[str, Any] | None = None) -> Any:
    host = urlsplit(url).hostname or ""
    pacer = _PACERS.get(host)
    last: Exception | None = None
    for attempt in range(RETRIES + 1):
        try:
            if pacer is not None:
                pacer.wait()
            response = client.get(url, params=params)
            if _blocked(response):
                raise BoardsBlocked(f"{host} 拒绝访问（HTTP {response.status_code}，限流或风控页），已停止请求，稍后再试")
            if response.status_code == 200:
                return response.json()
            last = BoardsError(f"HTTP {response.status_code}")
        except (httpx.HTTPError, ValueError) as exc:
            last = exc
        if attempt < RETRIES:
            time.sleep(BACKOFF * (attempt + 1))
    raise BoardsError(f"{host} 请求失败：{last}") from last


def fetch_boards(client: httpx.Client) -> list[Board]:
    boards = parse_nodes(_get(client, NODES_URL))
    if not boards:
        raise BoardsError("板块列表为空，接口格式可能已变化")
    return boards


def fetch_members(client: httpx.Client, board_id: str) -> list[str]:
    """Every member code of a board, paging until a short page."""
    members: list[str] = []
    for page in range(1, MAX_PAGES + 1):
        params = {"page": page, "num": PAGE_SIZE, "sort": "symbol", "asc": 1, "node": board_id, "symbol": "", "_s_r_a": "page"}
        rows = _get(client, MEMBERS_URL, params)
        rows = rows if isinstance(rows, list) else []
        fresh = [code for code in parse_members(rows) if code not in members]
        members.extend(fresh)
        # A short page ends the board; a page with nothing new means paging is ignored.
        if len(rows) < PAGE_SIZE or not fresh:
            return members
    raise BoardsError(f"{board_id} 页数超过 {MAX_PAGES}，疑似分页异常")


# --- sync ------------------------------------------------------------------------------

def run_boards_sync(
    store: Warehouse | None = None,
    client: httpx.Client | None = None,
    limit: int | None = None,
    on_progress: ProgressHook | None = None,
    today: date | None = None,
) -> BoardsSummary:
    """Fetch the current memberships of every board. A complete run replaces the
    stored snapshot; a partial one (limit, block, failures) is appended so that
    nothing already stored is lost, and the summary says it is incomplete."""
    store = store or warehouse("live")
    owned = client is None
    client = client or new_client()
    say = on_progress or (lambda _message: None)
    as_of = today or date.today()
    rows: list[dict[str, Any]] = []
    errors = streak = done = 0
    blocked = False
    reason = ""
    boards: list[Board] = []
    try:
        boards = fetch_boards(client)
        total = len(boards[:limit]) if limit else len(boards)
        for board in boards[:total]:
            try:
                codes = fetch_members(client, board.board_id)
            except BoardsBlocked as exc:
                blocked, reason = True, str(exc)
                break
            except BoardsError as exc:
                errors += 1
                streak += 1
                LOG.warning("board %s failed: %s", board.board_id, exc)
                say(f"板块 {board.name}（{board.board_id}）获取失败：{exc}")
                if streak >= MAX_CONSECUTIVE_ERRORS:
                    reason = f"连续 {streak} 个板块请求失败，已停止（{exc}）"
                    break
                continue
            streak = 0
            done += 1
            rows.extend(
                {"code": code, "kind": board.kind, "board_id": board.board_id, "board_name": board.name, "as_of": as_of}
                for code in codes
            )
            say(f"板块 {done}/{total} {board.name}（{board.kind}）：{len(codes)} 只")
    except BoardsBlocked as exc:
        blocked, reason = True, str(exc)
    except BoardsError as exc:
        reason = str(exc)
    finally:
        if owned:
            client.close()
    frame = pl.DataFrame(rows, schema=BOARD_SCHEMA).unique(subset=["code", "kind", "board_id"], keep="last").sort(
        ["kind", "board_id", "code"]
    )
    total = len(boards[:limit]) if limit else len(boards)
    complete = not limit and done == total and not errors and not blocked and not frame.is_empty()
    if complete:
        store.replace("boards", frame)
    else:
        store.write("boards", frame)
    parts = [f"板块成分{'同步完成' if complete else '未完整同步'}：{done}/{total} 个板块，{frame.height} 条成分关系"]
    if limit:
        parts.append(f"受 --limit {limit} 限制，仅追加不替换旧快照")
    if errors:
        parts.append(f"失败 {errors} 个板块")
    if reason:
        parts.append(f"{reason}；已拉取的数据已追加保存，旧快照未被替换")
    return BoardsSummary(total, done, frame.height, errors, blocked, complete, "；".join(parts))


# --- cli -------------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Sync Sina industry and concept board memberships (current snapshot)")
    parser.add_argument("--limit", type=int, default=None, help="only the first N boards (smoke test; appends instead of replacing)")
    args = parser.parse_args(argv)
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="replace", **({} if stream.isatty() else {"encoding": "utf-8"}))
    summary = run_boards_sync(limit=args.limit, on_progress=lambda message: print(message, flush=True))
    print(summary.message, flush=True)
    return 0 if summary.ok else 1


if __name__ == "__main__":
    sys.exit(main())
