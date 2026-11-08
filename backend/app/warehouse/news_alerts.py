"""News alerts (E075): every new message becomes an event judgement checked against what we hold and watch.

One process per trading day, alongside news.py (a failure here never touches the recorder). It only LOGS alerts:
nothing is sent anywhere. Every 30 s it reads the messages recorded so far today.

Reading: news.py writes `messages/part-*.parquet` only every 300 s (and `messages.parquet` at the end of the day),
but `raw/<source>.jsonl.gz` after every single poll, one gzip member per answer. So the raw files are read
incrementally: the compressed offset of the last complete member is kept per file, a member still being written
is left for the next cycle. The part / final parquet files are the fallback for a day that has no raw files.
Each message version (source + item id + content hash, news.py's `revision_id`) is judged once; the seen keys live
on disk (`alerts_seen.txt`, plus the keys already in `alerts.jsonl`), so a restart does not alert twice.

Judging: rules first (domain/news_rules.py). The optional LLM fallback runs only for a message that touches a
holding / watch-list code without a rule hit, or that looks macro / policy related with no rule hit at all.

Levels: P0 negative event on a holding; P1 negative macro / policy event mapped to an industry that contains a
holding; P2 an event on a watch-list code (either direction) or a positive event on a main-board code we do not
hold; INFO everything else that was classified. Neutral, unclassified messages are not alerted.

Output under `$PULSE_NEWS_DIR/<YYYY-MM-DD>/`: `alerts.jsonl` (append-only, one row per message version and level),
`alerts_summary.json`, `llm_usage.jsonl` (token usage per call, never the prompt), `alerts_state.json`,
`alerts_seen.txt`. Holdings and watch list: `$PULSE_HOLDINGS_FILE` (default `<data_dir>/holdings/book.json`).
Times: news.py's `received_at` (UTC) is the only time that counts; day and display are CST.
Usage: python -m backend.app.warehouse.news_alerts run | once [--date YYYY-MM-DD]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import re
import sys
import time
import zlib
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime, time as dtime, timezone
from pathlib import Path
from typing import Any, Callable

import httpx
import polars as pl

from ..config import ROOT, settings
from ..domain import news_rules as R
from ..domain.event_links import MAIN_PREFIX, load_members
from ..market.calendar import load_calendar
from .news import CST, output_root
from .store import warehouse

LOG = logging.getLogger("pulse.warehouse.news_alerts")

POLL_SECONDS = 30.0
STOP_AT = dtime(22, 30)
HOLDINGS_MAX_STALE_DAYS = 4  # a book dated up to a long weekend before the news day is still the current book
LLM_URL = "https://api.openai.com/v1/responses"
LLM_TIMEOUT = 30.0
LLM_PACE = 2.0
LLM_MAX_CONSECUTIVE_FAILURES = 5
LLM_DEFAULT_MODEL = "gpt-6-luna"
LLM_DEFAULT_CAP = 300
LLM_TEXT_CHARS = 1200
LLM_MAX_CANDIDATES = 60
CODE_PREFIXES = ("600", "601", "603", "605", "688", "689", "000", "001", "002", "003", "300", "301")
MAX_ROW_CODES = 20
LEVELS = ("P0", "P1", "P2", "INFO")
DEFAULT_SW_FILE = ROOT / ".work" / "lab" / "main" / "sw_industry.parquet"
DEFAULT_SECRETS_DIR = r"C:\pulse\secrets"


# --- configuration --------------------------------------------------------------------------------

def holdings_path() -> Path:
    override = os.getenv("PULSE_HOLDINGS_FILE")
    return Path(override) if override else settings.data_dir / "holdings" / "book.json"


def sw_path() -> Path:
    override = os.getenv("PULSE_SW_INDUSTRY")
    return Path(override) if override else DEFAULT_SW_FILE


def read_api_key() -> str | None:
    """OPENAI_API_KEY, else the first line of `$PULSE_SECRETS_DIR/openai.key`. The value goes nowhere else."""
    key = (os.getenv("OPENAI_API_KEY") or "").strip()
    if key:
        return key
    path = Path(os.getenv("PULSE_SECRETS_DIR") or DEFAULT_SECRETS_DIR) / "openai.key"
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, ValueError):
        return None
    return lines[0].strip() or None if lines else None


def _env_int(name: str, default: int) -> int:
    try:
        return max(0, int(os.getenv(name, "")))
    except ValueError:
        return default


def _now() -> datetime:
    return datetime.now(CST)


def _sleep(seconds: float) -> None:
    time.sleep(seconds)


def _atomic_write(path: Path, text: str) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_bytes(text.encode("utf-8"))  # bytes, so Windows does not turn \n into \r\n
    os.replace(tmp, path)


def _append_lines(path: Path, lines: list[str]) -> None:
    if lines:
        with path.open("ab") as fh:
            fh.write(("\n".join(lines) + "\n").encode("utf-8"))


# --- holdings and watch list ----------------------------------------------------------------------

@dataclass(frozen=True)
class Book:
    holdings: frozenset[str] = frozenset()
    watch: frozenset[str] = frozenset()
    date: str | None = None
    source: str | None = None
    warnings: tuple[str, ...] = ()


def norm_code(value: Any) -> str | None:
    m = re.fullmatch(r"(?:[a-zA-Z]{2}|\d\.)?(\d{6})", str(value).strip())
    return m.group(1) if m else None


def _codes_of(values: Any) -> frozenset[str]:
    return frozenset(c for c in (norm_code(v) for v in values) if c) if isinstance(values, list) else frozenset()


def load_book(day: date, path: Path | None = None) -> Book:
    """The holdings file; a missing, unreadable or old one is an empty book plus a warning, never an error."""
    path = path or holdings_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("not an object")
    except FileNotFoundError:
        return Book(warnings=(f"holdings file missing: {path}",))
    except (OSError, ValueError) as exc:
        return Book(warnings=(f"holdings file unreadable ({type(exc).__name__}): {path}",))
    stamp = str(data.get("date") or "")
    try:
        book_day = date.fromisoformat(stamp)
    except ValueError:
        return Book(date=stamp or None, warnings=(f"holdings file has no valid date ({stamp!r}); ignored",))
    if (day - book_day).days > HOLDINGS_MAX_STALE_DAYS:
        return Book(date=stamp, warnings=(f"holdings file is old ({stamp} for {day}); ignored",))
    warnings = (f"holdings file is dated {stamp}, after the news day {day}",) if book_day > day else ()
    return Book(_codes_of(data.get("holdings")), _codes_of(data.get("watch")), stamp, str(data.get("source") or "") or None, warnings)


# --- names, codes and industries ------------------------------------------------------------------

def _squash(name: str) -> str:
    return re.sub(r"\s+", "", R.normalize(name))


@dataclass(frozen=True)
class NameIndex:
    """Exact stock names (at least 3 characters, unique) by first character, and the known code set."""
    by_first: dict[str, tuple[tuple[str, str], ...]] = field(default_factory=dict)
    codes: frozenset[str] = frozenset()
    names: dict[str, str] = field(default_factory=dict)  # code -> normalised name

    @classmethod
    def from_frame(cls, frame: pl.DataFrame | None) -> "NameIndex":
        if frame is None or frame.is_empty():
            return cls()
        owners: dict[str, set[str]] = {}
        for code, name in frame.select("code", "name").sort("code").iter_rows():
            full = _squash(name or "")
            for variant in {full, re.sub(r"^\*?ST", "", full)}:
                if len(variant) >= 3:
                    owners.setdefault(variant, set()).add(code)
        grouped: dict[str, list[tuple[str, str]]] = {}
        for name, codes in sorted(owners.items()):
            if len(codes) == 1:  # a name two codes share would point at the wrong one half the time
                grouped.setdefault(name[0], []).append((name, next(iter(codes))))
        names = {code: _squash(name or "") for code, name in frame.select("code", "name").sort("code").iter_rows()}
        return cls({k: tuple(sorted(v, key=lambda x: -len(x[0]))) for k, v in grouped.items()}, frozenset(names), names)

    def find(self, text: str) -> list[str]:
        out: list[str] = []
        for i, ch in enumerate(text):
            for name, code in self.by_first.get(ch, ()):
                if text.startswith(name, i) and code not in out:
                    out.append(code)
        return out


def load_name_index() -> NameIndex:
    """Latest name list of the live warehouse's securities table; none yet means no name matching."""
    try:
        return NameIndex.from_frame(warehouse("live").read("securities"))
    except Exception as exc:  # a broken table must not stop the alerts
        LOG.warning("news_alerts: securities names unavailable (%s)", type(exc).__name__)
        return NameIndex()


