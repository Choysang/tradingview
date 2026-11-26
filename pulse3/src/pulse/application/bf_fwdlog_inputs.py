"""Inputs of the fwdlog lane: frozen data, the old calendar store (read only), recorder summaries and point-in-time frames of the imported snapshots.

Why: the three forward experiments read the recorded 60 s snapshots (lake datasets ``quote_snapshots_60s`` / ``ext_quote_snapshots``, imported by the live
lane), daily raw bars (``market.bar_projection``), and a few frozen or legacy inputs: the 申万 industry map and the seed of the ETF baseline (frozen data
with sha256 in ``pulse/fwdlog_data/manifest.json``), the calendar store of the old system (event catalysts for the M3 rule, read only until the cutover) and the
two recorder summaries (health). Nothing here writes to the legacy tree.
Invariants: only READY dataset versions that were ready by the cutoff are read (point in time); frozen files are verified against the manifest before
use (a changed file is an error, not a silent new rule); text files hash after CRLF -> LF normalisation; a missing or unreadable summary is reported
as ``{}`` / ``{"unreadable": true}``, never invented.
Not covered: the rules themselves (``domain.fwdlog``), publishing (``bf_fwdlog_service``).
"""
from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Iterable, Mapping, Sequence

import polars as pl

from pulse.domain.fwdlog import board as BD
from pulse.domain.fwdlog import state as ST
from pulse.domain.fwdlog import tail as TL
from pulse.domain.fwdlog import triggers as TR
from pulse.domain.fwdlog.common import CST, MAIN_PREFIX, UTC

DATA_DIR = Path(__file__).resolve().parents[1] / "fwdlog_data"
ENV_EVENTS = "PULSE_FWDLOG_EVENTS"
DEFAULT_EVENTS = "C:\\pulse3\\var\\legacy_calendar\\events.jsonl"      # frozen copy made by retire_legacy.ps1; until then pulse3_env.ps1 points PULSE_FWDLOG_EVENTS at the live old store
KNOWN_LAG_S = {"hot": 2.0, "auction_hot": 2.0, "after_hot": 2.0, "full": 5.0, "auction": 5.0, "auction_final": 5.0, "close": 5.0, "after": 5.0}
SESSION_KINDS = ("full", "hot", "close")
MAIN_PREFIX_LIST = list(MAIN_PREFIX)


class FrozenDataChanged(RuntimeError):
    pass


def file_sha256(path: Path) -> str:
    """sha256 of the bytes; text files hash with CRLF normalised to LF (a Windows checkout hashes the same as the repository)."""
    b = Path(path).read_bytes()
    if Path(path).suffix.lower() not in (".parquet", ".bin"):
        b = b.replace(b"\r\n", b"\n")
    return hashlib.sha256(b).hexdigest()


@dataclass(frozen=True)
class Frozen:
    manifest_sha256: str
    files: Mapping[str, str]
    sw: pl.DataFrame                                   # code, sw1, sw2, sw3, concept, joined
    seed_sums: Mapping[str, Decimal]                   # session date -> summed ETF amount (yuan)
    link_rules: tuple[BD.LinkRule, ...]
    skip_categories: tuple[str, ...]
    link_version: str

    @property
    def sw2(self) -> dict[str, str]:
        return dict(self.sw.select("code", "sw2").iter_rows())

    def members(self, as_of: date) -> dict[str, frozenset[str]]:
        """申万 level-2 industry -> main-board codes that had joined it by ``as_of`` (a stock that left earlier is missing, not misplaced)."""
        rows = self.sw.filter(pl.col("code").str.slice(0, 3).is_in(MAIN_PREFIX_LIST) & (pl.col("joined").is_null() | (pl.col("joined") <= as_of.isoformat())))
        return {k: frozenset(v) for k, v in rows.group_by("sw2").agg(pl.col("code")).iter_rows()}

    def doc_sha256(self, rel: str) -> str:
        return self.files[rel]


