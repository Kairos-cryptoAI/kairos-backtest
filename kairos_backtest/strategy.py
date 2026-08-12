from __future__ import annotations

import math
from bisect import bisect_right
from dataclasses import dataclass

import numpy as np
from kairos_core.enums import Side
from kairos_quant.candles import Candle
from kairos_quant.indicators import ema

from .timeframes import build_timeframes


@dataclass(frozen=True, slots=True)
class StrategySignal:
    timestamp_ms: int
    side: Side
    confidence: float
    reasons: tuple[str, ...]

    def __post_init__(self) -> None:
        if self.timestamp_ms < 0:
            raise ValueError("signal timestamp cannot be negative")
        if not math.isfinite(self.confidence) or not 0 <= self.confidence <= 1:
            raise ValueError("signal confidence must be finite and within [0, 1]")


Signal = StrategySignal


def _rsi_series(values: np.ndarray, period: int = 14) -> np.ndarray:
    output = np.full(values.size, 50.0)
    if values.size <= period:
        return output
    delta = np.diff(values)
    gains, losses = np.maximum(delta, 0), np.maximum(-delta, 0)
    gain, loss = gains[:period].mean(), losses[:period].mean()
    for index in range(period, delta.size):
        gain = (gain * (period - 1) + gains[index]) / period
        loss = (loss * (period - 1) + losses[index]) / period
        if loss == 0:
            output[index + 1] = 50.0 if gain == 0 else 100.0
        else:
            output[index + 1] = 100 - 100 / (1 + gain / loss)
    return output


def _bias_series(rows: list[Candle]) -> tuple[np.ndarray, np.ndarray]:
    closes = np.asarray([row.close for row in rows], dtype=float)
    sides = np.zeros(closes.size, dtype=np.int8)
    confidence = np.zeros(closes.size)
    if not closes.size:
        return sides, confidence
    e20, e50, e200 = ema(closes, 20), ema(closes, 50), ema(closes, 200)
    macd_line = ema(closes, 12) - ema(closes, 26)
    histogram = macd_line - ema(macd_line, 9)
    momentum = _rsi_series(closes)
    long_votes = (e20 > e50) & (e50 > e200)
    long_score = long_votes.astype(int) + (momentum >= 52) + (histogram > 0)
    short_votes = (e20 < e50) & (e50 < e200)
    short_score = short_votes.astype(int) + (momentum <= 48) + (histogram < 0)
    ready = np.arange(closes.size) >= 199
    sides[ready & (long_score >= 2) & (long_score > short_score)] = 1
    sides[ready & (short_score >= 2) & (short_score > long_score)] = -1
    confidence = np.maximum(long_score, short_score) / 3.0
    confidence[~ready] = 0
    return sides, confidence


def generate_signals(candles_1m: list[Candle]) -> list[StrategySignal]:
    """Run the production hierarchy using only timeframes closed at each timestamp."""
    frames = build_timeframes(candles_1m)
    close_times = {tf: [row.close_time_ms for row in rows] for tf, rows in frames.items()}
    decisions = {tf: _bias_series(rows) for tf, rows in frames.items()}
    output: list[StrategySignal] = []
    previous = 0
    for trigger in frames["5m"]:
        timestamp = trigger.close_time_ms
        current: dict[str, tuple[int, float]] = {}
        for timeframe in frames:
            index = bisect_right(close_times[timeframe], timestamp) - 1
            if index < 0:
                current[timeframe] = (0, 0.0)
            else:
                current[timeframe] = (
                    int(decisions[timeframe][0][index]),
                    float(decisions[timeframe][1][index]),
                )
        side_value, reasons = 0, []
        senior = (current["4h"][0], current["1h"][0])
        if senior[0] == senior[1] and senior[0] != 0:
            candidate = senior[0]
            setup = sum(current[tf][0] == candidate for tf in ("30m", "15m", "5m"))
            entry_opposes = any(current[tf][0] not in (candidate, 0) for tf in ("3m", "1m"))
            if setup >= 2 and not entry_opposes:
                side_value = candidate
                reasons = ["senior_aligned", f"setup_votes_{setup}"]
            elif entry_opposes:
                reasons = ["entry_veto"]
        else:
            reasons = ["senior_conflict"]
        if side_value != previous:
            side = Side.LONG if side_value == 1 else Side.SHORT if side_value == -1 else Side.FLAT
            score = sum(current[tf][1] for tf in ("4h", "1h", "30m", "15m", "5m")) / 5
            output.append(StrategySignal(timestamp, side, score if side_value else 0.0, tuple(reasons)))
            previous = side_value
    return output
