"""Experiment ledger import (LEGACY_TRIAGE #177 / #178) on real PostgreSQL: 92 of 92 entries, byte-exact source hash, idempotent, refuses a changed
file, a new version is a new import and the old one stays, nothing leaks into research.experiment*, retired-tool mapping matches the triage table."""
from __future__ import annotations

import hashlib
import json
import re
import shutil
from pathlib import Path

import pytest

from pulse.application import bf_lab_ledger as LED

pytestmark = pytest.mark.pg
REPO_ROOT = Path(__file__).resolve().parents[3]
SOURCE = REPO_ROOT / "docs" / "research" / "ledger" / "experiments.jsonl"
TRIAGE = Path(__file__).resolve().parents[2] / "docs" / "LEGACY_TRIAGE.md"
FROZEN = LED.DATA_DIR / "experiments.jsonl"
PINNED = "b493b767ded4fc7a1a7fff9ce0420b68511f94120c6d1f9a4f2bf57e73f170e1"


def n_artifacts(conn) -> int:
    return conn.execute("select count(*) n from ops.ext_artifact where contract_id=%s", (LED.CONTRACT_ID,)).fetchone()["n"]


def copy_data(tmp_path: Path) -> Path:
    d = tmp_path / "lab_data"
    shutil.copytree(LED.DATA_DIR, d, ignore=shutil.ignore_patterns("__pycache__"))
    return d


# ---- frozen data ----------------------------------------------------------------------------------------------------------------------
def test_frozen_file_is_the_pinned_byte_exact_copy_of_the_old_ledger():
    raw = FROZEN.read_bytes()
    assert hashlib.sha256(raw).hexdigest() == PINNED == LED.load_manifest()["versions"][0]["sha256"]
    assert b"\r" not in raw                                       # git must not have rewritten the line endings (-text in .gitattributes)
    if SOURCE.exists():                                           # the ledger is append-only: the frozen copy must be its exact prefix
        assert SOURCE.read_bytes().startswith(raw), "the frozen copy is not a prefix of docs/research/ledger/experiments.jsonl: an old entry changed"
    entries = LED.parse_ledger(raw)
    assert len(entries) == 92 == LED.load_manifest()["versions"][0]["entries"]
    verdicts = [e["verdict"] for e in entries]
    assert {v: verdicts.count(v) for v in LED.VERDICTS} == {"works": 16, "mixed": 21, "inconclusive": 27, "fails": 28}


def test_gitattributes_keeps_the_ledger_out_of_line_ending_conversion():
    assert "experiments.jsonl -text" in (LED.DATA_DIR / ".gitattributes").read_text(encoding="utf-8")


# ---- import ----------------------------------------------------------------------------------------------------------------------------
def test_import_stores_all_92_entries_verbatim_and_is_idempotent(fresh):
    conn, _ = fresh
    before_runs = conn.execute("select (select count(*) from research.experiment_run) a, (select count(*) from research.evaluation_context) b").fetchone()
    first = LED.import_ledger(conn)
    assert len(first) == 1 and first[0].entry_count == 92 and first[0].ledger_sha256 == PINNED and first[0].created is True
    stored = n_artifacts(conn)
    assert stored == 93                                           # 92 entries + 1 import record
    again = LED.import_ledger(conn)
    assert again[0].created is False and again[0].import_ref == first[0].import_ref and n_artifacts(conn) == stored
    info, entries = LED.load_entries(conn)
    assert info["ledger_sha256"] == PINNED and info["entry_count"] == 92 and info["is_latest"] is True and len(entries) == 92
    source = {e["id"]: e for e in LED.parse_ledger(FROZEN.read_bytes())}
    for e in entries:                                             # every field of the old entry survives, unchanged
        raw = source[e["id"]]
        assert {k: e[k] for k in LED.REQUIRED_KEYS} == {k: raw[k] for k in LED.REQUIRED_KEYS}
        assert e["supersedes"] == (raw.get("supersedes") or []) and e["caveat"] == raw.get("caveat")
    assert [e["id"] for e in entries] == sorted(source)
    after_runs = conn.execute("select (select count(*) from research.experiment_run) a, (select count(*) from research.evaluation_context) b").fetchone()
    assert after_runs == before_runs                              # the history is not mixed into pulse3's own trials


