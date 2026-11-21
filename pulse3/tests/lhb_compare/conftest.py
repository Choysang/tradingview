"""Fixtures of the LHB comparison tests: reuse the intelligence tests' private migrated database, lake, clock and service."""
from __future__ import annotations

from intel.conftest import Clock, clock, conn, fresh_dsn, lake, svc  # noqa: F401  (fixtures)
