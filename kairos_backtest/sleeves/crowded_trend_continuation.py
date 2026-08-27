"""Compatibility façade for the strategy-engine contextual generator."""

from kairos_strategy.sleeves.crowded_trend_continuation import (
    CrowdedTrendContinuationConfig,
    generate_crowded_trend_continuation_intents,
)

__all__ = [
    "CrowdedTrendContinuationConfig",
    "generate_crowded_trend_continuation_intents",
]
