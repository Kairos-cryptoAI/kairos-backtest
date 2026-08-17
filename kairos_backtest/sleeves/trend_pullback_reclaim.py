"""Causal trend-pullback reclaim sleeve built only from closed OHLCV bars."""

from __future__ import annotations

import hashlib
import json
import math
from bisect import bisect_right
from dataclasses import asdict, dataclass
from enum import StrEnum

import numpy as np
from kairos_core.enums import Side
from kairos_quant.candles import Candle

from ..strategy_models import ExitPlan, SleeveIntent
from ..timeframes import build_timeframes

_FIVE_MINUTES_MS = 5 * 60 * 1_000
_ONE_HOUR_MS = 60 * 60 * 1_000
_STRATEGY_VERSION = "trend_pullback_reclaim_v1"


class PullbackDepthVariant(StrEnum):
    """Mutually exclusive ATR-depth bands for the completed pullback bar.

    Shared boundaries belong to the deeper band: shallow is ``[0, 0.5)``,
    medium is ``[0.5, 1.0)``, and deep is ``[1.0, 1.5]``.  This makes every
    supported depth eligible for exactly one predeclared research variant.
    """

    SHALLOW = "shallow"
    MEDIUM = "medium"
    DEEP = "deep"

    @property
    def bounds_atr(self) -> tuple[float, float]:
        return {
            PullbackDepthVariant.SHALLOW: (0.0, 0.5),
            PullbackDepthVariant.MEDIUM: (0.5, 1.0),
            PullbackDepthVariant.DEEP: (1.0, 1.5),
        }[self]

    def contains(self, depth_atr: float) -> bool:
        """Return whether ``depth_atr`` belongs to this variant's sole band."""

        if isinstance(depth_atr, bool) or not isinstance(depth_atr, (int, float)):
            return False
        value = float(depth_atr)
        if not math.isfinite(value):
            return False
        lower, upper = self.bounds_atr
        if self is PullbackDepthVariant.DEEP:
            return lower <= value <= upper
        return lower <= value < upper


@dataclass(frozen=True, slots=True)
class TrendPullbackReclaimConfig:
    """Fixed, reproducible controls for the pullback-continuation hypothesis."""

    depth_variant: PullbackDepthVariant = PullbackDepthVariant.MEDIUM
    hourly_fast_ema_period: int = 24
    hourly_slow_ema_period: int = 72
    hourly_rising_lookback: int = 6
    hourly_efficiency_lookback: int = 24
    minimum_hourly_efficiency: float = 0.25
    reclaim_ema_period: int = 20
    trend_ema_period: int = 50
    atr_period: int = 14
    maximum_reclaim_extension_atr: float = 0.5
    stop_buffer_atr: float = 0.25
    minimum_stop_distance_atr: float = 0.5
    maximum_stop_distance_atr: float = 2.0
    target_reward_to_risk: float = 2.0
    max_hold_bars: int = 36
    intent_valid_bars: int = 1

    def __post_init__(self) -> None:
        if not isinstance(self.depth_variant, PullbackDepthVariant):
            raise ValueError("depth_variant must be a PullbackDepthVariant")
        integer_values = (
            self.hourly_fast_ema_period,
            self.hourly_slow_ema_period,
            self.hourly_rising_lookback,
            self.hourly_efficiency_lookback,
            self.reclaim_ema_period,
            self.trend_ema_period,
            self.atr_period,
            self.max_hold_bars,
            self.intent_valid_bars,
        )
        if any(isinstance(value, bool) or not isinstance(value, int) for value in integer_values):
            raise ValueError("pullback periods must be integers")
        if min(integer_values) <= 0:
            raise ValueError("pullback periods must be positive")
        if self.hourly_fast_ema_period >= self.hourly_slow_ema_period:
            raise ValueError("hourly fast EMA period must be below the slow EMA period")
        if self.reclaim_ema_period >= self.trend_ema_period:
            raise ValueError("reclaim EMA period must be below the trend EMA period")

        for name in (
            "minimum_hourly_efficiency",
            "maximum_reclaim_extension_atr",
            "stop_buffer_atr",
            "minimum_stop_distance_atr",
            "maximum_stop_distance_atr",
            "target_reward_to_risk",
        ):
            value = getattr(self, name)
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(value)
                or value <= 0
            ):
                raise ValueError(f"{name} must be finite and positive")
            object.__setattr__(self, name, float(value))
        if self.minimum_hourly_efficiency > 1:
            raise ValueError("minimum_hourly_efficiency must not exceed one")
        if self.maximum_stop_distance_atr < self.minimum_stop_distance_atr:
            raise ValueError("maximum stop distance cannot be below the minimum")

    @property
    def fingerprint(self) -> str:
        payload = json.dumps(asdict(self), sort_keys=True, separators=(",", ":")).encode()
        return hashlib.sha256(payload).hexdigest()