def text_codes(text: str, known: frozenset[str]) -> list[str]:
    out: list[str] = []
    for code in re.findall(r"(?<![\d.])(\d{6})(?!\d|\.\d)", text):
        ok = code in known if known else code.startswith(CODE_PREFIXES)
        if ok and code not in out:
            out.append(code)
    return out


def load_industry_members(day: date, path: Path | None = None) -> tuple[dict[str, frozenset[str]], str | None]:
    """申万二级 -> main-board members that had joined by `day` (event_links' point-in-time rule)."""
    path = path or sw_path()
    if not path.exists():
        return {}, f"industry file missing: {path}"
    try:
        return load_members(path, day), None
    except (OSError, pl.exceptions.PolarsError) as exc:
        return {}, f"industry file unreadable ({type(exc).__name__}): {path}"


# --- messages: incremental reading ----------------------------------------------------------------

@dataclass(frozen=True)
class Msg:
    source: str
    item_id: str
    revision_id: str
    title: str
    body: str
    codes: tuple[str, ...]
    received_at: datetime  # UTC
    published_at: str | None
    backlog: bool = False  # from a source's first poll of the day (news.py: not "new" for trading)

    @property
    def key(self) -> str:
        return f"{self.source}|{self.revision_id}"


def revision_id(item_id: str, title: str, body: str) -> str:
    digest = hashlib.sha256((title + "\n" + body).encode("utf-8")).hexdigest()  # as news.py computes its content hash
    return f"{item_id}@{digest[:12]}"


