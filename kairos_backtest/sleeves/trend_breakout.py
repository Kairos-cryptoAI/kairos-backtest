"""Causal five-minute trend breakout sleeve.

The sleeve deliberately has no position sizing policy.  It emits a bounded,
fully specified trade intent from complete OHLCV bars; portfolio construction
and risk allocation belong to the strategy orchestrator and risk manager.
"""

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
_BPS = 10_000.0
_STRATEGY_VERSION = "trend_breakout_v1"


@dataclass(frozen=True, slots=True)
class TrendBreakoutConfig:
    """Fixed research controls for the first trend sleeve.

    Defaults are methodology choices, not parameters selected on the July
    holdout.  Volume and taker-share confirmation remain opt-in because the
    OHLC-only breakout is the reproducible baseline.
    """

    donchian_lookback: int = 20
    atr_period: int = 14
    regime_lookback_hours: int = 12
    minimum_regime_efficiency: float = 0.30
    minimum_abs_hourly_slope: float = 0.00025
    minimum_breakout_atr: float = 0.0
    stop_atr_multiple: float = 1.25
    target_atr_multiple: float = 2.0
    trailing_activation_atr_multiple: float | None = 1.0
    trailing_distance_atr_multiple: float | None = 1.0
    max_hold_bars: int = 24
    intent_valid_bars: int = 1
    volume_lookback: int = 20
    minimum_volume_surprise: float | None = None
    minimum_directional_taker_share: float | None = None

    def __post_init__(self) -> None:
        integer_values = (
            self.donchian_lookback,
            self.atr_period,
            self.regime_lookback_hours,
            self.max_hold_bars,
            self.intent_valid_bars,
            self.volume_lookback,
        )
        if any(isinstance(value, bool) or not isinstance(value, int) for value in integer_values):
            raise ValueError("trend breakout periods must be integers")
        if min(integer_values) <= 0:
            raise ValueError("trend breakout periods must be positive")
        positive_values = (
            self.stop_atr_multiple,
            self.target_atr_multiple,
        )
        if not all(
            not isinstance(value, bool) and math.isfinite(value) and value > 0 for value in positive_values
        ):
            raise ValueError("ATR exit multiples must be finite and positive")
        if (
            isinstance(self.minimum_regime_efficiency, bool)
            or not math.isfinite(self.minimum_regime_efficiency)
            or not 0 <= self.minimum_regime_efficiency <= 1
        ):
            raise ValueError("minimum regime efficiency must be within [0, 1]")
        if (
            isinstance(self.minimum_abs_hourly_slope, bool)
            or not math.isfinite(self.minimum_abs_hourly_slope)
            or self.minimum_abs_hourly_slope < 0
        ):
            raise ValueError("minimum hourly slope must be finite and non-negative")
        if (
            isinstance(self.minimum_breakout_atr, bool)
            or not math.isfinite(self.minimum_breakout_atr)
            or not 0 <= self.minimum_breakout_atr <= 2
        ):
            raise ValueError("minimum breakout distance must be within [0, 2] ATR")
        trailing = (
            self.trailing_activation_atr_multiple,
            self.trailing_distance_atr_multiple,
        )
        if (trailing[0] is None) != (trailing[1] is None):
            raise ValueError("trend trailing activation and distance must be configured together")
        if any(
            value is not None and (isinstance(value, bool) or not math.isfinite(value) or value <= 0)
            for value in trailing
        ):
            raise ValueError("trend trailing ATR multiples must be finite and positive")
        if (
            self.trailing_activation_atr_multiple is not None
            and self.trailing_activation_atr_multiple >= self.target_atr_multiple
        ):
            raise ValueError("trend trailing activation must occur before the target")
        if self.minimum_volume_surprise is not None and (
            isinstance(self.minimum_volume_surprise, bool)
            or not math.isfinite(self.minimum_volume_surprise)
            or self.minimum_volume_surprise <= 0
        ):
            raise ValueError("minimum volume surprise must be finite and positive")
        taker_share = self.minimum_directional_taker_share
        if taker_share is not None and (
            isinstance(taker_share, bool) or not math.isfinite(taker_share) or not 0.5 <= taker_share <= 1
        ):
            raise ValueError("minimum directional taker share must be within [0.5, 1]")
        for name in (
            "minimum_regime_efficiency",
            "minimum_abs_hourly_slope",
            "minimum_breakout_atr",
            "stop_atr_multiple",
            "target_atr_multiple",
            "trailing_activation_atr_multiple",
            "trailing_distance_atr_multiple",
            "minimum_volume_surprise",
            "minimum_directional_taker_share",
        ):
            value = getattr(self, name)
            if value is not None:
                object.__setattr__(self, name, float(value))

    @property
    def fingerprint(self) -> str:
        payload = json.dumps(asdict(self), sort_keys=True, separators=(",", ":")).encode()
        return hashlib.sha256(payload).hexdigest()


