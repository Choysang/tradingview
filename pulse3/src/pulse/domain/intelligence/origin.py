"""Origin (propagation chain) detection and independent-origin counting (spec FINAL §5.1, sources_sectors §5.2, SS-A10).

Why: the unit of verification is the claim, and a claim's strength is the number of INDEPENDENT origins
behind it, not the number of pages. Three portals reposting one Xinhua item are one chain; an outlet whose
origin is unknown adds no vote; an official disclosure counts once however many portals carry it.

Invariants: an origin is only ``confirmed_original`` / ``confirmed_independent_report`` when the document
IS the originating publication (issuer disclosure, an outlet's own dispatch); a document that merely names
an upstream ("据新华社") is a ``republication`` whose chain key is the NAMED publisher, and an unnamed
upstream is ``unknown`` with no origin_group. Rules are keyword rules (``origin-rules-v1``), explainable
and uncalibrated. Votes never come from reposts or model opinions.
Not covered: verifying that a named upstream really published the item (that needs the upstream document).
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable

from .text import text_hash

POLICY_VERSION = "origin-rules-v1"
CONFIRMED = ("confirmed_original", "confirmed_independent_report")
INACTIVE_DOCUMENT_STATUS = ("withdrawn", "deleted_at_source")

# (regex, publisher key, kind). Order matters: first match by position in the text wins.
_PUBLISHERS: list[tuple[str, str, str]] = [
    (r"新华社|新华网|新华财经", "新华社", "wire"), (r"中新社|中国新闻社", "中新社", "wire"), (r"路透", "路透", "wire"),
    (r"彭博", "彭博", "wire"), (r"美联社", "美联社", "wire"), (r"央视新闻|央视财经|央视|CCTV", "央视", "broadcaster"),
    (r"证监会|中国证监会|交易所|上交所|深交所|北交所|央行|人民银行|国务院|发改委|国家发改委|工信部|财政部|商务部|统计局|国家统计局|"
     r"外汇局|银保监|金融监管总局|海关总署|网信", "官方机构", "government"),
    (r"证券时报", "证券时报", "media"), (r"上海证券报", "上海证券报", "media"), (r"中国证券报", "中国证券报", "media"),
    (r"证券日报", "证券日报", "media"), (r"第一财经", "第一财经", "media"), (r"界面新闻", "界面新闻", "media"),
    (r"每日经济新闻", "每日经济新闻", "media"), (r"21世纪经济报道", "21世纪经济报道", "media"), (r"澎湃", "澎湃新闻", "media"),
    (r"财联社", "财联社", "media"), (r"环球网|环球时报", "环球时报", "media"),
]
_ISSUER = re.compile(r"(?:公司|上市公司)?(?:发布|披露|刊发)?公告(?:称|显示|披露|表示)?|在?公告中(?:称|表示|披露)|业绩预告|业绩快报|互动易|投资者互动平台")
_UNNAMED = re.compile(r"据(?:悉|传|报道|知情人士|消息人士|媒体|市场消息)|市场传闻|网传|传闻|消息人士称")
_ATTRIB = [(re.compile(rf"据[^，。,：:]{{0,6}}?(?:{pat})|(?:{pat})(?:\d{{1,2}}月\d{{1,2}}日)?(?:电|讯|消息|报道|称|发布|通报|表示)"), key, kind) for pat, key, kind in _PUBLISHERS]


@dataclass(frozen=True)
class Attribution:
    publisher: str
    kind: str  # wire | broadcaster | government | media | issuer_disclosure | unnamed
    start: int
    end: int


def detect_attribution(text: str, *, own_publisher: str | None = None) -> Attribution | None:
    """Earliest named upstream in ``text``. ``own_publisher`` is skipped (an outlet's own dispatch is not attribution)."""
    best: Attribution | None = None
    for rx, key, kind in _ATTRIB:
        if key == own_publisher:
            continue
        m = rx.search(text)
        if m and (best is None or m.start() < best.start):
            best = Attribution(key, kind, m.start(), m.end())
    m = _ISSUER.search(text)
    if m and (best is None or m.start() < best.start):
        best = Attribution("issuer_disclosure", "issuer_disclosure", m.start(), m.end())
    if best is None:
        m = _UNNAMED.search(text)
        if m:
            best = Attribution("unnamed", "unnamed", m.start(), m.end())
    return best


def document_origin(*, source_id: str, title: str, day: str, own_dispatch: bool, attribution: Attribution | None) -> tuple[str | None, str]:
    """(origin_group, origin_status) for a media document from its own text only (claim-level merging happens later)."""
    core = text_hash(title)
    if own_dispatch:
        return f"own:{source_id}:{day}:{core}", "confirmed_independent_report"
    if attribution and attribution.kind != "unnamed":
        return f"chain:{attribution.publisher}:{day}:{core}", "republication"
    return None, "unknown"


@dataclass(frozen=True)
class OriginItem:
    origin_group: str | None
    origin_status: str
    document_status: str = "published"
    attribution_publisher: str | None = None
    relation: str = "supports"


@dataclass(frozen=True)
class OriginSummary:
    independent_origins: int
    origin_keys: tuple[str, ...]
    attributed_chains: tuple[str, ...]
    unknown_origin_documents: int
    republications_merged: int


def summarize(items: Iterable[OriginItem], relation: str | None = "supports") -> OriginSummary:
    """Count independent origins among ``items`` (optionally only those with ``relation``).

    One vote per distinct ``origin_group`` among confirmed documents; republications of an already-counted
    group are merged; unknown origins and withdrawn documents add nothing.
    """
    keys: set[str] = set()
    chains: set[str] = set()
    unknown = merged = 0
    its = [i for i in items if (relation is None or i.relation == relation) and i.document_status not in INACTIVE_DOCUMENT_STATUS]
    for i in its:
        if i.origin_status in CONFIRMED and i.origin_group:
            keys.add(i.origin_group)
    for i in its:
        if i.origin_status == "republication":
            merged += 1
            chains.add(i.attribution_publisher or i.origin_group or "unknown_chain")
            if i.origin_group in keys:
                continue
        elif i.origin_status == "unknown" or not i.origin_group:
            unknown += 1
    return OriginSummary(len(keys), tuple(sorted(keys)), tuple(sorted(chains)), unknown, merged)
