"""The research façade must expose the exact strategy-engine objects."""

from kairos_strategy.sleeves.regime_retest_reclaim import (
    RegimeRetestGenerationCounters,
    RegimeRetestGenerationEvidence,
    RegimeRetestReclaimVariant,
    RegimeRetestSetupEvent,
    RegimeRetestSetupEventType,
    RegimeVetoRetestReclaimConfig,
    generate_regime_veto_retest_reclaim_evidence,
    generate_regime_veto_retest_reclaim_intents,
)
from kairos_strategy.sleeves.right_tail_trend import (
    RightTailTrendConfig,
    generate_right_tail_trend_intents,
)

from kairos_backtest import sleeves, v2


def test_sleeves_and_v2_export_the_frozen_public_api():
    assert sleeves.RegimeRetestGenerationCounters is RegimeRetestGenerationCounters
    assert sleeves.RegimeRetestGenerationEvidence is RegimeRetestGenerationEvidence
    assert sleeves.RegimeRetestReclaimVariant is RegimeRetestReclaimVariant
    assert sleeves.RegimeRetestSetupEvent is RegimeRetestSetupEvent
    assert sleeves.RegimeRetestSetupEventType is RegimeRetestSetupEventType
    assert sleeves.RegimeVetoRetestReclaimConfig is RegimeVetoRetestReclaimConfig
    assert (
        sleeves.generate_regime_veto_retest_reclaim_evidence is generate_regime_veto_retest_reclaim_evidence
    )
    assert sleeves.generate_regime_veto_retest_reclaim_intents is generate_regime_veto_retest_reclaim_intents
    assert sleeves.RightTailTrendConfig is RightTailTrendConfig
    assert sleeves.generate_right_tail_trend_intents is generate_right_tail_trend_intents
    assert v2.RegimeRetestGenerationCounters is RegimeRetestGenerationCounters
    assert v2.RegimeRetestGenerationEvidence is RegimeRetestGenerationEvidence
    assert v2.RegimeRetestReclaimVariant is RegimeRetestReclaimVariant
    assert v2.RegimeRetestSetupEvent is RegimeRetestSetupEvent
    assert v2.RegimeRetestSetupEventType is RegimeRetestSetupEventType
    assert v2.RegimeVetoRetestReclaimConfig is RegimeVetoRetestReclaimConfig
    assert v2.RightTailTrendConfig is RightTailTrendConfig
    assert v2.generate_regime_veto_retest_reclaim_evidence is generate_regime_veto_retest_reclaim_evidence
    assert v2.generate_regime_veto_retest_reclaim_intents is generate_regime_veto_retest_reclaim_intents
    assert v2.generate_right_tail_trend_intents is generate_right_tail_trend_intents