@dataclass(frozen=True, slots=True)
class _Regime:
    direction: int
    slope_per_hour: float
    efficiency: float


def _contiguous_segments(rows: list[Candle], interval_ms: int) -> list[tuple[int, int]]:
    """Return half-open index ranges without carrying state across gaps."""
    if not rows:
        return []
    starts = [0]
    for index in range(1, len(rows)):
        previous, current = rows[index - 1], rows[index]
        if (
            current.open_time_ms - previous.open_time_ms != interval_ms
            or current.open_time_ms != previous.close_time_ms + 1
        ):
            starts.append(index)
    return list(zip(starts, (*starts[1:], len(rows)), strict=True))


def _wilder_atr(rows: list[Candle], period: int) -> np.ndarray:
    """Calculate Wilder ATR independently inside every contiguous segment."""
    values = np.full(len(rows), np.nan, dtype=float)
    for start, end in _contiguous_segments(rows, _FIVE_MINUTES_MS):
        segment_size = end - start
        if segment_size < period:
            continue
        true_ranges = np.empty(segment_size, dtype=float)
        for offset, index in enumerate(range(start, end)):
            row = rows[index]
            if offset == 0:
                true_ranges[offset] = row.high - row.low
                continue
            previous_close = rows[index - 1].close
            true_ranges[offset] = max(
                row.high - row.low,
                abs(row.high - previous_close),
                abs(row.low - previous_close),
            )
        seed_index = period - 1
        atr = float(np.mean(true_ranges[:period]))
        values[start + seed_index] = atr
        for offset in range(period, segment_size):
            atr = (atr * (period - 1) + true_ranges[offset]) / period
            values[start + offset] = atr
    return values


def _hourly_regimes(rows: list[Candle], config: TrendBreakoutConfig) -> list[_Regime | None]:
    """Measure log-price slope and Kaufman-style efficiency on closed hours."""
    output: list[_Regime | None] = [None] * len(rows)
    observations = config.regime_lookback_hours + 1
    x_axis = np.arange(observations, dtype=float)
    x_centered = x_axis - float(np.mean(x_axis))
    denominator = float(np.dot(x_centered, x_centered))
    for start, end in _contiguous_segments(rows, _ONE_HOUR_MS):
        for index in range(start + observations - 1, end):
            closes = np.asarray(
                [row.close for row in rows[index - observations + 1 : index + 1]],
                dtype=float,
            )
            log_closes = np.log(closes)
            slope = float(np.dot(x_centered, log_closes - np.mean(log_closes)) / denominator)
            movement = float(np.sum(np.abs(np.diff(closes))))
            efficiency = abs(float(closes[-1] - closes[0])) / movement if movement > 0 else 0.0
            direction = 0
            if efficiency >= config.minimum_regime_efficiency:
                if slope >= config.minimum_abs_hourly_slope:
                    direction = 1
                elif slope <= -config.minimum_abs_hourly_slope:
                    direction = -1
            output[index] = _Regime(direction, slope, efficiency)
    return output


