"""Causal OHLCV range mean-reversion sleeve with bounded protective exits."""

from __future__ import annotations

import hashlib
import json
import math
from bisect import bisect_right
from dataclasses import asdict, dataclass

import numpy as np
from kairos_core.enums import Side
from kairos_quant.candles import Candle

from ..strategy_models import ExitPlan, SleeveIntent
from ..timeframes import build_timeframes

_FIVE_MINUTES_MS = 5 * 60 * 1_000
_ONE_HOUR_MS = 60 * 60 * 1_000
_STRATEGY_VERSION = "range_mean_reversion_v1"


@dataclass(frozen=True, slots=True)
class RangeMeanReversionConfig:
    """Fixed controls for a range-only VWAP re-entry candidate."""

    vwap_lookback_bars: int = 24
    atr_period: int = 14
    regime_lookback_hours: int = 12
    maximum_regime_efficiency: float = 0.30
    maximum_abs_hourly_slope: float = 0.00025
    band_atr_multiple: float = 1.25
    stop_atr_multiple: float = 1.0
    target_atr_extension: float = 0.0
    minimum_gross_reward_to_risk: float | None = None
    max_hold_bars: int = 12
    intent_valid_bars: int = 1

    def __post_init__(self) -> None:
        integers = (
            self.vwap_lookback_bars,
            self.atr_period,
            self.regime_lookback_hours,
            self.max_hold_bars,
            self.intent_valid_bars,
        )
        if any(isinstance(value, bool) or not isinstance(value, int) for value in integers):
            raise ValueError("range periods must be integers")
        if min(integers) <= 0:
            raise ValueError("range periods must be positive")
        for name, value in (
            ("maximum_regime_efficiency", self.maximum_regime_efficiency),
            ("maximum_abs_hourly_slope", self.maximum_abs_hourly_slope),
            ("band_atr_multiple", self.band_atr_multiple),
            ("stop_atr_multiple", self.stop_atr_multiple),
            ("target_atr_extension", self.target_atr_extension),
        ):
            if isinstance(value, bool) or not math.isfinite(value) or value < 0:
                raise ValueError(f"{name} must be finite and non-negative")
        if not 0 <= self.maximum_regime_efficiency <= 1:
            raise ValueError("maximum_regime_efficiency must be within [0, 1]")
        if self.band_atr_multiple <= 0 or self.stop_atr_multiple <= 0:
            raise ValueError("ATR multiples must be positive")
        if self.target_atr_extension > 2:
            raise ValueError("target ATR extension must not exceed 2")
        if self.minimum_gross_reward_to_risk is not None and (
            isinstance(self.minimum_gross_reward_to_risk, bool)
            or not math.isfinite(self.minimum_gross_reward_to_risk)
            or not 1 <= self.minimum_gross_reward_to_risk <= 5
        ):
            raise ValueError("minimum gross reward-to-risk must be within [1, 5]")
        for name in (
            "maximum_regime_efficiency",
            "maximum_abs_hourly_slope",
            "band_atr_multiple",
            "stop_atr_multiple",
            "target_atr_extension",
        ):
            object.__setattr__(self, name, float(getattr(self, name)))
        if self.minimum_gross_reward_to_risk is not None:
            object.__setattr__(
                self,
                "minimum_gross_reward_to_risk",
                float(self.minimum_gross_reward_to_risk),
            )

    @property
    def fingerprint(self) -> str:
        payload = json.dumps(asdict(self), sort_keys=True, separators=(",", ":")).encode()
        return hashlib.sha256(payload).hexdigest()


@dataclass(frozen=True, slots=True)
class _RangeRegime:
    eligible: bool
    slope_per_hour: float
    efficiency: float


def _segments(rows: list[Candle], interval_ms: int) -> list[tuple[int, int]]:
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


def _wilder_atr(rows: list[Candle], period: int) -> np.ndarray:
    values = np.full(len(rows), np.nan, dtype=float)
    for start, end in _segments(rows, _FIVE_MINUTES_MS):
        if end - start < period:
            continue
        true_ranges = np.empty(end - start, dtype=float)
        for offset, index in enumerate(range(start, end)):
            row = rows[index]
            true_ranges[offset] = (
                row.high - row.low
                if offset == 0
                else max(
                    row.high - row.low,
                    abs(row.high - rows[index - 1].close),
                    abs(row.low - rows[index - 1].close),
                )
            )
        atr = float(np.mean(true_ranges[:period]))
        values[start + period - 1] = atr
        for offset in range(period, end - start):
            atr = (atr * (period - 1) + true_ranges[offset]) / period
            values[start + offset] = atr
    return values


def _rolling_prior_vwap(rows: list[Candle], lookback: int) -> np.ndarray:
    """Return VWAP from completed bars strictly before each decision bar."""

    values = np.full(len(rows), np.nan, dtype=float)
    for start, end in _segments(rows, _FIVE_MINUTES_MS):
        for index in range(start + lookback, end):
            history = rows[index - lookback : index]
            total_volume = sum(row.volume for row in history)
            if total_volume <= 0:
                continue
            values[index] = (
                sum(((row.high + row.low + row.close) / 3) * row.volume for row in history) / total_volume
            )
    return values


