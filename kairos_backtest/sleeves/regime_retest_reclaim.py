"""Causal regime-vetoed breakout retest/reclaim strategy sleeve.

An expansion candle only arms a setup.  Entry can be emitted after a later
closed five-minute candle retests the frozen pre-breakout boundary and closes
back through it.  Every rolling input excludes the expansion candle, the
hourly regime is strictly prior to that candle, and gaps or zero-volume source
candles reset all feature and setup state.
"""

from __future__ import annotations

import hashlib
import json
import math
from bisect import bisect_left
from dataclasses import asdict, dataclass
from enum import StrEnum

import numpy as np
from kairos_core.enums import Side
from kairos_quant.candles import Candle

from ..strategy_models import ExitPlan, SleeveIntent
from ..timeframes import aggregate
from ..validation import canonical_candles

_MINUTE_MS = 60_000
_FIVE_MINUTES_MS = 5 * _MINUTE_MS
_ONE_HOUR_MS = 60 * _MINUTE_MS
_BPS = 10_000.0
_STRATEGY_VERSION = "regime_veto_retest_reclaim_v1"
_EVENT_SCHEMA = "kairos.regime-retest-setup-event.v1"
_SETUP_INVENTORY_DOMAIN = b"kairos.regime-retest-setup-inventory.v1\0"
_OUTCOME_INVENTORY_DOMAIN = b"kairos.regime-retest-outcome-inventory.v1\0"


class RegimeRetestReclaimVariant(StrEnum):
    """The three preregistered explanations for a structural reclaim."""

    STRUCTURAL_RECLAIM = "structural_reclaim"
    FLOW_REACCELERATION = "flow_reacceleration"
    ABSORPTION_RECLAIM = "absorption_reclaim"


class RegimeRetestSetupEventType(StrEnum):
    """Causal state transitions retained by pre-run generation evidence."""

    STRUCTURAL_BREAKOUT_CANDIDATE = "structural_breakout_candidate"
    REGIME_REJECT = "regime_reject"
    EXPANSION_REJECT = "expansion_reject"
    ARMED_SETUP = "armed_setup"
    BOUNDARY_FAILURE = "boundary_failure"
    OVEREXTENSION = "overextension"
    EXPIRY = "expiry"
    STRUCTURAL_RECLAIM = "structural_reclaim"
    FLOW_MISMATCH = "flow_mismatch"
    RISK_GEOMETRY_REJECT = "risk_geometry_reject"
    EMITTED_INTENT = "emitted_intent"
    STATE_RESET = "state_reset"
    PENDING_SETUP = "pending_setup"


@dataclass(frozen=True, slots=True)
class RegimeVetoRetestReclaimConfig:
    """Frozen controls for the first regime-vetoed retest experiment."""

    variant: RegimeRetestReclaimVariant = RegimeRetestReclaimVariant.STRUCTURAL_RECLAIM
    baseline_lookback: int = 24
    breakout_lookback: int = 12
    atr_period: int = 24
    hourly_fast_ema_period: int = 24
    hourly_slow_ema_period: int = 72
    hourly_atr_period: int = 24
    hourly_slope_lookback: int = 6
    hourly_efficiency_lookback: int = 24
    setup_window_bars: int = 3

    long_minimum_hourly_slope_atr: float = 0.10
    long_minimum_hourly_efficiency: float = 0.20
    long_maximum_hourly_extension_atr: float = 1.50
    short_maximum_hourly_slope_atr: float = -0.25
    short_minimum_hourly_efficiency: float = 0.30
    short_maximum_hourly_extension_atr: float = 1.00

    long_minimum_atr_bps: float = 25.0
    long_maximum_atr_bps: float = 250.0
    short_minimum_atr_bps: float = 35.0
    short_maximum_atr_bps: float = 200.0
    long_minimum_range_expansion: float = 1.25
    long_minimum_volume_surprise: float = 1.25
    long_minimum_body_fraction: float = 0.30
    long_minimum_trigger_close_location: float = 0.70
    long_minimum_breakout_extension_atr: float = 0.05
    long_maximum_breakout_extension_atr: float = 1.00
    short_minimum_range_expansion: float = 1.50
    short_minimum_volume_surprise: float = 1.50
    short_minimum_body_fraction: float = 0.40
    short_minimum_trigger_close_location: float = 0.80
    short_minimum_breakout_extension_atr: float = 0.10
    short_maximum_breakout_extension_atr: float = 0.75

    long_failure_close_atr: float = 0.25
    long_maximum_pre_retest_advance_atr: float = 0.75
    long_retest_below_boundary_atr: float = 0.35
    long_retest_above_boundary_atr: float = 0.25
    long_minimum_reclaim_extension_atr: float = 0.05
    long_maximum_reclaim_extension_atr: float = 0.60
    long_minimum_reclaim_close_location: float = 0.60
    short_failure_close_atr: float = 0.20
    short_maximum_pre_retest_advance_atr: float = 0.50
    short_retest_below_boundary_atr: float = 0.20
    short_retest_above_boundary_atr: float = 0.25
    short_minimum_reclaim_extension_atr: float = 0.10
    short_maximum_reclaim_extension_atr: float = 0.50
    short_minimum_reclaim_close_location: float = 0.70

    long_minimum_reacceleration_imbalance: float = 0.10
    long_minimum_two_bar_imbalance: float = 0.05
    short_minimum_reacceleration_imbalance: float = 0.15
    short_minimum_two_bar_imbalance: float = 0.08
    long_maximum_absorption_imbalance: float = -0.05
    long_minimum_absorption_volume_surprise: float = 1.00
    short_maximum_absorption_imbalance: float = -0.10
    short_minimum_absorption_volume_surprise: float = 1.25

    long_stop_anchor_buffer_atr: float = 0.15
    long_stop_extreme_buffer_atr: float = 0.10
    long_minimum_risk_atr: float = 0.35
    long_maximum_risk_atr: float = 1.25
    long_target_reward_to_risk: float = 2.50
    long_max_hold_bars: int = 18
    short_stop_anchor_buffer_atr: float = 0.10
    short_stop_extreme_buffer_atr: float = 0.10
    short_minimum_risk_atr: float = 0.35
    short_maximum_risk_atr: float = 1.10
    short_target_reward_to_risk: float = 2.25
    short_max_hold_bars: int = 12
    intent_valid_bars: int = 1

    def __post_init__(self) -> None:
        if not isinstance(self.variant, RegimeRetestReclaimVariant):
            raise ValueError("variant must be a RegimeRetestReclaimVariant")
        periods = (
            self.baseline_lookback,
            self.breakout_lookback,
            self.atr_period,
            self.hourly_fast_ema_period,
            self.hourly_slow_ema_period,
            self.hourly_atr_period,
            self.hourly_slope_lookback,
            self.hourly_efficiency_lookback,
            self.setup_window_bars,
            self.long_max_hold_bars,
            self.short_max_hold_bars,
            self.intent_valid_bars,
        )
        if any(isinstance(value, bool) or not isinstance(value, int) for value in periods):
            raise ValueError("retest periods must be integers")
        if min(periods) <= 0:
            raise ValueError("retest periods must be positive")
        if self.hourly_fast_ema_period >= self.hourly_slow_ema_period:
            raise ValueError("hourly fast EMA period must be below the slow EMA period")

        float_fields = (
            "long_minimum_hourly_slope_atr",
            "long_minimum_hourly_efficiency",
            "long_maximum_hourly_extension_atr",
            "short_maximum_hourly_slope_atr",
            "short_minimum_hourly_efficiency",
            "short_maximum_hourly_extension_atr",
            "long_minimum_atr_bps",
            "long_maximum_atr_bps",
            "short_minimum_atr_bps",
            "short_maximum_atr_bps",
            "long_minimum_range_expansion",
            "long_minimum_volume_surprise",
            "long_minimum_body_fraction",
            "long_minimum_trigger_close_location",
            "long_minimum_breakout_extension_atr",
            "long_maximum_breakout_extension_atr",
            "short_minimum_range_expansion",
            "short_minimum_volume_surprise",
            "short_minimum_body_fraction",
            "short_minimum_trigger_close_location",
            "short_minimum_breakout_extension_atr",
            "short_maximum_breakout_extension_atr",
            "long_failure_close_atr",
            "long_maximum_pre_retest_advance_atr",
            "long_retest_below_boundary_atr",
            "long_retest_above_boundary_atr",
            "long_minimum_reclaim_extension_atr",
            "long_maximum_reclaim_extension_atr",
            "long_minimum_reclaim_close_location",
            "short_failure_close_atr",
            "short_maximum_pre_retest_advance_atr",
            "short_retest_below_boundary_atr",
            "short_retest_above_boundary_atr",
            "short_minimum_reclaim_extension_atr",
            "short_maximum_reclaim_extension_atr",
            "short_minimum_reclaim_close_location",
            "long_minimum_reacceleration_imbalance",
            "long_minimum_two_bar_imbalance",
            "short_minimum_reacceleration_imbalance",
            "short_minimum_two_bar_imbalance",
            "long_maximum_absorption_imbalance",
            "long_minimum_absorption_volume_surprise",
            "short_maximum_absorption_imbalance",
            "short_minimum_absorption_volume_surprise",
            "long_stop_anchor_buffer_atr",
            "long_stop_extreme_buffer_atr",
            "long_minimum_risk_atr",
            "long_maximum_risk_atr",
            "long_target_reward_to_risk",
            "short_stop_anchor_buffer_atr",
            "short_stop_extreme_buffer_atr",
            "short_minimum_risk_atr",
            "short_maximum_risk_atr",
            "short_target_reward_to_risk",
        )
        for name in float_fields:
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                raise ValueError(f"{name} must be finite")
            object.__setattr__(self, name, float(value))

        positive_fields = set(float_fields) - {
            "short_maximum_hourly_slope_atr",
            "long_maximum_absorption_imbalance",
            "short_maximum_absorption_imbalance",
        }
        if any(getattr(self, name) <= 0 for name in positive_fields):
            raise ValueError("positive retest thresholds must be greater than zero")
        if self.short_maximum_hourly_slope_atr >= 0:
            raise ValueError("short hourly slope threshold must be negative")
        if self.long_maximum_absorption_imbalance >= 0 or self.short_maximum_absorption_imbalance >= 0:
            raise ValueError("absorption imbalance thresholds must be negative")
        for name in (
            "long_minimum_hourly_efficiency",
            "short_minimum_hourly_efficiency",
            "long_minimum_body_fraction",
            "short_minimum_body_fraction",
            "long_minimum_trigger_close_location",
            "short_minimum_trigger_close_location",
            "long_minimum_reclaim_close_location",
            "short_minimum_reclaim_close_location",
            "long_minimum_reacceleration_imbalance",
            "long_minimum_two_bar_imbalance",
            "short_minimum_reacceleration_imbalance",
            "short_minimum_two_bar_imbalance",
        ):
            if getattr(self, name) > 1:
                raise ValueError(f"{name} must not exceed one")
        bounds = (
            (self.long_minimum_atr_bps, self.long_maximum_atr_bps),
            (self.short_minimum_atr_bps, self.short_maximum_atr_bps),
            (self.long_minimum_breakout_extension_atr, self.long_maximum_breakout_extension_atr),
            (self.short_minimum_breakout_extension_atr, self.short_maximum_breakout_extension_atr),
            (self.long_minimum_reclaim_extension_atr, self.long_maximum_reclaim_extension_atr),
            (self.short_minimum_reclaim_extension_atr, self.short_maximum_reclaim_extension_atr),
            (self.long_minimum_risk_atr, self.long_maximum_risk_atr),
            (self.short_minimum_risk_atr, self.short_maximum_risk_atr),
        )
        if any(lower > upper for lower, upper in bounds):
            raise ValueError("retest lower bounds must not exceed upper bounds")

    @property
    def fingerprint(self) -> str:
        payload = json.dumps(
            asdict(self),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        ).encode()
        return hashlib.sha256(payload).hexdigest()

    @property
    def maximum_holding_ms(self) -> int:
        return max(self.long_max_hold_bars, self.short_max_hold_bars) * _FIVE_MINUTES_MS


