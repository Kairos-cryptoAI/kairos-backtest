"""Compatibility exports for the deterministic legacy strategy generator."""

from kairos_strategy.legacy_strategy import (
    Signal,
    StrategyConfig,
    StrategySignal,
    _rsi_series,
    generate_signals,
)

__all__ = ["Signal", "StrategyConfig", "StrategySignal", "_rsi_series", "generate_signals"]
