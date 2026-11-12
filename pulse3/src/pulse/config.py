"""Runtime settings read once from the environment.

Only deployment facts live here (DSN, lake root, ports). Business thresholds are
versioned policies stored in the database, never environment variables.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    dsn: str
    lake_root: Path
    legacy_warehouse: Path | None
    api_host: str
    api_port: int


def load() -> Settings:
    root = Path(__file__).resolve().parents[2]
    legacy = os.environ.get("PULSE_LEGACY_WAREHOUSE")
    return Settings(
        dsn=os.environ.get("PULSE_DSN", "postgresql://postgres@127.0.0.1:55432/pulse"),
        lake_root=Path(os.environ.get("PULSE_LAKE", str(root / "var" / "lake"))),
        legacy_warehouse=Path(legacy) if legacy else None,
        api_host=os.environ.get("PULSE_API_HOST", "127.0.0.1"),
        api_port=int(os.environ.get("PULSE_API_PORT", "8200")),
    )