def test_a_changed_file_is_refused_before_anything_is_written(fresh, tmp_path):
    conn, _ = fresh
    d = copy_data(tmp_path)
    raw = (d / "experiments.jsonl").read_bytes()
    (d / "experiments.jsonl").write_bytes(raw.replace("E001".encode(), "E0O1".encode(), 1))
    with pytest.raises(LED.LedgerHashMismatch, match="sha256"):
        LED.import_ledger(conn, data_dir=d)
    assert n_artifacts(conn) == 0
    (d / "experiments.jsonl").write_bytes(raw + b"\n")            # even a trailing newline is a different file
    with pytest.raises(LED.LedgerHashMismatch):
        LED.import_ledger(conn, data_dir=d)
    assert n_artifacts(conn) == 0


def test_entry_count_and_verdict_vocabulary_are_checked(fresh, tmp_path):
    conn, _ = fresh
    d = copy_data(tmp_path)
    raw = (d / "experiments.jsonl").read_bytes().replace(b'"verdict": "works"', b'"verdict": "great"', 1)
    (d / "experiments.jsonl").write_bytes(raw)
    man = json.loads((d / "ledger_manifest.json").read_text(encoding="utf-8"))
    man["versions"][0]["sha256"] = hashlib.sha256(raw).hexdigest()
    (d / "ledger_manifest.json").write_text(json.dumps(man), encoding="utf-8")
    with pytest.raises(LED.LedgerHashMismatch, match="unknown verdict"):
        LED.import_ledger(conn, data_dir=d)
    assert n_artifacts(conn) == 0


def test_a_new_ledger_version_is_a_new_import_and_the_old_one_stays(fresh, tmp_path):
    conn, _ = fresh
    LED.import_ledger(conn)
    d = copy_data(tmp_path)
    extra = {"id": "E093", "date": "2026-11-20", "round": "9", "track": "llm", "name": "新条目", "hypothesis": "h", "config": "c", "results": {"k": "v"},
             "verdict": "fails", "why": "w", "regime": "r", "reactivate_when": "never", "artifacts": [], "supersedes": ["E001"]}
    new_raw = (d / "experiments.jsonl").read_bytes() + (json.dumps(extra, ensure_ascii=False) + "\n").encode("utf-8")
    (d / "experiments-2.jsonl").write_bytes(new_raw)
    man = json.loads((d / "ledger_manifest.json").read_text(encoding="utf-8"))
    man["versions"].append({"label": "next", "file": "experiments-2.jsonl", "sha256": hashlib.sha256(new_raw).hexdigest(), "entries": 93, "source": "later"})
    (d / "ledger_manifest.json").write_text(json.dumps(man), encoding="utf-8")
    res = LED.import_ledger(conn, data_dir=d)
    assert [(r.label, r.entry_count, r.created) for r in res] == [("2026-11-09", 92, False), ("next", 93, True)]
    imports = LED.list_imports(conn)
    assert [i["entry_count"] for i in imports] == [92, 93] and [i["is_latest"] for i in imports] == [False, True]
    _, latest = LED.load_entries(conn)
    assert len(latest) == 93 and next(e for e in latest if e["id"] == "E001")["superseded_by"] == ["E025", "E093"]
    _, old = LED.load_entries(conn, PINNED)
    assert len(old) == 92 and next(e for e in old if e["id"] == "E001")["superseded_by"] == ["E025"]
    with pytest.raises(LED.LedgerError) as ei:
        LED.load_entries(conn, "0" * 64)
    assert ei.value.status == 404


