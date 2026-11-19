"""Frozen inputs of the lane: sha256 in the manifest, refusal of an edited file, the rule table and the registry specs agree with the data folder."""
from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path

import pytest

from pulse.application import bf_fwdlog_inputs as IN
from pulse.application import bf_fwdlog_registry as REG

DATA = Path(IN.DATA_DIR)


def test_manifest_hashes_every_frozen_file():
    manifest = json.loads((DATA / "manifest.json").read_text(encoding="utf-8"))
    on_disk = {p.relative_to(DATA).as_posix() for p in DATA.rglob("*") if p.is_file() and p.name != "manifest.json"}
    assert set(manifest["files"]) == on_disk                                           # nothing frozen is unlisted, nothing listed is gone
    for rel, want in manifest["files"].items():
        assert IN.file_sha256(DATA / rel) == want and len(want) == 64


def test_text_hash_ignores_the_checkout_line_ending(tmp_path):
    a, b = tmp_path / "a.md", tmp_path / "b.md"
    a.write_bytes("第一行\n第二行\n".encode("utf-8"))
    b.write_bytes("第一行\r\n第二行\r\n".encode("utf-8"))
    assert IN.file_sha256(a) == IN.file_sha256(b) == hashlib.sha256("第一行\n第二行\n".encode("utf-8")).hexdigest()
    p1, p2 = tmp_path / "x.parquet", tmp_path / "y.parquet"
    p1.write_bytes(b"a\r\nb")
    p2.write_bytes(b"a\nb")
    assert IN.file_sha256(p1) != IN.file_sha256(p2)                                    # binary files are hashed as they are


def test_an_edited_frozen_file_is_refused(tmp_path):
    root = tmp_path / "data"
    shutil.copytree(DATA, root)
    assert IN.load_frozen(root).link_version == "event-links-v1"
    (root / "etf_amount_seed.csv").write_text("date,amount\n2026-11-09,1\n", encoding="utf-8")
    with pytest.raises(IN.FrozenDataChanged, match="etf_amount_seed.csv"):
        IN.load_frozen(root)


def test_frozen_content_matches_the_old_inputs():
    f = IN.load_frozen()
    assert len(f.link_rules) == 16 and f.skip_categories == ("macro_release", "market_rule")
    assert f.sw.height == 2658 and len(f.seed_sums) == 25 and max(f.seed_sums) == "2026-11-09"
    members = f.members(__import__("datetime").date(2026, 11, 12))
    assert "股份制银行Ⅱ" in members and "000001" in members["股份制银行Ⅱ"]
    assert "000001" not in f.members(__import__("datetime").date(2021, 1, 1)).get("股份制银行Ⅱ", frozenset())      # point in time: joined 2021-07-30


def test_registry_specs_point_at_frozen_documents_and_ledger_entries():
    f = IN.load_frozen()
    led = REG.ledger()
    assert [e.key for e in REG.EXPERIMENTS] == ["E077", "E078", "E089", "PAPER"]
    for e in REG.EXPERIMENTS:
        assert all(rel in f.files for rel in e.prereg) and all(i in led for i in e.ledger_ids)
        assert len(REG.implementation_digest(e, f)) == 64 and e.registered_on < __import__("datetime").date(2026, 11, 10)
    assert led["E077"]["config"].startswith("服务器 60 秒全市场快照") and "第 40 个交易日" in led["E077"]["reactivate_when"]
    assert "第 60 个交易日" in led["E078"]["reactivate_when"] and "10 段" in led["E089"]["reactivate_when"] and "20 个录制日" in led["E056"]["reactivate_when"]