@dataclass(frozen=True, slots=True)
class _HourlyRegime:
    direction: int
    fast_ema: float
    slow_ema: float
    prior_fast_ema: float
    efficiency: float


def _segments(rows: list[Candle], interval_ms: int) -> list[tuple[int, int]]:
    """Return half-open contiguous ranges without carrying feature state over gaps."""

    if not rows:
        return []
    starts = [0]
    for index, (previous, current) in enumerate(zip(rows, rows[1:], strict=False), start=1):
        if (
            current.open_time_ms - previous.open_time_ms != interval_ms
            or current.open_time_ms != previous.close_time_ms + 1
        ):
            starts.append(index)
    return list(zip(starts, (*starts[1:], len(rows)), strict=True))


def _segmented_ema(rows: list[Candle], period: int, interval_ms: int) -> np.ndarray:
    """Return SMA-seeded EMA values calculated independently within each segment."""

    values = np.full(len(rows), np.nan, dtype=float)
    multiplier = 2.0 / (period + 1)
    for start, end in _segments(rows, interval_ms):
        if end - start < period:
            continue
        seed_index = start + period - 1
        ema = float(np.mean([row.close for row in rows[start : seed_index + 1]]))
        values[seed_index] = ema
        for index in range(seed_index + 1, end):
            ema = (rows[index].close - ema) * multiplier + ema
            values[index] = ema
    return values


def _wilder_atr(rows: list[Candle], period: int) -> np.ndarray:
    """Return Wilder ATR without using a close from before a five-minute gap."""

    values = np.full(len(rows), np.nan, dtype=float)
    for start, end in _segments(rows, _FIVE_MINUTES_MS):
        if end - start < period:
            continue
        true_ranges = np.empty(end - start, dtype=float)
        for offset, index in enumerate(range(start, end)):
            row = rows[index]
            if offset == 0:
                true_ranges[offset] = row.high - row.low
            else:
                previous_close = rows[index - 1].close
                true_ranges[offset] = max(
                    row.high - row.low,
                    abs(row.high - previous_close),
                    abs(row.low - previous_close),
                )
        atr = float(np.mean(true_ranges[:period]))
        values[start + period - 1] = atr
        for offset in range(period, end - start):
            atr = (atr * (period - 1) + true_ranges[offset]) / period
            values[start + offset] = atr
    return values


