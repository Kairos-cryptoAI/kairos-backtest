"""Compatibility exports for strategy-owned immutable models.

The canonical implementations live in :mod:`kairos_strategy.models`.  Keeping
this module avoids breaking historical report readers while ensuring research
and runtime generation use identical classes.
"""

from kairos_strategy.models import ExitPlan, ExitReason, SleeveIntent, TradeRecord

__all__ = ["ExitPlan", "ExitReason", "SleeveIntent", "TradeRecord"]
