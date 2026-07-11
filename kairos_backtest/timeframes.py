from __future__ import annotations

from collections import defaultdict

from kairos_quant.candles import Candle

TIMEFRAME_MS = {
    "1m": 60_000,
    "3m": 180_000,
    "5m": 300_000,
    "15m": 900_000,
    "30m": 1_800_000,
    "1h": 3_600_000,
    "2h": 7_200_000,
    "4h": 14_400_000,
}


def aggregate(candles: list[Candle], timeframe: str) -> list[Candle]:
    size = TIMEFRAME_MS[timeframe]
    buckets: dict[int, list[Candle]] = defaultdict(list)
    for candle in candles:
        buckets[candle.open_time_ms // size * size].append(candle)
    result = []
    expected = size // 60_000
    for opened, rows in sorted(buckets.items()):
        if len(rows) != expected or any(
            b.open_time_ms - a.open_time_ms != 60_000 for a, b in zip(rows, rows[1:], strict=False)
        ):
            continue
        result.append(
            Candle(
                symbol=rows[0].symbol,
                timeframe=timeframe,
                open_time_ms=opened,
                close_time_ms=opened + size - 1,
                open=rows[0].open,
                high=max(row.high for row in rows),
                low=min(row.low for row in rows),
                close=rows[-1].close,
                volume=sum(row.volume for row in rows),
                quote_volume=sum(row.quote_volume for row in rows),
                taker_buy_volume=sum(row.taker_buy_volume for row in rows),
            )
        )
    return result


def build_timeframes(candles: list[Candle]) -> dict[str, list[Candle]]:
    return {
        timeframe: candles if timeframe == "1m" else aggregate(candles, timeframe)
        for timeframe in TIMEFRAME_MS
    }