def _canonical_number(value: float) -> str:
    if not math.isfinite(value):
        raise ValueError("intent features must be finite")
    return format(value, ".17g")


def _feature_hash(payload: dict[str, str | int]) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
    return hashlib.sha256(encoded).hexdigest()


def _passes_optional_flow_filters(
    rows: list[Candle],
    index: int,
    segment_start: int,
    side: Side,
    config: TrendBreakoutConfig,
) -> tuple[bool, float | None, float | None]:
    if config.minimum_volume_surprise is None and config.minimum_directional_taker_share is None:
        return True, None, None
    if index - segment_start < config.volume_lookback:
        return False, None, None
    current = rows[index]
    history = rows[index - config.volume_lookback : index]
    mean_volume = sum(row.volume for row in history) / config.volume_lookback
    if mean_volume <= 0 or current.volume <= 0:
        return False, None, None
    volume_surprise = current.volume / mean_volume
    taker_buy_share = current.taker_buy_volume / current.volume
    if config.minimum_volume_surprise is not None and volume_surprise < config.minimum_volume_surprise:
        return False, volume_surprise, taker_buy_share
    directional_share = taker_buy_share if side is Side.LONG else 1 - taker_buy_share
    if (
        config.minimum_directional_taker_share is not None
        and directional_share < config.minimum_directional_taker_share
    ):
        return False, volume_surprise, taker_buy_share
    return True, volume_surprise, taker_buy_share


