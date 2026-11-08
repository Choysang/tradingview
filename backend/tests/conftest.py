"""Test isolation: never write into the developer's watchlist or archive."""
from __future__ import annotations

import os
from pathlib import Path
import tempfile

_TMP = Path(tempfile.mkdtemp(prefix="pulse-tests-"))
os.environ.setdefault("PULSE_DATA_DIR", str(_TMP))
os.environ.setdefault("PULSE_SCHEDULER", "0")