def load_frozen(root: Path | None = None) -> Frozen:
    """Frozen inputs, verified against the manifest."""
    root = Path(root or DATA_DIR)
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    for rel, want in manifest["files"].items():
        got = file_sha256(root / rel)
        if got != want:
            raise FrozenDataChanged(f"{rel}: sha256 {got} != manifest {want}; frozen data is never edited in place")
    links = json.loads((root / "event_links_v1.json").read_text(encoding="utf-8"))
    seed: dict[str, Decimal] = {}
    for line in (root / "etf_amount_seed.csv").read_text(encoding="utf-8").splitlines()[1:]:
        if line.strip():
            d, a = line.split(",")[:2]
            seed[d] = Decimal(a)
    return Frozen(file_sha256(root / "manifest.json"), dict(manifest["files"]), pl.read_parquet(root / "sw_industry.parquet"), seed,
                  tuple(BD.LinkRule(r["pattern"], tuple(r["industries"]), r["theme"]) for r in links["rules"]), tuple(links["skip_categories"]), links["version"])


# ---- legacy files (read only) -----------------------------------------------------------------------------------------------------
def read_json(path: Path) -> dict:
    """A recorder summary: ``{}`` when absent, ``{"unreadable": True}`` when broken (a broken summary costs its fields, not the day's row)."""
    try:
        return json.loads(Path(path).read_text(encoding="utf-8")) if Path(path).exists() else {}
    except (OSError, ValueError):
        return {"unreadable": True}


NATIVE_EXT_MARKER = "recorder_summary"             # key of the native extended recorder's coverage document (``bf_ext_record``): its health lives there, not in summary.json


