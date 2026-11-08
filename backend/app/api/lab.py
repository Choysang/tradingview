"""Research lab: classic pattern scanner and factor IC analysis over daily bars."""
import threading
import time

from fastapi import APIRouter

from contracts.schema import (
    DataProvenance,
    FactorLabRequest,
    FactorLabResponse,
    PatternScanResponse,
    PatternSummary,
    ScreenerFilterRequest,
)
from ..core.engine import engine
from ..research import factor_lab, patterns

router = APIRouter(tags=["lab"])

WARM_CODES = 300  # LIVE warm-up size: today's top turnover; the panel itself uses every cached code
SNAPSHOT_CODES = 6000

# Unsliced scan of one panel; any `limit` is applied afterwards, so callers never evict each other.
_SCAN: tuple[tuple, list[PatternSummary], DataProvenance] | None = None
_SCAN_LOCK = threading.Lock()


def _universe() -> tuple[str, int, list[tuple[str, float | None]], dict[str, str]]:
    """Mode, snapshot size, (code, anchor price) by turnover, and code -> name."""
    market = engine()
    mode = market.provenance().mode
    result = market.query(
        ScreenerFilterRequest(sort_by="amount_today", limit=SNAPSHOT_CODES, exclude_st=False, exclude_new=False)
    )
    records = result.records
    return mode, result.total_matched, [(r.code, r.price) for r in records], {r.code: r.name for r in records}


def _scan(panel, names: dict[str, str], provenance: DataProvenance) -> list[PatternSummary]:
    """Scan once per distinct panel (mode, date, member set) and keep the latest."""
    global _SCAN
    key = (provenance.mode, provenance.trade_date, hash(tuple(panel["code"].unique().sort().to_list())))
    with _SCAN_LOCK:
        if _SCAN is not None and _SCAN[0] == key:
            return _SCAN[1]
    summaries = patterns.scan(panel, names, limit=10**9)
    with _SCAN_LOCK:
        _SCAN = (key, summaries, provenance)
    return summaries


@router.get("/lab/patterns", response_model=PatternScanResponse)
def scan_patterns(limit: int = 30) -> PatternScanResponse:
    started = time.perf_counter()
    limit = max(1, min(limit, 200))
    mode, universe, codes, names = _universe()
    panel, provenance = engine().history.panel(codes, mode, WARM_CODES)
    summaries = [
        s.model_copy(update={"hits": s.hits[:limit]}) for s in _scan(panel, names, provenance)
    ]
    return PatternScanResponse(
        provenance=provenance,
        universe=universe,
        scanned=provenance.coverage,
        execution_ms=round((time.perf_counter() - started) * 1000, 2),
        patterns=summaries,
    )


@router.post("/lab/factors", response_model=FactorLabResponse)
def analyse_factor(request: FactorLabRequest) -> FactorLabResponse:
    mode, _, codes, _ = _universe()
    panel, provenance = engine().history.panel(codes, mode, WARM_CODES)
    return factor_lab.run(panel, request, provenance)
