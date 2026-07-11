"""Point-in-time multi-timeframe synchronization for replay without look-ahead."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable

from kairos_quant.candles import Candle


def synchronize_closed_candles(candles: Iterable[Candle]) -> list[tuple[int, dict[str, Candle]]]:
    """At each 1m close expose only candles closed at or before that timestamp."""
    ordered = sorted(candles, key=lambda candle: (candle.close_time_ms, candle.timeframe))
    by_timestamp: dict[int, list[Candle]] = defaultdict(list)
    for candle in ordered:
        by_timestamp[candle.close_time_ms].append(candle)
    latest: dict[str, Candle] = {}
    points: list[tuple[int, dict[str, Candle]]] = []
    for timestamp in sorted(by_timestamp):
        for candle in by_timestamp[timestamp]:
            latest[candle.timeframe] = candle
        if "1m" in latest and latest["1m"].close_time_ms == timestamp:
            points.append((timestamp, dict(latest)))
    return points


def assert_no_lookahead(points: list[tuple[int, dict[str, Candle]]]) -> None:
    for timestamp, frames in points:
        if any(candle.close_time_ms > timestamp for candle in frames.values()):
            raise AssertionError("replay exposed a candle from the future")