def test_a_label_cannot_be_reused_for_a_different_file(fresh, tmp_path):
    conn, _ = fresh
    LED.import_ledger(conn)
    d = copy_data(tmp_path)
    raw = (d / "experiments.jsonl").read_bytes().replace("inconclusive".encode(), "mixed".encode(), 1)
    (d / "experiments.jsonl").write_bytes(raw)
    man = json.loads((d / "ledger_manifest.json").read_text(encoding="utf-8"))
    man["versions"][0]["sha256"] = hashlib.sha256(raw).hexdigest()          # a "consistent" manifest edit under the same label
    (d / "ledger_manifest.json").write_text(json.dumps(man), encoding="utf-8")
    with pytest.raises(LED.LedgerHashMismatch, match="was imported with hash"):
        LED.import_ledger(conn, data_dir=d)
    assert len(LED.list_imports(conn)) == 1


# ---- reads -------------------------------------------------------------------------------------------------------------------------
def test_list_filters_search_paging_and_counts(fresh):
    conn, _ = fresh
    empty = LED.ledger_list(conn, ledger_sha256=None, verdict=None, track=None, rnd=None, q=None, offset=0, limit=10)
    assert empty["ledger"] is None and empty["entries"] == [] and empty["empty_reason"].startswith("NOT_IMPORTED")
    LED.import_ledger(conn)
    lst = lambda **kw: LED.ledger_list(conn, **{"ledger_sha256": None, "verdict": None, "track": None, "rnd": None, "q": None, "offset": 0, "limit": 200, **kw})  # noqa: E731
    allr = lst()
    assert allr["matched"] == 92 and allr["counts"]["by_verdict"] == {"works": 16, "mixed": 21, "inconclusive": 27, "fails": 28}
    assert sum(allr["counts"]["by_track"].values()) == 92 and allr["counts"]["by_track"]["timing"] == 15
    assert lst(verdict="works")["matched"] == 16 and all(e["verdict"] == "works" for e in lst(verdict="works")["entries"])
    assert lst(track="timing")["matched"] == 15 and lst(verdict="fails", track="timing")["matched"] == sum(1 for e in lst(track="timing")["entries"] if e["verdict"] == "fails")
    one = lst(q="E044")["entries"]
    assert [e["id"] for e in one][0] == "E044"                                  # id match; other entries may quote it, the id itself is found
    assert {e["id"] for e in lst(q="净值均线 降仓")["entries"]} >= {"E036", "E040"}      # all terms must match, anywhere in the entry
    assert lst(q="no-such-phrase-xyz")["matched"] == 0
    assert lst(q="TRAD_BATTERY")["matched"] >= 1                                # case-insensitive, also searches the artifact paths
    p1, p2 = lst(limit=40), lst(limit=40, offset=40)
    assert p1["next_offset"] == 40 and p2["next_offset"] == 80 and lst(limit=40, offset=80)["next_offset"] is None
    assert [e["id"] for e in p1["entries"] + p2["entries"]] == [e["id"] for e in allr["entries"][:80]]
    for bad in ({"verdict": "great"}, {"limit": 0}, {"limit": 201}, {"offset": -1}):
        with pytest.raises(LED.LedgerError) as ei:
            lst(**bad)
        assert ei.value.status == 400


def test_entry_detail_links_supersession_and_retired_families(fresh):
    conn, _ = fresh
    LED.import_ledger(conn)
    d = LED.ledger_entry(conn, "E022", None)
    assert d["entry"]["supersedes"] == ["E019"] and [b["id"] for b in d["supersedes_entries"]] == ["E019"]
    assert d["supersedes_entries"][0]["name"] and d["supersedes_entries"][0]["verdict"] in LED.VERDICTS
    assert "E022" in [b["id"] for b in LED.ledger_entry(conn, "E019", None)["superseded_by_entries"]]
    e3 = LED.ledger_entry(conn, "E003", None)
    assert [f["family_id"] for f in e3["retired_families"]] == ["traditional_batteries"] and e3["entry"]["retired_families"] == ["traditional_batteries"]
    assert {b["id"] for b in e3["retired_families"][0]["entries"]} == {"E003", "E033", "E058", "E060"}
    assert LED.ledger_entry(conn, "E004", None)["retired_families"] == []       # a working factor study is not a retired tool
    with pytest.raises(LED.LedgerError) as ei:
        LED.ledger_entry(conn, "E999", None)
    assert ei.value.status == 404


