"""Compatibility namespace for strategy-engine sleeve generators.

Submodules are registered as aliases, rather than copied wrappers, so tests and
research instrumentation that patch a module helper still patch the exact
module executed by the runtime generator.
"""

from __future__ import annotations

import sys

from kairos_strategy.sleeves import (
    OrderFlowExpansionVariant,
    OrderFlowVolatilityExpansionConfig,
    PullbackDepthVariant,
    QuarterHourFlowConfig,
    RangeMeanReversionConfig,
    RegimeRetestGenerationCounters,
    RegimeRetestGenerationEvidence,
    RegimeRetestReclaimVariant,
    RegimeRetestSetupEvent,
    RegimeRetestSetupEventType,
    RegimeVetoRetestReclaimConfig,
    RightTailTrendConfig,
    TrendBreakoutConfig,
    TrendPullbackReclaimConfig,
    generate_orderflow_volatility_expansion_intents,
    generate_quarter_hour_flow_intents,
    generate_range_mean_reversion_intents,
    generate_regime_veto_retest_reclaim_evidence,
    generate_regime_veto_retest_reclaim_intents,
    generate_right_tail_trend_intents,
    generate_trend_breakout_intents,
    generate_trend_pullback_reclaim_intents,
    orderflow_volatility_expansion,
    quarter_hour_flow,
    range_mean_reversion,
    regime_retest_reclaim,
    right_tail_trend,
    trend_breakout,
    trend_pullback_reclaim,
)

for _name, _module in (
    ("orderflow_volatility_expansion", orderflow_volatility_expansion),
    ("quarter_hour_flow", quarter_hour_flow),
    ("range_mean_reversion", range_mean_reversion),
    ("regime_retest_reclaim", regime_retest_reclaim),
    ("right_tail_trend", right_tail_trend),
    ("trend_breakout", trend_breakout),
    ("trend_pullback_reclaim", trend_pullback_reclaim),
):
    sys.modules[f"{__name__}.{_name}"] = _module

__all__ = [
    "OrderFlowExpansionVariant",
    "OrderFlowVolatilityExpansionConfig",
    "PullbackDepthVariant",
    "QuarterHourFlowConfig",
    "RangeMeanReversionConfig",
    "RegimeRetestGenerationCounters",
    "RegimeRetestGenerationEvidence",
    "RegimeRetestReclaimVariant",
    "RegimeRetestSetupEvent",
    "RegimeRetestSetupEventType",
    "RegimeVetoRetestReclaimConfig",
    "RightTailTrendConfig",
    "TrendBreakoutConfig",
    "TrendPullbackReclaimConfig",
    "generate_orderflow_volatility_expansion_intents",
    "generate_quarter_hour_flow_intents",
    "generate_range_mean_reversion_intents",
    "generate_regime_veto_retest_reclaim_evidence",
    "generate_regime_veto_retest_reclaim_intents",
    "generate_right_tail_trend_intents",
    "generate_trend_breakout_intents",
    "generate_trend_pullback_reclaim_intents",
]