def generate_trend_breakout_intents(
    candles_1m: list[Candle],
    config: TrendBreakoutConfig | None = None,
) -> list[SleeveIntent]:
    """Emit right-closed Donchian breakouts with an hourly trend regime.

    The breakout channel is always ``rows[index-lookback:index]``: the current
    five-minute bar can cross a threshold, but can never contribute to that
    threshold.  The returned intent becomes eligible at the next millisecond
    (the next bar open for aligned input) and expires after a finite number of
    five-minute bars.
    """
    settings = TrendBreakoutConfig() if config is None else config
    if not isinstance(settings, TrendBreakoutConfig):
        raise ValueError("config must be a TrendBreakoutConfig")
    frames = build_timeframes(candles_1m)
    rows_5m, rows_1h = frames["5m"], frames["1h"]
    if not rows_5m or not rows_1h:
        return []

    atr_values = _wilder_atr(rows_5m, settings.atr_period)
    hourly_regimes = _hourly_regimes(rows_1h, settings)
    hourly_closes = [row.close_time_ms for row in rows_1h]
    segment_starts = [0] * len(rows_5m)
    for start, end in _contiguous_segments(rows_5m, _FIVE_MINUTES_MS):
        segment_starts[start:end] = [start] * (end - start)

    intents: list[SleeveIntent] = []
    for index, current in enumerate(rows_5m):
        segment_start = segment_starts[index]
        if index - segment_start < settings.donchian_lookback:
            continue
        atr = float(atr_values[index])
        if not math.isfinite(atr) or atr <= 0:
            continue
        hourly_index = bisect_right(hourly_closes, current.close_time_ms) - 1
        if hourly_index < 0:
            continue
        if rows_1h[hourly_index].close_time_ms < rows_5m[segment_start].open_time_ms:
            # A still-cached pre-gap hourly regime cannot authorize a trade in
            # the new five-minute segment.
            continue
        regime = hourly_regimes[hourly_index]
        if regime is None or regime.direction == 0:
            continue

        history = rows_5m[index - settings.donchian_lookback : index]
        channel_high = max(row.high for row in history)
        channel_low = min(row.low for row in history)
        side: Side | None = None
        if current.close > channel_high + settings.minimum_breakout_atr * atr and regime.direction == 1:
            side = Side.LONG
        elif current.close < channel_low - settings.minimum_breakout_atr * atr and regime.direction == -1:
            side = Side.SHORT
        if side is None:
            continue

        passed, volume_surprise, taker_buy_share = _passes_optional_flow_filters(
            rows_5m,
            index,
            segment_start,
            side,
            settings,
        )
        if not passed:
            continue

        reference = current.close
        breakout_distance_atr = (
            (reference - channel_high) / atr if side is Side.LONG else (channel_low - reference) / atr
        )
        if side is Side.LONG:
            stop = reference - settings.stop_atr_multiple * atr
            target = reference + settings.target_atr_multiple * atr
            trailing_activation = (
                reference + settings.trailing_activation_atr_multiple * atr
                if settings.trailing_activation_atr_multiple is not None
                else None
            )
        else:
            stop = reference + settings.stop_atr_multiple * atr
            target = reference - settings.target_atr_multiple * atr
            trailing_activation = (
                reference - settings.trailing_activation_atr_multiple * atr
                if settings.trailing_activation_atr_multiple is not None
                else None
            )
        trailing_distance = (
            settings.trailing_distance_atr_multiple * atr
            if settings.trailing_distance_atr_multiple is not None
            else None
        )
        if min(stop, target) <= 0 or not all(math.isfinite(value) for value in (stop, target)):
            continue

        valid_until_ms = current.close_time_ms + settings.intent_valid_bars * _FIVE_MINUTES_MS
        feature_payload: dict[str, str | int] = {
            "atr": _canonical_number(atr),
            "breakout_distance_atr": _canonical_number(breakout_distance_atr),
            "channel_high": _canonical_number(channel_high),
            "channel_low": _canonical_number(channel_low),
            "config_sha256": settings.fingerprint,
            "decision_ts_ms": current.close_time_ms,
            "hourly_efficiency": _canonical_number(regime.efficiency),
            "hourly_slope": _canonical_number(regime.slope_per_hour),
            "reference_price": _canonical_number(reference),
            "side": side.value,
            "strategy_version": _STRATEGY_VERSION,
            "symbol": current.symbol,
        }
        if volume_surprise is not None and taker_buy_share is not None:
            feature_payload["volume_surprise"] = _canonical_number(volume_surprise)
            feature_payload["taker_buy_share"] = _canonical_number(taker_buy_share)
        metadata = [
            ("breakout_distance_atr", _canonical_number(breakout_distance_atr)),
            ("config_sha256", settings.fingerprint),
            ("feature_hash", _feature_hash(feature_payload)),
            ("strategy_version", _STRATEGY_VERSION),
        ]
        if volume_surprise is not None and taker_buy_share is not None:
            metadata.extend(
                (
                    ("taker_buy_share", _canonical_number(taker_buy_share)),
                    ("volume_surprise", _canonical_number(volume_surprise)),
                )
            )
        intents.append(
            SleeveIntent(
                sleeve_id=_STRATEGY_VERSION,
                symbol=current.symbol,
                side=side,
                decision_ts_ms=current.close_time_ms,
                entry_eligible_ts_ms=current.close_time_ms + 1,
                entry_expires_ts_ms=valid_until_ms,
                reference_price=reference,
                signal_strength=min(
                    1.0,
                    max(
                        0.0,
                        (
                            regime.efficiency
                            + min(
                                1.0,
                                abs(regime.slope_per_hour) / max(settings.minimum_abs_hourly_slope, 1e-12),
                            )
                            + min(
                                1.0,
                                abs(current.close - (channel_high if side is Side.LONG else channel_low))
                                / atr,
                            )
                        )
                        / 3,
                    ),
                ),
                gross_reward_bps=settings.target_atr_multiple * atr / reference * _BPS,
                exit_plan=ExitPlan(
                    stop_price=stop,
                    target_price=target,
                    max_holding_ms=settings.max_hold_bars * _FIVE_MINUTES_MS,
                    trailing_activation_price=trailing_activation,
                    trailing_distance=trailing_distance,
                ),
                metadata=tuple(sorted(metadata)),
            )
        )
    return intents