def recorder_health(ext: "DayFrames | None", ext_dir: Path) -> tuple[dict, bool, dict]:
    """(summary, merged, provenance) of the extended recorder of one session for ``domain.fwdlog.state.health_state``.

    Native recorder (the ``ext_quote_snapshots`` version carries ``recorder_summary``): ``end`` = its end reason (``done`` when it finished),
    ``missed`` / ``failed_requests`` / ``error_count`` from the summary, ``blocked`` from ``blocked_hosts``; ``merged`` = the final version was
    published. It records no ``never_returned`` list: that key is absent, so the row says None, not 0. Legacy recorder: ``summary.json`` and
    ``quotes_ext.parquet`` of the old directory. Neither: an explicit ``note`` (never an all-empty health that looks like a healthy day).
    ``source`` says which one was read."""
    cov = (ext.manifest.get("coverage_document") or {}) if ext is not None else {}
    if NATIVE_EXT_MARKER in cov:
        rs = cov[NATIVE_EXT_MARKER] or {}
        end = cov.get("end_reason") or ("done" if cov.get("source_complete") else None)
        summary = {"end": end, "missed": rs.get("missed"), "failed_requests": rs.get("failed_requests"), "error_count": rs.get("error_count"),
                   "blocked": dict(cov.get("blocked_hosts") or {}), "source": "native_coverage"}
        if end is None:
            summary["note"] = "native recorder version is not final yet (no end reason)"
        body = json.dumps({k: cov.get(k) for k in ("recorder", NATIVE_EXT_MARKER, "blocked_hosts", "end_reason", "source_complete", "steps_done")}, sort_keys=True, default=str)
        return summary, bool(cov.get("source_complete")), {"health_source": "native_coverage", "ext_summary_sha256": hashlib.sha256(body.encode("utf-8")).hexdigest()}
    path = ext_dir / "summary.json"
    legacy = read_json(path)
    merged = (ext_dir / "quotes_ext.parquet").exists()
    if legacy and not legacy.get("unreadable"):
        return {**legacy, "source": "legacy_summary"}, merged, {"health_source": "legacy_summary", "ext_summary_sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
    if legacy.get("unreadable"):
        return {"unreadable": True, "source": "unreadable", "note": "legacy summary.json could not be read"}, merged, {"health_source": "unreadable", "ext_summary_sha256": None}
    return ({"source": "none", "note": "no recorder health record: no native coverage and no legacy summary.json for this session"}, merged,
            {"health_source": "none", "ext_summary_sha256": None})


def events_path() -> Path:
    return Path(os.environ.get(ENV_EVENTS) or DEFAULT_EVENTS)


def _aware(v: str | None) -> datetime | None:
    if v is None:
        return None
    t = datetime.fromisoformat(v.replace("Z", "+00:00"))
    return t if t.tzinfo else t.replace(tzinfo=CST)


def read_events(path: Path | None = None) -> tuple[list[BD.CalEvent], dict]:
    """Latest revision of every event of the old calendar store (read only) and a note of what was read (None events when the file is absent)."""
    path = Path(path or events_path())
    if not path.exists():
        return [], {"path_present": False, "events": 0, "sha256": None}
    raw = path.read_bytes()
    best: dict[str, tuple[int, dict]] = {}
    for line in raw.decode("utf-8").splitlines():
        if not line.strip():
            continue
        e = json.loads(line)
        cur = best.get(e["event_id"])
        if cur is None or e["revision"] > cur[0]:
            best[e["event_id"]] = (e["revision"], e)
    out = []
    for _, e in best.values():
        out.append(BD.CalEvent(e["event_id"], e["name"], e["category"], e["status"], date.fromisoformat(e["event_date"]), _aware(e["first_seen_at"]),
                               date.fromisoformat(e["event_end_date"]) if e.get("event_end_date") else None, _aware(e.get("event_at")), e.get("event_timezone"),
                               _aware(e.get("result_known_at")), frozenset(e.get("instruments") or ())))
    return out, {"path_present": True, "events": len(out), "sha256": hashlib.sha256(raw).hexdigest(),
                 "latest_first_seen_at": max((e.first_seen_at for e in out), default=None)}


# ---- day frames -----------------------------------------------------------------------------------------------------------------------
def d(v) -> Decimal | None:
    return None if v is None else (v if isinstance(v, Decimal) else Decimal(repr(v)) if isinstance(v, float) else Decimal(v))


@dataclass(frozen=True)
class DayFrames:
    day: date
    manifest: Mapping
    frame: pl.DataFrame                                # main-board rows of the day's dataset version
    complete: bool                                     # the old recorder wrote its done marker
    snapshot_id: object = None


QUOTE_COLUMNS = ("code", "name", "kind", "poll_at", "quote_at", "price", "prev_close", "open", "high", "limit_up", "bid1_px", "bid1_lots", "ask1_px", "vwap",
                 "volume_lots", "amount_yuan", "turnover", "float_cap", "volume_ratio")


def read_projected(lake, content_hash: str, columns: Sequence[str] | None, *, main_only_col: str | None = None) -> pl.DataFrame | None:
    """The members of one snapshot with only ``columns`` (and only main-board codes) in memory: a session has 1.4 M rows x 43 columns."""
    import io
    m = lake.read_manifest(content_hash)
    frames = []
    for x in m.members:
        if not x["path"].endswith(".parquet"):
            continue
        f = pl.read_parquet(io.BytesIO(lake.read_object(x)), columns=None if columns is None else [c for c in columns])
        if main_only_col:
            f = f.filter(pl.col(main_only_col).str.slice(0, 3).is_in(MAIN_PREFIX_LIST))
        frames.append(f)
    return pl.concat(frames, how="diagonal_relaxed", rechunk=False) if frames else None


def day_frames(live, dataset_id: str, day: date, cutoff: datetime | None, *, code_col: str = "code", main_only: bool = True,
               columns: Sequence[str] | None = QUOTE_COLUMNS) -> DayFrames | None:
    """The READY version of one session's dataset visible at ``cutoff`` (None when there is none)."""
    man = live.latest_ready(dataset_id, day, cutoff)
    if man is None:
        return None
    frame = read_projected(live.lake, man["content_hash"], columns if main_only else None, main_only_col=code_col if main_only else None)
    if frame is None:
        return None
    return DayFrames(day, man, frame, bool(man["coverage_document"].get("source_complete")), man["snapshot_id"])


def at_utc(day: date, t: time) -> datetime:
    return datetime.combine(day, t, tzinfo=CST).astimezone(UTC)


def snapshots(frame: pl.DataFrame) -> pl.DataFrame:
    """Main-board full / close polls (the old ``snapshots``)."""
    return frame.filter(pl.col("kind").is_in(["full", "close"])).select("code", "name", "kind", "poll_at", "price", "prev_close", "open", "limit_up", "turnover", "float_cap", "volume_ratio")


def first_in_window(snaps: pl.DataFrame, day: date, t: time, kinds: Sequence[str] = ("full",), slack_min: int = 3) -> dict[str, TL.Quote]:
    """Per code, the first snapshot of ``kinds`` polled in [t, t + slack] (the old ``at``: the slack ends at most at minute 59)."""
    lo, hi = at_utc(day, t), at_utc(day, time(t.hour, min(59, t.minute + slack_min)))
    w = snaps.filter(pl.col("kind").is_in(list(kinds)) & (pl.col("poll_at") >= lo) & (pl.col("poll_at") <= hi))
    w = w.sort(["code", "poll_at"], maintain_order=True).group_by("code", maintain_order=True).first()
    return {r["code"]: quote(r) for r in w.iter_rows(named=True)}


def quote(r: Mapping) -> TL.Quote:
    return TL.Quote(r["code"], r.get("name"), d(r.get("price")), d(r.get("prev_close")), d(r.get("open")), d(r.get("limit_up")), d(r.get("turnover")),
                    d(r.get("float_cap")), d(r.get("volume_ratio")))


def closing_quotes(snaps: pl.DataFrame, day: date) -> dict[str, TL.Quote]:
    """Closing snapshots: the first ``close`` poll 15:01-15:06, else the first ``full`` poll 15:00-15:02 (the old ``closing``)."""
    c = first_in_window(snaps, day, time(15, 1), ("close",), 5)
    return c if c else first_in_window(snaps, day, time(15, 0), ("full",), 2)


# ---- extended intraday ------------------------------------------------------------------------------------------------------------------
def ext_rows(frame: pl.DataFrame) -> list[ST.ExtRow]:
    cols = [c for c in ("symbol", "kind", "poll_at", "quote_at", "price", "prev_close", "amount", "volume", "open_interest", "open") if c in frame.columns]
    out = []
    for r in frame.select(cols).iter_rows(named=True):
        out.append(ST.ExtRow(r["symbol"], r["kind"], r["poll_at"], r.get("quote_at"), d(r.get("price")), d(r.get("prev_close")), d(r.get("amount")),
                             d(r.get("volume")), d(r.get("open_interest")), d(r.get("open"))))
    return out


def index_quotes(rows: Sequence[ST.ExtRow], symbol: str, day: date) -> tuple[dict, dict, dict]:
    """The index series at 10:00, 14:30 and the close for E078 (every kind counts; the close is the LAST row polled 15:00-15:05)."""
    sel = [r for r in rows if r.symbol == symbol]

    def mk(r: ST.ExtRow) -> TL.Quote:
        return TL.Quote(symbol, None, r.price, r.prev_close, r.open)

    def first(t: time) -> dict[str, TL.Quote]:
        lo, hi = at_utc(day, t), at_utc(day, time(t.hour, min(59, t.minute + 3)))
        c = [(r.poll_at, i) for i, r in enumerate(sel) if lo <= r.poll_at <= hi]
        return {symbol: mk(sel[min(c)[1]])} if c else {}

    lo, hi = at_utc(day, time(15, 0)), at_utc(day, time(15, 5))
    last = [(r.poll_at, i) for i, r in enumerate(sel) if lo <= r.poll_at <= hi]
    return first(time(10, 0)), first(time(14, 30)), ({symbol: mk(sel[max(last)[1]])} if last else {})
