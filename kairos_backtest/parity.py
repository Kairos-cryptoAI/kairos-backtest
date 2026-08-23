"""Research entry point into the canonical strategy-engine runtime wrapper."""

from __future__ import annotations

from collections.abc import Sequence

from kairos_core.contracts import StrategyIntentV1
from kairos_strategy.candles import Candle
from kairos_strategy.runtime import (
    canonical_intent_batch_bytes,
    generate_research_strategy_intents,
)


def generate_research_intents(
    strategy_id: str,
    candles: Sequence[Candle],
    config: object | None = None,
) -> tuple[StrategyIntentV1, ...]:
    """Return the same strict intents used by the Linux runtime wrapper."""

    return generate_research_strategy_intents(strategy_id, candles, config)


__all__ = ["canonical_intent_batch_bytes", "generate_research_intents"]