def gzip_members(path: Path, offset: int) -> tuple[list[bytes], int]:
    """Decompressed gzip members from `offset` on and the offset after the last complete one. A member still being
    written (or cut off) stops the scan without error: the next cycle finds it whole."""
    with path.open("rb") as fh:
        fh.seek(offset)
        data = fh.read()
    members: list[bytes] = []
    used = 0
    while data:
        d = zlib.decompressobj(wbits=31)
        try:
            out = d.decompress(data)
        except zlib.error:
            break
        if not d.eof:
            break
        members.append(out)
        used += len(data) - len(d.unused_data)
        data = d.unused_data
    return members, offset + used


def _stamp(text: Any) -> datetime | None:
    try:
        return datetime.fromisoformat(str(text)).astimezone(timezone.utc)
    except ValueError:
        return None


def raw_messages(day_dir: Path, offsets: dict[str, int]) -> list[Msg]:
    """New raw answers, offsets advanced in place. Items repeat across polls; the caller dedups by key."""
    out: list[Msg] = []
    for path in sorted((day_dir / "raw").glob("*.jsonl.gz")):
        source = path.name.removesuffix(".jsonl.gz")
        start = offsets.get(path.name, 0)
        members, offsets[path.name] = gzip_members(path, start)
        for index, blob in enumerate(members):
            for line in blob.decode("utf-8", "replace").splitlines():
                try:
                    answer = json.loads(line)
                except ValueError:
                    continue
                received = _stamp(answer.get("received_at"))
                if received is None:
                    continue
                for it in answer.get("items") or []:
                    title, body = str(it.get("title") or ""), str(it.get("body") or "")
                    item_id = str(it.get("item_id") or "")
                    if item_id:
                        out.append(Msg(source, item_id, revision_id(item_id, title, body), title, body, tuple(it.get("codes") or ()),
                                       received, it.get("source_published_at"), backlog=start == 0 and index == 0))
    return out


