"""The packaged pre-registration: the check fails closed when the file is missing or differs, and the shipped copy is the frozen document."""
from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from pulse.application import bf_llm_live_service as SV
from pulse.application import frozen_specs as FS
from pulse.domain.llm_live import rules as R

REPO_DOC = Path(__file__).resolve().parents[3] / R.SPEC_PATH


def test_the_shipped_copy_matches_the_pinned_hash():
    assert (FS.SPECS_DIR / SV.SPEC_FILE).is_file()
    assert SV.check_document() == R.SPEC_SHA256


@pytest.mark.skipif(not REPO_DOC.exists(), reason="the repository document is not next to the code")
def test_the_shipped_copy_is_the_frozen_text_of_the_repository_document():
    assert R.frozen_prefix((FS.SPECS_DIR / SV.SPEC_FILE).read_bytes()) == R.frozen_prefix(REPO_DOC.read_bytes())


def test_a_missing_packaged_file_fails_closed(tmp_path):
    with pytest.raises(FS.FrozenSpecError, match="missing or unreadable"):
        SV.check_document(specs_dir=tmp_path, repo_root=tmp_path)


def test_a_changed_packaged_file_fails_closed(tmp_path):
    raw = (FS.SPECS_DIR / SV.SPEC_FILE).read_bytes().replace("2026-11-09".encode(), "2026-11-10".encode(), 1)
    (tmp_path / SV.SPEC_FILE).write_bytes(raw)
    with pytest.raises(FS.FrozenSpecError, match="pinned"):
        SV.check_document(specs_dir=tmp_path, repo_root=tmp_path)


def test_a_result_section_appended_to_the_copy_does_not_break_the_check(tmp_path):
    shutil.copy(FS.SPECS_DIR / SV.SPEC_FILE, tmp_path / SV.SPEC_FILE)
    with (tmp_path / SV.SPEC_FILE).open("ab") as fh:
        fh.write("\n## 4. 结果\n未完成\n".encode())
    assert SV.check_document(specs_dir=tmp_path, repo_root=tmp_path) == R.SPEC_SHA256


def test_a_repository_copy_that_disagrees_fails_closed(tmp_path):
    repo = tmp_path / "repo"
    (repo / "docs" / "research").mkdir(parents=True)
    (repo / R.SPEC_PATH).write_bytes(b"edited frozen text")
    with pytest.raises(FS.FrozenSpecError, match="repository copy"):
        SV.check_document(repo_root=repo)


def test_the_worker_refuses_to_run_without_the_packaged_spec(monkeypatch, capsys):
    from pulse.workers import llm_live as W

    monkeypatch.setattr(FS, "SPECS_DIR", Path("Z:/no/such/dir"))
    assert W.run(["status"]) == 2
    assert "preregistration_invalid" in capsys.readouterr().out
