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


@dataclass(frozen=True, slots=True)
class StrategyConfig:
    """Causal state-transition controls on the five-minute decision clock."""

    confirmation_bars: int = 12
    minimum_hold_bars: int = 48
    minimum_confidence: float = 0.67

    def __post_init__(self) -> None:
        if self.confirmation_bars <= 0 or self.minimum_hold_bars < 0:
            raise ValueError("confirmation must be positive and minimum hold non-negative")
        if not math.isfinite(self.minimum_confidence) or not 0 <= self.minimum_confidence <= 1:
            raise ValueError("minimum confidence must be finite and within [0, 1]")


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
    signal_line = np.full(closes.size, np.nan)
    valid_macd = np.flatnonzero(np.isfinite(macd_line))
    if valid_macd.size:
        first = int(valid_macd[0])
        signal_line[first:] = ema(macd_line[first:], 9)
    histogram = macd_line - signal_line
    momentum = _rsi_series(closes)
    long_votes = (e20 > e50) & (e50 > e200)
    long_score = long_votes.astype(int) + (momentum >= 52) + (histogram > 0)
    short_votes = (e20 < e50) & (e50 < e200)
    short_score = short_votes.astype(int) + (momentum <= 48) + (histogram < 0)
    ready = (
        (np.arange(closes.size) >= 199)
        & np.isfinite(e20)
        & np.isfinite(e50)
        & np.isfinite(e200)
        & np.isfinite(histogram)
    )
    sides[ready & (long_score >= 2) & (long_score > short_score)] = 1
    sides[ready & (short_score >= 2) & (short_score > long_score)] = -1
    confidence = np.maximum(long_score, short_score) / 3.0
    confidence[~ready] = 0
    return sides, confidence


def generate_signals(
    candles_1m: list[Candle],
    config: StrategyConfig | None = None,
) -> list[StrategySignal]:
    """Run the hierarchy using only timeframes closed at each timestamp.

    Lower-timeframe disagreement vetoes an entry but does not liquidate an
    established senior trend. Transitions require consecutive confirmation and
    a minimum hold prevents round trips inside one higher-timeframe observation.
    """
    settings = config or StrategyConfig()
    frames = build_timeframes(candles_1m)
    close_times = {tf: [row.close_time_ms for row in rows] for tf, rows in frames.items()}
    decisions = {tf: _bias_series(rows) for tf, rows in frames.items()}
    output: list[StrategySignal] = []
    position = 0
    pending = 0
    pending_count = 0
    held_bars = settings.minimum_hold_bars
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
        candidate, reasons = 0, []
        senior = (current["4h"][0], current["1h"][0])
        regime = senior[0] if senior[0] == senior[1] and senior[0] != 0 else 0
        score = sum(current[tf][1] for tf in ("4h", "1h", "30m", "15m", "5m")) / 5
        if regime:
            setup = sum(current[tf][0] == regime for tf in ("30m", "15m", "5m"))
            entry_opposes = any(current[tf][0] not in (regime, 0) for tf in ("3m", "1m"))
            if setup >= 2 and not entry_opposes and score >= settings.minimum_confidence:
                candidate = regime
                reasons = ["senior_aligned", f"setup_votes_{setup}"]
            elif entry_opposes:
                reasons = ["entry_veto"]
        else:
            reasons = ["senior_conflict"]

        if position == 0:
            desired = candidate
        elif candidate == -position:
            desired = candidate
        elif regime == position:
            desired = position
        else:
            desired = 0

        held_bars += 1
        if desired == position:
            pending = 0
            pending_count = 0
            continue
        if position and held_bars < settings.minimum_hold_bars:
            continue
        if desired != pending:
            pending = desired
            pending_count = 1
        else:
            pending_count += 1
        if pending_count < settings.confirmation_bars:
            continue

        side = Side.LONG if desired == 1 else Side.SHORT if desired == -1 else Side.FLAT
        transition_reasons = reasons if desired else ["senior_regime_exit"]
        output.append(StrategySignal(timestamp, side, score if desired else 0.0, tuple(transition_reasons)))
        position = desired
        pending = 0
        pending_count = 0
        held_bars = 0
    return output
