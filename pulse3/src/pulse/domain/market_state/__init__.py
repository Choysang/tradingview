"""Market state domain (extension group A): session statistics, sentiment gauge, regime timeline, panels, tape, outcomes.

Why: PEX01/PEX02/PEX03/PEX09/PEX16 and the old-site overview need one pure definition of limit-ups, ladder,
promotion, breadth, regime and forward outcomes. No IO lives here; ``application.market_today_service`` loads
inputs and persists the results as versioned artifacts.
"""
from . import daily, mood, outcomes, panels, regime, tape

__all__ = ["daily", "mood", "outcomes", "panels", "regime", "tape"]
