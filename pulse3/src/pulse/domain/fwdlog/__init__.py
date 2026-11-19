"""Forward-experiment logs of the backfill lane ``fwdlog`` (LEGACY_TRIAGE batch 3: E077, E078, E089, E053-E059).

Why: the old system still runs three forward experiments every session. Their pre-registered rules stay UNCHANGED; this package
holds the rules as pure functions (Decimal, missing = None, no IO). ``application.bf_fwdlog_service`` loads the point-in-time
inputs and appends the rows; ``workers.fwdlog`` is the CLI.
"""
from . import board, box5, common, heat, paper, queue, state, stats, tail, triggers

__all__ = ["board", "box5", "common", "heat", "paper", "queue", "state", "stats", "tail", "triggers"]
