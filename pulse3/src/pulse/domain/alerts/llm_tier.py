"""LLM fallback for undecided news claims: which claims are asked, what is asked, how the answer becomes P0/P1/P2 (legacy row #134).

Why: the rules (``claims.extract_rules`` polarity + ``tiers.decide``) cannot say anything about a claim that names a holding or
a watch-list instrument but carries no direction. The old system (``news_alerts.AlertEngine.judge``) asked a model for exactly
those messages. This module is the pure part: no IO, no clock, no database.
Invariants:
* a claim is UNDECIDED for a user only when ``tiers.decide`` returned nothing (rules always win), the rules found no direction
  (polarity ``none`` or ``mixed``; a ``+``/``-`` claim is decided even when no tier follows), and the claim names a holding or
  a watch-list instrument. Digests (morning lists, market wraps, ...) are never undecided: they are never sent to a model;
* the question concerns the claim only (its own named instruments and the industry list), never the user's holdings, so one
  answer serves every user;
* the model's names are checked against the lists it was given; anything else is dropped; a malformed answer is ``None``;
* an LLM answer can only produce P0/P1/P2 hits through ``decide_llm``; ``LLM_MAY_RAISE_P0`` keeps the old system's behaviour
  (a negative answer naming a held code is P0). With it ``False`` the same answer is LOWERED to P1 over the industries of the
  held names. Every LLM hit carries ``trigger.llm`` and the model's confidence (``None`` when it gave none).
Not covered: the message body (the alert scan reads claims, whose headline is the title; a record without a title is not asked).
"""
from __future__ import annotations

import hashlib
import json
import math
import re
import unicodedata
from dataclasses import dataclass

from . import tiers as T

PROMPT_VERSION = "alert-llm-v1"
POLICY_VERSION = T.POLICY_VERSION + "+llm"
LLM_TEXT_CHARS = 1200
MAX_CANDIDATES = 60
LLM_MAY_RAISE_P0 = True      # old behaviour (news_alerts.decide_levels: a negative company verdict on a held code is P0); see module doc
UNDECIDED_POLARITIES = ("none", "mixed")

DIGEST = re.compile("精选|避雷针|一览|盘点|汇总|早报|午报|晚报|夜报|要闻|复盘|午评|收评|前瞻|周报")      # old news_rules.DIGEST, verbatim
SPEAKER = re.compile(r"^([一-龥A-Za-z*]{2,8}):")

SYSTEM_PROMPT = (
    "You judge one Chinese financial news message for an A-share trader. Decide whether it is bad news (negative), "
    "good news (positive) or irrelevant (neutral) for the listed candidate stocks and for 申万 level-2 industries. "
    "Answer with one JSON object and nothing else: {\"direction\": \"negative|positive|neutral\", \"magnitude\": 1-3, "
    "\"confidence\": 0-1, \"codes\": [affected codes taken from the candidate list], \"industries\": [affected industries "
    "taken from the industry list], \"reason\": \"one line\"}. Magnitude 1 is minor, 3 is severe. "
    "Use neutral when unsure.")


def normalize(text: str) -> str:
    return unicodedata.normalize("NFKC", text or "")


def clean_text(text: str) -> str:
    """Text safe for PostgreSQL ``text``/``jsonb``: no NUL (a DataError on insert) and no lone surrogate (not encodable)."""
    return text.replace("\x00", "").encode("utf-8", "replace").decode("utf-8")


def clean_json(value):
    """The same for every string inside a parsed JSON value (keys included)."""
    if isinstance(value, str):
        return clean_text(value)
    if isinstance(value, list):
        return [clean_json(v) for v in value]
    if isinstance(value, dict):
        return {clean_text(str(k)): clean_json(v) for k, v in value.items()}
    return value


def is_digest(headline: str) -> bool:
    """A morning list or a market wrap; its speaker alone decides when the title has one ("公司:明年前瞻..." is not a digest)."""
    head = normalize(headline).strip()
    m = SPEAKER.match(head)
    return bool(DIGEST.search(m.group(1) if m else head))


def message_key(claim: T.ClaimView) -> str:
    """One message = one claim and one headline text: the same message is never asked twice, a corrected headline is a new one."""
    return hashlib.sha256(f"{claim.claim_id}|{normalize(claim.headline).strip()}".encode("utf-8")).hexdigest()


def touches(claim: T.ClaimView, ctx: T.Context) -> bool:
    return bool(set(claim.instrument_ids) & (ctx.holdings | ctx.watch))


def is_undecided(claim: T.ClaimView, ctx: T.Context, rule_hits: list[T.TierHit]) -> bool:
    return (not rule_hits and not T.is_backfill(claim) and claim.polarity in UNDECIDED_POLARITIES and bool(normalize(claim.headline).strip())
            and touches(claim, ctx) and not is_digest(claim.headline))


# ---- the question -----------------------------------------------------------------------------------------------------------
@dataclass(frozen=True)
class Candidate:
    key: str                     # what the model sees and answers with: the 6-digit code, else "id:" + 8 hex of the instrument id
    instrument_id: str
    name: str


def candidates_of(claim: T.ClaimView, names: dict[str, dict]) -> list[Candidate]:
    out = []
    for iid in claim.instrument_ids[:MAX_CANDIDATES]:
        info = names.get(iid) or {}
        m = re.match(r"\d{6}", str(info.get("code") or ""))
        out.append(Candidate(m.group(0) if m else "id:" + iid.replace("-", "")[:8], iid, str(info.get("name") or "")))
    return out


