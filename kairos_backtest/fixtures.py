"""Deterministic, network-free fixtures for methodology regression tests.

Synthetic outcomes validate causality and cost monotonicity only. They are not
evidence that the strategy is profitable on a live venue.
"""

from __future__ import annotations

import math
import random

from kairos_core.enums import Side
from kairos_strategy.candles import Candle

from .strategy import StrategySignal


def synthetic_regime_candles(
    *,
    count: int = 3 * 24 * 60,
    seed: int = 42,
    symbol: str = "BTCUSDT",
) -> list[Candle]:
    """Build trend, shock, and choppy regimes with stable seeded noise."""
    if count < 120:
        raise ValueError("count must provide at least two hours of observations")
    rng = random.Random(seed)  # nosec B311
    price = 100.0
    rows: list[Candle] = []
    for index in range(count):
        progress = index / count
        drift = 0.00025 if progress < 0.4 else -0.0005 if progress < 0.65 else 0.00005
        shock = -0.04 if index == int(count * 0.4) else 0.0
        noise = rng.gauss(0.0, 0.0015 if progress < 0.65 else 0.003)
        open_price = price
        close = max(1.0, open_price * (1 + drift + shock + noise))
        amplitude = max(abs(close - open_price), open_price * (0.001 + abs(noise)))
        high = max(open_price, close) + amplitude
        low = max(0.01, min(open_price, close) - amplitude)
        volume = 1_000 + 300 * abs(math.sin(index / 31))
        rows.append(
            Candle(
                symbol=symbol,
                timeframe="1m",
                open_time_ms=index * 60_000,
                close_time_ms=(index + 1) * 60_000 - 1,
                open=open_price,
                high=high,
                low=low,
                close=close,
                volume=volume,
                quote_volume=volume * close,
                taker_buy_volume=volume / 2,
            )
        )
        price = close
    return rows


def causal_momentum_signals(
    candles: list[Candle],
    *,
    lookback: int = 30,
    threshold: float = 0.004,
) -> list[StrategySignal]:
    """Emit a simple lagged momentum state using data closed by each timestamp."""
    if lookback < 2 or not math.isfinite(threshold) or threshold <= 0:
        raise ValueError("lookback and threshold must be positive")
    signals: list[StrategySignal] = []
    previous = Side.FLAT
    for index in range(lookback, len(candles)):
        current = candles[index]
        change = current.close / candles[index - lookback].close - 1
        side = Side.LONG if change > threshold else Side.SHORT if change < -threshold else Side.FLAT
        if side == previous:
            continue
        confidence = min(1.0, abs(change) / (threshold * 3)) if side != Side.FLAT else 0.0
        signals.append(
            StrategySignal(
                timestamp_ms=current.close_time_ms,
                side=side,
                confidence=confidence,
                reasons=("causal_momentum_fixture",),
            )
        )
        previous = side
    return signals