def parquet_messages(day_dir: Path, done: set[str]) -> list[Msg]:
    """Fallback for a day without raw files: part files not read yet, else the final messages.parquet."""
    files = sorted((day_dir / "messages").glob("part-*.parquet")) if (day_dir / "messages").is_dir() else []
    if not files and (day_dir / "messages.parquet").exists():
        files = [day_dir / "messages.parquet"]
    out: list[Msg] = []
    for path in files:
        if path.name in done:
            continue
        try:
            frame = pl.read_parquet(path)
        except (OSError, pl.exceptions.PolarsError) as exc:  # cut off by a kill: try again next cycle
            LOG.warning("news_alerts: %s unreadable (%s)", path.name, type(exc).__name__)
            continue
        done.add(path.name)
        for r in frame.sort("received_at").iter_rows(named=True):
            published = r.get("source_published_at")
            out.append(Msg(r["source_id"], r["item_id"], r["revision_id"], r.get("title") or "", r.get("body") or "",
                           tuple(r.get("codes") or ()), r["received_at"].astimezone(timezone.utc),
                           published.isoformat() if published else None))
    return out


# --- level decision -------------------------------------------------------------------------------

@dataclass
class Verdict:
    """What a message means: a company-level direction and magnitude for `codes`, and macro hits with industries."""
    method: str
    company: tuple[str, int] | None = None
    codes: tuple[str, ...] = ()
    macro: tuple[tuple[str, int, tuple[str, ...]], ...] = ()  # direction, magnitude, industries
    rule_ids: tuple[str, ...] = ()
    llm_model: str | None = None

    @property
    def classified(self) -> bool:
        return self.company is not None or bool(self.macro)


def _merge_direction(directions: set[str]) -> str:
    return next(iter(directions)) if len(directions) == 1 else "mixed"


def decide_levels(v: Verdict, book: Book, members: dict[str, frozenset[str]]) -> dict[str, dict[str, Any]]:
    """level -> {direction, magnitude, holdings_hit, watch_hit, industries, trigger_codes}."""
    out: dict[str, dict[str, Any]] = {}
    held, watched = book.holdings, book.watch

    def info(direction: str, magnitude: int, holdings=(), watch=(), industries=(), trigger=()) -> dict[str, Any]:
        return {"direction": direction, "magnitude": magnitude, "holdings_hit": sorted(set(holdings)), "watch_hit": sorted(set(watch)),
                "industries": sorted(set(industries)), "trigger_codes": sorted(set(trigger))}

    p0_codes: set[str] = set()
    if v.company and v.company[0] == R.NEGATIVE:
        p0_codes = {c for c in v.codes if c in held}
        if p0_codes:
            out["P0"] = info(R.NEGATIVE, v.company[1], p0_codes, p0_codes & watched, trigger=p0_codes)
    p1_mag, p1_hold, p1_ind = 0, set(), set()
    for direction, magnitude, industries in v.macro:
        if direction != R.NEGATIVE:
            continue
        for ind in industries:
            hit = members.get(ind, frozenset()) & held
            if hit:
                p1_mag, p1_hold, p1_ind = max(p1_mag, magnitude), p1_hold | hit, p1_ind | {ind}
    if p1_hold:
        out["P1"] = info(R.NEGATIVE, p1_mag, p1_hold, p1_hold & watched, p1_ind)
    parts: list[tuple[str, int]] = []
    p2_watch, p2_trigger, p2_ind = set(), set(), set()
    if v.company:
        watch_codes = {c for c in v.codes if c in watched} - p0_codes
        unheld = {c for c in v.codes if v.company[0] == R.POSITIVE and c not in held and c.startswith(MAIN_PREFIX)}
        if watch_codes or unheld:
            parts.append(v.company)
            p2_watch, p2_trigger = p2_watch | watch_codes, p2_trigger | watch_codes | unheld
    for direction, magnitude, industries in v.macro:
        for ind in industries:
            hit = members.get(ind, frozenset()) & watched
            if hit:
                parts.append((direction, magnitude))
                p2_watch, p2_trigger, p2_ind = p2_watch | hit, p2_trigger | hit, p2_ind | {ind}
    if parts:
        out["P2"] = info(_merge_direction({d for d, _ in parts}), max(m for _, m in parts), (), p2_watch, p2_ind, p2_trigger)
    if not out and v.classified:
        directions = {v.company[0]} if v.company else {d for d, _, _ in v.macro}
        mags = [v.company[1]] if v.company else [m for _, m, _ in v.macro]
        inds = {i for _, _, inds_ in v.macro for i in inds_}
        reached = set().union(*(members.get(i, frozenset()) for i in inds)) if inds else set()
        out["INFO"] = info(_merge_direction(directions), max(mags), (set(v.codes) | reached) & held, (set(v.codes) | reached) & watched, inds)
    return out


