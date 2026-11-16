"""Legacy recorder files: read-only import source for intraday snapshots, 7x24 news, guba samples, extended intraday (BF live lane).

Why: until the M6 cutover the old system (same IP) records every session. A second recorder here would double the
requests to Sina/Tencent and risk a block for both systems, so pulse3 imports those recordings as a declared legacy
SOURCE (old DATA may be read by an adapter, old code never). Rows: LEGACY_TRIAGE #103/#185 (intraday), #131/#187 (news),
#104/#186 (extended intraday); the seal facts (#5/#124) derive from the snapshots.

Layout read (root ``PULSE_LEGACY_DATA``, default ``C:\\pulse\\data``), one directory per session date::

    intraday/<d>/quotes/part-*.parquet   main-board snapshots (full polls), intraday/<d>/hot/part-*.parquet (hot subset), done.json;
                                         after the close the parts are compacted into intraday/<d>/quotes.parquet and hot.parquet
    intraday_ext/<d>/(parts/*.parquet | quotes_ext.parquet)   ETF / index futures / A50 / CNH
    news/<d>/messages/part-*.parquet, news/<d>/guba/*.parquet, news/<d>/alerts.jsonl

Invariants:
* files are opened for reading only; nothing is renamed, truncated or rewritten (tests compare bytes and mtime);
* source times are kept: naive Asia/Shanghai datetimes are converted to UTC (``poll_at`` = when the old recorder fetched,
  ``quote_at`` = the exchange time of the quote); the import time is recorded by the caller as ``available_at``;
* a price of ``0`` in a quote column means "absent" (empty book level, no open yet) and becomes NULL, like the Sina profile's
  ``zero_is_missing``; lots / volumes stay as recorded (float64, never rounded);
* a part that cannot be read yet (still being written) is reported, not guessed; a missing required column is a
  ``SchemaMismatch`` (never silently null).
What the data is: ~60 s SAMPLED snapshots of the vendor quote, NOT tick data; anything derived (first seal time, broken
count) is an estimate bounded by the sampling gap. Naive news timestamps: ``received_at`` is UTC; ``source_published_at`` /
``source_modified_at`` are read as UTC when they sit within two hours of ``received_at`` that way, otherwise as Asia/Shanghai
(the zone used is recorded on every message as ``source_time_zone``). Guba and alerts files are passed through unchanged.
Not covered: writing to the legacy tree, decoding the old system's private caches.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

import polars as pl

from pulse.domain.intelligence.feeds import NewsRecord, ParsedNews

SOURCE_ID = "legacy_recordings"
NEWS_SOURCE_ID = "legacy_news_recorder"
SCHEMA_VERSION = "legacy-recorder-parquet-20261113"
MAPPING_VERSION = "1.0.0"
ENV_ROOT = "PULSE_LEGACY_DATA"
DEFAULT_ROOT = "C:\\pulse\\data"
CST = "Asia/Shanghai"
UTC = timezone.utc

#: endpoint ids of the declared source (registry.py builds the EndpointSpec rows)
EP_QUOTES, EP_HOT, EP_EXT, EP_GUBA, EP_ALERTS, EP_NEWS = "intraday_quotes", "intraday_hot", "intraday_ext", "guba_samples", "alerts_jsonl", "news_messages"

_CAPABILITY = {
    "capability_class": "sampled_snapshots_estimated",
    "sampled_interval_seconds_nominal": 60,
    "not_tick_data": True,
    "estimated": True,
    "note": "Recorded by the old system at about one snapshot per instrument per minute; a first-seal time or a broken-board count "
            "derived from it is an estimate with an uncertainty of up to one sampling gap.",
}
_COMMON = {"timezone": "Asia/Shanghai", "mode": "legacy_recording_import_read_only", "calendar_id": "xshg_xshe_sessions",
           "entitlement_status": "own_recording_of_public_quote_endpoints", "source_times": "naive Asia/Shanghai in the files, converted to UTC",
           "available_at": "time of import into pulse3 (the recording is not available to pulse3 earlier)",
           "retire": "after the M6 cutover pulse3's own recorder (intraday-record) replaces this import"}
ENDPOINT_POLICIES: dict[str, dict] = {
    EP_QUOTES: {**_COMMON, **_CAPABILITY, "files": "intraday/<d>/quotes/part-*.parquet", "universe": "main board, full polls"},
    EP_HOT: {**_COMMON, **_CAPABILITY, "files": "intraday/<d>/hot/part-*.parquet", "universe": "hot subset polled more often"},
    EP_EXT: {**_COMMON, **_CAPABILITY, "files": "intraday_ext/<d>/", "universe": "ETF, index futures, A50, CNH",
             "units": "price in the instrument's own currency/points; volume/amount as the vendor gives them"},
    EP_GUBA: {**_COMMON, "files": "news/<d>/guba/*.parquet", "coverage": "sampled_not_exhaustive", "capability_class": "community_sample"},
    EP_ALERTS: {**_COMMON, "files": "news/<d>/alerts.jsonl", "use": "comparison only (M5 reconciliation); never copied into pulse3 alerts",
                "capability_class": "legacy_alert_log"},
    EP_NEWS: {**_COMMON, "files": "news/<d>/messages/part-*.parquet", "revisions": "revision_id/supersedes chain of the recorder, kept as document revisions",
              "capability_class": "recorded_feed", "rate_limit": "none (file import)"},
}

QUOTE_REQUIRED = ("code", "name", "kind", "poll_ts", "quote_ts", "amount_yuan", "price", "prev_close", "open", "volume_lots", "outer_lots", "inner_lots",
                  *[f"bid{i}_px" for i in range(1, 6)], *[f"bid{i}_lots" for i in range(1, 6)],
                  *[f"ask{i}_px" for i in range(1, 6)], *[f"ask{i}_lots" for i in range(1, 6)],
                  "high", "low", "turnover", "float_cap", "total_cap", "limit_up", "limit_down", "volume_ratio", "vwap", "after_amount_wan", "after_lots")
_QUOTE_PX = ("price", "prev_close", "open", "high", "low", "vwap", "limit_up", "limit_down",
             *[f"bid{i}_px" for i in range(1, 6)], *[f"ask{i}_px" for i in range(1, 6)])
_QUOTE_DEC = {"amount_yuan": (20, 2), "float_cap": (20, 4), "total_cap": (20, 4), "turnover": (12, 4), "volume_ratio": (12, 4), "after_amount_wan": (20, 4)}
_QUOTE_FLOAT = ("volume_lots", "outer_lots", "inner_lots", *[f"bid{i}_lots" for i in range(1, 6)], *[f"ask{i}_lots" for i in range(1, 6)], "after_lots")
EXT_REQUIRED = ("symbol", "name", "asset", "host", "kind", "poll_ts", "quote_ts", "price", "prev_close", "prev_kind", "open", "high", "low", "volume",
                "amount", "bid1", "ask1")
EXT_OPTIONAL = ("open_interest", "contract")
_EXT_DEC = ("price", "prev_close", "open", "high", "low", "bid1", "ask1", "open_interest")
NEWS_REQUIRED = ("source_id", "item_id", "title", "received_at")
NEWS_OPTIONAL = ("revision_id", "supersedes", "content_hash", "body", "url", "source_published_at", "source_modified_at", "codes", "comments", "level")


class LegacyRecordingsError(RuntimeError):
    pass


class SchemaMismatch(LegacyRecordingsError):
    """A recorder file lost a column the mapping needs; nothing is imported from it."""


class PartUnreadable(LegacyRecordingsError):
    """The file cannot be parsed yet (for example still being written); it is retried by the next run."""


@dataclass(frozen=True)
class SourcePart:
    stream: str                 # quotes | hot | ext | guba | news | alerts
    name: str                   # POSIX path relative to the root, e.g. "intraday/2026-11-12/quotes/part-0001.parquet"
    path: Path
    size: int
    sha256: str

    def read_bytes(self) -> bytes:
        return self.path.read_bytes()

    @property
    def short(self) -> str:
        """Name relative to the day directory (stable across roots)."""
        return "/".join(self.name.split("/")[2:])


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _utc_expr(col: str, dtype: pl.DataType, assume: str = CST) -> pl.Expr:
    """CST-naive or tz-aware datetime column -> UTC instants, millisecond precision kept."""
    e = pl.col(col)
    if getattr(dtype, "time_zone", None) is None:
        e = e.cast(pl.Datetime("ms")).dt.replace_time_zone(assume)
    return e.dt.convert_time_zone("UTC").cast(pl.Datetime("ms", "UTC"))


def _dec(name: str, precision: int, scale: int, *, zero_is_missing: bool = False) -> pl.Expr:
    e = pl.col(name).cast(pl.Float64).fill_nan(None)
    if zero_is_missing:
        e = pl.when(e == 0).then(None).otherwise(e)
    return e.cast(pl.Decimal(precision, scale)).alias(name)


def _need(df: pl.DataFrame, required: tuple[str, ...], what: str) -> None:
    missing = [c for c in required if c not in df.columns]
    if missing:
        raise SchemaMismatch(f"{what}: missing columns {missing}")


class LegacyRecordings:
    def __init__(self, root: Path | str | None = None):
        self.root = Path(root or os.environ.get(ENV_ROOT) or DEFAULT_ROOT)

    # ---- discovery -----------------------------------------------------------------------------------------
    def day_dir(self, kind: str, day: date) -> Path:
        return self.root / kind / day.isoformat()

    def _part(self, stream: str, path: Path) -> SourcePart:
        return SourcePart(stream, path.relative_to(self.root).as_posix(), path, path.stat().st_size, _sha256_file(path))

    def parts(self, stream: str, day: date) -> list[SourcePart]:
        """Files of one stream and session, name-ordered. The compacted file is preferred over the loose parts: after the close the
        old recorder writes ``intraday/<d>/quotes.parquet`` (``hot.parquet``, ``intraday_ext/<d>/quotes_ext.parquet``) and empties
        the part folder; the dataset then holds the compacted file only (see ``_publish_parts``)."""
        if stream in ("quotes", "hot"):
            compact = self.day_dir("intraday", day) / f"{stream}.parquet"
            paths = [compact] if compact.is_file() else sorted((self.day_dir("intraday", day) / stream).glob("part-*.parquet"))
        elif stream == "ext":
            d = self.day_dir("intraday_ext", day)
            compact = d / "quotes_ext.parquet"
            paths = [compact] if compact.is_file() else sorted((d / "parts").glob("*.parquet")) + sorted(d.glob("part-*.parquet"))
        elif stream == "news":
            paths = sorted((self.day_dir("news", day) / "messages").glob("part-*.parquet"))
        elif stream == "guba":
            paths = sorted((self.day_dir("news", day) / "guba").glob("*.parquet"))
        elif stream == "alerts":
            p = self.day_dir("news", day) / "alerts.jsonl"
            paths = [p] if p.is_file() else []
        else:
            raise ValueError(f"unknown stream {stream!r}")
        return [self._part(stream, p) for p in paths if p.is_file()]

    def done_marker(self, day: date) -> dict | None:
        p = self.day_dir("intraday", day) / "done.json"
        if not p.is_file():
            return None
        try:
            doc = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {"unreadable": True}
        return doc if isinstance(doc, dict) else {"value": doc}

    # ---- quotes (main-board snapshots) ------------------------------------------------------------------------------
    def read_quotes(self, part: SourcePart, session_date: date) -> pl.DataFrame:
        df = self._read_parquet(part)
        _need(df, QUOTE_REQUIRED, part.name)
        sch = df.schema
        out = df.select(
            pl.lit(part.stream).alias("source_stream"), pl.lit(part.short).alias("source_part"), pl.lit(session_date).alias("session_date"),
            pl.col("code").cast(pl.String), pl.col("name").cast(pl.String), pl.col("kind").cast(pl.String),
            _utc_expr("poll_ts", sch["poll_ts"]).alias("poll_at"), _utc_expr("quote_ts", sch["quote_ts"]).alias("quote_at"),
            *[_dec(c, 18, 4, zero_is_missing=True) for c in _QUOTE_PX],
            *[_dec(c, *p) for c, p in _QUOTE_DEC.items()],
            *[pl.col(c).cast(pl.Float64).fill_nan(None) for c in _QUOTE_FLOAT])
        return out

    # ---- extended intraday ---------------------------------------------------------------------------------------------
    def read_ext(self, part: SourcePart, session_date: date) -> pl.DataFrame:
        df = self._read_parquet(part)
        _need(df, EXT_REQUIRED, part.name)
        for c in EXT_OPTIONAL:
            if c not in df.columns:
                df = df.with_columns(pl.lit(None, dtype=pl.Float64 if c == "open_interest" else pl.String).alias(c))
        sch = df.schema
        return df.select(
            pl.lit(part.short).alias("source_part"), pl.lit(session_date).alias("session_date"),
            *[pl.col(c).cast(pl.String) for c in ("symbol", "name", "asset", "host", "kind", "prev_kind", "contract")],
            _utc_expr("poll_ts", sch["poll_ts"]).alias("poll_at"), _utc_expr("quote_ts", sch["quote_ts"]).alias("quote_at"),
            *[_dec(c, 24, 6) for c in _EXT_DEC],
            pl.col("volume").cast(pl.Float64).fill_nan(None), pl.col("amount").cast(pl.Float64).fill_nan(None))

    # ---- guba / alerts: passed through ----------------------------------------------------------------------------------
    def read_table(self, part: SourcePart) -> pl.DataFrame:
        """Guba sample file as recorded (columns and naive times unchanged; the zone of naive columns is not declared)."""
        return self._read_parquet(part)

    def read_alert_lines(self, part: SourcePart) -> pl.DataFrame:
        rows = []
        text = part.path.read_text(encoding="utf-8", errors="replace")
        for i, line in enumerate(text.splitlines(), start=1):
            if not line.strip():
                continue
            try:
                json.loads(line)
                ok = True
            except ValueError:
                ok = False
            rows.append({"line_no": i, "line_sha256": hashlib.sha256(line.encode("utf-8")).hexdigest(), "raw_json": line, "parsed_ok": ok})
        return pl.DataFrame(rows, schema={"line_no": pl.Int64, "line_sha256": pl.String, "raw_json": pl.String, "parsed_ok": pl.Boolean})

    # ---- news ----------------------------------------------------------------------------------------------------------
    def read_news(self, part: SourcePart) -> list[dict]:
        """Message revisions of one part as plain dicts (times as UTC ``datetime`` or None), received order."""
        df = self._read_parquet(part)
        _need(df, NEWS_REQUIRED, part.name)
        for c in NEWS_OPTIONAL:
            if c not in df.columns:
                df = df.with_columns(pl.lit(None, dtype=pl.List(pl.String) if c == "codes" else pl.String).alias(c))
        sch = df.schema
        recv = _utc_expr("received_at", sch["received_at"], "UTC")
        naive = [c for c in ("source_published_at", "source_modified_at") if sch[c] != pl.Null and getattr(sch[c], "time_zone", None) is None]
        zone = infer_naive_zone(df, recv) if naive else "tz-aware"
        cols = []
        for c in ("source_published_at", "source_modified_at"):
            if sch[c] == pl.Null:
                cols.append(pl.col(c).cast(pl.Datetime("ms", "UTC")))
            else:
                cols.append(_utc_expr(c, sch[c], zone if zone != "tz-aware" else CST).alias(c))
        out = df.with_columns(recv.alias("received_at"), *cols)
        rows = []
        for r in out.iter_rows(named=True):
            codes = r["codes"]
            if isinstance(codes, str):
                codes = [x for x in re.split(r"[,\s;|]+", codes) if x]
            rows.append({
                "origin_source_id": str(r["source_id"]), "item_id": str(r["item_id"]),
                "revision_id": None if r["revision_id"] is None else str(r["revision_id"]),
                "supersedes": None if r["supersedes"] is None else str(r["supersedes"]),
                "content_hash": r["content_hash"] or hashlib.sha256(f"{r['title']}\n{r['body'] or ''}".encode("utf-8")).hexdigest(),
                "title": str(r["title"] or ""), "body": str(r["body"] or ""), "url": r["url"],
                "source_published_at": r["source_published_at"], "source_modified_at": r["source_modified_at"], "received_at": r["received_at"],
                "codes": [str(x) for x in (codes or [])], "comments": r["comments"], "level": None if r["level"] is None else str(r["level"]),
                "source_time_zone": zone, "part": part.short, "part_sha256": part.sha256})
        rows.sort(key=lambda m: (m["received_at"], m["revision_id"] or ""))
        return rows

    # ---- internals -----------------------------------------------------------------------------------------------------
    @staticmethod
    def _read_parquet(part: SourcePart) -> pl.DataFrame:
        try:
            return pl.read_parquet(part.path)
        except Exception as exc:  # noqa: BLE001 - parquet readers raise many types for a partial file
            raise PartUnreadable(f"{part.name}: {type(exc).__name__}: {exc}") from exc


def infer_naive_zone(df: pl.DataFrame, received_utc: pl.Expr) -> str:
    """Zone of naive source timestamps: ``UTC`` when they sit within 2 h of ``received_at`` read as UTC, else Asia/Shanghai.

    The wrong guess would shift every item by 8 h; read as CST while really UTC, items look 8 h old and every alert is
    discarded as a late backfill. ``received_at`` (recorded in UTC by the recorder) is the only anchor in the file."""
    for c in ("source_published_at", "source_modified_at"):
        if c in df.columns and df.schema[c] != pl.Null and getattr(df.schema[c], "time_zone", None) is None:
            probe = df.select(_utc_expr(c, df.schema[c], "UTC").alias("p"), received_utc.alias("r")).drop_nulls()
            if probe.height:
                med = (probe["r"] - probe["p"]).dt.total_seconds().median()
                return "UTC" if med is not None and abs(float(med)) <= 2 * 3600 else CST
    return CST


# ---- news records for the intelligence path --------------------------------------------------------------------------------
_OWN = re.compile(r"^财联社\d{1,2}月\d{1,2}日电")


def _ref(code: str) -> str:
    """Vendor-style ref accepted by ``stock_code_from_ref``: bare 6-digit codes get their exchange prefix; others stay verbatim."""
    c = code.strip()
    if re.fullmatch(r"\d{6}", c):
        return ("sh" if c[0] == "6" else "bj" if c[0] in "489" else "sz") + c
    m = re.fullmatch(r"(\d{6})\.(SH|SZ|BJ)", c, flags=re.I)
    return m.group(2).lower() + m.group(1) if m else c


def _iso(v: datetime | None) -> str | None:
    return None if v is None else v.astimezone(UTC).isoformat().replace("+00:00", "Z")


def recorded_news_bytes(m: dict) -> bytes:
    """Lossless raw object of ONE message revision (JSON, UTF-8, keys sorted): what ``parse_recorded_news`` reads back."""
    doc = {**m, "format": "legacy-news-revision-v1", "source_published_at": _iso(m["source_published_at"]),
           "source_modified_at": _iso(m["source_modified_at"]), "received_at": _iso(m["received_at"])}
    return json.dumps(doc, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def source_record_id(m: dict) -> str:
    return f"{m['origin_source_id']}:{m['item_id']}"


def parse_recorded_news(body: bytes, source_id: str = NEWS_SOURCE_ID) -> ParsedNews:
    doc = json.loads(body.decode("utf-8"))
    if doc.get("format") != "legacy-news-revision-v1":
        raise LegacyRecordingsError("not a recorded news revision")

    def dt(v: Any) -> datetime | None:
        return None if v is None else datetime.fromisoformat(v.replace("Z", "+00:00"))

    title = doc["title"] or (doc["body"][:40] if doc["body"] else "")
    pub = dt(doc["source_published_at"])
    rec = NewsRecord(source_id, source_record_id({"origin_source_id": doc["origin_source_id"], "item_id": doc["item_id"]}), title, doc["body"], pub,
                     "second" if pub else "unknown", tuple(_ref(c) for c in doc["codes"]), bool(_OWN.match(doc["body"] or "")), doc)
    return ParsedNews((rec,), None, False)
