"""Fail-closed check of a pre-registration's frozen text that ships WITH the app.

Why: ``docs/research`` is not deployed to the server, so a check that only looks for the document next to the code silently passes there (it returned None). A pre-registered experiment
must read the document it was frozen with: the frozen text is shipped as package data (``pulse/research_specs/``) and verified against the pinned sha256 before any command runs.
Invariants: a missing packaged file, an unreadable file or a different hash raises ``FrozenSpecError`` (never a pass and never None); a repository copy of the document, when present next to
the code, must agree too (a developer who edits the frozen text sees it at once); nothing is read from the network.
Not covered: what each lane does with the document (its own ``rules`` module defines the frozen prefix and the pinned hash).
"""
from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

SPECS_DIR = Path(__file__).resolve().parents[1] / "research_specs"
REPO_ROOT = Path(__file__).resolve().parents[4]


class FrozenSpecError(RuntimeError):
    """The packaged pre-registration is missing, unreadable or differs from its pinned hash."""


def verify(name: str, expected_sha256: str, frozen_hash: Callable[[bytes], str], *, repo_path: str | None = None, specs_dir: Path | None = None,
           repo_root: Path | None = None) -> str:
    """The frozen hash of ``research_specs/<name>`` when it equals ``expected_sha256``; ``repo_path`` (relative to the repository) is compared too when that file exists."""
    path = (specs_dir or SPECS_DIR) / name
    try:
        got = frozen_hash(path.read_bytes())
    except OSError as exc:
        raise FrozenSpecError(f"the packaged pre-registration {path} is missing or unreadable ({type(exc).__name__}); refusing to run without it") from None
    if got != expected_sha256:
        raise FrozenSpecError(f"{path}: frozen sha256 {got} != pinned {expected_sha256}; a changed pre-registration is a new experiment")
    if repo_path is not None:
        repo = (repo_root or REPO_ROOT) / repo_path
        if repo.exists() and frozen_hash(repo.read_bytes()) != expected_sha256:
            raise FrozenSpecError(f"{repo}: the repository copy's frozen text differs from the pinned sha256 {expected_sha256}")
    return got