def _hourly_regimes(
    rows: list[Candle],
    config: TrendPullbackReclaimConfig,
) -> list[_HourlyRegime | None]:
    """Classify each closed hour using only same-segment closed observations."""

    output: list[_HourlyRegime | None] = [None] * len(rows)
    fast = _segmented_ema(rows, config.hourly_fast_ema_period, _ONE_HOUR_MS)
    slow = _segmented_ema(rows, config.hourly_slow_ema_period, _ONE_HOUR_MS)
    lookback = config.hourly_efficiency_lookback
    for start, end in _segments(rows, _ONE_HOUR_MS):
        first = start + max(
            config.hourly_slow_ema_period - 1,
            config.hourly_rising_lookback + config.hourly_fast_ema_period - 1,
            lookback,
        )
        for index in range(first, end):
            fast_now = float(fast[index])
            slow_now = float(slow[index])
            fast_prior = float(fast[index - config.hourly_rising_lookback])
            closes = [row.close for row in rows[index - lookback : index + 1]]
            travel = sum(
                abs(current - previous) for previous, current in zip(closes, closes[1:], strict=False)
            )
            efficiency = abs(closes[-1] - closes[0]) / travel if travel > 0 else 0.0
            direction = 0
            if efficiency >= config.minimum_hourly_efficiency:
                if fast_now > slow_now and fast_now > fast_prior:
                    direction = 1
                elif fast_now < slow_now and fast_now < fast_prior:
                    direction = -1
            output[index] = _HourlyRegime(
                direction=direction,
                fast_ema=fast_now,
                slow_ema=slow_now,
                prior_fast_ema=fast_prior,
                efficiency=efficiency,
            )
    return output


def _number(value: float) -> str:
    if not math.isfinite(value):
        raise ValueError("pullback intent evidence must be finite")
    normalized = 0.0 if value == 0 else value
    return format(normalized, ".17g")


def _feature_hash(payload: dict[str, str | int]) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
    return hashlib.sha256(encoded).hexdigest()


