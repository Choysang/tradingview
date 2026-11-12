"""Community sampling, dedup, independent authors and growth (spec FINAL §5 / sources_sectors §6, SS-A23/A24).

Why: social discussion is an ATTENTION PROXY measured on a declared sample. A sample states its entry
point, sort, half-open time window and coverage; posts are deduplicated; "independent authors" counts
distinct pseudonyms, never people or buyers. Engagement belongs to ``sampled_at``, not to the post's creation
time, so an engagement count seen three days later cannot explain a price ten minutes after posting.

Invariants: coverage is ``complete_for_declared_scope`` only when the list is ordered by publish time and the
oldest fetched post is at or before the window start (nothing in the window can sit beyond the fetched pages);
otherwise ``partial``. The author pseudonym is a one-way digest of the platform user id (minimal identity;
no profile, no holdings inference). Output labels never claim buying/selling by retail investors. Read-only:
nothing here posts, replies or contacts anyone.
Not covered: stance/sarcasm models (model provider tasks), reply threads, likes (absent in the list).
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime, timedelta

from .feeds import GubaPost
from .text import normalize

METRICS_BASIS = "attention_proxy_not_trading_flow"
ANALYSIS_MODEL_VERSION = "rules-community-v1"
FORBIDDEN_LABELS = ("retail_buying", "retail_buy", "net_buy", "散户买入", "散户净买", "真实买入")


def pseudonym(user_id: str | None) -> str | None:
    return None if not user_id else "gb:" + hashlib.sha256(user_id.encode()).hexdigest()[:12]


def title_hash(title: str) -> str:
    return hashlib.sha1(normalize(title).encode()).hexdigest()[:12]


@dataclass(frozen=True)
class Scope:
    entry: str
    sort: str
    window_start: datetime
    window_end: datetime  # == sampled_at, exclusive
    pages_fetched: int
    coverage_status: str


def coverage_status(posts: list[GubaPost], *, window_start: datetime, sorted_by_publish_time: bool) -> str:
    unpinned = [p for p in posts if not p.pinned]
    if not sorted_by_publish_time or not unpinned:
        return "partial" if unpinned else "unknown"
    return "complete_for_declared_scope" if min(p.published_at for p in unpinned) <= window_start else "partial"


def in_scope(posts: list[GubaPost], code: str, scope: Scope) -> list[GubaPost]:
    """Posts of the instrument's own stock bar whose publish time lies in [window_start, window_end)."""
    return [p for p in posts if p.bar_code == code and not p.pinned and scope.window_start <= p.published_at < scope.window_end]


def summarize(posts: list[GubaPost], code: str, scope: Scope) -> dict:
    sel = in_scope(posts, code, scope)
    authors = {pseudonym(p.user_id) for p in sel if p.user_id}
    clusters: dict[str, int] = {}
    for p in sel:
        clusters[title_hash(p.title)] = clusters.get(title_hash(p.title), 0) + 1
    dup_posts = sum(n - 1 for n in clusters.values() if n > 1)
    mid = scope.window_start + (scope.window_end - scope.window_start) / 2
    growth = None
    growth_reason = None
    if scope.coverage_status == "complete_for_declared_scope":
        early = sum(1 for p in sel if p.published_at < mid)
        late = len(sel) - early
        growth = {"early_half_posts": early, "late_half_posts": late, "ratio_late_to_early": None if early == 0 else round(late / early, 4)}
    else:
        growth_reason = "growth needs complete coverage of the declared window"
    return {
        "basis": METRICS_BASIS, "code": code, "window_start": scope.window_start.isoformat(), "window_end": scope.window_end.isoformat(),
        "coverage_status": scope.coverage_status, "posts_in_window": len(sel), "distinct_author_pseudonyms": len(authors),
        "posts_without_author": sum(1 for p in sel if not p.user_id), "duplicate_posts": dup_posts, "unique_title_clusters": len(clusters),
        "comment_count_total": sum(p.comment_count or 0 for p in sel), "click_count_total": sum(p.click_count or 0 for p in sel),
        "growth": growth, "growth_unavailable_reason": growth_reason,
        "note": "attention proxy on a declared sample; not trading flow and not a count of investors",
    }


def assert_no_forbidden_labels(obj: object) -> None:
    text = repr(obj)
    for t in FORBIDDEN_LABELS:
        if t in text:
            raise ValueError(f"forbidden community label {t!r}")


def visible_at(available_at: datetime, decision_as_of: datetime) -> bool:
    """SS-A23: an engagement snapshot is usable only from its actual availability, whatever the post's creation time."""
    return available_at <= decision_as_of


def default_window(sampled_at: datetime, hours: int = 24) -> tuple[datetime, datetime]:
    return sampled_at - timedelta(hours=hours), sampled_at