def test_entries_keep_their_caveat(fresh):
    conn, _ = fresh
    LED.import_ledger(conn)
    _, entries = LED.load_entries(conn)
    assert {e["id"] for e in entries if e["caveat"]} == {"E048", "E049", "E050"}


# ---- retired tools -----------------------------------------------------------------------------------------------------------------
def triage_families() -> list[tuple[str, set[str]]]:
    text = TRIAGE.read_text(encoding="utf-8")
    block = text.split("Retired research tools", 1)[1].split("\n## ", 1)[0]
    rows = [ln for ln in block.splitlines() if ln.startswith("|") and not ln.startswith("|---") and not ln.startswith("| Family")]
    return [(r.split("|")[1].strip(), set(re.findall(r"E\d{3}", r.split("|")[2]))) for r in rows]


def test_retired_mapping_equals_the_triage_table_and_points_at_real_entries():
    if not TRIAGE.exists():
        pytest.skip("docs/LEGACY_TRIAGE.md not found")
    data = LED.load_retired()["families"]
    tri = triage_families()
    assert len(tri) == len(data) == 10
    for (fam_text, ids), fam in zip(tri, data):
        assert ids == set(fam["ledger_ids"]), fam["family_id"]
        assert fam["tools"][0].split()[0].casefold().rstrip("*_") in fam_text.casefold(), fam["family_id"]     # same family, same first tool
    ledger_ids = {e["id"] for e in LED.parse_ledger(FROZEN.read_bytes())}
    for f in data:
        assert set(f["ledger_ids"]) <= ledger_ids and set(f["also_see"]) <= ledger_ids and f["reason"] and f["reason_zh"] and f["tools"]
    assert len({f["family_id"] for f in data}) == 10
    used = [i for f in data for i in f["ledger_ids"]]
    assert len(used) == len(set(used)) == 4 + 4 + 5 + 3 + 2 + 1 + 3 + 5 + 3 + 7   # no ledger id sits in two retired families


def test_retired_lookup_by_tool_and_by_ledger_id(fresh):
    conn, _ = fresh
    nothing_imported = LED.retired_tools(conn, tool="rl_fqi", ledger_id=None)
    assert nothing_imported["ledger"] is None and nothing_imported["families"][0]["entries"] == [{"id": "E015", "name": None, "verdict": None}]
    LED.import_ledger(conn)
    r = LED.retired_tools(conn, tool="RL_FQI", ledger_id=None)
    assert [f["family_id"] for f in r["families"]] == ["stock_level_rl"] and r["families"][0]["entries"][0]["verdict"] == "fails" and r["ledger"]["entry_count"] == 92
    assert [f["family_id"] for f in LED.retired_tools(conn, tool="trad_battery", ledger_id=None)["families"]] == ["traditional_batteries"]
    assert [f["family_id"] for f in LED.retired_tools(conn, tool=None, ledger_id="E072")["families"]] == ["us_overnight"]
    assert [f["family_id"] for f in LED.retired_tools(conn, tool=None, ledger_id="E062")["families"]] == ["traditional_batteries"]      # also_see
    assert LED.retired_tools(conn, tool="rl_fqi", ledger_id="E072")["families"] == []
    assert LED.retired_tools(conn, tool="unknown_tool", ledger_id=None)["families"] == []
    assert len(LED.retired_tools(conn, tool=None, ledger_id=None)["families"]) == 10