def _hourly_range_regimes(rows: list[Candle], config: RangeMeanReversionConfig) -> list[_RangeRegime | None]:
    output: list[_RangeRegime | None] = [None] * len(rows)
    observations = config.regime_lookback_hours + 1
    x_axis = np.arange(observations, dtype=float)
    centered = x_axis - float(np.mean(x_axis))
    denominator = float(np.dot(centered, centered))
    for start, end in _segments(rows, _ONE_HOUR_MS):
        for index in range(start + observations - 1, end):
            closes = np.asarray(
                [row.close for row in rows[index - observations + 1 : index + 1]],
                dtype=float,
            )
            log_closes = np.log(closes)
            slope = float(np.dot(centered, log_closes - np.mean(log_closes)) / denominator)
            travel = float(np.sum(np.abs(np.diff(closes))))
            efficiency = abs(float(closes[-1] - closes[0])) / travel if travel else 0.0
            output[index] = _RangeRegime(
                eligible=(
                    efficiency <= config.maximum_regime_efficiency
                    and abs(slope) <= config.maximum_abs_hourly_slope
                ),
                slope_per_hour=slope,
                efficiency=efficiency,
            )
    return output


def _number(value: float) -> str:
    if not math.isfinite(value):
        raise ValueError("range intent evidence must be finite")
    return format(value, ".17g")


def generate_range_mean_reversion_intents(
    candles_1m: list[Candle],
    config: RangeMeanReversionConfig | None = None,
) -> list[SleeveIntent]:
    """Emit a trade only after an excursion closes back inside its prior VWAP band."""

    settings = RangeMeanReversionConfig() if config is None else config
    if not isinstance(settings, RangeMeanReversionConfig):
        raise ValueError("config must be a RangeMeanReversionConfig")
    frames = build_timeframes(candles_1m)
    rows_5m, rows_1h = frames["5m"], frames["1h"]
    if len(rows_5m) < 2 or not rows_1h:
        return []

    atr = _wilder_atr(rows_5m, settings.atr_period)
    vwap = _rolling_prior_vwap(rows_5m, settings.vwap_lookback_bars)
    regimes = _hourly_range_regimes(rows_1h, settings)
    hourly_closes = [row.close_time_ms for row in rows_1h]
    segment_start = [0] * len(rows_5m)
    for start, end in _segments(rows_5m, _FIVE_MINUTES_MS):
        segment_start[start:end] = [start] * (end - start)

    intents: list[SleeveIntent] = []
    for index in range(1, len(rows_5m)):
        if index - 1 < segment_start[index]:
            continue
        current, previous = rows_5m[index], rows_5m[index - 1]
        current_atr, previous_atr = float(atr[index]), float(atr[index - 1])
        current_vwap, previous_vwap = float(vwap[index]), float(vwap[index - 1])
        if not all(
            math.isfinite(value) and value > 0
            for value in (current_atr, previous_atr, current_vwap, previous_vwap)
        ):
            continue
        hourly_index = bisect_right(hourly_closes, current.close_time_ms) - 1
        if hourly_index < 0:
            continue
        if rows_1h[hourly_index].close_time_ms < rows_5m[segment_start[index]].open_time_ms:
            continue
        regime = regimes[hourly_index]
        if regime is None or not regime.eligible:
            continue

        previous_lower = previous_vwap - settings.band_atr_multiple * previous_atr
        previous_upper = previous_vwap + settings.band_atr_multiple * previous_atr
        current_lower = current_vwap - settings.band_atr_multiple * current_atr
        current_upper = current_vwap + settings.band_atr_multiple * current_atr
        side: Side | None = None
        excursion_atr = 0.0
        if previous.close < previous_lower and current_lower <= current.close < current_vwap:
            side = Side.LONG
            excursion_atr = (previous_lower - previous.close) / previous_atr
        elif previous.close > previous_upper and current_vwap < current.close <= current_upper:
            side = Side.SHORT
            excursion_atr = (previous.close - previous_upper) / previous_atr
        if side is None:
            continue

        reference = current.close
        target = (
            current_vwap + settings.target_atr_extension * current_atr
            if side is Side.LONG
            else current_vwap - settings.target_atr_extension * current_atr
        )
        reward_distance = abs(target - reference)
        stop_distance = settings.stop_atr_multiple * current_atr
        if settings.minimum_gross_reward_to_risk is not None:
            stop_distance = min(
                stop_distance,
                reward_distance / settings.minimum_gross_reward_to_risk,
            )
        stop = reference - stop_distance if side is Side.LONG else reference + stop_distance
        if stop <= 0 or target == reference:
            continue
        gross_reward_bps = abs(target - reference) / reference * 10_000
        flatness = 1 - regime.efficiency
        signal_strength = min(1.0, max(0.0, 0.5 + 0.25 * excursion_atr + 0.25 * flatness))
        intents.append(
            SleeveIntent(
                sleeve_id=_STRATEGY_VERSION,
                symbol=current.symbol,
                side=side,
                decision_ts_ms=current.close_time_ms,
                entry_eligible_ts_ms=current.close_time_ms + 1,
                entry_expires_ts_ms=current.close_time_ms + settings.intent_valid_bars * _FIVE_MINUTES_MS,
                reference_price=reference,
                signal_strength=signal_strength,
                gross_reward_bps=gross_reward_bps,
                exit_plan=ExitPlan(
                    stop_price=stop,
                    target_price=target,
                    max_holding_ms=settings.max_hold_bars * _FIVE_MINUTES_MS,
                ),
                metadata=(
                    ("atr", _number(current_atr)),
                    ("config_sha256", settings.fingerprint),
                    ("excursion_atr", _number(excursion_atr)),
                    ("regime_efficiency", _number(regime.efficiency)),
                    ("regime_slope_per_hour", _number(regime.slope_per_hour)),
                    ("gross_reward_to_risk", _number(reward_distance / stop_distance)),
                    ("strategy_version", _STRATEGY_VERSION),
                    ("vwap", _number(current_vwap)),
                ),
            )
        )
    return intents