def rule_verdict(msg: Msg, codes: tuple[str, ...]) -> Verdict:
    cls = R.classify(msg.title, msg.body)
    macro = tuple((h.direction, h.magnitude, h.industries) for h in cls.macro)
    return Verdict("rule", cls.company_verdict(), codes, macro, tuple(h.rule_id for h in cls.hits))


# --- LLM fallback ---------------------------------------------------------------------------------

SYSTEM_PROMPT = (
    "You judge one Chinese financial news message for an A-share trader. Decide whether it is bad news (negative), "
    "good news (positive) or irrelevant (neutral) for the listed candidate stocks and for 申万 level-2 industries. "
    "Answer with one JSON object and nothing else: {\"direction\": \"negative|positive|neutral\", \"magnitude\": 1-3, "
    "\"codes\": [affected codes taken from the candidate list], \"industries\": [affected industries taken from the "
    "industry list], \"reason\": \"one line\"}. Magnitude 1 is minor, 3 is severe. Use neutral when unsure.")


class LLMGate:
    """Caps, pacing and the failure breaker around the Responses API call; counters live in the engine state."""

    def __init__(self, day_dir: Path, counters: dict[str, Any], key: str | None, model: str, cap: int,
                 client: httpx.Client | None = None, sleep: Callable[[float], None] | None = None) -> None:
        self.usage_path = day_dir / "llm_usage.jsonl"
        self.counters, self.model, self.cap = counters, model, cap
        self._key, self._client, self._sleep = key, client, sleep or _sleep
        self._last = 0.0
        for k in ("calls", "failures", "consecutive"):
            counters.setdefault(k, 0)
        counters.setdefault("disabled", False)

    def status(self) -> str | None:
        """None when a call is allowed, else why not."""
        if not self._key:
            return "unavailable"
        if self.counters["disabled"]:
            return "disabled"
        return "cap" if self.counters["calls"] >= self.cap else None

    def ask(self, ref: str, prompt: str) -> tuple[str, str]:
        """(status, answer text): status is ok or a short failure label; never carries the response body."""
        wait = LLM_PACE - (time.monotonic() - self._last)
        if self._last and wait > 0:
            self._sleep(wait)
        self.counters["calls"] += 1
        status, text, usage = "ok", "", {}
        try:
            post = self._client.post if self._client else httpx.post
            r = post(LLM_URL, headers={"Authorization": f"Bearer {self._key}"}, timeout=LLM_TIMEOUT,
                            json={"model": self.model, "instructions": SYSTEM_PROMPT, "input": prompt})
            if r.status_code != 200:
                status = f"http_{r.status_code}"
            else:
                data = r.json()
                if not isinstance(data, dict):
                    raise ValueError("answer is not an object")
                usage = data.get("usage") or {}
                text = data.get("output_text") or "".join(
                    p.get("text", "") for item in data.get("output", []) if item.get("type") == "message"
                    for p in item.get("content", []) if p.get("type") in ("output_text", "text"))
        except (httpx.HTTPError, ValueError) as exc:
            status = f"error_{type(exc).__name__}"
        self._last = time.monotonic()
        self._usage_row(ref, status, usage)
        return status, text

    def record(self, ok: bool) -> None:
        if ok:
            self.counters["consecutive"] = 0
            return
        self.counters["failures"] += 1
        self.counters["consecutive"] += 1
        if self.counters["consecutive"] >= LLM_MAX_CONSECUTIVE_FAILURES:
            self.counters["disabled"] = True
            LOG.warning("news_alerts: LLM disabled for the day after %d failures in a row", self.counters["consecutive"])

    def _usage_row(self, ref: str, status: str, usage: dict[str, Any]) -> None:
        row = {"ts": _now().isoformat(timespec="seconds"), "ref": ref, "model": self.model, "status": status,
               **{k: usage.get(k) for k in ("input_tokens", "output_tokens", "total_tokens")}}
        _append_lines(self.usage_path, [json.dumps(row, ensure_ascii=False)])