def build_prompt(claim: T.ClaimView, candidates: list[Candidate], industries: list[str]) -> str:
    lines = [f"- {c.key} {c.name}".rstrip() for c in candidates]
    return (f"Message:\n{normalize(claim.headline).strip()[:LLM_TEXT_CHARS]}\nCategory hint: {claim.category or 'none'}"
            + "\n\nCandidate stocks:\n" + ("\n".join(lines) or "(none)") + "\n\nIndustry list: " + "、".join(industries))


def prompt_hash(prompt: str, system: str = SYSTEM_PROMPT) -> str:
    return hashlib.sha256(f"{PROMPT_VERSION}\n{system}\n{prompt}".encode("utf-8")).hexdigest()


# ---- the answer -------------------------------------------------------------------------------------------------------------
@dataclass(frozen=True)
class Judgement:
    direction: str                                   # negative | positive | neutral
    magnitude: int                                   # 1..3
    confidence: float | None                         # 0..1, None when the model gave none
    instrument_ids: tuple[str, ...] = ()
    industries: tuple[str, ...] = ()
    reason: str = ""
    judgement_id: str | None = None
    model: str | None = None

    def to_json(self) -> dict:
        return {"direction": self.direction, "magnitude": self.magnitude, "confidence": self.confidence,
                "instrument_ids": list(self.instrument_ids), "industries": list(self.industries), "reason": self.reason}

    @staticmethod
    def from_json(d: dict, *, judgement_id: str | None = None, model: str | None = None) -> "Judgement":
        return Judgement(d["direction"], int(d["magnitude"]), d.get("confidence"), tuple(d.get("instrument_ids") or ()),
                         tuple(d.get("industries") or ()), str(d.get("reason") or ""), judgement_id, model)


def _finite(x):
    """Numbers the model sent: 1e999 (inf) and 10**400 (too big for a float) are a malformed answer, not a crash."""
    if isinstance(x, float) and not math.isfinite(x):
        raise ValueError("not finite")
    if isinstance(x, int) and not isinstance(x, bool) and abs(x) > 10**15:
        raise OverflowError("number out of range")
    return x


def parse_answer(text: str, candidates: list[Candidate], industries: set[str]) -> Judgement | None:
    m = re.search(r"\{.*\}", text or "", re.S)
    try:
        data = json.loads(m.group(0)) if m else None
        direction = str(data["direction"]).lower()
        magnitude = min(3, max(1, int(_finite(data.get("magnitude")) or 1)))
        by_key = {c.key: c.instrument_id for c in candidates}
        wanted = {str(x).strip() for x in data.get("codes") or []}
        ids = tuple(sorted({by_key[k] for k in wanted if k in by_key}))
        inds = tuple(sorted({str(i) for i in data.get("industries") or [] if str(i) in industries}))
        conf = data.get("confidence")
        confidence = round(min(1.0, max(0.0, float(_finite(conf)))), 4) if isinstance(conf, (int, float)) and not isinstance(conf, bool) else None
        reason = clean_text(str(data.get("reason") or ""))[:300]
    except (ValueError, KeyError, TypeError, AttributeError, OverflowError):      # OverflowError: 1e999 / 10**400 in a number field
        return None
    if direction not in ("negative", "positive", "neutral"):
        return None
    return Judgement(direction, magnitude, confidence, ids, inds, reason)


# ---- the answer as tiers ----------------------------------------------------------------------------------------------------
def decide_llm(claim: T.ClaimView, ctx: T.Context, j: Judgement) -> list[T.TierHit]:
    """P0/P1/P2 hits of one LLM judgement for one user (the caller has checked the claim was undecided). Neutral = nothing."""
    if j.direction == "neutral" or T.is_backfill(claim):
        return []
    named = set(j.instrument_ids) & set(claim.instrument_ids)
    held = named & ctx.holdings
    trig = {"llm": True, "confidence": j.confidence, "model": j.model}
    hits: list[T.TierHit] = []
    p0: set[str] = set()
    if j.direction == "negative":
        touched: dict[str, list[str]] = {i: sorted(ctx.holding_industries[i]) for i in j.industries if ctx.holding_industries.get(i)}
        if held and LLM_MAY_RAISE_P0:
            p0 = held
            hits.append(T.TierHit("P0", "LLM-judged: negative claim names a holding", tuple(sorted(held)), trigger=trig))
        elif held:                                           # lowered, never raised: P1 over the industries the held names belong to
            for ind, members in ctx.holding_industries.items():
                if held & members:
                    touched.setdefault(ind, sorted(members))
        if touched:
            hits.append(T.TierHit("P1", "LLM-judged: negative claim in an industry that contains a holding", (), tuple(sorted(touched)),
                                  tuple(sorted({h for hs in touched.values() for h in hs})), trig))
    watched = (named & ctx.watch) - p0
    if watched:
        hits.append(T.TierHit("P2", "LLM-judged: claim names a watch-list instrument", tuple(sorted(watched)), trigger=trig))
    elif j.direction == "positive" and (named - ctx.holdings) & ctx.main_board:
        hits.append(T.TierHit("P2", "LLM-judged: positive claim names a main-board instrument that is not held",
                              tuple(sorted((named - ctx.holdings) & ctx.main_board)), trigger=trig))
    return hits


def llm_evidence(j: Judgement, hit: T.TierHit) -> dict:
    """Keys added to the evidence body of an alert that came from the model (rule alerts get none of them)."""
    return {"judged_by": "llm", "llm_judged": True, "llm_confidence": j.confidence, "llm_model": j.model, "llm_judgement_id": j.judgement_id,
            "llm_magnitude": j.magnitude, "llm_reason": j.reason, "llm_prompt_version": PROMPT_VERSION,
            "direction": j.direction, "policy_version": POLICY_VERSION}
