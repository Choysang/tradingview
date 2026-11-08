"""Environment-driven settings. One instance is imported by every module."""
from dataclasses import dataclass, field
from pathlib import Path
import os

ROOT = Path(__file__).resolve().parents[2]


def _env_float(name: str, default: float, minimum: float) -> float:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        return max(minimum, float(raw))
    except ValueError:
        return default


@dataclass(frozen=True)
class Settings:
    mode: str = os.getenv("PULSE_MODE", "SIM").upper()
    refresh_seconds: float = _env_float("PULSE_REFRESH_SECONDS", 2.0, 0.5)
    data_dir: Path = Path(os.getenv("PULSE_DATA_DIR", str(ROOT / "backend" / "data")))
    history_db: Path = Path(
        os.getenv("PULSE_HISTORY_DB", str(ROOT / "backend" / "data" / "history.duckdb"))
    )
    cors_origins: tuple[str, ...] = field(
        default_factory=lambda: tuple(
            origin.strip()
            for origin in os.getenv(
                "PULSE_CORS_ORIGINS", "http://127.0.0.1:5173,http://localhost:5173"
            ).split(",")
            if origin.strip()
        )
    )
    version: str = "3.0.0"

    @property
    def store_db(self) -> Path:
        return self.data_dir / "pulse.db"


settings = Settings()
