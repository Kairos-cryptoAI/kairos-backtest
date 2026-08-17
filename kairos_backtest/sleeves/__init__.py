"""Deterministic, causal strategy sleeves used by the research harness."""

from .range_mean_reversion import (
    RangeMeanReversionConfig,
    generate_range_mean_reversion_intents,
)
from .trend_breakout import TrendBreakoutConfig, generate_trend_breakout_intents
from .trend_pullback_reclaim import (
    PullbackDepthVariant,
    TrendPullbackReclaimConfig,
    generate_trend_pullback_reclaim_intents,
)

__all__ = [
    "PullbackDepthVariant",
    "RangeMeanReversionConfig",
    "TrendBreakoutConfig",
    "TrendPullbackReclaimConfig",
    "generate_range_mean_reversion_intents",
    "generate_trend_breakout_intents",
    "generate_trend_pullback_reclaim_intents",
]
