"""Canonical input validation shared by replay and historical evaluation."""

from __future__ import annotations

import math
from collections.abc import Iterable

from kairos_quant.candles import Candle


def canonical_candles(
    candles: Iterable[Candle],
    *,
    expected_timeframe: str | None = None,
) -> list[Candle]:
    """Return one chronological, non-overlapping candle series.

    Sorting makes equivalent input iterables reproducible. Duplicate timestamps,
    mixed symbols and overlapping intervals are rejected because silently choosing
    one row would make both the fingerprint and evaluation order ambiguous.
    """
    ordered = sorted(
        candles,
        key=lambda candle: (
            candle.open_time_ms,
            candle.close_time_ms,
            candle.symbol,
            candle.timeframe,
        ),
    )
    if not ordered:
        return []

    symbol = ordered[0].symbol
    timeframe = expected_timeframe or ordered[0].timeframe
    previous: Candle | None = None
    for candle in ordered:
        values = (
            candle.open,
            candle.high,
            candle.low,
            candle.close,
            candle.volume,
            candle.quote_volume,
            candle.taker_buy_volume,
        )
        if not all(math.isfinite(value) for value in values):
            raise ValueError("candle values must be finite")
        if candle.symbol != symbol:
            raise ValueError("one evaluation cannot mix candle symbols")
        if candle.timeframe != timeframe:
            raise ValueError(f"expected {timeframe} candles, received {candle.timeframe}")
        if previous is not None:
            if candle.open_time_ms == previous.open_time_ms:
                raise ValueError(f"duplicate candle open timestamp {candle.open_time_ms}")
            if candle.open_time_ms <= previous.close_time_ms:
                raise ValueError("candle intervals cannot overlap")
        previous = candle
    return ordered
