"""The packaged pre-registrations: each experiment's check fails closed when its file is missing or differs, and the shipped copies are the frozen documents."""
from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from pulse.application import bf_llm_live_service as SV
from pulse.application import frozen_specs as FS
from pulse.domain.llm_live import experiments as X

E092, E100 = X.E092, X.E100


@pytest.mark.parametrize("exp", [E092, E100], ids=lambda e: e.key)
def test_the_shipped_copy_matches_the_pinned_hash(exp):
    assert (FS.SPECS_DIR / exp.spec_file).is_file()
    assert SV.check_document(exp) == exp.spec_sha256


def test_the_pinned_hashes_are_the_ones_the_documents_froze():
    assert E092.spec_sha256 == "59db7d208f37299874eea5eaa0adbe104d2af6753d792b5f2cd3dfc60b1c9d1f"
    assert E100.spec_sha256 == "b8eba28e6ed09a5cba59a34348ccd8536d2759f1c02c242779277ae855bc9139"
    assert E092.spec_sha256 != E100.spec_sha256 and E092.spec_file != E100.spec_file


@pytest.mark.parametrize("exp", [E092, E100], ids=lambda e: e.key)
def test_the_shipped_copy_is_the_frozen_text_of_the_repository_document(exp):
    repo_doc = FS.REPO_ROOT / exp.spec_path
    if not repo_doc.exists():
        pytest.skip("the repository document is not next to the code")
    assert exp.frozen_hash((FS.SPECS_DIR / exp.spec_file).read_bytes()) == exp.frozen_hash(repo_doc.read_bytes()) == exp.spec_sha256


@pytest.mark.parametrize("exp", [E092, E100], ids=lambda e: e.key)
def test_a_missing_packaged_file_fails_closed(exp, tmp_path):
    with pytest.raises(FS.FrozenSpecError, match="missing or unreadable"):
        SV.check_document(exp, specs_dir=tmp_path, repo_root=tmp_path)


@pytest.mark.parametrize("exp", [E092, E100], ids=lambda e: e.key)
def test_a_changed_packaged_file_fails_closed(exp, tmp_path):
    raw = (FS.SPECS_DIR / exp.spec_file).read_bytes().replace("2026-11".encode(), "2026-12".encode(), 1)
    (tmp_path / exp.spec_file).write_bytes(raw)
    with pytest.raises(FS.FrozenSpecError, match="pinned"):
        SV.check_document(exp, specs_dir=tmp_path, repo_root=tmp_path)


def test_the_other_experiments_document_does_not_pass_for_this_one(tmp_path):
    shutil.copy(FS.SPECS_DIR / E092.spec_file, tmp_path / E100.spec_file)
    with pytest.raises(FS.FrozenSpecError, match="pinned"):
        SV.check_document(E100, specs_dir=tmp_path, repo_root=tmp_path)


def test_results_appended_after_the_frozen_part_do_not_break_the_check(tmp_path):
    for exp, tail in ((E092, "\n## 4. 结果\n未完成\n"), (E100, "xx")):
        shutil.copy(FS.SPECS_DIR / exp.spec_file, tmp_path / exp.spec_file)
    with (tmp_path / E092.spec_file).open("ab") as fh:
        fh.write("\n## 4. 结果\n未完成\n".encode())
    raw = (tmp_path / E100.spec_file).read_text(encoding="utf-8").replace("（空。）", "判定：inconclusive")
    (tmp_path / E100.spec_file).write_text(raw, encoding="utf-8", newline="\n")           # the E100 result section is free text after the frozen part
    assert SV.check_document(E092, specs_dir=tmp_path, repo_root=tmp_path) == E092.spec_sha256
    assert SV.check_document(E100, specs_dir=tmp_path, repo_root=tmp_path) == E100.spec_sha256


def test_a_repository_copy_that_disagrees_fails_closed(tmp_path):
    repo = tmp_path / "repo"
    (repo / "docs" / "research").mkdir(parents=True)
    (repo / E100.spec_path).write_bytes(b"edited frozen text")
    with pytest.raises(FS.FrozenSpecError, match="repository copy"):
        SV.check_document(E100, repo_root=repo)


def test_the_worker_refuses_to_run_without_the_packaged_spec_of_the_chosen_experiment(monkeypatch, capsys):
    from pulse.workers import llm_live as W

    monkeypatch.setattr(FS, "SPECS_DIR", Path("Z:/no/such/dir"))
    assert W.run(["status", "--experiment", "E100"]) == 2
    out = capsys.readouterr().out
    assert "preregistration_invalid" in out and "E100" in out