@dataclass(frozen=True, slots=True)
class RegimeRetestGenerationCounters:
    """Self-reconciling generation counts for one side or their total."""

    structural_breakout_candidates: int = 0
    regime_rejects: int = 0
    expansion_rejects: int = 0
    armed_setups: int = 0
    boundary_failures: int = 0
    overextensions: int = 0
    expiries: int = 0
    structural_reclaims: int = 0
    flow_mismatches: int = 0
    risk_geometry_rejects: int = 0
    emitted_intents: int = 0
    state_resets: int = 0
    pending_setups: int = 0

    def __post_init__(self) -> None:
        values = tuple(asdict(self).values())
        if any(isinstance(value, bool) or not isinstance(value, int) or value < 0 for value in values):
            raise ValueError("generation counters must be non-negative integers")
        if self.structural_breakout_candidates != (
            self.regime_rejects + self.expansion_rejects + self.armed_setups
        ):
            raise ValueError("breakout candidates must partition into reject or armed outcomes")
        if self.armed_setups != (
            self.boundary_failures
            + self.overextensions
            + self.expiries
            + self.structural_reclaims
            + self.state_resets
            + self.pending_setups
        ):
            raise ValueError("armed setups must have exactly one terminal generation outcome")
        if self.structural_reclaims != (
            self.flow_mismatches + self.risk_geometry_rejects + self.emitted_intents
        ):
            raise ValueError("structural reclaims must partition into one admission outcome")

    def __add__(
        self,
        other: RegimeRetestGenerationCounters,
    ) -> RegimeRetestGenerationCounters:
        if not isinstance(other, RegimeRetestGenerationCounters):
            return NotImplemented
        return RegimeRetestGenerationCounters(
            **{name: getattr(self, name) + getattr(other, name) for name in asdict(self)}
        )

    def to_dict(self) -> dict[str, int]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class RegimeRetestSetupEvent:
    """One ordered, canonical event in a structural setup's causal lifecycle."""

    sequence: int
    event_type: RegimeRetestSetupEventType
    setup_id: str
    symbol: str
    side: Side
    decision_ts_ms: int
    trigger_ts_ms: int
    metadata: tuple[tuple[str, str], ...] = ()
    intent_id: str | None = None
    event_sha256: str = ""

    def __post_init__(self) -> None:
        if isinstance(self.sequence, bool) or not isinstance(self.sequence, int) or self.sequence < 0:
            raise ValueError("event sequence must be a non-negative integer")
        if not isinstance(self.event_type, RegimeRetestSetupEventType):
            raise TypeError("event_type must be a RegimeRetestSetupEventType")
        _lowercase_sha256("setup_id", self.setup_id)
        if not isinstance(self.symbol, str) or not self.symbol or self.symbol != self.symbol.strip().upper():
            raise ValueError("event symbol must be normalized uppercase text")
        if not isinstance(self.side, Side) or self.side is Side.FLAT:
            raise ValueError("event side must be LONG or SHORT")
        for name, value in (
            ("decision_ts_ms", self.decision_ts_ms),
            ("trigger_ts_ms", self.trigger_ts_ms),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{name} must be a non-negative integer")
        if self.decision_ts_ms < self.trigger_ts_ms:
            raise ValueError("event decision cannot predate its trigger")
        if not isinstance(self.metadata, tuple):
            raise TypeError("event metadata must be an immutable tuple")
        pairs: list[tuple[str, str]] = []
        for item in self.metadata:
            if (
                not isinstance(item, tuple)
                or len(item) != 2
                or not all(isinstance(value, str) for value in item)
                or not item[0]
                or item[0] != item[0].strip()
            ):
                raise ValueError("event metadata must contain normalized string pairs")
            pairs.append(item)
        keys = [key for key, _ in pairs]
        if len(keys) != len(set(keys)):
            raise ValueError("event metadata keys must be unique")
        canonical_metadata = tuple(sorted(pairs))
        object.__setattr__(self, "metadata", canonical_metadata)
        if self.event_type is RegimeRetestSetupEventType.EMITTED_INTENT:
            if self.intent_id is None:
                raise ValueError("an emitted event must bind its intent")
            _lowercase_sha256("intent_id", self.intent_id)
        elif self.intent_id is not None:
            raise ValueError("only an emitted event may bind an intent")
        expected = _event_sha256(self)
        if self.event_sha256:
            _lowercase_sha256("event_sha256", self.event_sha256)
            if self.event_sha256 != expected:
                raise ValueError("event SHA-256 does not match its canonical payload")
        else:
            object.__setattr__(self, "event_sha256", expected)

    def to_dict(self) -> dict[str, object]:
        return {
            "decision_ts_ms": self.decision_ts_ms,
            "event_sha256": self.event_sha256,
            "event_type": self.event_type.value,
            "intent_id": self.intent_id,
            "metadata": dict(self.metadata),
            "schema": _EVENT_SCHEMA,
            "sequence": self.sequence,
            "setup_id": self.setup_id,
            "side": self.side.value,
            "symbol": self.symbol,
            "trigger_ts_ms": self.trigger_ts_ms,
        }


@dataclass(frozen=True, slots=True)
class RegimeRetestGenerationEvidence:
    """Immutable pre-run intents, diagnostics and their inventory commitments."""

    config_sha256: str
    variant: RegimeRetestReclaimVariant
    intents: tuple[SleeveIntent, ...]
    events: tuple[RegimeRetestSetupEvent, ...]
    long_counters: RegimeRetestGenerationCounters
    short_counters: RegimeRetestGenerationCounters
    total_counters: RegimeRetestGenerationCounters
    setup_inventory_sha256: str
    outcome_inventory_sha256: str

    def __post_init__(self) -> None:
        _lowercase_sha256("config_sha256", self.config_sha256)
        if not isinstance(self.variant, RegimeRetestReclaimVariant):
            raise TypeError("generation variant must be a RegimeRetestReclaimVariant")
        if not isinstance(self.intents, tuple) or any(
            not isinstance(intent, SleeveIntent) for intent in self.intents
        ):
            raise TypeError("generation intents must be an immutable SleeveIntent tuple")
        if not isinstance(self.events, tuple) or any(
            not isinstance(event, RegimeRetestSetupEvent) for event in self.events
        ):
            raise TypeError("generation events must be immutable RegimeRetestSetupEvent values")
        counters = (self.long_counters, self.short_counters, self.total_counters)
        if any(not isinstance(item, RegimeRetestGenerationCounters) for item in counters):
            raise TypeError("generation evidence requires typed counters")
        if self.total_counters != self.long_counters + self.short_counters:
            raise ValueError("total counters must equal the two side counters")
        if tuple(event.sequence for event in self.events) != tuple(range(len(self.events))):
            raise ValueError("generation event sequences must be contiguous and ordered")
        if any(
            current.decision_ts_ms < previous.decision_ts_ms
            for previous, current in zip(self.events, self.events[1:], strict=False)
        ):
            raise ValueError("generation events must be causally ordered")
        _validate_setup_event_state_machine(self.events, self.config_sha256)
        expected_long = _counters_from_events(self.events, Side.LONG)
        expected_short = _counters_from_events(self.events, Side.SHORT)
        if self.long_counters != expected_long or self.short_counters != expected_short:
            raise ValueError("generation counters do not match the retained event inventory")
        emitted_ids = tuple(
            event.intent_id
            for event in self.events
            if event.event_type is RegimeRetestSetupEventType.EMITTED_INTENT
        )
        if emitted_ids != tuple(intent.intent_id for intent in self.intents):
            raise ValueError("emitted events and generated intent inventory differ")
        if any(
            intent.sleeve_id != _STRATEGY_VERSION
            or dict(intent.metadata).get("config_sha256") != self.config_sha256
            or dict(intent.metadata).get("variant") != self.variant.value
            for intent in self.intents
        ):
            raise ValueError("generation intents do not match the evidence configuration")
        _validate_emitted_intent_linkage(self.events, self.intents, self.config_sha256)
        for name, actual, expected in (
            (
                "setup_inventory_sha256",
                self.setup_inventory_sha256,
                _inventory_sha256(self.events, setup_only=True),
            ),
            (
                "outcome_inventory_sha256",
                self.outcome_inventory_sha256,
                _inventory_sha256(self.events, setup_only=False),
            ),
        ):
            _lowercase_sha256(name, actual)
            if actual != expected:
                raise ValueError(f"{name} does not match the canonical event inventory")

    def to_dict(self) -> dict[str, object]:
        return {
            "config_sha256": self.config_sha256,
            "events": [event.to_dict() for event in self.events],
            "intents": [intent.intent_id for intent in self.intents],
            "long_counters": self.long_counters.to_dict(),
            "outcome_inventory_sha256": self.outcome_inventory_sha256,
            "setup_inventory_sha256": self.setup_inventory_sha256,
            "short_counters": self.short_counters.to_dict(),
            "strategy_version": _STRATEGY_VERSION,
            "total_counters": self.total_counters.to_dict(),
            "variant": self.variant.value,
        }


@dataclass(frozen=True, slots=True)
class _HourlyContext:
    close: float
    fast_ema: float
    slow_ema: float
    prior_fast_ema: float
    atr: float
    efficiency: float


@dataclass(frozen=True, slots=True)
class _ActiveSetup:
    setup_id: str
    side: Side
    trigger_index: int
    trigger_timestamp_ms: int
    boundary: float
    atr: float
    atr_bps: float
    trigger_high: float
    trigger_low: float
    range_expansion: float
    volume_surprise: float
    body_fraction: float
    close_location: float
    breakout_extension_atr: float
    volume_baseline: float
    trigger_directional_imbalance: float
    hourly: _HourlyContext


def _segments(
    rows: list[Candle],
    interval_ms: int,
    invalid_opens: frozenset[int] = frozenset(),
) -> list[tuple[int, int]]:
    """Return positive, contiguous segments and treat source-zero buckets as gaps."""

    segments: list[tuple[int, int]] = []
    start: int | None = None
    previous: Candle | None = None
    for index, current in enumerate(rows):
        invalid = (
            current.open_time_ms in invalid_opens or not math.isfinite(current.volume) or current.volume <= 0
        )
        if invalid:
            if start is not None:
                segments.append((start, index))
            start = None
            previous = None
            continue
        if start is None:
            start = index
        elif previous is not None and (
            current.open_time_ms - previous.open_time_ms != interval_ms
            or current.open_time_ms != previous.close_time_ms + 1
        ):
            segments.append((start, index))
            start = index
        previous = current
    if start is not None:
        segments.append((start, len(rows)))
    return segments


def _true_ranges(
    rows: list[Candle],
    interval_ms: int,
    invalid_opens: frozenset[int],
) -> np.ndarray:
    values = np.full(len(rows), np.nan, dtype=float)
    for start, end in _segments(rows, interval_ms, invalid_opens):
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


def _wilder_atr(
    rows: list[Candle],
    period: int,
    interval_ms: int,
    invalid_opens: frozenset[int],
) -> np.ndarray:
    values = np.full(len(rows), np.nan, dtype=float)
    true_ranges = _true_ranges(rows, interval_ms, invalid_opens)
    for start, end in _segments(rows, interval_ms, invalid_opens):
        if end - start < period:
            continue
        seed = start + period - 1
        atr = float(np.mean(true_ranges[start : seed + 1]))
        values[seed] = atr
        for index in range(seed + 1, end):
            atr = (atr * (period - 1) + float(true_ranges[index])) / period
            values[index] = atr
    return values


def _segmented_ema(
    rows: list[Candle],
    period: int,
    interval_ms: int,
    invalid_opens: frozenset[int],
) -> np.ndarray:
    values = np.full(len(rows), np.nan, dtype=float)
    multiplier = 2.0 / (period + 1)
    for start, end in _segments(rows, interval_ms, invalid_opens):
        if end - start < period:
            continue
        seed = start + period - 1
        ema = float(np.mean([row.close for row in rows[start : seed + 1]]))
        values[seed] = ema
        for index in range(seed + 1, end):
            ema = (rows[index].close - ema) * multiplier + ema
            values[index] = ema
    return values


def _hourly_contexts(
    rows: list[Candle],
    settings: RegimeVetoRetestReclaimConfig,
    invalid_opens: frozenset[int],
) -> list[_HourlyContext | None]:
    contexts: list[_HourlyContext | None] = [None] * len(rows)
    fast = _segmented_ema(rows, settings.hourly_fast_ema_period, _ONE_HOUR_MS, invalid_opens)
    slow = _segmented_ema(rows, settings.hourly_slow_ema_period, _ONE_HOUR_MS, invalid_opens)
    atr = _wilder_atr(rows, settings.hourly_atr_period, _ONE_HOUR_MS, invalid_opens)
    for start, end in _segments(rows, _ONE_HOUR_MS, invalid_opens):
        first = start + max(
            settings.hourly_slow_ema_period - 1,
            settings.hourly_fast_ema_period - 1 + settings.hourly_slope_lookback,
            settings.hourly_atr_period - 1,
            settings.hourly_efficiency_lookback,
        )
        for index in range(first, end):
            values = (
                float(fast[index]),
                float(slow[index]),
                float(fast[index - settings.hourly_slope_lookback]),
                float(atr[index]),
            )
            if not all(math.isfinite(value) and value > 0 for value in values):
                continue
            closes = [row.close for row in rows[index - settings.hourly_efficiency_lookback : index + 1]]
            travel = math.fsum(
                abs(current - previous) for previous, current in zip(closes, closes[1:], strict=False)
            )
            efficiency = abs(closes[-1] - closes[0]) / travel if travel > 0 else 0.0
            contexts[index] = _HourlyContext(
                close=rows[index].close,
                fast_ema=values[0],
                slow_ema=values[1],
                prior_fast_ema=values[2],
                atr=values[3],
                efficiency=efficiency,
            )
    return contexts


def _number(value: float) -> str:
    if not math.isfinite(value):
        raise ValueError("retest intent evidence must be finite")
    normalized = 0.0 if value == 0 else value
    return format(normalized, ".17g")


def _lowercase_sha256(name: str, value: str) -> None:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or value != value.lower()
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ValueError(f"{name} must be a lowercase SHA-256")


def _canonical_hash_value(value: object) -> object:
    """Normalize numeric evidence before hashing it into an inventory."""

    if isinstance(value, StrEnum):
        return value.value
    if isinstance(value, Side):
        return value.value
    if isinstance(value, dict):
        if any(not isinstance(key, str) for key in value):
            raise TypeError("canonical hash mappings require string keys")
        return {key: _canonical_hash_value(value[key]) for key in sorted(value)}
    if isinstance(value, (tuple, list)):
        return [_canonical_hash_value(item) for item in value]
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("canonical hash values must be finite")
        normalized = 0.0 if value == 0 else value
        if normalized.is_integer():
            return int(normalized)
        return {"__float_hex__": normalized.hex()}
    raise TypeError(f"unsupported canonical hash value: {type(value).__name__}")


def _canonical_json_bytes(value: object) -> bytes:
    return json.dumps(
        _canonical_hash_value(value),
        allow_nan=False,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()


def _event_payload(event: RegimeRetestSetupEvent) -> dict[str, object]:
    return {
        "decision_ts_ms": event.decision_ts_ms,
        "event_type": event.event_type.value,
        "intent_id": event.intent_id,
        "metadata": dict(event.metadata),
        "schema": _EVENT_SCHEMA,
        "sequence": event.sequence,
        "setup_id": event.setup_id,
        "side": event.side.value,
        "symbol": event.symbol,
        "trigger_ts_ms": event.trigger_ts_ms,
    }


def _event_sha256(event: RegimeRetestSetupEvent) -> str:
    return hashlib.sha256(
        b"kairos.regime-retest-setup-event.v1\0" + _canonical_json_bytes(_event_payload(event))
    ).hexdigest()


def _inventory_sha256(
    events: tuple[RegimeRetestSetupEvent, ...],
    *,
    setup_only: bool,
) -> str:
    domain = _SETUP_INVENTORY_DOMAIN if setup_only else _OUTCOME_INVENTORY_DOMAIN
    digest = hashlib.sha256(domain)
    for event in events:
        if setup_only and event.event_type is not RegimeRetestSetupEventType.STRUCTURAL_BREAKOUT_CANDIDATE:
            continue
        digest.update(_canonical_json_bytes(event.to_dict()))
        digest.update(b"\n")
    return digest.hexdigest()


_EVENT_COUNTER_FIELDS = {
    RegimeRetestSetupEventType.STRUCTURAL_BREAKOUT_CANDIDATE: "structural_breakout_candidates",
    RegimeRetestSetupEventType.REGIME_REJECT: "regime_rejects",
    RegimeRetestSetupEventType.EXPANSION_REJECT: "expansion_rejects",
    RegimeRetestSetupEventType.ARMED_SETUP: "armed_setups",
    RegimeRetestSetupEventType.BOUNDARY_FAILURE: "boundary_failures",
    RegimeRetestSetupEventType.OVEREXTENSION: "overextensions",
    RegimeRetestSetupEventType.EXPIRY: "expiries",
    RegimeRetestSetupEventType.STRUCTURAL_RECLAIM: "structural_reclaims",
    RegimeRetestSetupEventType.FLOW_MISMATCH: "flow_mismatches",
    RegimeRetestSetupEventType.RISK_GEOMETRY_REJECT: "risk_geometry_rejects",
    RegimeRetestSetupEventType.EMITTED_INTENT: "emitted_intents",
    RegimeRetestSetupEventType.STATE_RESET: "state_resets",
    RegimeRetestSetupEventType.PENDING_SETUP: "pending_setups",
}


def _counters_from_events(
    events: tuple[RegimeRetestSetupEvent, ...],
    side: Side,
) -> RegimeRetestGenerationCounters:
    counts = {name: 0 for name in RegimeRetestGenerationCounters.__dataclass_fields__}
    for event in events:
        if event.side is side:
            counts[_EVENT_COUNTER_FIELDS[event.event_type]] += 1
    return RegimeRetestGenerationCounters(**counts)


def _setup_id(
    *,
    symbol: str,
    side: Side,
    trigger_ts_ms: int,
    boundary: float,
    atr: float,
    config_sha256: str,
) -> str:
    return hashlib.sha256(
        b"kairos.regime-retest-setup.v1\0"
        + _canonical_json_bytes(
            {
                "atr": _number(atr),
                "boundary": _number(boundary),
                "config_sha256": config_sha256,
                "side": side.value,
                "strategy_version": _STRATEGY_VERSION,
                "symbol": symbol,
                "trigger_ts_ms": trigger_ts_ms,
            }
        )
    ).hexdigest()


def _canonical_metadata_number(
    metadata: dict[str, str],
    name: str,
) -> float:
    raw = metadata.get(name)
    if raw is None:
        raise ValueError(f"setup evidence is missing {name}")
    try:
        value = float(raw)
    except ValueError as error:
        raise ValueError(f"setup evidence {name} must be canonical numeric text") from error
    if not math.isfinite(value) or _number(value) != raw:
        raise ValueError(f"setup evidence {name} must be canonical numeric text")
    return value


def _validate_elapsed_bars(event: RegimeRetestSetupEvent) -> None:
    elapsed_ms = event.decision_ts_ms - event.trigger_ts_ms
    if elapsed_ms < _FIVE_MINUTES_MS or elapsed_ms % _FIVE_MINUTES_MS:
        raise ValueError("setup outcome must occur on a later closed five-minute bar")
    if dict(event.metadata).get("elapsed_bars") != str(elapsed_ms // _FIVE_MINUTES_MS):
        raise ValueError("setup outcome elapsed_bars does not match its causal timestamps")


def _validate_state_reset(event: RegimeRetestSetupEvent) -> None:
    metadata = dict(event.metadata)
    if set(metadata) != {"reason"} or metadata["reason"] not in {"gap", "zero_volume"}:
        raise ValueError("state reset reason must be exactly gap or zero_volume")
    if event.decision_ts_ms <= event.trigger_ts_ms:
        raise ValueError("state reset must occur after its armed setup")
    bucket_offset = event.decision_ts_ms % _FIVE_MINUTES_MS
    if metadata["reason"] == "gap" and bucket_offset != 0:
        raise ValueError("gap reset must be timestamped at the next observed bucket open")
    if metadata["reason"] == "zero_volume" and bucket_offset != _FIVE_MINUTES_MS - 1:
        raise ValueError("zero-volume reset must be timestamped at the invalid bucket close")


def _validate_setup_event_state_machine(
    events: tuple[RegimeRetestSetupEvent, ...],
    config_sha256: str,
) -> None:
    """Validate every setup as one exact, side-stable causal lifecycle."""

    grouped: dict[str, list[RegimeRetestSetupEvent]] = {}
    for event in events:
        grouped.setdefault(event.setup_id, []).append(event)

    immediate_rejects = {
        RegimeRetestSetupEventType.REGIME_REJECT,
        RegimeRetestSetupEventType.EXPANSION_REJECT,
    }
    armed_terminals = {
        RegimeRetestSetupEventType.BOUNDARY_FAILURE,
        RegimeRetestSetupEventType.OVEREXTENSION,
        RegimeRetestSetupEventType.EXPIRY,
        RegimeRetestSetupEventType.PENDING_SETUP,
        RegimeRetestSetupEventType.STATE_RESET,
    }
    reclaim_terminals = {
        RegimeRetestSetupEventType.FLOW_MISMATCH,
        RegimeRetestSetupEventType.RISK_GEOMETRY_REJECT,
        RegimeRetestSetupEventType.EMITTED_INTENT,
    }
    for setup_id, lifecycle in grouped.items():
        candidate = lifecycle[0]
        if candidate.event_type is not RegimeRetestSetupEventType.STRUCTURAL_BREAKOUT_CANDIDATE:
            raise ValueError("every setup lifecycle must begin with its structural candidate")
        if candidate.decision_ts_ms != candidate.trigger_ts_ms:
            raise ValueError("structural candidate decision must equal its trigger timestamp")
        if candidate.trigger_ts_ms % _FIVE_MINUTES_MS != _FIVE_MINUTES_MS - 1:
            raise ValueError("structural candidate must be timestamped at a five-minute close")
        if any(
            event.symbol != candidate.symbol
            or event.side is not candidate.side
            or event.trigger_ts_ms != candidate.trigger_ts_ms
            for event in lifecycle[1:]
        ):
            raise ValueError("setup lifecycle changed symbol, side, or trigger timestamp")

        candidate_metadata = dict(candidate.metadata)
        expected_setup_id = _setup_id(
            symbol=candidate.symbol,
            side=candidate.side,
            trigger_ts_ms=candidate.trigger_ts_ms,
            boundary=_canonical_metadata_number(candidate_metadata, "boundary"),
            atr=_canonical_metadata_number(candidate_metadata, "atr"),
            config_sha256=config_sha256,
        )
        if setup_id != expected_setup_id:
            raise ValueError("setup_id does not match its canonical structural candidate")

        event_types = tuple(event.event_type for event in lifecycle)
        if len(event_types) == 2 and event_types[1] in immediate_rejects:
            if lifecycle[1].decision_ts_ms != candidate.trigger_ts_ms:
                raise ValueError("candidate rejection must occur at the trigger decision")
            continue
        if len(event_types) < 3 or event_types[1] is not RegimeRetestSetupEventType.ARMED_SETUP:
            raise ValueError("candidate must resolve once to reject or armed")
        if lifecycle[1].decision_ts_ms != candidate.trigger_ts_ms:
            raise ValueError("armed setup must be recorded at the trigger decision")

        terminal = lifecycle[2]
        if terminal.event_type in armed_terminals and len(event_types) == 3:
            if terminal.event_type in {
                RegimeRetestSetupEventType.BOUNDARY_FAILURE,
                RegimeRetestSetupEventType.OVEREXTENSION,
                RegimeRetestSetupEventType.EXPIRY,
            }:
                _validate_elapsed_bars(terminal)
            elif terminal.event_type is RegimeRetestSetupEventType.PENDING_SETUP:
                if terminal.decision_ts_ms < terminal.trigger_ts_ms:
                    raise ValueError("pending setup cannot predate its trigger")
                if terminal.decision_ts_ms % _FIVE_MINUTES_MS != _FIVE_MINUTES_MS - 1:
                    raise ValueError("pending setup must be timestamped at a five-minute close")
                if dict(terminal.metadata) != {"reason": "source_ended_before_resolution"}:
                    raise ValueError("pending setup must retain its exact source-end reason")
            else:
                _validate_state_reset(terminal)
            continue
        if (
            len(event_types) == 4
            and terminal.event_type is RegimeRetestSetupEventType.STRUCTURAL_RECLAIM
            and event_types[3] in reclaim_terminals
        ):
            admission = lifecycle[3]
            _validate_elapsed_bars(terminal)
            _validate_elapsed_bars(admission)
            if admission.decision_ts_ms != terminal.decision_ts_ms:
                raise ValueError("reclaim admission must share the structural reclaim decision")
            continue
        raise ValueError("setup lifecycle contains an invalid order or duplicate terminal outcome")


def _validate_emitted_intent_linkage(
    events: tuple[RegimeRetestSetupEvent, ...],
    intents: tuple[SleeveIntent, ...],
    config_sha256: str,
) -> None:
    emitted = tuple(
        event for event in events if event.event_type is RegimeRetestSetupEventType.EMITTED_INTENT
    )
    emitted_ids = tuple(event.intent_id for event in emitted)
    intent_ids = tuple(intent.intent_id for intent in intents)
    if emitted_ids != intent_ids or len(set(emitted_ids)) != len(emitted_ids):
        raise ValueError("emitted events and intents must have exact one-to-one identity")

    for event, intent in zip(emitted, intents, strict=True):
        if (
            event.intent_id != intent.intent_id
            or event.symbol != intent.symbol
            or event.side is not intent.side
            or event.decision_ts_ms != intent.decision_ts_ms
        ):
            raise ValueError("emitted event identity does not match its linked intent")
        metadata = dict(intent.metadata)
        raw_trigger = metadata.get("trigger_ts_ms")
        try:
            trigger_ts_ms = int(raw_trigger) if raw_trigger is not None else -1
        except ValueError as error:
            raise ValueError("intent trigger timestamp must be canonical integer text") from error
        if raw_trigger != str(trigger_ts_ms) or trigger_ts_ms < 0:
            raise ValueError("intent trigger timestamp must be canonical integer text")
        expected_setup_id = _setup_id(
            symbol=intent.symbol,
            side=intent.side,
            trigger_ts_ms=trigger_ts_ms,
            boundary=_canonical_metadata_number(metadata, "boundary"),
            atr=_canonical_metadata_number(metadata, "atr"),
            config_sha256=config_sha256,
        )
        if event.trigger_ts_ms != trigger_ts_ms or event.setup_id != expected_setup_id:
            raise ValueError("emitted event trigger or setup does not match its linked intent")


def _append_event(
    events: list[RegimeRetestSetupEvent],
    *,
    event_type: RegimeRetestSetupEventType,
    setup_id: str,
    symbol: str,
    side: Side,
    decision_ts_ms: int,
    trigger_ts_ms: int,
    metadata: tuple[tuple[str, str], ...] = (),
    intent_id: str | None = None,
) -> None:
    events.append(
        RegimeRetestSetupEvent(
            sequence=len(events),
            event_type=event_type,
            setup_id=setup_id,
            symbol=symbol,
            side=side,
            decision_ts_ms=decision_ts_ms,
            trigger_ts_ms=trigger_ts_ms,
            metadata=metadata,
            intent_id=intent_id,
        )
    )


def _feature_hash(payload: dict[str, str | int]) -> str:
    return hashlib.sha256(
        json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        ).encode()
    ).hexdigest()


def _imbalance(row: Candle) -> float:
    if not math.isfinite(row.volume) or row.volume <= 0:
        return math.nan
    return (2 * row.taker_buy_volume - row.volume) / row.volume


def _regime_allows(
    side: Side,
    context: _HourlyContext,
    settings: RegimeVetoRetestReclaimConfig,
) -> tuple[bool, float, float]:
    slope_atr = (context.fast_ema - context.prior_fast_ema) / context.atr
    if side is Side.LONG:
        extension_atr = (context.close - context.fast_ema) / context.atr
        allowed = (
            context.fast_ema > context.slow_ema
            and context.close >= context.fast_ema
            and slope_atr >= settings.long_minimum_hourly_slope_atr
            and context.efficiency >= settings.long_minimum_hourly_efficiency
            and 0 <= extension_atr <= settings.long_maximum_hourly_extension_atr
        )
    else:
        extension_atr = (context.fast_ema - context.close) / context.atr
        allowed = (
            context.fast_ema < context.slow_ema
            and context.close <= context.fast_ema
            and slope_atr <= settings.short_maximum_hourly_slope_atr
            and context.efficiency >= settings.short_minimum_hourly_efficiency
            and 0 <= extension_atr <= settings.short_maximum_hourly_extension_atr
        )
    return allowed, slope_atr, extension_atr


def _trigger_setup(
    *,
    rows: list[Candle],
    index: int,
    previous_atr: float,
    true_ranges: np.ndarray,
    current_true_range: float,
    hourly: _HourlyContext,
    settings: RegimeVetoRetestReclaimConfig,
) -> _ActiveSetup | None:
    current = rows[index]
    prior_ranges = true_ranges[index - settings.baseline_lookback : index]
    prior_volumes = [row.volume for row in rows[index - settings.baseline_lookback : index]]
    range_baseline = float(np.median(prior_ranges))
    volume_baseline = float(np.median(prior_volumes))
    candle_range = current.high - current.low
    if not all(
        math.isfinite(value) and value > 0
        for value in (
            previous_atr,
            current_true_range,
            range_baseline,
            volume_baseline,
            candle_range,
            current.volume,
        )
    ):
        return None
    range_expansion = current_true_range / range_baseline
    volume_surprise = current.volume / volume_baseline
    body_fraction = abs(current.close - current.open) / candle_range
    prior = rows[index - settings.breakout_lookback : index]
    prior_high = max(row.high for row in prior)
    prior_low = min(row.low for row in prior)
    atr_bps = previous_atr / rows[index - 1].close * _BPS

    side: Side
    boundary: float
    close_location: float
    breakout_extension_atr: float
    if current.close > prior_high:
        side = Side.LONG
        boundary = prior_high
        close_location = (current.close - current.low) / candle_range
        breakout_extension_atr = (current.close - boundary) / previous_atr
        allowed, _, _ = _regime_allows(side, hourly, settings)
        eligible = (
            allowed
            and settings.long_minimum_atr_bps <= atr_bps <= settings.long_maximum_atr_bps
            and range_expansion >= settings.long_minimum_range_expansion
            and volume_surprise >= settings.long_minimum_volume_surprise
            and body_fraction >= settings.long_minimum_body_fraction
            and close_location >= settings.long_minimum_trigger_close_location
            and settings.long_minimum_breakout_extension_atr
            <= breakout_extension_atr
            <= settings.long_maximum_breakout_extension_atr
        )
    elif current.close < prior_low:
        side = Side.SHORT
        boundary = prior_low
        close_location = (current.high - current.close) / candle_range
        breakout_extension_atr = (boundary - current.close) / previous_atr
        allowed, _, _ = _regime_allows(side, hourly, settings)
        eligible = (
            allowed
            and settings.short_minimum_atr_bps <= atr_bps <= settings.short_maximum_atr_bps
            and range_expansion >= settings.short_minimum_range_expansion
            and volume_surprise >= settings.short_minimum_volume_surprise
            and body_fraction >= settings.short_minimum_body_fraction
            and close_location >= settings.short_minimum_trigger_close_location
            and settings.short_minimum_breakout_extension_atr
            <= breakout_extension_atr
            <= settings.short_maximum_breakout_extension_atr
        )
    else:
        return None
    if not eligible:
        return None
    return _ActiveSetup(
        setup_id=_setup_id(
            symbol=current.symbol,
            side=side,
            trigger_ts_ms=current.close_time_ms,
            boundary=boundary,
            atr=previous_atr,
            config_sha256=settings.fingerprint,
        ),
        side=side,
        trigger_index=index,
        trigger_timestamp_ms=current.close_time_ms,
        boundary=boundary,
        atr=previous_atr,
        atr_bps=atr_bps,
        trigger_high=current.high,
        trigger_low=current.low,
        range_expansion=range_expansion,
        volume_surprise=volume_surprise,
        body_fraction=body_fraction,
        close_location=close_location,
        breakout_extension_atr=breakout_extension_atr,
        volume_baseline=volume_baseline,
        trigger_directional_imbalance=(1 if side is Side.LONG else -1) * _imbalance(current),
        hourly=hourly,
    )


def _reclaim_state(
    row: Candle,
    setup: _ActiveSetup,
    settings: RegimeVetoRetestReclaimConfig,
) -> tuple[bool, bool, bool, float]:
    """Return boundary failure, overextension, reclaim and close location."""

    boundary, atr = setup.boundary, setup.atr
    candle_range = row.high - row.low
    if not math.isfinite(candle_range) or candle_range <= 0:
        return False, False, False, math.nan
    if setup.side is Side.LONG:
        close_location = (row.close - row.low) / candle_range
        boundary_failed = row.close < boundary - settings.long_failure_close_atr * atr
        overextended = row.high > setup.trigger_high + settings.long_maximum_pre_retest_advance_atr * atr
        reclaimed = (
            boundary - settings.long_retest_below_boundary_atr * atr
            <= row.low
            <= boundary + settings.long_retest_above_boundary_atr * atr
            and boundary + settings.long_minimum_reclaim_extension_atr * atr
            <= row.close
            <= boundary + settings.long_maximum_reclaim_extension_atr * atr
            and row.close > row.open
            and close_location >= settings.long_minimum_reclaim_close_location
        )
    else:
        close_location = (row.high - row.close) / candle_range
        boundary_failed = row.close > boundary + settings.short_failure_close_atr * atr
        overextended = row.low < setup.trigger_low - settings.short_maximum_pre_retest_advance_atr * atr
        reclaimed = (
            boundary - settings.short_retest_below_boundary_atr * atr
            <= row.high
            <= boundary + settings.short_retest_above_boundary_atr * atr
            and boundary - settings.short_maximum_reclaim_extension_atr * atr
            <= row.close
            <= boundary - settings.short_minimum_reclaim_extension_atr * atr
            and row.close < row.open
            and close_location >= settings.short_minimum_reclaim_close_location
        )
    return boundary_failed, overextended, reclaimed, close_location


def _variant_allows(
    *,
    setup: _ActiveSetup,
    row: Candle,
    previous: Candle,
    settings: RegimeVetoRetestReclaimConfig,
) -> tuple[bool, float, float, float]:
    direction = 1 if setup.side is Side.LONG else -1
    current_flow = direction * _imbalance(row)
    total_volume = previous.volume + row.volume
    two_bar_flow = (
        direction
        * (2 * previous.taker_buy_volume - previous.volume + 2 * row.taker_buy_volume - row.volume)
        / total_volume
        if total_volume > 0
        else math.nan
    )
    reclaim_volume_surprise = row.volume / setup.volume_baseline
    if not all(math.isfinite(value) for value in (current_flow, two_bar_flow, reclaim_volume_surprise)):
        return False, current_flow, two_bar_flow, reclaim_volume_surprise
    if settings.variant is RegimeRetestReclaimVariant.STRUCTURAL_RECLAIM:
        allowed = True
    elif settings.variant is RegimeRetestReclaimVariant.FLOW_REACCELERATION:
        if setup.side is Side.LONG:
            allowed = (
                current_flow >= settings.long_minimum_reacceleration_imbalance
                and two_bar_flow >= settings.long_minimum_two_bar_imbalance
            )
        else:
            allowed = (
                current_flow >= settings.short_minimum_reacceleration_imbalance
                and two_bar_flow >= settings.short_minimum_two_bar_imbalance
            )
    elif setup.side is Side.LONG:
        allowed = (
            current_flow <= settings.long_maximum_absorption_imbalance
            and reclaim_volume_surprise >= settings.long_minimum_absorption_volume_surprise
        )
    else:
        allowed = (
            current_flow <= settings.short_maximum_absorption_imbalance
            and reclaim_volume_surprise >= settings.short_minimum_absorption_volume_surprise
        )
    return allowed, current_flow, two_bar_flow, reclaim_volume_surprise


def _build_intent(
    *,
    rows: list[Candle],
    index: int,
    setup: _ActiveSetup,
    reclaim_close_location: float,
    settings: RegimeVetoRetestReclaimConfig,
) -> SleeveIntent | None:
    current = rows[index]
    allowed, current_flow, two_bar_flow, reclaim_volume_surprise = _variant_allows(
        setup=setup,
        row=current,
        previous=rows[index - 1],
        settings=settings,
    )
    if not allowed:
        return None
    reference = current.close
    if setup.side is Side.LONG:
        stop = (
            min(
                current.low,
                setup.boundary - settings.long_stop_anchor_buffer_atr * setup.atr,
            )
            - settings.long_stop_extreme_buffer_atr * setup.atr
        )
        risk = reference - stop
        risk_atr = risk / setup.atr
        if not settings.long_minimum_risk_atr <= risk_atr <= settings.long_maximum_risk_atr:
            return None
        target = reference + settings.long_target_reward_to_risk * risk
        reward_to_risk = settings.long_target_reward_to_risk
        max_hold_bars = settings.long_max_hold_bars
        reclaim_extension_atr = (reference - setup.boundary) / setup.atr
    else:
        stop = (
            max(
                current.high,
                setup.boundary + settings.short_stop_anchor_buffer_atr * setup.atr,
            )
            + settings.short_stop_extreme_buffer_atr * setup.atr
        )
        risk = stop - reference
        risk_atr = risk / setup.atr
        if not settings.short_minimum_risk_atr <= risk_atr <= settings.short_maximum_risk_atr:
            return None
        target = reference - settings.short_target_reward_to_risk * risk
        reward_to_risk = settings.short_target_reward_to_risk
        max_hold_bars = settings.short_max_hold_bars
        reclaim_extension_atr = (setup.boundary - reference) / setup.atr
    if not all(math.isfinite(value) and value > 0 for value in (stop, target, risk)):
        return None
    regime_allowed, hourly_slope_atr, hourly_extension_atr = _regime_allows(
        setup.side,
        setup.hourly,
        settings,
    )
    if not regime_allowed:
        raise RuntimeError("armed setup lost its frozen hourly authorization")

    feature_payload: dict[str, str | int] = {
        "atr": _number(setup.atr),
        "atr_bps": _number(setup.atr_bps),
        "body_fraction": _number(setup.body_fraction),
        "boundary": _number(setup.boundary),
        "breakout_extension_atr": _number(setup.breakout_extension_atr),
        "config_sha256": settings.fingerprint,
        "decision_ts_ms": current.close_time_ms,
        "hourly_atr": _number(setup.hourly.atr),
        "hourly_close": _number(setup.hourly.close),
        "hourly_efficiency": _number(setup.hourly.efficiency),
        "hourly_extension_atr": _number(hourly_extension_atr),
        "hourly_fast_ema": _number(setup.hourly.fast_ema),
        "hourly_fast_ema_prior": _number(setup.hourly.prior_fast_ema),
        "hourly_slope_atr": _number(hourly_slope_atr),
        "hourly_slow_ema": _number(setup.hourly.slow_ema),
        "range_expansion": _number(setup.range_expansion),
        "reclaim_close_location": _number(reclaim_close_location),
        "reclaim_directional_imbalance": _number(current_flow),
        "reclaim_extension_atr": _number(reclaim_extension_atr),
        "reclaim_volume_surprise": _number(reclaim_volume_surprise),
        "reference_price": _number(reference),
        "retest_bars_elapsed": index - setup.trigger_index,
        "risk_atr": _number(risk_atr),
        "side": setup.side.value,
        "stop_price": _number(stop),
        "strategy_version": _STRATEGY_VERSION,
        "symbol": current.symbol,
        "target_price": _number(target),
        "trigger_close_location": _number(setup.close_location),
        "trigger_directional_imbalance": _number(setup.trigger_directional_imbalance),
        "trigger_ts_ms": setup.trigger_timestamp_ms,
        "two_bar_directional_imbalance": _number(two_bar_flow),
        "variant": settings.variant.value,
        "volume_surprise": _number(setup.volume_surprise),
    }
    minimum_range = (
        settings.long_minimum_range_expansion
        if setup.side is Side.LONG
        else settings.short_minimum_range_expansion
    )
    minimum_volume = (
        settings.long_minimum_volume_surprise
        if setup.side is Side.LONG
        else settings.short_minimum_volume_surprise
    )
    maximum_reclaim = (
        settings.long_maximum_reclaim_extension_atr
        if setup.side is Side.LONG
        else settings.short_maximum_reclaim_extension_atr
    )
    signal_strength = (
        math.fsum(
            (
                min(1.0, setup.range_expansion / (2 * minimum_range)),
                min(1.0, setup.volume_surprise / (2 * minimum_volume)),
                min(1.0, setup.hourly.efficiency),
                min(1.0, max(0.0, reclaim_close_location)),
                max(0.0, 1 - reclaim_extension_atr / maximum_reclaim),
            )
        )
        / 5
    )
    return SleeveIntent(
        sleeve_id=_STRATEGY_VERSION,
        symbol=current.symbol,
        side=setup.side,
        decision_ts_ms=current.close_time_ms,
        entry_eligible_ts_ms=current.close_time_ms + 1,
        entry_expires_ts_ms=(current.close_time_ms + settings.intent_valid_bars * _FIVE_MINUTES_MS),
        reference_price=reference,
        signal_strength=signal_strength,
        gross_reward_bps=abs(target - reference) / reference * _BPS,
        exit_plan=ExitPlan(
            stop_price=stop,
            target_price=target,
            max_holding_ms=max_hold_bars * _FIVE_MINUTES_MS,
        ),
        metadata=(
            ("atr", _number(setup.atr)),
            ("atr_bps", _number(setup.atr_bps)),
            ("body_fraction", _number(setup.body_fraction)),
            ("boundary", _number(setup.boundary)),
            ("breakout_extension_atr", _number(setup.breakout_extension_atr)),
            ("config_sha256", settings.fingerprint),
            ("feature_hash", _feature_hash(feature_payload)),
            ("hourly_atr", _number(setup.hourly.atr)),
            ("hourly_close", _number(setup.hourly.close)),
            ("hourly_efficiency", _number(setup.hourly.efficiency)),
            ("hourly_extension_atr", _number(hourly_extension_atr)),
            ("hourly_fast_ema", _number(setup.hourly.fast_ema)),
            ("hourly_fast_ema_prior", _number(setup.hourly.prior_fast_ema)),
            ("hourly_slope_atr", _number(hourly_slope_atr)),
            ("hourly_slow_ema", _number(setup.hourly.slow_ema)),
            ("range_expansion", _number(setup.range_expansion)),
            ("reclaim_close_location", _number(reclaim_close_location)),
            ("reclaim_directional_imbalance", _number(current_flow)),
            ("reclaim_extension_atr", _number(reclaim_extension_atr)),
            ("reclaim_volume_surprise", _number(reclaim_volume_surprise)),
            ("retest_bars_elapsed", str(index - setup.trigger_index)),
            ("risk_atr", _number(risk_atr)),
            ("strategy_version", _STRATEGY_VERSION),
            ("trigger_close_location", _number(setup.close_location)),
            (
                "trigger_directional_imbalance",
                _number(setup.trigger_directional_imbalance),
            ),
            ("trigger_ts_ms", str(setup.trigger_timestamp_ms)),
            ("two_bar_directional_imbalance", _number(two_bar_flow)),
            ("variant", settings.variant.value),
            ("volume_surprise", _number(setup.volume_surprise)),
            ("reward_to_risk", _number(reward_to_risk)),
        ),
    )


def generate_regime_veto_retest_reclaim_evidence(
    candles_1m: list[Candle],
    config: RegimeVetoRetestReclaimConfig | None = None,
) -> RegimeRetestGenerationEvidence:
    """Generate intents plus a self-validating causal setup/outcome inventory."""

    if config is None:
        settings = RegimeVetoRetestReclaimConfig()
    elif isinstance(config, RegimeVetoRetestReclaimConfig):
        settings = config
    else:
        raise ValueError("config must be a RegimeVetoRetestReclaimConfig or None")

    def evidence(
        intents: list[SleeveIntent],
        events: list[RegimeRetestSetupEvent],
    ) -> RegimeRetestGenerationEvidence:
        immutable_intents = tuple(intents)
        immutable_events = tuple(events)
        long_counters = _counters_from_events(immutable_events, Side.LONG)
        short_counters = _counters_from_events(immutable_events, Side.SHORT)
        return RegimeRetestGenerationEvidence(
            config_sha256=settings.fingerprint,
            variant=settings.variant,
            intents=immutable_intents,
            events=immutable_events,
            long_counters=long_counters,
            short_counters=short_counters,
            total_counters=long_counters + short_counters,
            setup_inventory_sha256=_inventory_sha256(immutable_events, setup_only=True),
            outcome_inventory_sha256=_inventory_sha256(immutable_events, setup_only=False),
        )

    ordered = canonical_candles(candles_1m, expected_timeframe="1m")
    if not ordered:
        return evidence([], [])
    rows_5m = aggregate(ordered, "5m")
    rows_1h = aggregate(ordered, "1h")
    if not rows_5m:
        return evidence([], [])

    invalid_5m = frozenset(
        row.open_time_ms // _FIVE_MINUTES_MS * _FIVE_MINUTES_MS for row in ordered if row.volume <= 0
    )
    invalid_1h = frozenset(
        row.open_time_ms // _ONE_HOUR_MS * _ONE_HOUR_MS for row in ordered if row.volume <= 0
    )
    true_ranges = _true_ranges(rows_5m, _FIVE_MINUTES_MS, invalid_5m)
    atr_values = _wilder_atr(
        rows_5m,
        settings.atr_period,
        _FIVE_MINUTES_MS,
        invalid_5m,
    )
    hourly_contexts = _hourly_contexts(rows_1h, settings, invalid_1h)
    hourly_closes = [row.close_time_ms for row in rows_1h]
    five_segments = _segments(rows_5m, _FIVE_MINUTES_MS, invalid_5m)
    required_prior = max(settings.baseline_lookback, settings.breakout_lookback, settings.atr_period)

    intents: list[SleeveIntent] = []
    events: list[RegimeRetestSetupEvent] = []
    for segment_start, segment_end in five_segments:
        active: _ActiveSetup | None = None
        for index in range(segment_start, segment_end):
            current = rows_5m[index]
            if active is not None:
                elapsed = index - active.trigger_index
                boundary_failed, overextended, reclaimed, reclaim_close_location = _reclaim_state(
                    current,
                    active,
                    settings,
                )
                outcome_metadata = (
                    ("close", _number(current.close)),
                    ("elapsed_bars", str(elapsed)),
                    ("high", _number(current.high)),
                    ("low", _number(current.low)),
                )
                if boundary_failed:
                    _append_event(
                        events,
                        event_type=RegimeRetestSetupEventType.BOUNDARY_FAILURE,
                        setup_id=active.setup_id,
                        symbol=current.symbol,
                        side=active.side,
                        decision_ts_ms=current.close_time_ms,
                        trigger_ts_ms=active.trigger_timestamp_ms,
                        metadata=outcome_metadata,
                    )
                    active = None
                    continue
                if overextended:
                    _append_event(
                        events,
                        event_type=RegimeRetestSetupEventType.OVEREXTENSION,
                        setup_id=active.setup_id,
                        symbol=current.symbol,
                        side=active.side,
                        decision_ts_ms=current.close_time_ms,
                        trigger_ts_ms=active.trigger_timestamp_ms,
                        metadata=outcome_metadata,
                    )
                    active = None
                    continue
                if reclaimed:
                    _append_event(
                        events,
                        event_type=RegimeRetestSetupEventType.STRUCTURAL_RECLAIM,
                        setup_id=active.setup_id,
                        symbol=current.symbol,
                        side=active.side,
                        decision_ts_ms=current.close_time_ms,
                        trigger_ts_ms=active.trigger_timestamp_ms,
                        metadata=(
                            *outcome_metadata,
                            ("close_location", _number(reclaim_close_location)),
                        ),
                    )
                    flow_allowed, current_flow, two_bar_flow, reclaim_volume_surprise = _variant_allows(
                        setup=active,
                        row=current,
                        previous=rows_5m[index - 1],
                        settings=settings,
                    )
                    admission_metadata = (
                        ("directional_imbalance", _number(current_flow)),
                        ("elapsed_bars", str(elapsed)),
                        ("two_bar_directional_imbalance", _number(two_bar_flow)),
                        ("volume_surprise", _number(reclaim_volume_surprise)),
                    )
                    if not flow_allowed:
                        _append_event(
                            events,
                            event_type=RegimeRetestSetupEventType.FLOW_MISMATCH,
                            setup_id=active.setup_id,
                            symbol=current.symbol,
                            side=active.side,
                            decision_ts_ms=current.close_time_ms,
                            trigger_ts_ms=active.trigger_timestamp_ms,
                            metadata=admission_metadata,
                        )
                        active = None
                        continue
                    intent = _build_intent(
                        rows=rows_5m,
                        index=index,
                        setup=active,
                        reclaim_close_location=reclaim_close_location,
                        settings=settings,
                    )
                    if intent is None:
                        _append_event(
                            events,
                            event_type=RegimeRetestSetupEventType.RISK_GEOMETRY_REJECT,
                            setup_id=active.setup_id,
                            symbol=current.symbol,
                            side=active.side,
                            decision_ts_ms=current.close_time_ms,
                            trigger_ts_ms=active.trigger_timestamp_ms,
                            metadata=admission_metadata,
                        )
                    else:
                        intents.append(intent)
                        _append_event(
                            events,
                            event_type=RegimeRetestSetupEventType.EMITTED_INTENT,
                            setup_id=active.setup_id,
                            symbol=current.symbol,
                            side=active.side,
                            decision_ts_ms=current.close_time_ms,
                            trigger_ts_ms=active.trigger_timestamp_ms,
                            metadata=admission_metadata,
                            intent_id=intent.intent_id,
                        )
                    active = None
                    continue
                if elapsed >= settings.setup_window_bars:
                    _append_event(
                        events,
                        event_type=RegimeRetestSetupEventType.EXPIRY,
                        setup_id=active.setup_id,
                        symbol=current.symbol,
                        side=active.side,
                        decision_ts_ms=current.close_time_ms,
                        trigger_ts_ms=active.trigger_timestamp_ms,
                        metadata=outcome_metadata,
                    )
                    active = None
                continue

            if index < segment_start + required_prior:
                continue
            previous_atr = float(atr_values[index - 1])
            current_true_range = float(true_ranges[index])
            if not all(math.isfinite(value) and value > 0 for value in (previous_atr, current_true_range)):
                continue
            prior = rows_5m[index - settings.breakout_lookback : index]
            prior_high = max(row.high for row in prior)
            prior_low = min(row.low for row in prior)
            if current.close > prior_high:
                candidate_side = Side.LONG
                boundary = prior_high
            elif current.close < prior_low:
                candidate_side = Side.SHORT
                boundary = prior_low
            else:
                continue
            setup_id = _setup_id(
                symbol=current.symbol,
                side=candidate_side,
                trigger_ts_ms=current.close_time_ms,
                boundary=boundary,
                atr=previous_atr,
                config_sha256=settings.fingerprint,
            )
            candidate_metadata = (
                ("atr", _number(previous_atr)),
                ("boundary", _number(boundary)),
                ("close", _number(current.close)),
                ("true_range", _number(current_true_range)),
            )
            _append_event(
                events,
                event_type=RegimeRetestSetupEventType.STRUCTURAL_BREAKOUT_CANDIDATE,
                setup_id=setup_id,
                symbol=current.symbol,
                side=candidate_side,
                decision_ts_ms=current.close_time_ms,
                trigger_ts_ms=current.close_time_ms,
                metadata=candidate_metadata,
            )
            hourly_index = bisect_left(hourly_closes, current.open_time_ms) - 1
            hourly: _HourlyContext | None = None
            regime_reason = "missing_strict_prior_hour"
            if hourly_index >= 0:
                hourly_row = rows_1h[hourly_index]
                hourly = hourly_contexts[hourly_index]
                if hourly_row.open_time_ms < rows_5m[segment_start].open_time_ms:
                    hourly = None
                    regime_reason = "pre_segment_hour"
            if hourly is None:
                _append_event(
                    events,
                    event_type=RegimeRetestSetupEventType.REGIME_REJECT,
                    setup_id=setup_id,
                    symbol=current.symbol,
                    side=candidate_side,
                    decision_ts_ms=current.close_time_ms,
                    trigger_ts_ms=current.close_time_ms,
                    metadata=((*candidate_metadata, ("reason", regime_reason))),
                )
                continue
            regime_allowed, hourly_slope_atr, hourly_extension_atr = _regime_allows(
                candidate_side,
                hourly,
                settings,
            )
            if not regime_allowed:
                _append_event(
                    events,
                    event_type=RegimeRetestSetupEventType.REGIME_REJECT,
                    setup_id=setup_id,
                    symbol=current.symbol,
                    side=candidate_side,
                    decision_ts_ms=current.close_time_ms,
                    trigger_ts_ms=current.close_time_ms,
                    metadata=(
                        *candidate_metadata,
                        ("hourly_efficiency", _number(hourly.efficiency)),
                        ("hourly_extension_atr", _number(hourly_extension_atr)),
                        ("hourly_slope_atr", _number(hourly_slope_atr)),
                        ("reason", "hourly_veto"),
                    ),
                )
                continue
            armed = _trigger_setup(
                rows=rows_5m,
                index=index,
                previous_atr=previous_atr,
                true_ranges=true_ranges,
                current_true_range=current_true_range,
                hourly=hourly,
                settings=settings,
            )
            if armed is None:
                _append_event(
                    events,
                    event_type=RegimeRetestSetupEventType.EXPANSION_REJECT,
                    setup_id=setup_id,
                    symbol=current.symbol,
                    side=candidate_side,
                    decision_ts_ms=current.close_time_ms,
                    trigger_ts_ms=current.close_time_ms,
                    metadata=((*candidate_metadata, ("reason", "frozen_expansion_thresholds"))),
                )
                continue
            if armed.setup_id != setup_id or armed.side is not candidate_side:
                raise RuntimeError("armed setup identity differs from its structural candidate")
            active = armed
            _append_event(
                events,
                event_type=RegimeRetestSetupEventType.ARMED_SETUP,
                setup_id=setup_id,
                symbol=current.symbol,
                side=candidate_side,
                decision_ts_ms=current.close_time_ms,
                trigger_ts_ms=current.close_time_ms,
                metadata=candidate_metadata,
            )

        if active is not None:
            if segment_end < len(rows_5m):
                next_row = rows_5m[segment_end]
                is_zero_reset = next_row.open_time_ms in invalid_5m
                reset_timestamp = next_row.close_time_ms if is_zero_reset else next_row.open_time_ms
                _append_event(
                    events,
                    event_type=RegimeRetestSetupEventType.STATE_RESET,
                    setup_id=active.setup_id,
                    symbol=next_row.symbol,
                    side=active.side,
                    decision_ts_ms=reset_timestamp,
                    trigger_ts_ms=active.trigger_timestamp_ms,
                    metadata=(("reason", "zero_volume" if is_zero_reset else "gap"),),
                )
            else:
                _append_event(
                    events,
                    event_type=RegimeRetestSetupEventType.PENDING_SETUP,
                    setup_id=active.setup_id,
                    symbol=rows_5m[segment_end - 1].symbol,
                    side=active.side,
                    decision_ts_ms=rows_5m[segment_end - 1].close_time_ms,
                    trigger_ts_ms=active.trigger_timestamp_ms,
                    metadata=(("reason", "source_ended_before_resolution"),),
                )
    return evidence(intents, events)


def generate_regime_veto_retest_reclaim_intents(
    candles_1m: list[Candle],
    config: RegimeVetoRetestReclaimConfig | None = None,
) -> list[SleeveIntent]:
    """Backward-compatible intent list derived from immutable diagnostics."""

    return list(generate_regime_veto_retest_reclaim_evidence(candles_1m, config).intents)


__all__ = [
    "RegimeRetestGenerationCounters",
    "RegimeRetestGenerationEvidence",
    "RegimeRetestReclaimVariant",
    "RegimeRetestSetupEvent",
    "RegimeRetestSetupEventType",
    "RegimeVetoRetestReclaimConfig",
    "generate_regime_veto_retest_reclaim_evidence",
    "generate_regime_veto_retest_reclaim_intents",
]
