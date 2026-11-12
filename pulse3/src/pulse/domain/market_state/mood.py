"""Sentiment thermometer (0-100) with its declared weights, a versioned and *uncalibrated* indicator.

Why: the old overview (MoodHero) showed a 0-100 temperature with six named bands. The weights are kept
as the old site declared them (limit-up ratio 0.40, breadth 0.25, height 0.20, seal quality 0.15) so the
number stays comparable, but nothing here is fitted or validated: ``MOOD_VERSION`` says so, the response
carries ``calibrated: false`` and the page must say it is a descriptive gauge, not a forecast.
Invariants: every component is None-aware; a missing component is reported and the score is computed
only if all four components are known (otherwise None, never a partial or zero-filled number).
Components (all from ``SessionStats``): limit_ratio = min(1, limit_up / limit_verified / 0.08);
breadth = up / (up + down); height = min(max_height, 7) / 7; seal_quality = limit_up / (limit_up + broken)
(old formula: 1 - broken/attempted; with no attempted board the quality is unknown).
Not covered: intraday session curve, any calibration against outcomes.
"""
from __future__ import annotations

from decimal import ROUND_HALF_EVEN, Decimal
from typing import Any

from .daily import SessionStats

MOOD_VERSION = "mood-v1-old-site-weights-uncalibrated"
D = Decimal
WEIGHTS = {"limit_ratio": D("0.40"), "breadth": D("0.25"), "height": D("0.20"), "seal_quality": D("0.15")}
LIMIT_RATIO_SATURATION = D("0.08")
HEIGHT_CAP = 7
# upper edges (exclusive) of the six bands, as in the old site
BANDS: tuple[tuple[int, str, str], ...] = (
    (25, "冰点", "亏钱效应主导，空间被压缩"),
    (40, "低迷", "赚钱效应稀薄，接力谨慎"),
    (55, "中性", "分歧与修复并存"),
    (70, "升温", "梯队完整，接力意愿回升"),
    (85, "亢奋", "涨停扩散，注意高位分歧"),
    (101, "高潮", "情绪顶点附近，风险收益比下降"),
)


def band_of(score: Decimal | None) -> dict[str, Any] | None:
    if score is None:
        return None
    for i, (limit, word, hint) in enumerate(BANDS):
        if score < limit:
            return {"index": i, "word": word, "hint": hint, "lower": 0 if i == 0 else BANDS[i - 1][0], "upper": min(limit, 100)}
    raise ValueError("score above 100")


def components(st: SessionStats) -> dict[str, Decimal | None]:
    limit_ratio = None
    if st.limit_verified:
        limit_ratio = min(D(1), D(st.limit_up) / D(st.limit_verified) / LIMIT_RATIO_SATURATION)
    breadth = None if st.up + st.down == 0 else D(st.up) / D(st.up + st.down)
    height = min(D(st.max_height), D(HEIGHT_CAP)) / D(HEIGHT_CAP) if st.limit_verified else None
    attempted = st.limit_up + st.broken
    seal_quality = None if attempted == 0 else D(st.limit_up) / D(attempted)
    return {"limit_ratio": limit_ratio, "breadth": breadth, "height": height, "seal_quality": seal_quality}


def score(st: SessionStats) -> dict[str, Any]:
    """{score, band, components, weights, missing, version, calibrated}. ``score`` is None unless all four are known."""
    comps = components(st)
    missing = sorted(k for k, v in comps.items() if v is None)
    total = None
    if not missing:
        total = (sum(WEIGHTS[k] * comps[k] for k in WEIGHTS) * 100).quantize(D("0.1"), rounding=ROUND_HALF_EVEN)   # type: ignore[operator]
    return {
        "score": total, "band": band_of(total), "version": MOOD_VERSION, "calibrated": False,
        "weights": dict(WEIGHTS), "saturation": {"limit_ratio": LIMIT_RATIO_SATURATION, "height_cap": HEIGHT_CAP},
        "components": {k: (None if v is None else v.quantize(D("0.0001"))) for k, v in comps.items()},
        "missing_components": missing,
        "bands": [{"upper": u, "word": w, "hint": h} for u, w, h in BANDS],
    }
