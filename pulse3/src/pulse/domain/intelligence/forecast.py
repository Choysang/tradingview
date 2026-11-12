"""Community binary forecasts as claims (extension binding ``pulse.community_forecast@1.0``), spec RECON §8.

Why: a community post that states a falsifiable, explicit prediction is frozen as its own claim: original
wording, target, horizon, and an explicit probability ONLY when the source quoted one. The model summary is
derived content; the original forecast is never edited.

Invariants: payload passes ``contracts.check('CommunityForecastPayload', ..., SUPPLEMENTAL_ID)`` AND the
semantic rules of the package reference (forecast before a positive-duration horizon; probability only when
``source_explicit`` and inside [0,1]; claim subject equals payload instrument; predicate/assertion fixed;
source publication cannot be after the forecast cutoff). Knowledge visibility uses ACTUAL platform
availability: a late-discovered forecast is a retrospective cohort, never backdated.
Not covered: extracting forecasts from free text (needs a language model; the rules provider emits none),
outcome scoring (CommunityOutcomePayload) and target-artifact registration.
"""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from pulse import contracts

CONTRACT_ID = "pulse.community_forecast"
SCHEMA_VERSION = "1.0"
TOKEN = f"{CONTRACT_ID}@{SCHEMA_VERSION}"
PREDICATE = "community_binary_forecast"
SUBJECT_KIND = "instrument"
ASSERTION = "forecast"


def _t(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def validate_forecast(payload: dict) -> dict:
    contracts.check("CommunityForecastPayload", payload, schema_id=contracts.SUPPLEMENTAL_ID)
    if not _t(payload["forecast_as_of"]) <= _t(payload["horizon_start_at"]) < _t(payload["horizon_end_at"]):
        raise ValueError("forecast must precede its positive-duration horizon")
    p = payload["prediction_probability"]
    if payload["probability_origin"] == "not_provided":
        if p is not None:
            raise ValueError("cannot infer a source probability")
    elif p is None or not Decimal(0) <= Decimal(p) <= Decimal(1):
        raise ValueError("explicit probability must lie in [0,1]")
    return payload


def validate_bound_claim(payload: dict, *, claim_schema_version: str, subject_kind: str, subject_key: str | None, predicate: str,
                         assertion_type: str) -> None:
    if claim_schema_version != TOKEN:
        raise ValueError("unknown or ambiguous qualified claim schema token")
    if subject_kind != SUBJECT_KIND or predicate != PREDICATE or assertion_type != ASSERTION:
        raise ValueError("claim does not match the community_forecast binding")
    validate_forecast(payload)
    if subject_key != payload["instrument_id"]:
        raise ValueError("forecast subject differs from payload instrument")


def known_at(payload: dict, *, source_published_at: datetime, document_available_at: datetime, claim_available_at: datetime,
             decision_as_of: datetime) -> bool:
    """Visible at ``decision_as_of`` only when original cutoff AND actual document/claim availability allow it."""
    validate_forecast(payload)
    original = _t(payload["forecast_as_of"])
    if source_published_at > original:
        raise ValueError("source version does not support original prediction cutoff")
    if document_available_at < source_published_at or claim_available_at < document_available_at:
        raise ValueError("actual platform availability is inconsistent")
    return max(original, document_available_at, claim_available_at) <= decision_as_of
