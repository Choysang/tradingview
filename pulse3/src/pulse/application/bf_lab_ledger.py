"""Backfill lane ``lab``, part 1: the old experiment ledger as read-only history, and the retired-tool lookup (LEGACY_TRIAGE #177 #178).

Why: the old lab recorded 92 experiments (verdict, why it failed, when to try again). pulse3's ``research.experiment`` trials are a different
thing: they are registered BEFORE they run. The old ledger is history, so it is stored apart, read-only, and linked from the experiment archive.
Storage (no schema change): one immutable ``ops.ext_artifact`` per ledger entry (``lab-ledger-entry-v1``) and one per import
(``lab-ledger-import-v1``, listing the entry refs). Artifacts are content addressed and append-only, so an import cannot be edited afterwards.
Invariants:
  * the source is the frozen file ``lab_data/experiments.jsonl`` (byte-exact copy; ``-text`` in .gitattributes) whose sha256 is pinned in
    ``lab_data/ledger_manifest.json``; an import REFUSES a file whose hash differs from the manifest (``LedgerHashMismatch``), and refuses a
    manifest label that was already imported under another hash;
  * the import is idempotent (same bytes, same refs, nothing is written twice) and all-or-nothing (one transaction);
  * a newer ledger is a new manifest entry and a new import with its own hash; the old import stays and stays readable (``ledger_sha256``);
  * entries are stored verbatim (every key the old ledger had, including ``caveat``); the read adds ``superseded_by`` and the retired families;
  * nothing here writes to ``research.experiment*``.
Not covered: editing or annotating entries, parsing ``results`` into numbers (they are the old lab's own sentences, shown as text).
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import psycopg

from pulse import contracts
from pulse.application import ops_service as OPS

CONTRACT_ID = contracts.backfill_id("lab")
SCHEMA_VERSION = "1.0.0"
ENTRY_KIND = "lab-ledger-entry"
ENTRY_VERSION = "lab-ledger-entry-v1"
IMPORT_KIND = "lab-ledger-import"
IMPORT_VERSION = "lab-ledger-import-v1"
DATA_DIR = Path(__file__).resolve().parent.parent / "lab_data"
VERDICTS = ("works", "mixed", "inconclusive", "fails")
REQUIRED_KEYS = ("id", "date", "round", "track", "name", "hypothesis", "config", "results", "verdict", "why", "regime", "reactivate_when", "artifacts")
MAX_LIMIT = 200


class LedgerError(Exception):
    """A read problem the router maps to an HTTP error."""

    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


class LedgerHashMismatch(RuntimeError):
    """The ledger file on disk is not the file whose hash the manifest pins."""


@dataclass(frozen=True)
class ImportResult:
    label: str
    ledger_sha256: str
    entry_count: int
    import_ref: str
    created: bool                       # False = the same import already existed (idempotent re-run)


# --------------------------------------------------------------------------------------------------------
# manifest + frozen data
# --------------------------------------------------------------------------------------------------------
def load_manifest(data_dir: Path | None = None) -> dict[str, Any]:
    return json.loads(((data_dir or DATA_DIR) / "ledger_manifest.json").read_text(encoding="utf-8"))


def load_retired(data_dir: Path | None = None) -> dict[str, Any]:
    return json.loads(((data_dir or DATA_DIR) / "retired_tools.json").read_text(encoding="utf-8"))


def parse_ledger(raw: bytes) -> list[dict[str, Any]]:
    """The entries of a ledger file, validated: one JSON object per line, unique ids, the required keys, a known verdict."""
    entries: list[dict[str, Any]] = []
    for n, line in enumerate(raw.decode("utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        e = json.loads(line)
        missing = [k for k in REQUIRED_KEYS if k not in e]
        if missing:
            raise LedgerHashMismatch(f"ledger line {n}: missing keys {missing}")
        if e["verdict"] not in VERDICTS:
            raise LedgerHashMismatch(f"ledger line {n} ({e['id']}): unknown verdict {e['verdict']!r}")
        entries.append(e)
    ids = [e["id"] for e in entries]
    if len(set(ids)) != len(ids):
        raise LedgerHashMismatch("ledger has duplicate entry ids")
    return entries


# --------------------------------------------------------------------------------------------------------
# import (CLI: ``python -m pulse.workers.lab import-ledger``)
# --------------------------------------------------------------------------------------------------------
def import_ledger(conn: psycopg.Connection, *, data_dir: Path | None = None) -> list[ImportResult]:
    """Import every version the manifest lists. Idempotent; raises ``LedgerHashMismatch`` before writing anything when a file does not match."""
    root = data_dir or DATA_DIR
    manifest = load_manifest(root)
    prepared: list[tuple[dict[str, Any], str, list[dict[str, Any]]]] = []
    labels = [v["label"] for v in manifest["versions"]]
    if len(set(labels)) != len(labels):
        raise LedgerHashMismatch("ledger manifest repeats a label")
    for v in manifest["versions"]:
        raw = (root / v["file"]).read_bytes()
        digest = hashlib.sha256(raw).hexdigest()
        if digest != v["sha256"]:
            raise LedgerHashMismatch(f"{v['file']} (label {v['label']}): sha256 {digest} is not the pinned {v['sha256']}; a changed ledger is a new "
                                     "manifest entry with a new file, never an edit of an imported one")
        entries = parse_ledger(raw)
        if len(entries) != v["entries"]:
            raise LedgerHashMismatch(f"{v['file']}: {len(entries)} entries, the manifest says {v['entries']}")
        prepared.append((v, digest, entries))
    out: list[ImportResult] = []
    with conn.transaction():
        for v, digest, entries in prepared:
            clash = conn.execute("SELECT body->>'ledger_sha256' AS h FROM ops.ext_artifact WHERE contract_id=%s AND body->>'version'=%s"
                                 " AND body->>'label'=%s AND body->>'ledger_sha256' <> %s", (CONTRACT_ID, IMPORT_VERSION, v["label"], digest)).fetchone()
            if clash is not None:
                raise LedgerHashMismatch(f"label {v['label']} was imported with hash {clash['h']}; the file now hashes to {digest}")
            refs = []
            for seq, e in enumerate(entries, start=1):
                art = OPS.put_artifact(conn, kind=ENTRY_KIND, contract_id=CONTRACT_ID, schema_version=SCHEMA_VERSION,
                                       body={"version": ENTRY_VERSION, "ledger_sha256": digest, "seq": seq, "entry": e})
                refs.append({"id": e["id"], "ref": art["ref"]})
            existed = conn.execute("SELECT 1 FROM ops.ext_artifact WHERE contract_id=%s AND body->>'version'=%s AND body->>'ledger_sha256'=%s",
                                   (CONTRACT_ID, IMPORT_VERSION, digest)).fetchone() is not None
            art = OPS.put_artifact(conn, kind=IMPORT_KIND, contract_id=CONTRACT_ID, schema_version=SCHEMA_VERSION,
                                   body={"version": IMPORT_VERSION, "label": v["label"], "source_file": v["source"], "ledger_sha256": digest,
                                         "entry_count": len(entries), "entries": refs})
            out.append(ImportResult(v["label"], digest, len(entries), art["ref"], not existed))
    return out


# --------------------------------------------------------------------------------------------------------
# reads
# --------------------------------------------------------------------------------------------------------
def _info(row: dict[str, Any], latest_sha: str) -> dict[str, Any]:
    b = row["body"]
    return {"ledger_sha256": b["ledger_sha256"], "label": b["label"], "entry_count": b["entry_count"], "imported_at": row["created_at"],
            "import_ref": row["ref"], "is_latest": b["ledger_sha256"] == latest_sha, "source_file": b["source_file"]}


def list_imports(conn: psycopg.Connection) -> list[dict[str, Any]]:
    """Imports, oldest first (the last one is the current ledger)."""
    rows = conn.execute("SELECT ref, body, created_at FROM ops.ext_artifact WHERE contract_id=%s AND body->>'version'=%s ORDER BY created_at, ref",
                        (CONTRACT_ID, IMPORT_VERSION)).fetchall()
    latest = rows[-1]["body"]["ledger_sha256"] if rows else ""
    return [_info(r, latest) for r in rows]


def not_imported(conn: psycopg.Connection) -> list[dict[str, str]]:
    done = {i["ledger_sha256"] for i in list_imports(conn)}
    return [{"label": v["label"], "file": v["file"], "sha256": v["sha256"]} for v in load_manifest()["versions"] if v["sha256"] not in done]


def _retired_index() -> dict[str, list[str]]:
    idx: dict[str, list[str]] = {}
    for f in load_retired()["families"]:
        for eid in f["ledger_ids"]:
            idx.setdefault(eid, []).append(f["family_id"])
    return idx


def load_entries(conn: psycopg.Connection, ledger_sha256: str | None = None) -> tuple[dict[str, Any], list[dict[str, Any]]] | None:
    """(import info, entries as served) of the import with ``ledger_sha256`` (default: the latest), or None when nothing is imported.
    Entries are in id order, each with ``superseded_by`` and ``retired_families`` added; absent optional keys are normalised (``supersedes`` [], ``caveat`` null)."""
    imports = list_imports(conn)
    if not imports:
        return None
    if ledger_sha256 is None:
        info = imports[-1]
    else:
        info = next((i for i in imports if i["ledger_sha256"] == ledger_sha256), None)
        if info is None:
            raise LedgerError(404, "NOT_FOUND", "没有这个版本的台账导入")
    imp = conn.execute("SELECT body FROM ops.ext_artifact WHERE ref=%s", (info["import_ref"],)).fetchone()["body"]
    refs = [e["ref"] for e in imp["entries"]]
    rows = conn.execute("SELECT body FROM ops.ext_artifact WHERE ref = ANY(%s)", (refs,)).fetchall()
    raw = sorted((r["body"]["entry"] for r in rows), key=lambda e: e["id"])
    if len(raw) != info["entry_count"]:
        raise LedgerError(500, "INTERNAL", "台账导入记录与条目数量不一致")
    by_super: dict[str, list[str]] = {}
    for e in raw:
        for old in e.get("supersedes") or []:
            by_super.setdefault(old, []).append(e["id"])
    retired = _retired_index()
    served = [{**{k: e[k] for k in REQUIRED_KEYS}, "supersedes": list(e.get("supersedes") or []), "superseded_by": sorted(by_super.get(e["id"], [])),
               "caveat": e.get("caveat"), "retired_families": retired.get(e["id"], [])} for e in raw]
    return info, served


def _haystack(e: dict[str, Any]) -> str:
    parts = [e["id"], e["track"], e["round"], e["name"], e["hypothesis"], e["config"], e["why"], e["regime"], e["reactivate_when"], e["caveat"] or "",
             *e["results"].keys(), *e["results"].values(), *e["artifacts"]]
    return "\n".join(parts).casefold()


def ledger_list(conn: psycopg.Connection, *, ledger_sha256: str | None, verdict: str | None, track: str | None, rnd: str | None, q: str | None,
                offset: int, limit: int) -> dict[str, Any]:
    if verdict is not None and verdict not in VERDICTS:
        raise LedgerError(400, "INVALID_CONTRACT", f"verdict 必须是 {', '.join(VERDICTS)}")
    if not 1 <= limit <= MAX_LIMIT or offset < 0:
        raise LedgerError(400, "INVALID_CONTRACT", f"limit 必须在 1 到 {MAX_LIMIT} 之间，offset 不能为负")
    loaded = load_entries(conn, ledger_sha256)
    note = "这是旧实验室留下的历史记录，只读；它们不是 pulse3 的实验（pulse3 的试验先登记、后运行）。"
    if loaded is None:
        return {"ledger": None, "empty_reason": "NOT_IMPORTED：先运行 uv run python -m pulse.workers.lab import-ledger", "counts": None, "matched": 0,
                "offset": offset, "limit": limit, "next_offset": None, "entries": [], "note": note}
    info, entries = loaded
    counts = {"total": len(entries), "by_verdict": {v: sum(1 for e in entries if e["verdict"] == v) for v in VERDICTS},
              "by_track": {t: sum(1 for e in entries if e["track"] == t) for t in sorted({e["track"] for e in entries})}}
    terms = [t.casefold() for t in (q or "").split()]
    hit = [e for e in entries if (verdict is None or e["verdict"] == verdict) and (track is None or e["track"] == track) and (rnd is None or e["round"] == rnd)
           and all(t in _haystack(e) for t in terms)]
    page = hit[offset:offset + limit]
    return {"ledger": info, "empty_reason": None, "counts": counts, "matched": len(hit), "offset": offset, "limit": limit,
            "next_offset": offset + limit if offset + limit < len(hit) else None, "entries": page, "note": note}


def _brief(e: dict[str, Any] | None, eid: str) -> dict[str, Any]:
    return {"id": eid, "name": None if e is None else e["name"], "verdict": None if e is None else e["verdict"]}


def _family_view(f: dict[str, Any], by_id: dict[str, dict[str, Any]]) -> dict[str, Any]:
    return {**f, "entries": [_brief(by_id.get(i), i) for i in f["ledger_ids"]]}


def ledger_entry(conn: psycopg.Connection, entry_id: str, ledger_sha256: str | None) -> dict[str, Any]:
    loaded = load_entries(conn, ledger_sha256)
    if loaded is None:
        raise LedgerError(404, "NOT_FOUND", "台账还没有导入：uv run python -m pulse.workers.lab import-ledger")
    info, entries = loaded
    by_id = {e["id"]: e for e in entries}
    e = by_id.get(entry_id)
    if e is None:
        raise LedgerError(404, "NOT_FOUND", f"台账里没有 {entry_id}")
    fams = {f["family_id"]: f for f in load_retired()["families"]}
    return {"ledger": info, "entry": e, "supersedes_entries": [_brief(by_id.get(i), i) for i in e["supersedes"]],
            "superseded_by_entries": [_brief(by_id.get(i), i) for i in e["superseded_by"]],
            "retired_families": [_family_view(fams[f], by_id) for f in e["retired_families"]]}


def retired_tools(conn: psycopg.Connection, *, tool: str | None, ledger_id: str | None) -> dict[str, Any]:
    """The retired-tool mapping (family -> ledger ids -> reason). ``tool`` matches a tool or script name (case-insensitive, substring), ``ledger_id``
    a family that lists the id. Both given = both must match. Entries are resolved against the latest import (null names when nothing is imported)."""
    data = load_retired()
    loaded = load_entries(conn, None)
    by_id = {e["id"]: e for e in loaded[1]} if loaded else {}
    fams = data["families"]
    if tool:
        t = tool.casefold()
        fams = [f for f in fams if any(t in x.casefold() for x in [*f["tools"], f["family_id"], f["name"]])]
    if ledger_id:
        fams = [f for f in fams if ledger_id in f["ledger_ids"] or ledger_id in f["also_see"]]
    return {"version": data["version"], "source": data["source"], "ledger": None if loaded is None else loaded[0],
            "query": {"tool": tool or None, "ledger_id": ledger_id or None}, "families": [_family_view(f, by_id) for f in fams]}
