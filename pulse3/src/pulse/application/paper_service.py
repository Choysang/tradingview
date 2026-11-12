"""Seed the research paper registry (research.paper_revision) from the package registry.

Why: FINAL_SYSTEM_SPEC §10 — every paper is a versioned method card with
publication status, read depth, author claims and reproduction state kept
apart. The 46 core cards come from spec_package/research/papers_registry.json.
Read depth maps to the contract enum without upgrading it ("targeted_fulltext"
is section-level reading, not full text). Author results are not local results.
"""
from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timezone
from pathlib import Path

import psycopg
from psycopg.types.json import Jsonb

NS = uuid.UUID("6f1d3b8e-55a3-4c5b-9d0e-2a7c1f0b9e11")
READ_DEPTH = {"targeted_fulltext": "full_text_sections", "abstract_only": "abstract",
              "abstract_and_publisher_excerpt": "abstract"}
PUB_STATUS = {"preprint": "preprint", "conference_accepted_official_catalog": "accepted",
              "accepted_in_press_track_unverified": "accepted"}


def _status(code: str) -> str:
    if code in PUB_STATUS:
        return PUB_STATUS[code]
    return "published" if code.endswith("published") or "_published_" in code else "unknown"


def _year(p: dict) -> int | None:
    v = (p.get("dates") or {}).get("version_date") or ""
    return int(v[:4]) if v[:4].isdigit() else None


def seed_papers(conn: psycopg.Connection, registry: Path) -> int:
    papers = json.loads(registry.read_text(encoding="utf-8"))["papers"]
    now = datetime.now(timezone.utc)
    n = 0
    with conn.transaction():
        for p in papers:
            card = {k: p.get(k) for k in ("name", "method", "limits", "adaptation", "data", "evaluation", "costs",
                                          "author_claim", "reproduction", "code_url", "module", "dates",
                                          "reproduction_status", "implementation_status", "fulltext_url",
                                          "adoption_original")}
            card["local_reproduction"] = {"status": p.get("reproduction_status") or "not_started", "results": None}
            body = json.dumps(card, sort_keys=True, ensure_ascii=False).encode("utf-8")
            canonical = f"pulse-registry:{p['paper_id']}"
            cur = conn.execute(
                "insert into research.paper_revision(paper_id, paper_revision, canonical_id, title, primary_url, venue,"
                " publication_year, publication_status, publication_status_note, read_depth, raw_object_id,"
                " method_card, content_hash, published_at, available_at, recorded_at)"
                " values (%s,1,%s,%s,%s,%s,%s,%s,%s,%s,null,%s,%s,null,%s,%s) on conflict do nothing",
                (uuid.uuid5(NS, canonical), canonical, p["title"], p["primary_url"], p.get("venue"), _year(p),
                 _status(p.get("publication_status_code") or ""), p.get("publication_status_note") or "unknown",
                 READ_DEPTH.get(p.get("read_depth"), "metadata"), Jsonb(card), hashlib.sha256(body).hexdigest(),
                 now, now))
            n += cur.rowcount
    return n