def generate_trend_pullback_reclaim_intents(
    candles_1m: list[Candle],
    config: TrendPullbackReclaimConfig | None = None,
) -> list[SleeveIntent]:
    """Emit next-open intents after a completed pullback and reclaim pair."""

    if config is None:
        settings = TrendPullbackReclaimConfig()
    elif isinstance(config, TrendPullbackReclaimConfig):
        settings = config
    else:
        raise ValueError("config must be a TrendPullbackReclaimConfig or None")
    frames = build_timeframes(candles_1m)
    rows_5m, rows_1h = frames["5m"], frames["1h"]
    if len(rows_5m) < 2 or not rows_1h:
        return []

    ema20 = _segmented_ema(rows_5m, settings.reclaim_ema_period, _FIVE_MINUTES_MS)
    ema50 = _segmented_ema(rows_5m, settings.trend_ema_period, _FIVE_MINUTES_MS)
    atr = _wilder_atr(rows_5m, settings.atr_period)
    regimes = _hourly_regimes(rows_1h, settings)
    hourly_closes = [row.close_time_ms for row in rows_1h]
    five_minute_segment_start = [0] * len(rows_5m)
    for start, end in _segments(rows_5m, _FIVE_MINUTES_MS):
        five_minute_segment_start[start:end] = [start] * (end - start)

    intents: list[SleeveIntent] = []
    for index in range(1, len(rows_5m)):
        segment_start = five_minute_segment_start[index]
        if index - 1 < segment_start:
            continue
        previous, current = rows_5m[index - 1], rows_5m[index]
        previous_ema20 = float(ema20[index - 1])
        current_ema20 = float(ema20[index])
        previous_ema50 = float(ema50[index - 1])
        previous_atr = float(atr[index - 1])
        current_atr = float(atr[index])
        if not all(
            math.isfinite(value) and value > 0
            for value in (
                previous_ema20,
                current_ema20,
                previous_ema50,
                previous_atr,
                current_atr,
            )
        ):
            continue

        hourly_index = bisect_right(hourly_closes, current.close_time_ms) - 1
        if hourly_index < 0:
            continue
        if rows_1h[hourly_index].close_time_ms < rows_5m[segment_start].open_time_ms:
            # Never allow a pre-gap hourly regime to authorize a new segment.
            continue
        regime = regimes[hourly_index]
        if regime is None or regime.direction == 0:
            continue

        side: Side
        if regime.direction > 0:
            depth_atr = (previous_ema20 - previous.low) / previous_atr
            reclaim_extension_atr = (current.close - current_ema20) / current_atr
            if not (
                previous.close > previous_ema50
                and current.close > current_ema20
                and current.close > previous.high
                and 0 <= reclaim_extension_atr <= settings.maximum_reclaim_extension_atr
            ):
                continue
            side = Side.LONG
            stop = previous.low - settings.stop_buffer_atr * current_atr
            risk_distance = current.close - stop
            target = current.close + settings.target_reward_to_risk * risk_distance
        else:
            depth_atr = (previous.high - previous_ema20) / previous_atr
            reclaim_extension_atr = (current_ema20 - current.close) / current_atr
            if not (
                previous.close < previous_ema50
                and current.close < current_ema20
                and current.close < previous.low
                and 0 <= reclaim_extension_atr <= settings.maximum_reclaim_extension_atr
            ):
                continue
            side = Side.SHORT
            stop = previous.high + settings.stop_buffer_atr * current_atr
            risk_distance = stop - current.close
            target = current.close - settings.target_reward_to_risk * risk_distance

        if not settings.depth_variant.contains(depth_atr):
            continue
        risk_atr = risk_distance / current_atr
        if not settings.minimum_stop_distance_atr <= risk_atr <= settings.maximum_stop_distance_atr:
            continue
        if stop <= 0 or target <= 0:
            continue

        reference = current.close
        depth_lower, depth_upper = settings.depth_variant.bounds_atr
        feature_payload: dict[str, str | int] = {
            "atr": _number(current_atr),
            "config_sha256": settings.fingerprint,
            "decision_ts_ms": current.close_time_ms,
            "depth_atr": _number(depth_atr),
            "depth_variant": settings.depth_variant.value,
            "ema20": _number(current_ema20),
            "ema50_previous": _number(previous_ema50),
            "hourly_efficiency": _number(regime.efficiency),
            "hourly_fast_ema": _number(regime.fast_ema),
            "hourly_fast_ema_prior": _number(regime.prior_fast_ema),
            "hourly_slow_ema": _number(regime.slow_ema),
            "reclaim_extension_atr": _number(reclaim_extension_atr),
            "reference_price": _number(reference),
            "risk_atr": _number(risk_atr),
            "side": side.value,
            "stop_price": _number(stop),
            "strategy_version": _STRATEGY_VERSION,
            "symbol": current.symbol,
            "target_price": _number(target),
        }
        signal_strength = min(
            1.0,
            max(
                0.0,
                (regime.efficiency + (1 - reclaim_extension_atr / settings.maximum_reclaim_extension_atr))
                / 2,
            ),
        )
        intents.append(
            SleeveIntent(
                sleeve_id=_STRATEGY_VERSION,
                symbol=current.symbol,
                side=side,
                decision_ts_ms=current.close_time_ms,
                entry_eligible_ts_ms=current.close_time_ms + 1,
                entry_expires_ts_ms=(current.close_time_ms + settings.intent_valid_bars * _FIVE_MINUTES_MS),
                reference_price=reference,
                signal_strength=signal_strength,
                gross_reward_bps=abs(target - reference) / reference * 10_000,
                exit_plan=ExitPlan(
                    stop_price=stop,
                    target_price=target,
                    max_holding_ms=settings.max_hold_bars * _FIVE_MINUTES_MS,
                ),
                metadata=(
                    ("atr", _number(current_atr)),
                    ("config_sha256", settings.fingerprint),
                    ("depth_atr", _number(depth_atr)),
                    ("depth_lower_atr", _number(depth_lower)),
                    ("depth_upper_atr", _number(depth_upper)),
                    ("depth_variant", settings.depth_variant.value),
                    ("ema20", _number(current_ema20)),
                    ("ema50_previous", _number(previous_ema50)),
                    ("feature_hash", _feature_hash(feature_payload)),
                    ("hourly_efficiency", _number(regime.efficiency)),
                    ("hourly_fast_ema", _number(regime.fast_ema)),
                    ("hourly_fast_ema_prior", _number(regime.prior_fast_ema)),
                    ("hourly_slow_ema", _number(regime.slow_ema)),
                    ("reclaim_extension_atr", _number(reclaim_extension_atr)),
                    ("risk_atr", _number(risk_atr)),
                    ("strategy_version", _STRATEGY_VERSION),
                ),
            )
        )
    return intents