def parse_llm_answer(text: str, candidates: set[str], industries: set[str]) -> tuple[str, int, tuple[str, ...], tuple[str, ...]] | None:
    m = re.search(r"\{.*\}", text, re.S)
    try:
        data = json.loads(m.group(0)) if m else None
        direction = str(data["direction"]).lower()
        magnitude = min(3, max(1, int(data.get("magnitude") or 1)))
        codes = tuple(sorted({c for c in (norm_code(x) for x in data.get("codes") or []) if c in candidates}))
        inds = tuple(sorted({str(i) for i in data.get("industries") or [] if str(i) in industries}))
    except (ValueError, KeyError, TypeError, AttributeError):
        return None
    if direction not in (R.NEGATIVE, R.POSITIVE, "neutral"):
        return None
    return direction, magnitude, codes, inds


# --- engine ---------------------------------------------------------------------------------------

class AlertEngine:
    def __init__(self, day_dir: Path, day: date, *, book_path: Path | None = None, names: NameIndex | None = None,
                 members: dict[str, frozenset[str]] | None = None, key_loader: Callable[[], str | None] = read_api_key,
                 llm_client: httpx.Client | None = None, sleep: Callable[[float], None] | None = None) -> None:
        self.day_dir, self.day = day_dir, day
        self.book_path = book_path
        self.names = names if names is not None else load_name_index()
        self.warnings: list[str] = []
        if members is None:
            members, warning = load_industry_members(day)
            if warning:
                self.warnings.append(warning)
        self.members = members
        self.code_industry = {c: i for i, codes in sorted(members.items()) for c in sorted(codes)}
        self.state_path = day_dir / "alerts_state.json"
        self.state = self._load_state()
        self.llm = LLMGate(day_dir, self.state["llm"], key_loader(),
                           os.getenv("PULSE_ALERT_MODEL") or LLM_DEFAULT_MODEL, _env_int("PULSE_ALERT_LLM_CAP", LLM_DEFAULT_CAP),
                           llm_client, sleep)
        self.seen: set[str] = set()
        self.counts = {"levels": Counter(), "methods": Counter(), "sources": Counter()}
        self._load_seen()
        self.processed = len(self.seen)
        self.book = Book()

    # state on disk
    def _load_state(self) -> dict[str, Any]:
        try:
            state = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            state = {}
        state.setdefault("raw_offsets", {})
        state.setdefault("parts_done", [])
        state.setdefault("llm", {})
        return state

    def _load_seen(self) -> None:
        try:
            self.seen.update(x for x in (self.day_dir / "alerts_seen.txt").read_text(encoding="utf-8").split("\n") if x)
        except OSError:
            pass
        for row in read_alert_rows(self.day_dir):  # alerts written before a crash cost no duplicate
            self.seen.add(f"{row.get('source')}|{row.get('revision_id')}")
            self.counts["levels"][row.get("level")] += 1
            self.counts["methods"][row.get("method")] += 1
            self.counts["sources"][row.get("source")] += 1

    # one cycle
    def step(self) -> int:
        """Read what is new, judge it, append the alerts, refresh summary and state. Returns messages judged."""
        if not self.day_dir.is_dir():
            return 0
        self.book = load_book(self.day, self.book_path)
        raw_files = list((self.day_dir / "raw").glob("*.jsonl.gz")) if (self.day_dir / "raw").is_dir() else []
        done = set(self.state["parts_done"])
        batch = raw_messages(self.day_dir, self.state["raw_offsets"]) if raw_files else parquet_messages(self.day_dir, done)
        self.state["parts_done"] = sorted(done)
        fresh: list[Msg] = []
        for m in sorted(batch, key=lambda x: x.received_at):
            if m.key not in self.seen:
                self.seen.add(m.key)
                fresh.append(m)
        rows = [row for m in fresh for row in self.safe_judge(m)]
        _append_lines(self.day_dir / "alerts.jsonl", [json.dumps(r, ensure_ascii=False) for r in rows])
        _append_lines(self.day_dir / "alerts_seen.txt", [m.key for m in fresh])
        for r in rows:
            self.counts["levels"][r["level"]] += 1
            self.counts["methods"][r["method"]] += 1
            self.counts["sources"][r["source"]] += 1
        self.processed += len(fresh)
        _atomic_write(self.state_path, json.dumps(self.state, ensure_ascii=False))
        self.write_summary()
        return len(fresh)

    def mapped_codes(self, m: Msg, text: str) -> tuple[str, ...]:
        out: list[str] = []
        for code in [*(c for c in (norm_code(x) for x in m.codes) if c), *text_codes(text, self.names.codes), *self.names.find(text)]:
            if code not in out:
                out.append(code)
        return tuple(out)

    def safe_judge(self, m: Msg) -> list[dict[str, Any]]:
        try:
            return self.judge(m)
        except Exception as exc:  # one odd message must not lose the batch (its key is already seen)
            LOG.error("news_alerts: judging %s failed (%s)", m.key, type(exc).__name__)
            return []

    def judge(self, m: Msg) -> list[dict[str, Any]]:
        text = R.normalize(f"{m.title}\n{m.body}")
        codes = self.mapped_codes(m, text)
        verdict = rule_verdict(m, codes)
        llm_status = "not_needed"
        touches = bool(set(codes) & (self.book.holdings | self.book.watch))
        if not verdict.rule_ids and (touches or R.looks_macro(m.title, m.body)):
            verdict, llm_status = self.ask_llm(m, text, codes, verdict, touches)
        rows = []
        for level, info in decide_levels(verdict, self.book, self.members).items():
            rows.append({
                "received_at": m.received_at.astimezone(CST).isoformat(timespec="seconds"), "item_id": m.item_id,
                "revision_id": m.revision_id, "source": m.source, "title": m.title[:80], "level": level,
                "direction": info["direction"], "magnitude": info["magnitude"], "codes": list(codes[:MAX_ROW_CODES]),
                "trigger_codes": info["trigger_codes"], "holdings_hit": info["holdings_hit"], "watch_hit": info["watch_hit"],
                "industries": info["industries"], "method": verdict.method, "rules": list(verdict.rule_ids),
                "llm": llm_status, "llm_model": verdict.llm_model, "backlog": m.backlog, "published_at": m.published_at,
                "holdings_date": self.book.date, "rules_version": R.RULES_VERSION,
                "alert_ts": _now().isoformat(timespec="seconds")})
        return rows

    def ask_llm(self, m: Msg, text: str, codes: tuple[str, ...], rule: Verdict, touches: bool) -> tuple[Verdict, str]:
        status = self.llm.status()
        if status:
            skips = self.llm.counters.setdefault("skips", {})
            skips[status] = skips.get(status, 0) + 1
            return rule, status
        candidates = list(codes) if touches else []
        if not touches:  # macro-looking: the model picks among what we hold and watch
            candidates = sorted(self.book.holdings | self.book.watch)
        candidates = candidates[:LLM_MAX_CANDIDATES]
        industries = sorted(self.members)
        lines = [f"- {c} {self.names.names.get(c, '')} {self.code_industry.get(c, '')}".rstrip() for c in candidates]
        prompt = (f"Source: {m.source}\nMessage:\n{text[:LLM_TEXT_CHARS]}\n\nCandidate stocks:\n" + ("\n".join(lines) or "(none)")
                  + "\n\nIndustry list: " + "、".join(industries))
        answer_status, answer = self.llm.ask(m.key, prompt)
        parsed = parse_llm_answer(answer, set(candidates), set(industries)) if answer_status == "ok" else None
        self.llm.record(parsed is not None)
        if parsed is None:
            return rule, "failed" if answer_status == "ok" else f"failed_{answer_status}"
        direction, magnitude, hit_codes, hit_inds = parsed
        if direction == "neutral":
            return rule, "neutral"
        macro = ((direction, magnitude, hit_inds),) if hit_inds else ()
        verdict = Verdict("llm", (direction, magnitude) if hit_codes else None, hit_codes, macro, (), self.llm.model)
        return verdict, "ok"

    def write_summary(self) -> None:
        warnings = [*self.warnings, *self.book.warnings]
        summary = {"day": self.day.isoformat(), "updated_at": _now().isoformat(timespec="seconds"), "rules_version": R.RULES_VERSION,
                   "processed": self.processed, "levels": {k: self.counts["levels"].get(k, 0) for k in LEVELS},
                   "methods": dict(sorted(self.counts["methods"].items())), "sources": dict(sorted(self.counts["sources"].items())),
                   "llm": {"model": self.llm.model, "available": bool(self.llm._key), "cap": self.llm.cap,
                           "calls": self.llm.counters["calls"], "failures": self.llm.counters["failures"],
                           "disabled": self.llm.counters["disabled"], "skips": dict(self.llm.counters.get("skips", {}))},
                   "holdings_date": self.book.date, "holdings": len(self.book.holdings), "watch": len(self.book.watch),
                   "warnings": warnings}
        _atomic_write(self.day_dir / "alerts_summary.json", json.dumps(summary, ensure_ascii=False, indent=1))


