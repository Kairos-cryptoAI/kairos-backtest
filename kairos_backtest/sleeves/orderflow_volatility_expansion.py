"""Causal order-flow-confirmed volatility expansion sleeve.

The signal is evaluated only after a complete five-minute candle.  Every
baseline excludes that signal candle, every stateful feature resets at a data
gap, and an emitted intent can first enter at the following five-minute open.
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import asdict, dataclass
from enum import StrEnum
from statistics import median

import numpy as np
from kairos_core.enums import Side
from kairos_quant.candles import Candle

from ..strategy_models import ExitPlan, SleeveIntent
from ..timeframes import aggregate

_FIVE_MINUTES_MS = 5 * 60 * 1_000
_BPS = 10_000.0
_STRATEGY_VERSION = "orderflow_volatility_expansion_v1"


class OrderFlowExpansionVariant(StrEnum):
    """Predeclared, mutually exclusive order-flow explanations.

    The raw predicates can overlap.  Ownership is deliberately fixed and is
    independent of the configured variant: flip-release owns an overlap first,
    persistence second, and an otherwise unowned impulse last.
    """

    IMPULSE = "impulse"
    PERSISTENCE = "persistence"
    FLIP_RELEASE = "flip_release"


@dataclass(frozen=True, slots=True)
class OrderFlowVolatilityExpansionConfig:
    """Fixed controls for the first order-flow expansion experiment."""

    variant: OrderFlowExpansionVariant = OrderFlowExpansionVariant.IMPULSE
    baseline_lookback: int = 24
    compression_short_lookback: int = 6
    compression_long_lookback: int = 72
    atr_period: int = 24
    persistence_lookback: int = 3
    persistence_minimum_directional_bars: int = 2
    flip_lookback: int = 3
    maximum_compression_ratio: float = 0.75
    minimum_range_expansion: float = 1.5
    minimum_volume_surprise: float = 1.5
    minimum_close_location: float = 0.75
    minimum_body_fraction: float = 0.35
    minimum_atr_bps: float = 25.0
    maximum_atr_bps: float = 250.0
    minimum_impulse_directional_imbalance: float = 0.20
    minimum_persistence_directional_imbalance: float = 0.12
    minimum_persistence_bar_imbalance: float = 0.05
    minimum_persistence_current_imbalance: float = 0.05
    maximum_flip_prior_directional_imbalance: float = -0.10
    minimum_flip_current_imbalance: float = 0.20
    stop_atr_multiple: float = 1.25
    target_reward_to_risk: float = 3.0
    max_hold_bars: int = 12
    intent_valid_bars: int = 1

    def __post_init__(self) -> None:
        if not isinstance(self.variant, OrderFlowExpansionVariant):
            raise ValueError("variant must be an OrderFlowExpansionVariant")

        periods = (
            self.baseline_lookback,
            self.compression_short_lookback,
            self.compression_long_lookback,
            self.atr_period,
            self.persistence_lookback,
            self.persistence_minimum_directional_bars,
            self.flip_lookback,
            self.max_hold_bars,
            self.intent_valid_bars,
        )
        if any(isinstance(value, bool) or not isinstance(value, int) for value in periods):
            raise ValueError("order-flow periods and counts must be integers")
        if min(periods) <= 0:
            raise ValueError("order-flow periods and counts must be positive")
        if self.compression_short_lookback >= self.compression_long_lookback:
            raise ValueError("short compression lookback must be below the long lookback")
        if self.baseline_lookback > self.compression_long_lookback:
            raise ValueError("baseline lookback cannot exceed the long compression lookback")
        if self.persistence_minimum_directional_bars > self.persistence_lookback:
            raise ValueError("directional bar count cannot exceed the persistence lookback")

        positive_names = (
            "maximum_compression_ratio",
            "minimum_range_expansion",
            "minimum_volume_surprise",
            "minimum_close_location",
            "minimum_body_fraction",
            "minimum_atr_bps",
            "maximum_atr_bps",
            "minimum_impulse_directional_imbalance",
            "minimum_persistence_directional_imbalance",
            "minimum_persistence_bar_imbalance",
            "minimum_persistence_current_imbalance",
            "minimum_flip_current_imbalance",
            "stop_atr_multiple",
            "target_reward_to_risk",
        )
        for name in positive_names:
            value = getattr(self, name)
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(value)
                or value <= 0
            ):
                raise ValueError(f"{name} must be finite and positive")
            object.__setattr__(self, name, float(value))

        flip_limit = self.maximum_flip_prior_directional_imbalance
        if (
            isinstance(flip_limit, bool)
            or not isinstance(flip_limit, (int, float))
            or not math.isfinite(flip_limit)
            or not -1 <= flip_limit < 0
        ):
            raise ValueError("maximum flip prior imbalance must be within [-1, 0)")
        object.__setattr__(
            self,
            "maximum_flip_prior_directional_imbalance",
            float(flip_limit),
        )

        if self.maximum_compression_ratio > 1:
            raise ValueError("maximum compression ratio must not exceed one")
        if self.minimum_range_expansion < 1:
            raise ValueError("minimum range expansion must be at least one")
        if self.minimum_volume_surprise < 1:
            raise ValueError("minimum volume surprise must be at least one")
        if self.maximum_atr_bps < self.minimum_atr_bps:
            raise ValueError("maximum ATR bps cannot be below the minimum")
        for name in (
            "minimum_close_location",
            "minimum_body_fraction",
            "minimum_impulse_directional_imbalance",
            "minimum_persistence_directional_imbalance",
            "minimum_persistence_bar_imbalance",
            "minimum_persistence_current_imbalance",
            "minimum_flip_current_imbalance",
        ):
            if getattr(self, name) > 1:
                raise ValueError(f"{name} must not exceed one")

    @property
    def fingerprint(self) -> str:
        payload = json.dumps(
            asdict(self),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        ).encode()
        return hashlib.sha256(payload).hexdigest()


def _segments(rows: list[Candle]) -> list[tuple[int, int]]:
    """Return contiguous positive-volume ranges without carrying stale state."""

    if not rows:
        return []
    segments: list[tuple[int, int]] = []
    start: int | None = None
    previous: Candle | None = None
    for index, current in enumerate(rows):
        if not math.isfinite(current.volume) or current.volume <= 0:
            if start is not None:
                segments.append((start, index))
            start = None
            previous = None
            continue
        if start is None:
            start = index
        elif previous is not None and (
            current.open_time_ms - previous.open_time_ms != _FIVE_MINUTES_MS
            or current.open_time_ms != previous.close_time_ms + 1
        ):
            segments.append((start, index))
            start = index
        previous = current
    if start is not None:
        segments.append((start, len(rows)))
    return segments


def _true_ranges(rows: list[Candle]) -> np.ndarray:
    """Return gap-segmented true ranges."""

    values = np.full(len(rows), np.nan, dtype=float)
    for start, end in _segments(rows):
        for index in range(start, end):
            row = rows[index]
            if index == start:
                values[index] = row.high - row.low
            else:
                previous_close = rows[index - 1].close
                values[index] = max(
                    row.high - row.low,
                    abs(row.high - previous_close),
                    abs(row.low - previous_close),
                )
    return values


def _wilder_atr(rows: list[Candle], period: int) -> np.ndarray:
    """Return a Wilder ATR seeded and advanced independently per segment."""

    values = np.full(len(rows), np.nan, dtype=float)
    true_ranges = _true_ranges(rows)
    for start, end in _segments(rows):
        if end - start < period:
            continue
        seed = start + period - 1
        atr = float(np.mean(true_ranges[start : seed + 1]))
        values[seed] = atr
        for index in range(seed + 1, end):
            atr = (atr * (period - 1) + float(true_ranges[index])) / period
            values[index] = atr
    return values


def _number(value: float) -> str:
    if not math.isfinite(value):
        raise ValueError("order-flow intent evidence must be finite")
    normalized = 0.0 if value == 0 else value
    return format(normalized, ".17g")


def _feature_hash(payload: dict[str, str | int]) -> str:
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def _raw_variant_predicates(
    *,
    direction: int,
    deltas: list[float],
    volumes: list[float],
    current_directional_imbalance: float,
    settings: OrderFlowVolatilityExpansionConfig,
) -> tuple[
    dict[OrderFlowExpansionVariant, bool],
    float,
    int,
    float,
]:
    persistence_deltas = deltas[-settings.persistence_lookback :]
    persistence_volumes = volumes[-settings.persistence_lookback :]
    persistence_volume = sum(persistence_volumes)
    persistence_directional_imbalance = (
        direction * sum(persistence_deltas) / persistence_volume if persistence_volume > 0 else math.nan
    )
    persistence_directional_bars = sum(
        direction * delta / volume >= settings.minimum_persistence_bar_imbalance
        for delta, volume in zip(persistence_deltas, persistence_volumes, strict=True)
        if volume > 0
    )

    flip_deltas = deltas[-settings.flip_lookback - 1 : -1]
    flip_volumes = volumes[-settings.flip_lookback - 1 : -1]
    flip_volume = sum(flip_volumes)
    flip_prior_directional_imbalance = (
        direction * sum(flip_deltas) / flip_volume if flip_volume > 0 else math.nan
    )

    impulse = (
        math.isfinite(current_directional_imbalance)
        and current_directional_imbalance >= settings.minimum_impulse_directional_imbalance
    )
    persistence = (
        math.isfinite(persistence_directional_imbalance)
        and persistence_directional_imbalance >= settings.minimum_persistence_directional_imbalance
        and persistence_directional_bars >= settings.persistence_minimum_directional_bars
        and current_directional_imbalance >= settings.minimum_persistence_current_imbalance
    )
    flip_release = (
        math.isfinite(flip_prior_directional_imbalance)
        and flip_prior_directional_imbalance <= settings.maximum_flip_prior_directional_imbalance
        and current_directional_imbalance >= settings.minimum_flip_current_imbalance
    )
    return (
        {
            OrderFlowExpansionVariant.IMPULSE: impulse,
            OrderFlowExpansionVariant.PERSISTENCE: persistence,
            OrderFlowExpansionVariant.FLIP_RELEASE: flip_release,
        },
        persistence_directional_imbalance,
        persistence_directional_bars,
        flip_prior_directional_imbalance,
    )


def _assigned_variant(
    predicates: dict[OrderFlowExpansionVariant, bool],
) -> OrderFlowExpansionVariant | None:
    for variant in (
        OrderFlowExpansionVariant.FLIP_RELEASE,
        OrderFlowExpansionVariant.PERSISTENCE,
        OrderFlowExpansionVariant.IMPULSE,
    ):
        if predicates[variant]:
            return variant
    return None


def _signal_strength(
    *,
    range_expansion: float,
    volume_surprise: float,
    close_location: float,
    body_fraction: float,
    flow_strength: float,
    settings: OrderFlowVolatilityExpansionConfig,
) -> float:
    """Return a bounded rule diagnostic; it is not a probability or size."""

    components = (
        min(1.0, range_expansion / (2 * settings.minimum_range_expansion)),
        min(1.0, volume_surprise / (2 * settings.minimum_volume_surprise)),
        min(1.0, max(0.0, close_location)),
        min(1.0, max(0.0, body_fraction)),
        min(1.0, max(0.0, flow_strength)),
    )
    return sum(components) / len(components)


def generate_orderflow_volatility_expansion_intents(
    candles_1m: list[Candle],
    config: OrderFlowVolatilityExpansionConfig | None = None,
) -> list[SleeveIntent]:
    """Emit one-bar next-open intents for compressed, flow-backed expansions."""

    if config is None:
        settings = OrderFlowVolatilityExpansionConfig()
    elif isinstance(config, OrderFlowVolatilityExpansionConfig):
        settings = config
    else:
        raise ValueError("config must be an OrderFlowVolatilityExpansionConfig or None")

    rows = aggregate(candles_1m, "5m")
    if not rows:
        return []
    true_ranges = _true_ranges(rows)
    atr_values = _wilder_atr(rows, settings.atr_period)

    intents: list[SleeveIntent] = []
    required_prior = max(
        settings.baseline_lookback,
        settings.compression_long_lookback,
        settings.persistence_lookback - 1,
        settings.flip_lookback,
        settings.atr_period,
    )
    for segment_start, segment_end in _segments(rows):
        for index in range(segment_start + required_prior, segment_end):
            current = rows[index]
            previous_atr = float(atr_values[index - 1])
            current_true_range = float(true_ranges[index])
            candle_range = current.high - current.low
            if not all(
                math.isfinite(value) and value > 0
                for value in (
                    previous_atr,
                    current_true_range,
                    candle_range,
                    current.volume,
                    current.close,
                )
            ):
                continue

            prior_true_ranges = true_ranges[index - settings.baseline_lookback : index]
            short_true_ranges = true_ranges[index - settings.compression_short_lookback : index]
            long_true_ranges = true_ranges[index - settings.compression_long_lookback : index]
            prior_volumes = [row.volume for row in rows[index - settings.baseline_lookback : index]]
            range_baseline = float(median(prior_true_ranges))
            short_range = float(median(short_true_ranges))
            long_range = float(median(long_true_ranges))
            volume_baseline = float(median(prior_volumes))
            if not all(
                math.isfinite(value) and value > 0
                for value in (range_baseline, short_range, long_range, volume_baseline)
            ):
                continue

            range_expansion = current_true_range / range_baseline
            volume_surprise = current.volume / volume_baseline
            compression_ratio = short_range / long_range
            atr_bps = previous_atr / rows[index - 1].close * _BPS
            if current.close > current.open:
                side = Side.LONG
                direction = 1
                close_location = (current.close - current.low) / candle_range
            elif current.close < current.open:
                side = Side.SHORT
                direction = -1
                close_location = (current.high - current.close) / candle_range
            else:
                continue
            body_fraction = abs(current.close - current.open) / candle_range

            if not all(
                math.isfinite(value)
                for value in (
                    range_expansion,
                    volume_surprise,
                    compression_ratio,
                    atr_bps,
                    close_location,
                    body_fraction,
                )
            ):
                continue
            if not (
                compression_ratio <= settings.maximum_compression_ratio
                and range_expansion >= settings.minimum_range_expansion
                and volume_surprise >= settings.minimum_volume_surprise
                and close_location >= settings.minimum_close_location
                and body_fraction >= settings.minimum_body_fraction
                and settings.minimum_atr_bps <= atr_bps <= settings.maximum_atr_bps
            ):
                continue

            flow_start = index - max(settings.persistence_lookback - 1, settings.flip_lookback)
            flow_rows = rows[flow_start : index + 1]
            volumes = [row.volume for row in flow_rows]
            if any(not math.isfinite(volume) or volume <= 0 for volume in volumes):
                continue
            deltas = [2 * row.taker_buy_volume - row.volume for row in flow_rows]
            current_imbalance = deltas[-1] / volumes[-1]
            current_directional_imbalance = direction * current_imbalance
            (
                predicates,
                persistence_directional_imbalance,
                persistence_directional_bars,
                flip_prior_directional_imbalance,
            ) = _raw_variant_predicates(
                direction=direction,
                deltas=deltas,
                volumes=volumes,
                current_directional_imbalance=current_directional_imbalance,
                settings=settings,
            )
            assigned = _assigned_variant(predicates)
            if assigned is not settings.variant:
                continue

            reference = current.close
            risk_distance = settings.stop_atr_multiple * previous_atr
            if side is Side.LONG:
                stop = reference - risk_distance
                target = reference + settings.target_reward_to_risk * risk_distance
            else:
                stop = reference + risk_distance
                target = reference - settings.target_reward_to_risk * risk_distance
            if not all(math.isfinite(value) and value > 0 for value in (stop, target)):
                continue

            raw_flags = {
                "flip_release_raw": int(predicates[OrderFlowExpansionVariant.FLIP_RELEASE]),
                "impulse_raw": int(predicates[OrderFlowExpansionVariant.IMPULSE]),
                "persistence_raw": int(predicates[OrderFlowExpansionVariant.PERSISTENCE]),
            }
            feature_payload: dict[str, str | int] = {
                "assigned_variant": assigned.value,
                "atr_bps": _number(atr_bps),
                "body_fraction": _number(body_fraction),
                "close_location": _number(close_location),
                "compression_ratio": _number(compression_ratio),
                "config_sha256": settings.fingerprint,
                "current_imbalance": _number(current_imbalance),
                "current_directional_imbalance": _number(current_directional_imbalance),
                "decision_ts_ms": current.close_time_ms,
                "flip_prior_directional_imbalance": _number(flip_prior_directional_imbalance),
                **raw_flags,
                "persistence_directional_bars": persistence_directional_bars,
                "persistence_directional_imbalance": _number(persistence_directional_imbalance),
                "prior_directional_imbalance": _number(flip_prior_directional_imbalance),
                "range_expansion": _number(range_expansion),
                "reference_price": _number(reference),
                "side": side.value,
                "stop_price": _number(stop),
                "strategy_version": _STRATEGY_VERSION,
                "symbol": current.symbol,
                "target_price": _number(target),
                "volume_surprise": _number(volume_surprise),
            }
            if assigned is OrderFlowExpansionVariant.FLIP_RELEASE:
                flow_strength = (
                    current_directional_imbalance + max(0.0, -flip_prior_directional_imbalance)
                ) / 2
            elif assigned is OrderFlowExpansionVariant.PERSISTENCE:
                flow_strength = persistence_directional_imbalance
            else:
                flow_strength = current_directional_imbalance

            intents.append(
                SleeveIntent(
                    sleeve_id=_STRATEGY_VERSION,
                    symbol=current.symbol,
                    side=side,
                    decision_ts_ms=current.close_time_ms,
                    entry_eligible_ts_ms=current.close_time_ms + 1,
                    entry_expires_ts_ms=(
                        current.close_time_ms + settings.intent_valid_bars * _FIVE_MINUTES_MS
                    ),
                    reference_price=reference,
                    signal_strength=_signal_strength(
                        range_expansion=range_expansion,
                        volume_surprise=volume_surprise,
                        close_location=close_location,
                        body_fraction=body_fraction,
                        flow_strength=flow_strength,
                        settings=settings,
                    ),
                    gross_reward_bps=abs(target - reference) / reference * _BPS,
                    exit_plan=ExitPlan(
                        stop_price=stop,
                        target_price=target,
                        max_holding_ms=settings.max_hold_bars * _FIVE_MINUTES_MS,
                    ),
                    metadata=(
                        ("assigned_variant", assigned.value),
                        ("atr_bps", _number(atr_bps)),
                        ("body_fraction", _number(body_fraction)),
                        ("close_location", _number(close_location)),
                        ("compression_ratio", _number(compression_ratio)),
                        ("config_sha256", settings.fingerprint),
                        ("current_imbalance", _number(current_imbalance)),
                        (
                            "current_directional_imbalance",
                            _number(current_directional_imbalance),
                        ),
                        (
                            "feature_hash",
                            _feature_hash(feature_payload),
                        ),
                        (
                            "flip_prior_directional_imbalance",
                            _number(flip_prior_directional_imbalance),
                        ),
                        ("flip_release_raw", str(raw_flags["flip_release_raw"])),
                        ("impulse_raw", str(raw_flags["impulse_raw"])),
                        (
                            "persistence_directional_bars",
                            str(persistence_directional_bars),
                        ),
                        (
                            "persistence_directional_imbalance",
                            _number(persistence_directional_imbalance),
                        ),
                        ("persistence_raw", str(raw_flags["persistence_raw"])),
                        (
                            "prior_directional_imbalance",
                            _number(flip_prior_directional_imbalance),
                        ),
                        ("range_expansion", _number(range_expansion)),
                        ("strategy_version", _STRATEGY_VERSION),
                        ("variant", settings.variant.value),
                        ("volume_surprise", _number(volume_surprise)),
                    ),
                )
            )
    return intents