# --- reading alerts back --------------------------------------------------------------------------

def read_alert_rows(day_dir: Path) -> list[dict[str, Any]]:
    try:
        raw = (day_dir / "alerts.jsonl").read_bytes().decode("utf-8", "replace")
    except OSError:
        return []
    rows = []
    for line in raw.splitlines():
        try:
            rows.append(json.loads(line))
        except ValueError:  # the line being written right now
            continue
    return rows


def latest_alerts(day: str | date, level: str | None = None, n: int = 50) -> list[dict[str, Any]]:
    """The newest `n` alert rows of a news day (optionally one level), newest first. No file means an empty list."""
    rows = read_alert_rows(output_root() / (day.isoformat() if isinstance(day, date) else str(day)))
    if level:
        rows = [r for r in rows if str(r.get("level")).upper() == level.upper()]
    return rows[::-1][:max(0, n)]


# --- process --------------------------------------------------------------------------------------

def run(once: bool = False, day: date | None = None) -> int:
    today = day or _now().date()
    if not once and not load_calendar().is_trading_day(today):
        LOG.info("news_alerts: %s is not a trading day", today)
        return 0
    day_dir = output_root() / today.isoformat()
    if once and not day_dir.is_dir():
        LOG.error("news_alerts: no news recorded for %s (%s)", today, day_dir)
        return 1
    day_dir.mkdir(parents=True, exist_ok=True)
    handler = logging.FileHandler(day_dir / "alerts_run.log", encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logging.getLogger().addHandler(handler)
    try:
        engine = AlertEngine(day_dir, today)
        if once:
            engine.step()
            print(json.dumps(json.loads((day_dir / "alerts_summary.json").read_text(encoding="utf-8")), ensure_ascii=False))
            return 0
        while True:
            try:
                engine.step()
            except Exception:  # one bad cycle must not end the day
                LOG.exception("news_alerts: cycle failed")
            now = _now()
            if now.time() >= STOP_AT:
                return 0
            left = (datetime.combine(now.date(), STOP_AT, tzinfo=CST) - now).total_seconds()
            _sleep(min(POLL_SECONDS, left))
    finally:
        logging.getLogger().removeHandler(handler)
        handler.close()


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="command", required=True)
    sub.add_parser("run")
    once = sub.add_parser("once")
    once.add_argument("--date", type=date.fromisoformat, default=None)
    args = ap.parse_args(argv)
    return run(once=args.command == "once", day=getattr(args, "date", None))


if __name__ == "__main__":
    sys.exit(main())
