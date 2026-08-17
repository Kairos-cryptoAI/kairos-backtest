"""Causal, fully costed evaluation of one managed sleeve cell."""

from __future__ import annotations

import hashlib
import json
import math
import random
from bisect import bisect_left
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from enum import Enum, StrEnum

from kairos_core.enums import OrderSide, Side
from kairos_quant.candles import Candle

from .barrier_engine import BarrierExitDecision, ManagedPosition
from .cost_risk import AdmissionReason, AllInCostModel, RiskLimits, size_and_admit
from .execution import ExecutionConfig, FillSimulator
from .portfolio import CellEquityCurve, DailyCellSnapshot
from .strategy_models import ExitReason, SleeveIntent, TradeRecord
from .validation import canonical_candles

_MINUTE_MS = 60_000
_DAY_MS = 24 * 60 * _MINUTE_MS
_MINUTES_PER_DAY = _DAY_MS // _MINUTE_MS
_QUANTITY_REL_TOLERANCE = 1e-12


def _minute_open_at_or_after(timestamp_ms: int) -> int:
    return ((timestamp_ms + _MINUTE_MS - 1) // _MINUTE_MS) * _MINUTE_MS


def _quantities_equal(left: float, right: float) -> bool:
    """Compare positive-scale quantities without a unit-sized absolute floor."""

    return left == right or math.isclose(
        left,
        right,
        rel_tol=_QUANTITY_REL_TOLERANCE,
        abs_tol=0.0,
    )


def _lowercase_sha256(name: str, value: str) -> None:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or value != value.lower()
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ValueError(f"{name} must be a lowercase SHA-256")


def _canonical_assumption_value(value: object) -> object:
    """Normalize immutable assumptions into deterministic, numeric JSON values."""

    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Mapping):
        if any(not isinstance(key, str) for key in value):
            raise TypeError("canonical assumption mappings require string keys")
        return {key: _canonical_assumption_value(value[key]) for key in sorted(value)}
    if isinstance(value, (tuple, list)):
        return [_canonical_assumption_value(item) for item in value]
    if value is None or isinstance(value, (str, bool)):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("canonical assumptions cannot contain non-finite numbers")
        normalized = 0.0 if value == 0 else value
        if normalized.is_integer():
            return int(normalized)
        return {"__float_hex__": normalized.hex()}
    raise TypeError(f"unsupported canonical assumption value: {type(value).__name__}")


class IntentDispositionReason(StrEnum):
    """Final, mutually exclusive outcome of one immutable sleeve intent."""

    ENTERED = "entered"
    EXPIRED = "expired"
    OVERLAPPING_POSITION = "overlapping_position"
    INSUFFICIENT_TERMINAL_HORIZON = "insufficient_terminal_horizon"
    NO_LIQUIDITY = "no_liquidity"
    ENTRY_OUTSIDE_EXIT_PLAN = "entry_outside_exit_plan"
    REWARD_BELOW_COST_HURDLE = AdmissionReason.REWARD_BELOW_COST_HURDLE.value
    REWARD_RISK_TOO_LOW = AdmissionReason.REWARD_RISK_TOO_LOW.value
    INVALID_STOP_SIDE = AdmissionReason.INVALID_STOP_SIDE.value
    INVALID_TARGET_SIDE = AdmissionReason.INVALID_TARGET_SIDE.value
    STOP_TOO_TIGHT = AdmissionReason.STOP_TOO_TIGHT.value
    STOP_TOO_WIDE = AdmissionReason.STOP_TOO_WIDE.value


class ManagedFillPhase(StrEnum):
    """Execution phase owning one causal-capacity reservation."""

    ENTRY = "entry"
    EXIT = "exit"


class ManagedExecutionOrigin(StrEnum):
    """Causal order origin carried by every managed fill event."""

    ENTRY_SIGNAL = "entry_signal"
    RESTING_BARRIER = "resting_barrier"
    APPLICATION_EXIT = "application_exit"


@dataclass(frozen=True, slots=True)
class ManagedEvaluationPolicy:
    """Bound the terminal part of an otherwise resting exit plan.

    Stop, target and an already activated trailing stop are modelled as orders
    resting at the venue.  A timeout and every residual retry are application
    decisions and become executable only at the first one-minute open at or
    after their decision timestamp plus ``application_exit_latency_ms``.

    The absolute liquidation deadline is ``entry + max_hold +
    terminal_liquidation_grace_ms``.  The grace is deliberately not described
    as time since an early resting-barrier attempt.
    """

    application_exit_latency_ms: int = 250
    terminal_liquidation_grace_ms: int = 60 * _MINUTE_MS

    def __post_init__(self) -> None:
        for name, value in (
            ("application_exit_latency_ms", self.application_exit_latency_ms),
            ("terminal_liquidation_grace_ms", self.terminal_liquidation_grace_ms),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{name} must be a non-negative integer")
        if self.terminal_liquidation_grace_ms % _MINUTE_MS:
            raise ValueError("terminal liquidation grace must align with one-minute candles")
        if self.application_exit_latency_ms > self.terminal_liquidation_grace_ms:
            raise ValueError("application exit latency cannot exceed the terminal liquidation grace")


@dataclass(frozen=True, slots=True)
class ManagedEvaluationAssumptions:
    """Canonical immutable assumptions that produced one managed result."""

    execution: ExecutionConfig
    costs: AllInCostModel
    limits: RiskLimits
    policy: ManagedEvaluationPolicy
    seed: int
    assumptions_sha256: str = field(init=False)

    def __post_init__(self) -> None:
        if not isinstance(self.execution, ExecutionConfig):
            raise TypeError("assumptions execution must be an ExecutionConfig")
        if not isinstance(self.costs, AllInCostModel):
            raise TypeError("assumptions costs must be an AllInCostModel")
        if not isinstance(self.limits, RiskLimits):
            raise TypeError("assumptions limits must be RiskLimits")
        if not isinstance(self.policy, ManagedEvaluationPolicy):
            raise TypeError("assumptions policy must be a ManagedEvaluationPolicy")
        if isinstance(self.seed, bool) or not isinstance(self.seed, int):
            raise TypeError("assumptions seed must be an integer")
        payload = {
            "costs": asdict(self.costs),
            "execution": asdict(self.execution),
            "limits": asdict(self.limits),
            "policy": asdict(self.policy),
            "seed": self.seed,
        }
        encoded = json.dumps(
            _canonical_assumption_value(payload),
            allow_nan=False,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode()
        object.__setattr__(self, "assumptions_sha256", hashlib.sha256(encoded).hexdigest())


@dataclass(frozen=True, slots=True)
class ManagedFillEvent:
    """One submitted entry or protective-exit attempt with its capacity ledger."""

    intent_id: str
    phase: ManagedFillPhase
    execution_origin: ManagedExecutionOrigin
    execution_draw_index: int
    exit_reason: ExitReason | None
    exit_decision: BarrierExitDecision | None
    order_decision_timestamp_ms: int
    execution_eligible_timestamp_ms: int
    execution_candle_open_time_ms: int
    fill_timestamp_ms: int
    requested_quantity: float
    filled_quantity: float
    price: float
    reference_price: float
    fee_usd: float
    implementation_shortfall_usd: float
    causal_volume: float
    capacity_before: float
    capacity_after: float

    def __post_init__(self) -> None:
        _lowercase_sha256("fill event intent_id", self.intent_id)
        if not isinstance(self.phase, ManagedFillPhase):
            raise TypeError("fill event phase must be a ManagedFillPhase")
        if not isinstance(self.execution_origin, ManagedExecutionOrigin):
            raise TypeError("fill event execution_origin must be a ManagedExecutionOrigin")
        if (
            isinstance(self.execution_draw_index, bool)
            or not isinstance(self.execution_draw_index, int)
            or self.execution_draw_index < 0
        ):
            raise ValueError("fill event execution_draw_index must be a non-negative integer")
        for name, value in (
            ("order_decision_timestamp_ms", self.order_decision_timestamp_ms),
            ("execution_eligible_timestamp_ms", self.execution_eligible_timestamp_ms),
            ("execution_candle_open_time_ms", self.execution_candle_open_time_ms),
            ("fill_timestamp_ms", self.fill_timestamp_ms),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{name} must be a non-negative integer")
        if self.execution_eligible_timestamp_ms < self.order_decision_timestamp_ms:
            raise ValueError("execution eligibility cannot predate its order decision")
        if self.fill_timestamp_ms < self.execution_eligible_timestamp_ms:
            raise ValueError("fill cannot predate its execution eligibility")
        if self.fill_timestamp_ms < self.execution_candle_open_time_ms:
            raise ValueError("fill cannot predate its execution candle")
        if self.fill_timestamp_ms >= self.execution_candle_open_time_ms + _MINUTE_MS:
            raise ValueError("fill must remain inside its execution candle")
        if self.phase is ManagedFillPhase.ENTRY and (
            self.fill_timestamp_ms != self.execution_candle_open_time_ms
        ):
            raise ValueError("entry fills must execute exactly at their scheduled open")
        if self.phase is ManagedFillPhase.ENTRY:
            if self.execution_origin is not ManagedExecutionOrigin.ENTRY_SIGNAL:
                raise ValueError("entry fills must originate from an entry signal")
            if self.exit_reason is not None or self.exit_decision is not None:
                raise ValueError("entry fills cannot contain exit-decision evidence")
        elif self.execution_origin is ManagedExecutionOrigin.ENTRY_SIGNAL:
            raise ValueError("exit fills cannot originate from an entry signal")
        else:
            if not isinstance(self.exit_reason, ExitReason):
                raise TypeError("exit fills require an ExitReason")
            if not isinstance(self.exit_decision, BarrierExitDecision):
                raise TypeError("exit fills require a BarrierExitDecision")
            if (
                self.exit_decision.intent_id != self.intent_id
                or self.exit_decision.exit_reason is not self.exit_reason
                or not _quantities_equal(
                    self.exit_decision.requested_quantity,
                    self.requested_quantity,
                )
                or self.exit_decision.reference_price != self.reference_price
                or self.exit_decision.valid_until_timestamp_ms
                != self.execution_candle_open_time_ms + _MINUTE_MS - 1
                or not self.execution_candle_open_time_ms
                <= self.exit_decision.decision_timestamp_ms
                <= self.exit_decision.valid_until_timestamp_ms
                or not self.exit_decision.decision_timestamp_ms
                <= self.fill_timestamp_ms
                <= self.exit_decision.valid_until_timestamp_ms
            ):
                raise ValueError("exit fill and barrier decision evidence are inconsistent")
        if self.execution_origin is ManagedExecutionOrigin.APPLICATION_EXIT and (
            self.fill_timestamp_ms != self.execution_candle_open_time_ms
        ):
            raise ValueError("application exits must execute exactly at a minute open")
        if self.execution_origin is ManagedExecutionOrigin.RESTING_BARRIER and not (
            self.order_decision_timestamp_ms == self.execution_eligible_timestamp_ms == self.fill_timestamp_ms
        ):
            raise ValueError("resting barriers must be immediately eligible at their trigger")
        nonnegative = (
            self.requested_quantity,
            self.filled_quantity,
            self.fee_usd,
            self.implementation_shortfall_usd,
            self.causal_volume,
            self.capacity_before,
            self.capacity_after,
        )
        if any(
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
            or value < 0
            for value in nonnegative
        ):
            raise ValueError("fill quantities, costs and capacity must be finite and non-negative")
        if (
            not math.isfinite(self.price)
            or self.price <= 0
            or not math.isfinite(self.reference_price)
            or self.reference_price <= 0
        ):
            raise ValueError("fill event price and reference must be finite and positive")
        if self.requested_quantity <= 0:
            raise ValueError("fill event requested quantity must be positive")
        if self.filled_quantity > self.requested_quantity and not _quantities_equal(
            self.filled_quantity,
            self.requested_quantity,
        ):
            raise ValueError("fill event cannot exceed its requested quantity")
        expected_before = math.fsum((self.capacity_after, self.filled_quantity))
        if not _quantities_equal(self.capacity_before, expected_before):
            raise ValueError("fill event does not conserve shared candle capacity")


@dataclass(frozen=True, slots=True)
class IntentDisposition:
    """Auditable decision made for exactly one input intent."""

    intent_id: str
    intent: SleeveIntent
    reason: IntentDispositionReason
    scheduled_entry_timestamp_ms: int | None
    entry_reference_price: float | None = None
    entry_price_bounds: tuple[float, float] | None = None
    admission_equity_usd: float | None = None
    causal_volume: float | None = None
    capacity_before_entry: float | None = None
    requested_quantity: float = 0.0
    filled_quantity: float = 0.0

    def __post_init__(self) -> None:
        _lowercase_sha256("disposition intent_id", self.intent_id)
        if not isinstance(self.intent, SleeveIntent) or self.intent.intent_id != self.intent_id:
            raise ValueError("disposition must retain its complete immutable intent")
        if not isinstance(self.reason, IntentDispositionReason):
            raise TypeError("disposition reason must be an IntentDispositionReason")
        timestamp = self.scheduled_entry_timestamp_ms
        if timestamp is not None and (
            isinstance(timestamp, bool) or not isinstance(timestamp, int) or timestamp < 0
        ):
            raise ValueError("scheduled entry timestamp must be a non-negative integer")
        quantities = (self.requested_quantity, self.filled_quantity)
        if any(
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
            or value < 0
            for value in quantities
        ):
            raise ValueError("disposition quantities must be finite and non-negative")
        if self.filled_quantity > self.requested_quantity and not _quantities_equal(
            self.filled_quantity,
            self.requested_quantity,
        ):
            raise ValueError("filled disposition quantity cannot exceed requested quantity")
        entered = self.reason is IntentDispositionReason.ENTERED
        if entered is not (self.filled_quantity > 0):
            raise ValueError("only an entered disposition can contain a positive fill")
        if not entered and (self.requested_quantity or self.filled_quantity):
            raise ValueError("rejected dispositions cannot contain allocated quantities")
        replay_values = (
            self.entry_reference_price,
            self.admission_equity_usd,
            self.causal_volume,
            self.capacity_before_entry,
        )
        if any(
            value is not None
            and (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(value)
                or value < 0
            )
            for value in replay_values
        ):
            raise ValueError("disposition replay values must be finite and non-negative")
        if self.entry_reference_price is not None and self.entry_reference_price <= 0:
            raise ValueError("entry replay reference price must be positive")
        if self.admission_equity_usd is not None and self.admission_equity_usd <= 0:
            raise ValueError("entry admission equity must be positive")
        if self.entry_price_bounds is not None:
            if (
                not isinstance(self.entry_price_bounds, tuple)
                or len(self.entry_price_bounds) != 2
                or any(
                    isinstance(value, bool)
                    or not isinstance(value, (int, float))
                    or not math.isfinite(value)
                    or value <= 0
                    for value in self.entry_price_bounds
                )
                or self.entry_price_bounds[0] > self.entry_price_bounds[1]
            ):
                raise ValueError("entry price bounds must be an ordered positive pair")
        if self.scheduled_entry_timestamp_ms is None and any(
            value is not None
            for value in (
                self.entry_reference_price,
                self.entry_price_bounds,
                self.admission_equity_usd,
                self.causal_volume,
                self.capacity_before_entry,
            )
        ):
            raise ValueError("unscheduled dispositions cannot contain entry replay evidence")


@dataclass(frozen=True, slots=True)
class ManagedFundingEvent:
    """One funding settlement charged while a managed position was open."""

    intent_id: str
    timestamp_ms: int
    signed_quantity: float
    mark_price: float
    charged_cost_usd: float
    observed: bool

    def __post_init__(self) -> None:
        _lowercase_sha256("funding event intent_id", self.intent_id)
        if (
            isinstance(self.timestamp_ms, bool)
            or not isinstance(self.timestamp_ms, int)
            or self.timestamp_ms < 0
        ):
            raise ValueError("funding event timestamp must be a non-negative integer")
        for name, value in (
            ("signed_quantity", self.signed_quantity),
            ("mark_price", self.mark_price),
            ("charged_cost_usd", self.charged_cost_usd),
        ):
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                raise ValueError(f"funding event {name} must be finite")
        if self.signed_quantity == 0:
            raise ValueError("funding events require a live signed quantity")
        if self.mark_price <= 0 or self.charged_cost_usd < 0:
            raise ValueError("funding event mark must be positive and cost non-negative")
        if not isinstance(self.observed, bool):
            raise TypeError("funding event observed flag must be boolean")


@dataclass(frozen=True, slots=True)
class ManagedCellCounters:
    """Deterministic aggregate event counts for one managed evaluation."""

    intents: int
    entered: int
    rejected: int
    overlapping: int
    expired: int
    insufficient_terminal_horizon: int
    no_liquidity: int
    partial_entries: int
    exit_fill_attempts: int
    partial_exits: int
    zero_fill_exits: int
    funding_settlements_expected: int
    funding_settlements_observed: int

    def __post_init__(self) -> None:
        values = (
            self.intents,
            self.entered,
            self.rejected,
            self.overlapping,
            self.expired,
            self.insufficient_terminal_horizon,
            self.no_liquidity,
            self.partial_entries,
            self.exit_fill_attempts,
            self.partial_exits,
            self.zero_fill_exits,
            self.funding_settlements_expected,
            self.funding_settlements_observed,
        )
        if any(isinstance(value, bool) or not isinstance(value, int) or value < 0 for value in values):
            raise ValueError("managed evaluation counters must be non-negative integers")
        if self.entered + self.rejected != self.intents:
            raise ValueError("entered and rejected counters must partition all intents")
        if self.partial_entries > self.entered:
            raise ValueError("partial entries cannot exceed entered intents")
        if self.partial_exits + self.zero_fill_exits > self.exit_fill_attempts:
            raise ValueError("partial and zero exit fills cannot exceed exit attempts")
        if self.funding_settlements_observed > self.funding_settlements_expected:
            raise ValueError("observed funding settlements cannot exceed expected settlements")


def _replay_evidence_sha256(
    *,
    cell: CellEquityCurve,
    dispositions: tuple[IntentDisposition, ...],
    fills: tuple[ManagedFillEvent, ...],
    funding_events: tuple[ManagedFundingEvent, ...],
    assumptions_sha256: str,
    counters: ManagedCellCounters,
    fees_usd: float,
    carry_cost_usd: float,
    implementation_shortfall_usd: float,
    turnover_usd: float,
) -> str:
    """Self-check the replay payload; external registry sealing provides attestation."""

    payload = {
        "aggregates": {
            "carry_cost_usd": carry_cost_usd,
            "fees_usd": fees_usd,
            "implementation_shortfall_usd": implementation_shortfall_usd,
            "turnover_usd": turnover_usd,
        },
        "assumptions_sha256": assumptions_sha256,
        "cell": {
            "cell_id": cell.cell_id,
            "closing_equity_usd": cell.closing_equity_usd,
            "dates": tuple(day.isoformat() for day in cell.dates),
            "initial_equity_usd": cell.initial_equity_usd,
            "sleeve_id": cell.sleeve_id,
            "symbol": cell.symbol,
            "trade_ids": tuple(trade.intent.intent_id for trade in cell.trades),
        },
        "counters": asdict(counters),
        "dispositions": tuple(asdict(item) for item in dispositions),
        "fills": tuple(asdict(item) for item in fills),
        "funding_events": tuple(asdict(item) for item in funding_events),
        "schema": "kairos.managed-replay-evidence.v1",
    }
    encoded = json.dumps(
        _canonical_assumption_value(payload),
        allow_nan=False,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True, slots=True)
class ManagedCellResult:
    """Frozen cell evidence plus execution diagnostics.

    ``replay_verified`` means the object passed all deterministic checks in
    this module.  It is not a cryptographic provenance claim.  This layer
    deliberately leaves ``external_attestation_verified`` false; a signed,
    append-only trial registry must attest the canonical evidence separately.
    """

    cell: CellEquityCurve
    dispositions: tuple[IntentDisposition, ...]
    fills: tuple[ManagedFillEvent, ...]
    funding_events: tuple[ManagedFundingEvent, ...]
    assumptions: ManagedEvaluationAssumptions
    assumptions_sha256: str
    replay_evidence_sha256: str
    counters: ManagedCellCounters
    fees_usd: float
    carry_cost_usd: float
    implementation_shortfall_usd: float
    turnover_usd: float
    replay_verified: bool = field(init=False, default=False)
    external_attestation_verified: bool = field(init=False, default=False)

    def __post_init__(self) -> None:
        if not isinstance(self.cell, CellEquityCurve):
            raise TypeError("cell must be a CellEquityCurve")
        if not isinstance(self.counters, ManagedCellCounters):
            raise TypeError("counters must be ManagedCellCounters")
        if not isinstance(self.dispositions, tuple) or any(
            not isinstance(item, IntentDisposition) for item in self.dispositions
        ):
            raise TypeError("dispositions must be an immutable tuple")
        if len(self.dispositions) != self.counters.intents:
            raise ValueError("dispositions must cover every intent exactly once")
        if len({item.intent_id for item in self.dispositions}) != len(self.dispositions):
            raise ValueError("disposition intent identities must be unique")
        if not isinstance(self.fills, tuple) or any(
            not isinstance(item, ManagedFillEvent) for item in self.fills
        ):
            raise TypeError("fills must be an immutable tuple of ManagedFillEvent values")
        if not isinstance(self.funding_events, tuple) or any(
            not isinstance(item, ManagedFundingEvent) for item in self.funding_events
        ):
            raise TypeError("funding_events must be an immutable tuple of ManagedFundingEvent values")
        if not isinstance(self.assumptions, ManagedEvaluationAssumptions):
            raise TypeError("assumptions must be ManagedEvaluationAssumptions")
        _lowercase_sha256("assumptions_sha256", self.assumptions_sha256)
        _lowercase_sha256("replay_evidence_sha256", self.replay_evidence_sha256)
        if self.assumptions_sha256 != self.assumptions.assumptions_sha256:
            raise ValueError("managed assumptions do not match their sealed SHA-256")
        tolerance = max(1e-9, self.cell.initial_equity_usd * 1e-12)
        fill_order = tuple(
            (item.execution_candle_open_time_ms, item.fill_timestamp_ms) for item in self.fills
        )
        if fill_order != tuple(sorted(fill_order)):
            raise ValueError("managed fills must be in causal execution order")
        previous_by_candle: dict[int, tuple[float, float]] = {}
        for draw_index, item in enumerate(self.fills):
            if item.execution_draw_index != draw_index:
                raise ValueError("managed fills must retain their complete RNG draw order")
            previous = previous_by_candle.get(item.execution_candle_open_time_ms)
            if previous is None:
                expected_capacity = item.causal_volume * self.assumptions.execution.max_volume_participation
                if not _quantities_equal(item.capacity_before, expected_capacity):
                    raise ValueError("first candle fill capacity contradicts causal volume")
            elif not (
                _quantities_equal(item.causal_volume, previous[0])
                and _quantities_equal(item.capacity_before, previous[1])
            ):
                raise ValueError("same-candle fills must share one contiguous capacity ledger")
            expected_fill = min(item.requested_quantity, item.capacity_before)
            if not _quantities_equal(item.filled_quantity, expected_fill):
                raise ValueError("fill quantity contradicts its causal candle capacity")
            previous_by_candle[item.execution_candle_open_time_ms] = (
                item.causal_volume,
                item.capacity_after,
            )

        disposition_by_id = {item.intent_id: item for item in self.dispositions}
        if any(item.intent_id != item.intent.intent_id for item in self.dispositions):
            raise ValueError("disposition inventory lost its canonical intent identity")
        entered_ids = {
            item.intent_id for item in self.dispositions if item.reason is IntentDispositionReason.ENTERED
        }
        trade_ids = {trade.intent.intent_id for trade in self.cell.trades}
        if entered_ids != trade_ids:
            raise ValueError("terminal-flat entered intents and closed trades must match exactly")
        if any(item.intent_id not in entered_ids for item in self.fills):
            raise ValueError("only entered intents may own managed fill events")
        entry_fills = tuple(item for item in self.fills if item.phase is ManagedFillPhase.ENTRY)
        entry_by_id = {item.intent_id: item for item in entry_fills}
        trade_by_id = {trade.intent.intent_id: trade for trade in self.cell.trades}
        if len(entry_by_id) != len(entry_fills) or set(entry_by_id) != entered_ids:
            raise ValueError("every entered intent must have exactly one IOC entry fill")

        first_open = int(
            datetime.combine(self.cell.snapshots[0].day, datetime.min.time(), tzinfo=UTC).timestamp() * 1_000
        )
        last_day_open = int(
            datetime.combine(self.cell.snapshots[-1].day, datetime.min.time(), tzinfo=UTC).timestamp() * 1_000
        )
        last_open = last_day_open + _DAY_MS - _MINUTE_MS
        funding_interval = self.assumptions.execution.funding.settlement_interval_ms
        if funding_interval % _MINUTE_MS:
            raise ValueError("sealed funding cadence must align with one-minute candles")
        first_funding = (first_open // funding_interval + 1) * funding_interval
        settlement_timestamps = range(first_funding, last_open + 1, funding_interval)
        expected_settlements = 0
        observed_settlements = 0
        for timestamp_ms in settlement_timestamps:
            _, observed = self.assumptions.execution.funding.settlement_cost(0.0, timestamp_ms)
            expected_settlements += 1
            observed_settlements += int(observed)
        if (
            self.counters.funding_settlements_expected != expected_settlements
            or self.counters.funding_settlements_observed != observed_settlements
        ):
            raise ValueError("funding counters contradict the complete evaluation cadence")
        admission_reasons = {
            IntentDispositionReason.REWARD_BELOW_COST_HURDLE,
            IntentDispositionReason.REWARD_RISK_TOO_LOW,
            IntentDispositionReason.INVALID_STOP_SIDE,
            IntentDispositionReason.INVALID_TARGET_SIDE,
            IntentDispositionReason.STOP_TOO_TIGHT,
            IntentDispositionReason.STOP_TOO_WIDE,
        }
        for disposition in self.dispositions:
            intent = disposition.intent
            causal_timestamp = max(
                intent.entry_eligible_ts_ms,
                intent.decision_ts_ms + self.assumptions.execution.latency_ms,
            )
            expected_entry_open = _minute_open_at_or_after(causal_timestamp)
            if expected_entry_open > last_open:
                expected_reason = (
                    IntentDispositionReason.EXPIRED
                    if causal_timestamp > intent.entry_expires_ts_ms
                    else IntentDispositionReason.INSUFFICIENT_TERMINAL_HORIZON
                )
                if (
                    disposition.scheduled_entry_timestamp_ms is not None
                    or disposition.reason is not expected_reason
                ):
                    raise ValueError("unscheduled disposition contradicts its evaluation horizon")
                continue
            if expected_entry_open < first_open:
                raise ValueError("intent entry schedule predates the managed evaluation")
            if disposition.scheduled_entry_timestamp_ms != expected_entry_open:
                raise ValueError("disposition entry schedule contradicts its immutable intent")
            if expected_entry_open > intent.entry_expires_ts_ms:
                if disposition.reason is not IntentDispositionReason.EXPIRED:
                    raise ValueError("expired disposition contradicts its immutable intent")
                continue
            liquidation_deadline = (
                expected_entry_open
                + intent.exit_plan.max_holding_ms
                + self.assumptions.policy.terminal_liquidation_grace_ms
            )
            if liquidation_deadline > last_open:
                if disposition.reason is not IntentDispositionReason.INSUFFICIENT_TERMINAL_HORIZON:
                    raise ValueError("terminal-horizon disposition contradicts its immutable intent")
                continue
            if (
                disposition.entry_reference_price is None
                or disposition.entry_price_bounds is None
                or disposition.causal_volume is None
                or disposition.capacity_before_entry is None
            ):
                raise ValueError("scheduled disposition is missing causal entry replay evidence")
            expected_bounds = _entry_price_bounds_for_open(
                disposition.entry_reference_price,
                intent,
                self.assumptions.execution,
            )
            if disposition.entry_price_bounds != expected_bounds:
                raise ValueError("disposition entry bounds contradict execution assumptions")
            initial_capacity = disposition.causal_volume * self.assumptions.execution.max_volume_participation
            if disposition.capacity_before_entry > initial_capacity and not _quantities_equal(
                disposition.capacity_before_entry,
                initial_capacity,
            ):
                raise ValueError("disposition capacity exceeds its causal volume budget")
            if disposition.reason is IntentDispositionReason.OVERLAPPING_POSITION:
                if not any(
                    trade.entry_timestamp_ms <= expected_entry_open < trade.exit_timestamp_ms
                    for trade in self.cell.trades
                ):
                    raise ValueError("overlap disposition has no live managed position")
                continue
            outside_plan = any(not _entry_is_inside_plan(intent, price) for price in expected_bounds)
            if outside_plan:
                if disposition.reason is not IntentDispositionReason.ENTRY_OUTSIDE_EXIT_PLAN:
                    raise ValueError("entry-plan disposition contradicts its replay bounds")
                continue
            if disposition.admission_equity_usd is None:
                raise ValueError("admitted disposition is missing its replay equity")
            expected_equity = self.cell.initial_equity_usd + math.fsum(
                trade.net_pnl_usd
                for trade in self.cell.trades
                if trade.exit_timestamp_ms <= expected_entry_open
            )
            equity_tolerance = max(1e-9, abs(expected_equity) * 1e-12)
            if not math.isclose(
                disposition.admission_equity_usd,
                expected_equity,
                rel_tol=1e-12,
                abs_tol=equity_tolerance,
            ):
                raise ValueError("disposition admission equity contradicts the closed-trade ledger")
            plan = intent.exit_plan
            admissions = tuple(
                size_and_admit(
                    side=intent.side,
                    entry_price=price,
                    stop_price=plan.stop_price,
                    target_price=plan.target_price,
                    equity_usd=disposition.admission_equity_usd,
                    costs=self.assumptions.costs,
                    limits=self.assumptions.limits,
                )
                for price in expected_bounds
            )
            rejected_admission = next(
                (admission for admission in admissions if not admission.accepted),
                None,
            )
            if rejected_admission is not None:
                if (
                    disposition.reason not in admission_reasons
                    or disposition.reason is not _admission_disposition(rejected_admission.reason)
                ):
                    raise ValueError("admission rejection contradicts cost and risk assumptions")
                continue
            expected_request = min(admission.quantity for admission in admissions)
            if disposition.reason is IntentDispositionReason.NO_LIQUIDITY:
                if disposition.capacity_before_entry != 0:
                    raise ValueError("no-liquidity disposition retained executable capacity")
                continue
            if disposition.reason is not IntentDispositionReason.ENTERED:
                raise ValueError("accepted admission has an invalid terminal disposition")
            if not _quantities_equal(disposition.requested_quantity, expected_request):
                raise ValueError("entered quantity contradicts cost-aware risk sizing")
            entry_fill = entry_by_id.get(disposition.intent_id)
            if entry_fill is None or not _quantities_equal(
                entry_fill.capacity_before,
                disposition.capacity_before_entry,
            ):
                raise ValueError("entered disposition lost its causal capacity evidence")

        for intent_id in entered_ids:
            disposition = disposition_by_id[intent_id]
            fill = entry_by_id[intent_id]
            trade = trade_by_id[intent_id]
            intent = trade.intent
            entry_eligible = max(
                intent.entry_eligible_ts_ms,
                intent.decision_ts_ms + self.assumptions.execution.latency_ms,
            )
            expected_entry_open = _minute_open_at_or_after(entry_eligible)
            if not (
                math.isclose(
                    disposition.requested_quantity,
                    fill.requested_quantity,
                    rel_tol=_QUANTITY_REL_TOLERANCE,
                    abs_tol=0.0,
                )
                and math.isclose(
                    disposition.filled_quantity,
                    fill.filled_quantity,
                    rel_tol=_QUANTITY_REL_TOLERANCE,
                    abs_tol=0.0,
                )
            ):
                raise ValueError("entry disposition and fill quantities must match")
            if not (
                disposition.scheduled_entry_timestamp_ms == fill.fill_timestamp_ms == trade.entry_timestamp_ms
                and fill.execution_origin is ManagedExecutionOrigin.ENTRY_SIGNAL
                and fill.order_decision_timestamp_ms == intent.decision_ts_ms
                and fill.execution_eligible_timestamp_ms == entry_eligible
                and fill.execution_candle_open_time_ms == expected_entry_open
                and math.isclose(
                    fill.filled_quantity,
                    trade.quantity,
                    rel_tol=_QUANTITY_REL_TOLERANCE,
                    abs_tol=0.0,
                )
                and math.isclose(
                    fill.price,
                    trade.entry_price,
                    rel_tol=1e-12,
                    abs_tol=tolerance,
                )
                and math.isclose(
                    fill.fee_usd,
                    trade.entry_fee_usd,
                    rel_tol=1e-12,
                    abs_tol=tolerance,
                )
            ):
                raise ValueError("entry fill evidence is inconsistent with its closed trade")

        exit_fills = tuple(item for item in self.fills if item.phase is ManagedFillPhase.EXIT)
        exits_by_id: dict[str, list[ManagedFillEvent]] = {}
        for item in exit_fills:
            exits_by_id.setdefault(item.intent_id, []).append(item)
        for intent_id in entered_ids:
            trade = trade_by_id[intent_id]
            owned_exits = tuple(exits_by_id.get(intent_id, ()))
            filled_quantity = math.fsum(item.filled_quantity for item in owned_exits)
            exit_notional = math.fsum(item.filled_quantity * item.price for item in owned_exits)
            exit_fee = math.fsum(item.fee_usd for item in owned_exits)
            total_shortfall = entry_by_id[intent_id].implementation_shortfall_usd + math.fsum(
                item.implementation_shortfall_usd for item in owned_exits
            )
            if not owned_exits or not (
                _quantities_equal(filled_quantity, trade.quantity)
                and math.isclose(
                    exit_notional / filled_quantity,
                    trade.exit_price,
                    rel_tol=1e-12,
                    abs_tol=tolerance,
                )
                and max(item.fill_timestamp_ms for item in owned_exits) == trade.exit_timestamp_ms
                and math.isclose(
                    exit_fee,
                    trade.exit_fee_usd,
                    rel_tol=1e-12,
                    abs_tol=tolerance,
                )
                and math.isclose(
                    total_shortfall,
                    trade.implementation_shortfall_usd,
                    rel_tol=1e-12,
                    abs_tol=tolerance,
                )
            ):
                raise ValueError("exit fill evidence is inconsistent with its closed trade")
            nominal_timeout = trade.entry_timestamp_ms + trade.intent.exit_plan.max_holding_ms
            liquidation_deadline = nominal_timeout + self.assumptions.policy.terminal_liquidation_grace_ms
            previous_exit: ManagedFillEvent | None = None
            pending_reason: ExitReason | None = None
            remaining_quantity = trade.quantity
            previous_sequence = 0
            for item in owned_exits:
                decision = item.exit_decision
                if decision is None or item.exit_reason is None:
                    raise ValueError("exit fill lost its immutable barrier decision")
                if decision.decision_sequence <= previous_sequence:
                    raise ValueError("exit decisions must progress monotonically")
                previous_sequence = decision.decision_sequence
                if not _quantities_equal(item.requested_quantity, remaining_quantity):
                    raise ValueError("exit retry must request the complete residual quantity")
                remaining_quantity = math.fsum((remaining_quantity, -item.filled_quantity))
                if remaining_quantity < 0 and not _quantities_equal(remaining_quantity, 0.0):
                    raise ValueError("exit fills exceeded the managed position")
                if item.fill_timestamp_ms > liquidation_deadline:
                    raise ValueError("exit fill exceeded the sealed terminal liquidation deadline")
                if item.execution_origin is ManagedExecutionOrigin.RESTING_BARRIER:
                    if (
                        previous_exit is not None
                        or item.fill_timestamp_ms > nominal_timeout
                        or item.exit_reason is ExitReason.TIMEOUT
                    ):
                        raise ValueError("resting barrier evidence is inconsistent with exit state")
                    pending_reason = item.exit_reason
                elif item.execution_origin is ManagedExecutionOrigin.APPLICATION_EXIT:
                    if previous_exit is None:
                        if item.exit_reason is not ExitReason.TIMEOUT:
                            raise ValueError("first application exit must originate from a timeout")
                        pending_reason = item.exit_reason
                    elif item.exit_reason is not pending_reason:
                        raise ValueError("residual retry changed its pending exit reason")
                    expected_decision = (
                        _minute_open_at_or_after(nominal_timeout)
                        if previous_exit is None
                        else previous_exit.fill_timestamp_ms
                    )
                    expected_eligible = (
                        expected_decision + self.assumptions.policy.application_exit_latency_ms
                    )
                    expected_open = _minute_open_at_or_after(expected_eligible)
                    if previous_exit is not None:
                        expected_open = max(
                            expected_open,
                            previous_exit.execution_candle_open_time_ms + _MINUTE_MS,
                        )
                    if not (
                        item.order_decision_timestamp_ms == expected_decision
                        and item.execution_eligible_timestamp_ms == expected_eligible
                        and item.execution_candle_open_time_ms == expected_open
                        and decision.decision_timestamp_ms == item.fill_timestamp_ms
                    ):
                        raise ValueError("application exit timing contradicts its sealed policy")
                else:
                    raise ValueError("exit fill has an invalid execution origin")
                if item.exit_reason is not pending_reason:
                    raise ValueError("managed exit fills must preserve one terminal reason")
                previous_exit = item
            if not _quantities_equal(remaining_quantity, 0.0):
                raise ValueError("managed exit ledger retained a residual quantity")
            if trade.exit_reason is not pending_reason:
                raise ValueError("closed trade reason contradicts its exit-fill ledger")
            if trade.ambiguous_intrabar is not any(
                item.exit_decision is not None and item.exit_decision.ambiguous_intrabar
                for item in owned_exits
            ):
                raise ValueError("trade ambiguity contradicts its barrier decisions")

        _validate_execution_cost_coverage(
            self.assumptions.execution,
            self.assumptions.costs,
        )
        for trade in self.cell.trades:
            required_funding_bps = _required_funding_bps(
                trade.intent,
                trade.entry_timestamp_ms,
                self.assumptions.execution,
                self.assumptions.policy,
            )
            if self.assumptions.costs.adverse_funding_bps + 1e-12 < required_funding_bps:
                raise ValueError("sealed admission costs do not cover the funding horizon")

        replay_random = random.Random(self.assumptions.seed)  # nosec B311
        base_cost_bps = self.assumptions.execution.spread_bps / 2 + self.assumptions.execution.slippage_bps
        for item in self.fills:
            trade = trade_by_id[item.intent_id]
            side = _order_side(trade.intent.side, exiting=item.phase is ManagedFillPhase.EXIT)
            direction = 1.0 if side is OrderSide.BUY else -1.0
            jitter = replay_random.uniform(
                -self.assumptions.execution.slippage_jitter_bps,
                self.assumptions.execution.slippage_jitter_bps,
            )
            expected_cost_bps = max(0.0, base_cost_bps + jitter)
            expected_price = item.reference_price * (1 + direction * expected_cost_bps / 10_000)
            expected_fee = item.filled_quantity * item.price * self.assumptions.execution.fee_bps / 10_000
            expected_shortfall = item.filled_quantity * abs(item.price - item.reference_price)
            if not (
                math.isclose(item.price, expected_price, rel_tol=1e-14, abs_tol=0.0)
                and math.isclose(item.fee_usd, expected_fee, rel_tol=1e-12, abs_tol=tolerance)
                and math.isclose(
                    item.implementation_shortfall_usd,
                    expected_shortfall,
                    rel_tol=1e-12,
                    abs_tol=tolerance,
                )
            ):
                raise ValueError("fill economics contradict the seeded execution replay")

        funding_by_intent: dict[str, list[ManagedFundingEvent]] = {}
        previous_funding_timestamp = -1
        for event in self.funding_events:
            if event.timestamp_ms < previous_funding_timestamp:
                raise ValueError("funding events must be in chronological order")
            previous_funding_timestamp = event.timestamp_ms
            funding_trade = trade_by_id.get(event.intent_id)
            if funding_trade is None or not (
                funding_trade.entry_timestamp_ms < event.timestamp_ms <= funding_trade.exit_timestamp_ms
            ):
                raise ValueError("funding event does not belong to an open managed trade")
            exited_before_settlement = math.fsum(
                fill.filled_quantity
                for fill in exits_by_id.get(event.intent_id, ())
                if fill.fill_timestamp_ms < event.timestamp_ms
            )
            remaining_at_settlement = math.fsum((funding_trade.quantity, -exited_before_settlement))
            direction = 1.0 if funding_trade.intent.side is Side.LONG else -1.0
            if not _quantities_equal(
                event.signed_quantity,
                direction * remaining_at_settlement,
            ):
                raise ValueError("funding quantity contradicts prior exit fills")
            expected_raw_cost, expected_observed = self.assumptions.execution.funding.settlement_cost(
                event.signed_quantity * event.mark_price,
                event.timestamp_ms,
            )
            expected_cost = max(0.0, expected_raw_cost)
            if not (
                event.timestamp_ms % self.assumptions.execution.funding.settlement_interval_ms == 0
                and event.observed is expected_observed
                and math.isclose(
                    event.charged_cost_usd,
                    expected_cost,
                    rel_tol=1e-12,
                    abs_tol=tolerance,
                )
            ):
                raise ValueError("funding event contradicts the sealed funding assumptions")
            funding_by_intent.setdefault(event.intent_id, []).append(event)
        if not math.isclose(
            math.fsum(event.charged_cost_usd for event in self.funding_events),
            self.carry_cost_usd,
            rel_tol=1e-12,
            abs_tol=tolerance,
        ):
            raise ValueError("funding-event costs do not reconcile to managed carry")
        for trade in self.cell.trades:
            if not math.isclose(
                math.fsum(
                    event.charged_cost_usd for event in funding_by_intent.get(trade.intent.intent_id, ())
                ),
                trade.carry_cost_usd,
                rel_tol=1e-12,
                abs_tol=tolerance,
            ):
                raise ValueError("trade carry does not reconcile to its funding events")
        computed_partial_entries = sum(
            0 < item.filled_quantity < item.requested_quantity for item in entry_fills
        )
        computed_partial_exits = sum(
            0 < item.filled_quantity < item.requested_quantity for item in exit_fills
        )
        computed_zero_exits = sum(item.filled_quantity == 0 for item in exit_fills)
        reason_counts = {
            reason: sum(item.reason is reason for item in self.dispositions)
            for reason in IntentDispositionReason
        }
        if (
            self.counters.entered != len(entered_ids)
            or self.counters.rejected != len(self.dispositions) - len(entered_ids)
            or self.counters.overlapping != reason_counts[IntentDispositionReason.OVERLAPPING_POSITION]
            or self.counters.expired != reason_counts[IntentDispositionReason.EXPIRED]
            or self.counters.insufficient_terminal_horizon
            != reason_counts[IntentDispositionReason.INSUFFICIENT_TERMINAL_HORIZON]
            or self.counters.no_liquidity != reason_counts[IntentDispositionReason.NO_LIQUIDITY]
            or self.counters.partial_entries != computed_partial_entries
            or self.counters.exit_fill_attempts != len(exit_fills)
            or self.counters.partial_exits != computed_partial_exits
            or self.counters.zero_fill_exits != computed_zero_exits
        ):
            raise ValueError("managed counters are inconsistent with dispositions and fills")
        values = (
            self.fees_usd,
            self.carry_cost_usd,
            self.implementation_shortfall_usd,
            self.turnover_usd,
        )
        if any(not math.isfinite(value) or value < 0 for value in values):
            raise ValueError("managed execution costs must be finite and non-negative")
        if not math.isclose(
            self.fees_usd,
            sum(item.fee_usd for item in self.fills),
            rel_tol=1e-12,
            abs_tol=tolerance,
        ):
            raise ValueError("managed fill fees are inconsistent with their event ledger")
        if not math.isclose(
            self.implementation_shortfall_usd,
            sum(item.implementation_shortfall_usd for item in self.fills),
            rel_tol=1e-12,
            abs_tol=tolerance,
        ):
            raise ValueError("managed shortfall is inconsistent with its event ledger")
        if not math.isclose(
            self.turnover_usd,
            sum(item.filled_quantity * item.price for item in self.fills),
            rel_tol=1e-12,
            abs_tol=tolerance,
        ):
            raise ValueError("managed turnover is inconsistent with its event ledger")
        closed_fees = sum(trade.entry_fee_usd + trade.exit_fee_usd for trade in self.cell.trades)
        closed_carry = sum(trade.carry_cost_usd for trade in self.cell.trades)
        closed_shortfall = sum(trade.implementation_shortfall_usd for trade in self.cell.trades)
        if not (
            math.isclose(self.fees_usd, closed_fees, rel_tol=1e-12, abs_tol=tolerance)
            and math.isclose(
                self.carry_cost_usd,
                closed_carry,
                rel_tol=1e-12,
                abs_tol=tolerance,
            )
            and math.isclose(
                self.implementation_shortfall_usd,
                closed_shortfall,
                rel_tol=1e-12,
                abs_tol=tolerance,
            )
        ):
            raise ValueError("terminal execution costs must reconcile to closed trades")
        expected_replay_sha256 = _replay_evidence_sha256(
            cell=self.cell,
            dispositions=self.dispositions,
            fills=self.fills,
            funding_events=self.funding_events,
            assumptions_sha256=self.assumptions_sha256,
            counters=self.counters,
            fees_usd=self.fees_usd,
            carry_cost_usd=self.carry_cost_usd,
            implementation_shortfall_usd=self.implementation_shortfall_usd,
            turnover_usd=self.turnover_usd,
        )
        if self.replay_evidence_sha256 != expected_replay_sha256:
            raise ValueError("managed replay evidence does not match its self-check SHA-256")
        object.__setattr__(self, "replay_verified", True)
        object.__setattr__(self, "external_attestation_verified", False)

    @property
    def equity_curve(self) -> CellEquityCurve:
        """Compatibility alias naming the primary evidence explicitly."""

        return self.cell

    @property
    def policy(self) -> ManagedEvaluationPolicy:
        """Return the policy sealed inside the canonical assumptions."""

        return self.assumptions.policy

    @property
    def trades(self) -> tuple[TradeRecord, ...]:
        return self.cell.trades


@dataclass(slots=True)
class _MutableCounters:
    entered: int = 0
    overlapping: int = 0
    expired: int = 0
    insufficient_terminal_horizon: int = 0
    no_liquidity: int = 0
    partial_entries: int = 0
    exit_fill_attempts: int = 0
    partial_exits: int = 0
    zero_fill_exits: int = 0
    funding_settlements_expected: int = 0
    funding_settlements_observed: int = 0


def _normalized_identifier(name: str, value: str) -> None:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError(f"{name} must be a non-empty normalized string")


def _validated_candles(candles_1m: list[Candle]) -> list[Candle]:
    if not isinstance(candles_1m, list) or not candles_1m:
        raise ValueError("candles_1m must be a non-empty list")
    if any(not isinstance(candle, Candle) for candle in candles_1m):
        raise TypeError("candles_1m must contain Candle values")
    ordered = canonical_candles(candles_1m, expected_timeframe="1m")
    if ordered != candles_1m:
        raise ValueError("candles_1m must already be chronologically sorted")
    first_open = ordered[0].open_time_ms
    if first_open % _DAY_MS:
        raise ValueError("managed evaluation must start at an aligned UTC day boundary")
    if len(ordered) % _MINUTES_PER_DAY:
        raise ValueError("managed evaluation requires complete UTC days")
    if len(ordered) < 2 * _MINUTES_PER_DAY:
        raise ValueError("managed evaluation requires at least two complete UTC days")
    for index, candle in enumerate(ordered):
        expected_open = first_open + index * _MINUTE_MS
        if candle.open_time_ms != expected_open or candle.close_time_ms != expected_open + _MINUTE_MS - 1:
            raise ValueError("managed evaluation candles must be contiguous aligned one-minute bars")
    if (ordered[-1].close_time_ms + 1) % _DAY_MS:
        raise ValueError("managed evaluation must end at a complete UTC day boundary")
    return ordered


def _intent_sort_key(intent: SleeveIntent) -> tuple[int, str]:
    return intent.entry_eligible_ts_ms, intent.intent_id


def _validated_intents(
    intents: list[SleeveIntent],
    *,
    sleeve_id: str,
    symbol: str,
    candle_close_times: set[int],
) -> list[SleeveIntent]:
    if not isinstance(intents, list):
        raise TypeError("intents must be a list")
    if any(not isinstance(intent, SleeveIntent) for intent in intents):
        raise TypeError("intents must contain SleeveIntent values")
    identities = [intent.intent_id for intent in intents]
    if len(set(identities)) != len(identities):
        raise ValueError("intent identities must be unique")
    if intents != sorted(intents, key=_intent_sort_key):
        raise ValueError("intents must be sorted by entry eligibility and identity")
    for intent in intents:
        timestamps = (
            intent.decision_ts_ms,
            intent.entry_eligible_ts_ms,
            intent.entry_expires_ts_ms,
            intent.exit_plan.max_holding_ms,
        )
        if any(isinstance(value, bool) or not isinstance(value, int) for value in timestamps):
            raise TypeError("intent eligibility, expiry and holding horizon must be typed integers")
        if intent.sleeve_id != sleeve_id or intent.symbol != symbol:
            raise ValueError("every intent must belong to the evaluated sleeve and symbol")
        if intent.decision_ts_ms not in candle_close_times:
            raise ValueError("every intent decision must match a fully closed input candle")
        if intent.exit_plan.max_holding_ms % _MINUTE_MS:
            raise ValueError("intent holding horizons must align with one-minute execution candles")
    return intents


def _entry_price_bounds(
    candle: Candle,
    intent: SleeveIntent,
    execution: ExecutionConfig,
) -> tuple[float, float]:
    """Return the complete price range the configured entry simulator can emit."""

    return _entry_price_bounds_for_open(candle.open, intent, execution)


def _entry_price_bounds_for_open(
    candle_open: float,
    intent: SleeveIntent,
    execution: ExecutionConfig,
) -> tuple[float, float]:
    """Return the simulator's complete entry range from its sealed open."""

    base_bps = execution.spread_bps / 2 + execution.slippage_bps
    minimum_cost_bps = max(0.0, base_bps - execution.slippage_jitter_bps)
    maximum_cost_bps = base_bps + execution.slippage_jitter_bps
    if intent.side is Side.LONG:
        lower = candle_open * (1 + minimum_cost_bps / 10_000)
        upper = candle_open * (1 + maximum_cost_bps / 10_000)
    else:
        lower = candle_open * (1 - maximum_cost_bps / 10_000)
        upper = candle_open * (1 - minimum_cost_bps / 10_000)
    if not all(math.isfinite(price) and price > 0 for price in (lower, upper)):
        raise ValueError("execution assumptions imply a non-positive entry price range")
    return lower, upper


def _entry_is_inside_plan(intent: SleeveIntent, price: float) -> bool:
    plan = intent.exit_plan
    if intent.side is Side.LONG:
        return plan.stop_price < price < plan.target_price
    return plan.target_price < price < plan.stop_price


def _admission_disposition(reason: AdmissionReason) -> IntentDispositionReason:
    if reason is AdmissionReason.ACCEPTED:
        raise ValueError("accepted admission has no rejection disposition")
    return IntentDispositionReason(reason.value)


def _validate_execution_cost_coverage(
    execution: ExecutionConfig,
    costs: AllInCostModel,
) -> None:
    required_slippage = execution.slippage_bps + execution.slippage_jitter_bps
    tolerance = 1e-12
    if (
        costs.fee_bps_per_side + tolerance < execution.fee_bps
        or costs.spread_bps + tolerance < execution.spread_bps
        or costs.slippage_bps_per_side + tolerance < required_slippage
    ):
        raise ValueError("admission cost model must dominate configured execution costs")


def _required_funding_bps(
    intent: SleeveIntent,
    entry_timestamp_ms: int,
    execution: ExecutionConfig,
    policy: ManagedEvaluationPolicy,
) -> float:
    funding = execution.funding
    if not funding.available:
        return 0.0
    deadline = entry_timestamp_ms + intent.exit_plan.max_holding_ms + policy.terminal_liquidation_grace_ms
    first = (entry_timestamp_ms // funding.settlement_interval_ms + 1) * (funding.settlement_interval_ms)
    settlement_timestamps = tuple(range(first, deadline + 1, funding.settlement_interval_ms))
    if funding.evidence == "assumed":
        rate = funding.rate_8h_bps
        if rate is None:
            raise RuntimeError("validated assumed funding lost its configured rate")
        return len(settlement_timestamps) * rate / 8
    observations = {
        observation.timestamp_ms: observation.rate_8h_bps for observation in funding.historical_rates
    }
    if any(timestamp not in observations for timestamp in settlement_timestamps):
        raise ValueError("historical funding is incomplete through the bounded liquidation horizon")
    direction = 1.0 if intent.side is Side.LONG else -1.0
    return sum(max(0.0, direction * observations[timestamp] / 8) for timestamp in settlement_timestamps)


def _order_side(side: Side, *, exiting: bool) -> OrderSide:
    if exiting:
        return OrderSide.SELL if side is Side.LONG else OrderSide.BUY
    return OrderSide.BUY if side is Side.LONG else OrderSide.SELL


def _capacity_as_volume(capacity: float, execution: ExecutionConfig) -> float:
    return capacity / execution.max_volume_participation


def _signed_quantity(position: ManagedPosition | None) -> float:
    if position is None:
        return 0.0
    direction = 1.0 if position.state.intent.side is Side.LONG else -1.0
    return direction * position.state.remaining_quantity


def evaluate_sleeve_cell(
    candles_1m: list[Candle],
    intents: list[SleeveIntent],
    *,
    cell_id: str,
    sleeve_id: str,
    symbol: str,
    initial_equity_usd: float,
    execution: ExecutionConfig,
    costs: AllInCostModel,
    limits: RiskLimits,
    policy: ManagedEvaluationPolicy | None = None,
    seed: int = 0,
) -> ManagedCellResult:
    """Evaluate one sleeve-symbol cell with causal volume and managed barriers.

    Intents are IOC at the first one-minute open at or after both their causal
    eligibility and configured latency.  Entry fills use only the preceding
    fully closed candle's volume.  A cell may own at most one position, and a
    position must be fully closed before the terminal UTC-day close.
    """

    for name, value in (("cell_id", cell_id), ("sleeve_id", sleeve_id), ("symbol", symbol)):
        _normalized_identifier(name, value)
    if (
        isinstance(initial_equity_usd, bool)
        or not isinstance(initial_equity_usd, (int, float))
        or not math.isfinite(initial_equity_usd)
        or initial_equity_usd <= 0
    ):
        raise ValueError("initial_equity_usd must be finite and positive")
    if not isinstance(execution, ExecutionConfig):
        raise TypeError("execution must be an ExecutionConfig")
    if not isinstance(costs, AllInCostModel):
        raise TypeError("costs must be an AllInCostModel")
    if not isinstance(limits, RiskLimits):
        raise TypeError("limits must be RiskLimits")
    effective_policy = (
        ManagedEvaluationPolicy(application_exit_latency_ms=execution.latency_ms)
        if policy is None
        else policy
    )
    if not isinstance(effective_policy, ManagedEvaluationPolicy):
        raise TypeError("policy must be a ManagedEvaluationPolicy")
    if isinstance(seed, bool) or not isinstance(seed, int):
        raise TypeError("seed must be an integer")
    if execution.funding.settlement_interval_ms % _MINUTE_MS:
        raise ValueError("funding settlement cadence must align with one-minute candles")
    _validate_execution_cost_coverage(execution, costs)
    assumptions = ManagedEvaluationAssumptions(
        execution=execution,
        costs=costs,
        limits=limits,
        policy=effective_policy,
        seed=seed,
    )

    candles = _validated_candles(candles_1m)
    if candles[0].symbol != symbol:
        raise ValueError("candle symbol must match the evaluated cell symbol")
    candidates = _validated_intents(
        intents,
        sleeve_id=sleeve_id,
        symbol=symbol,
        candle_close_times={candle.close_time_ms for candle in candles},
    )
    open_times = [candle.open_time_ms for candle in candles]
    scheduled: dict[int, list[SleeveIntent]] = {}
    dispositions: list[IntentDisposition] = []
    fill_events: list[ManagedFillEvent] = []
    funding_events: list[ManagedFundingEvent] = []
    counters = _MutableCounters()
    last_open = candles[-1].open_time_ms
    for intent in candidates:
        causal_timestamp = max(
            intent.entry_eligible_ts_ms,
            intent.decision_ts_ms + execution.latency_ms,
        )
        index = bisect_left(open_times, causal_timestamp)
        if index >= len(candles):
            reason = (
                IntentDispositionReason.EXPIRED
                if causal_timestamp > intent.entry_expires_ts_ms
                else IntentDispositionReason.INSUFFICIENT_TERMINAL_HORIZON
            )
            dispositions.append(
                IntentDisposition(
                    intent_id=intent.intent_id,
                    intent=intent,
                    reason=reason,
                    scheduled_entry_timestamp_ms=None,
                )
            )
            if reason is IntentDispositionReason.EXPIRED:
                counters.expired += 1
            else:
                counters.insufficient_terminal_horizon += 1
            continue
        entry_timestamp = candles[index].open_time_ms
        if (
            entry_timestamp <= intent.entry_expires_ts_ms
            and entry_timestamp
            + intent.exit_plan.max_holding_ms
            + effective_policy.terminal_liquidation_grace_ms
            <= last_open
        ):
            required_funding_bps = _required_funding_bps(
                intent,
                entry_timestamp,
                execution,
                effective_policy,
            )
            if costs.adverse_funding_bps + 1e-12 < required_funding_bps:
                raise ValueError(
                    "admission cost model must cover adverse funding through the holding horizon"
                )
        scheduled.setdefault(index, []).append(intent)

    simulator = FillSimulator(execution, seed=seed)
    position: ManagedPosition | None = None
    application_exit_ready_index: int | None = None
    application_exit_decision_timestamp_ms: int | None = None
    application_exit_eligible_timestamp_ms: int | None = None
    liquidation_deadline_ms: int | None = None
    trades: list[TradeRecord] = []
    cash = float(initial_equity_usd)
    fees = carry = shortfall = turnover = 0.0
    daily_snapshots: list[DailyCellSnapshot] = []
    interval = execution.funding.settlement_interval_ms
    next_funding = (candles[0].open_time_ms // interval + 1) * interval

    def marked_equity(mark_price: float) -> float:
        equity = cash + _signed_quantity(position) * mark_price
        if not math.isfinite(equity) or equity <= 0:
            raise ValueError("managed cell equity became non-positive or non-finite")
        return equity

    def settle_funding(open_timestamp_ms: int, mark_price: float) -> float:
        nonlocal cash, carry, next_funding
        candle_carry = 0.0
        while next_funding <= open_timestamp_ms:
            signed_quantity = _signed_quantity(position)
            raw_cost, observed = execution.funding.settlement_cost(
                signed_quantity * mark_price,
                next_funding,
            )
            if not math.isfinite(raw_cost):
                raise ValueError("funding settlement produced a non-finite cost")
            cost = max(0.0, raw_cost)
            cash -= cost
            carry += cost
            candle_carry += cost
            counters.funding_settlements_expected += 1
            counters.funding_settlements_observed += int(observed)
            if signed_quantity != 0:
                if position is None:
                    raise RuntimeError("non-zero funding quantity lost its managed position")
                funding_events.append(
                    ManagedFundingEvent(
                        intent_id=position.state.intent.intent_id,
                        timestamp_ms=next_funding,
                        signed_quantity=signed_quantity,
                        mark_price=mark_price,
                        charged_cost_usd=cost,
                        observed=observed,
                    )
                )
            next_funding += interval
        return candle_carry

    def schedule_application_exit(
        decision_timestamp_ms: int,
        *,
        minimum_index: int,
    ) -> tuple[int, int]:
        eligible_timestamp_ms = decision_timestamp_ms + effective_policy.application_exit_latency_ms
        ready_index = max(bisect_left(open_times, eligible_timestamp_ms), minimum_index)
        if (
            ready_index >= len(candles)
            or liquidation_deadline_ms is None
            or open_times[ready_index] > liquidation_deadline_ms
        ):
            raise ValueError("application-managed exit cannot fill within its bounded liquidation horizon")
        return ready_index, eligible_timestamp_ms

    def execute_exit(
        active: ManagedPosition,
        decision: BarrierExitDecision,
        candle: Candle,
        remaining_capacity: float,
        *,
        execution_origin: ManagedExecutionOrigin,
        order_decision_timestamp_ms: int,
        execution_eligible_timestamp_ms: int,
    ) -> tuple[ManagedPosition | None, float, int]:
        nonlocal cash, fees, shortfall, turnover
        state = active.state
        side = _order_side(state.intent.side, exiting=True)
        fill = simulator.fill(
            candle,
            side,
            decision.requested_quantity,
            available_volume=_capacity_as_volume(remaining_capacity, execution),
            timestamp_ms=decision.decision_timestamp_ms,
            reference_price=decision.reference_price,
        )
        counters.exit_fill_attempts += 1
        if not math.isfinite(fill.price) or fill.price <= 0:
            raise ValueError("simulated exit price must be finite and positive")
        capacity_after = max(0.0, remaining_capacity - fill.filled_quantity)
        fill_events.append(
            ManagedFillEvent(
                intent_id=state.intent.intent_id,
                phase=ManagedFillPhase.EXIT,
                execution_origin=execution_origin,
                execution_draw_index=len(fill_events),
                exit_reason=decision.exit_reason,
                exit_decision=decision,
                order_decision_timestamp_ms=order_decision_timestamp_ms,
                execution_eligible_timestamp_ms=execution_eligible_timestamp_ms,
                execution_candle_open_time_ms=candle.open_time_ms,
                fill_timestamp_ms=fill.timestamp_ms,
                requested_quantity=fill.requested_quantity,
                filled_quantity=fill.filled_quantity,
                price=fill.price,
                reference_price=fill.reference_price,
                fee_usd=fill.fee_usd,
                implementation_shortfall_usd=fill.implementation_shortfall_usd,
                causal_volume=causal_volume,
                capacity_before=remaining_capacity,
                capacity_after=capacity_after,
            )
        )
        if fill.filled_quantity == 0:
            counters.zero_fill_exits += 1
            return active, remaining_capacity, fill.timestamp_ms
        if fill.filled_quantity < fill.requested_quantity:
            counters.partial_exits += 1
        signed = fill.filled_quantity if side is OrderSide.BUY else -fill.filled_quantity
        cash -= signed * fill.price + fill.fee_usd
        fees += fill.fee_usd
        shortfall += fill.implementation_shortfall_usd
        turnover += fill.filled_quantity * fill.price
        result = active.apply_exit_fill(
            decision,
            fill_timestamp_ms=fill.timestamp_ms,
            fill_price=fill.price,
            filled_quantity=fill.filled_quantity,
            fee_usd=fill.fee_usd,
            implementation_shortfall_usd=fill.implementation_shortfall_usd,
        )
        capacity = capacity_after
        if result.trade_record is None:
            return active, capacity, fill.timestamp_ms
        trades.append(result.trade_record)
        return None, capacity, fill.timestamp_ms

    for index, candle in enumerate(candles):
        open_carry = settle_funding(candle.open_time_ms, candle.open)
        marked_equity(candle.open)
        if (
            position is not None
            and liquidation_deadline_ms is not None
            and candle.open_time_ms > liquidation_deadline_ms
        ):
            raise ValueError("managed position exceeded its bounded liquidation horizon")
        causal_volume = candles[index - 1].volume if index else 0.0
        remaining_capacity = causal_volume * execution.max_volume_participation
        entered_at_this_open = False

        # Consume the active position first, but distinguish decisions available
        # at the open from decisions learned only after this candle completes.
        close_decision: BarrierExitDecision | None = None
        if position is not None:
            was_pending = position.state.exit_pending
            decision = position.on_candle(candle, carry_cost_usd=open_carry)
            if decision is not None:
                application_managed = was_pending or decision.exit_reason is ExitReason.TIMEOUT
                if application_managed:
                    if application_exit_ready_index is None:
                        application_exit_decision_timestamp_ms = decision.decision_timestamp_ms
                        (
                            application_exit_ready_index,
                            application_exit_eligible_timestamp_ms,
                        ) = schedule_application_exit(
                            application_exit_decision_timestamp_ms,
                            minimum_index=index,
                        )
                    if index >= application_exit_ready_index:
                        if (
                            application_exit_decision_timestamp_ms is None
                            or application_exit_eligible_timestamp_ms is None
                        ):
                            raise RuntimeError("application exit lost its causal scheduling evidence")
                        position, remaining_capacity, attempt_timestamp_ms = execute_exit(
                            position,
                            decision,
                            candle,
                            remaining_capacity,
                            execution_origin=ManagedExecutionOrigin.APPLICATION_EXIT,
                            order_decision_timestamp_ms=application_exit_decision_timestamp_ms,
                            execution_eligible_timestamp_ms=application_exit_eligible_timestamp_ms,
                        )
                        application_exit_ready_index = None
                        application_exit_decision_timestamp_ms = None
                        application_exit_eligible_timestamp_ms = None
                        if position is None:
                            liquidation_deadline_ms = None
                        else:
                            application_exit_decision_timestamp_ms = attempt_timestamp_ms
                            (
                                application_exit_ready_index,
                                application_exit_eligible_timestamp_ms,
                            ) = schedule_application_exit(
                                attempt_timestamp_ms,
                                minimum_index=index + 1,
                            )
                elif decision.decision_timestamp_ms == candle.open_time_ms:
                    position, remaining_capacity, attempt_timestamp_ms = execute_exit(
                        position,
                        decision,
                        candle,
                        remaining_capacity,
                        execution_origin=ManagedExecutionOrigin.RESTING_BARRIER,
                        order_decision_timestamp_ms=decision.decision_timestamp_ms,
                        execution_eligible_timestamp_ms=decision.decision_timestamp_ms,
                    )
                    if position is None:
                        liquidation_deadline_ms = None
                        application_exit_ready_index = None
                        application_exit_decision_timestamp_ms = None
                        application_exit_eligible_timestamp_ms = None
                    else:
                        application_exit_decision_timestamp_ms = attempt_timestamp_ms
                        (
                            application_exit_ready_index,
                            application_exit_eligible_timestamp_ms,
                        ) = schedule_application_exit(
                            attempt_timestamp_ms,
                            minimum_index=index + 1,
                        )
                else:
                    close_decision = decision

        for intent in scheduled.get(index, []):
            entry_timestamp = candle.open_time_ms
            price_bounds = _entry_price_bounds(candle, intent, execution)
            entry_evidence: dict[str, object] = {
                "intent_id": intent.intent_id,
                "intent": intent,
                "scheduled_entry_timestamp_ms": entry_timestamp,
                "entry_reference_price": candle.open,
                "entry_price_bounds": price_bounds,
                "causal_volume": causal_volume,
                "capacity_before_entry": remaining_capacity,
            }
            if entry_timestamp > intent.entry_expires_ts_ms:
                dispositions.append(
                    IntentDisposition(
                        **entry_evidence,  # type: ignore[arg-type]
                        reason=IntentDispositionReason.EXPIRED,
                    )
                )
                counters.expired += 1
                continue
            if (
                entry_timestamp
                + intent.exit_plan.max_holding_ms
                + effective_policy.terminal_liquidation_grace_ms
                > last_open
            ):
                dispositions.append(
                    IntentDisposition(
                        **entry_evidence,  # type: ignore[arg-type]
                        reason=IntentDispositionReason.INSUFFICIENT_TERMINAL_HORIZON,
                    )
                )
                counters.insufficient_terminal_horizon += 1
                continue
            if position is not None:
                dispositions.append(
                    IntentDisposition(
                        **entry_evidence,  # type: ignore[arg-type]
                        reason=IntentDispositionReason.OVERLAPPING_POSITION,
                    )
                )
                counters.overlapping += 1
                continue

            if any(not _entry_is_inside_plan(intent, price) for price in price_bounds):
                dispositions.append(
                    IntentDisposition(
                        **entry_evidence,  # type: ignore[arg-type]
                        reason=IntentDispositionReason.ENTRY_OUTSIDE_EXIT_PLAN,
                    )
                )
                continue
            plan = intent.exit_plan
            equity = marked_equity(candle.open)
            admissions = tuple(
                size_and_admit(
                    side=intent.side,
                    entry_price=price,
                    stop_price=plan.stop_price,
                    target_price=plan.target_price,
                    equity_usd=equity,
                    costs=costs,
                    limits=limits,
                )
                for price in price_bounds
            )
            rejected_admission = next(
                (admission for admission in admissions if not admission.accepted),
                None,
            )
            if rejected_admission is not None:
                dispositions.append(
                    IntentDisposition(
                        **entry_evidence,  # type: ignore[arg-type]
                        reason=_admission_disposition(rejected_admission.reason),
                        admission_equity_usd=equity,
                    )
                )
                continue
            if remaining_capacity == 0:
                dispositions.append(
                    IntentDisposition(
                        **entry_evidence,  # type: ignore[arg-type]
                        reason=IntentDispositionReason.NO_LIQUIDITY,
                        admission_equity_usd=equity,
                    )
                )
                counters.no_liquidity += 1
                continue

            requested_quantity = min(admission.quantity for admission in admissions)
            side = _order_side(intent.side, exiting=False)
            fill = simulator.fill(
                candle,
                side,
                requested_quantity,
                available_volume=_capacity_as_volume(remaining_capacity, execution),
                timestamp_ms=entry_timestamp,
                reference_price=candle.open,
            )
            if not math.isfinite(fill.price) or fill.price <= 0:
                raise ValueError("simulated entry price must be finite and positive")
            if fill.filled_quantity == 0:
                raise RuntimeError("positive causal entry capacity produced a zero fill")
            lower_bound, upper_bound = price_bounds
            bound_tolerance = max(1e-12, candle.open * 1e-12)
            if not (
                lower_bound - bound_tolerance <= fill.price <= upper_bound + bound_tolerance
                and _entry_is_inside_plan(intent, fill.price)
            ):
                raise ValueError("simulated entry escaped its pre-admitted price range")
            signed = fill.filled_quantity if side is OrderSide.BUY else -fill.filled_quantity
            cash -= signed * fill.price + fill.fee_usd
            fees += fill.fee_usd
            shortfall += fill.implementation_shortfall_usd
            turnover += fill.filled_quantity * fill.price
            capacity_after = max(0.0, remaining_capacity - fill.filled_quantity)
            fill_events.append(
                ManagedFillEvent(
                    intent_id=intent.intent_id,
                    phase=ManagedFillPhase.ENTRY,
                    execution_origin=ManagedExecutionOrigin.ENTRY_SIGNAL,
                    execution_draw_index=len(fill_events),
                    exit_reason=None,
                    exit_decision=None,
                    order_decision_timestamp_ms=intent.decision_ts_ms,
                    execution_eligible_timestamp_ms=max(
                        intent.entry_eligible_ts_ms,
                        intent.decision_ts_ms + execution.latency_ms,
                    ),
                    execution_candle_open_time_ms=candle.open_time_ms,
                    fill_timestamp_ms=fill.timestamp_ms,
                    requested_quantity=fill.requested_quantity,
                    filled_quantity=fill.filled_quantity,
                    price=fill.price,
                    reference_price=fill.reference_price,
                    fee_usd=fill.fee_usd,
                    implementation_shortfall_usd=fill.implementation_shortfall_usd,
                    causal_volume=causal_volume,
                    capacity_before=remaining_capacity,
                    capacity_after=capacity_after,
                )
            )
            remaining_capacity = capacity_after
            position = ManagedPosition(
                intent,
                entry_timestamp_ms=fill.timestamp_ms,
                entry_price=fill.price,
                quantity=fill.filled_quantity,
                entry_fee_usd=fill.fee_usd,
                entry_implementation_shortfall_usd=fill.implementation_shortfall_usd,
            )
            liquidation_deadline_ms = (
                entry_timestamp
                + intent.exit_plan.max_holding_ms
                + effective_policy.terminal_liquidation_grace_ms
            )
            application_exit_ready_index = None
            application_exit_decision_timestamp_ms = None
            application_exit_eligible_timestamp_ms = None
            counters.entered += 1
            if fill.filled_quantity < fill.requested_quantity:
                counters.partial_entries += 1
            dispositions.append(
                IntentDisposition(
                    **entry_evidence,  # type: ignore[arg-type]
                    reason=IntentDispositionReason.ENTERED,
                    admission_equity_usd=equity,
                    requested_quantity=fill.requested_quantity,
                    filled_quantity=fill.filled_quantity,
                )
            )
            entered_at_this_open = True

        # All candidates scheduled at this open observe the same point-in-time
        # ownership.  Only after their dispositions are frozen may the newly
        # entered position consume this candle's intrabar range.
        if entered_at_this_open:
            if position is None:
                raise RuntimeError("newly entered position disappeared before its entry candle")
            same_candle_decision = position.on_candle(candle)
            if same_candle_decision is not None:
                position, remaining_capacity, attempt_timestamp_ms = execute_exit(
                    position,
                    same_candle_decision,
                    candle,
                    remaining_capacity,
                    execution_origin=ManagedExecutionOrigin.RESTING_BARRIER,
                    order_decision_timestamp_ms=same_candle_decision.decision_timestamp_ms,
                    execution_eligible_timestamp_ms=same_candle_decision.decision_timestamp_ms,
                )
                if position is None:
                    liquidation_deadline_ms = None
                    application_exit_ready_index = None
                    application_exit_decision_timestamp_ms = None
                    application_exit_eligible_timestamp_ms = None
                else:
                    application_exit_decision_timestamp_ms = attempt_timestamp_ms
                    (
                        application_exit_ready_index,
                        application_exit_eligible_timestamp_ms,
                    ) = schedule_application_exit(
                        attempt_timestamp_ms,
                        minimum_index=index + 1,
                    )

        if close_decision is not None:
            if position is None:
                raise RuntimeError("active close decision lost its managed position")
            position, remaining_capacity, attempt_timestamp_ms = execute_exit(
                position,
                close_decision,
                candle,
                remaining_capacity,
                execution_origin=ManagedExecutionOrigin.RESTING_BARRIER,
                order_decision_timestamp_ms=close_decision.decision_timestamp_ms,
                execution_eligible_timestamp_ms=close_decision.decision_timestamp_ms,
            )
            if position is None:
                liquidation_deadline_ms = None
                application_exit_ready_index = None
                application_exit_decision_timestamp_ms = None
                application_exit_eligible_timestamp_ms = None
            else:
                application_exit_decision_timestamp_ms = attempt_timestamp_ms
                (
                    application_exit_ready_index,
                    application_exit_eligible_timestamp_ms,
                ) = schedule_application_exit(
                    attempt_timestamp_ms,
                    minimum_index=index + 1,
                )

        equity = marked_equity(candle.close)
        if (candle.close_time_ms + 1) % _DAY_MS == 0:
            state = position.state if position is not None else None
            daily_snapshots.append(
                DailyCellSnapshot(
                    day=datetime.fromtimestamp(candle.open_time_ms / 1_000, tz=UTC).date(),
                    closing_equity_usd=equity,
                    cumulative_realized_pnl_usd=sum(trade.net_pnl_usd for trade in trades),
                    mark_price=candle.close if state is not None else None,
                    open_position=state,
                )
            )

    if position is not None or _signed_quantity(position):
        raise ValueError("terminal managed cell position must be fully flat")
    final_equity = marked_equity(candles[-1].close)
    if not math.isclose(
        final_equity,
        daily_snapshots[-1].closing_equity_usd,
        rel_tol=1e-12,
        abs_tol=1e-9,
    ):
        raise RuntimeError("terminal daily mark and managed cash are inconsistent")

    disposition_by_id = {disposition.intent_id: disposition for disposition in dispositions}
    if len(disposition_by_id) != len(candidates):
        raise RuntimeError("managed replay did not disposition every intent exactly once")
    ordered_dispositions = tuple(disposition_by_id[intent.intent_id] for intent in candidates)
    reason_counts = {reason: 0 for reason in IntentDispositionReason}
    for disposition in ordered_dispositions:
        reason_counts[disposition.reason] += 1
    entered = reason_counts[IntentDispositionReason.ENTERED]
    rejected = len(candidates) - entered
    immutable_counters = ManagedCellCounters(
        intents=len(candidates),
        entered=entered,
        rejected=rejected,
        overlapping=reason_counts[IntentDispositionReason.OVERLAPPING_POSITION],
        expired=reason_counts[IntentDispositionReason.EXPIRED],
        insufficient_terminal_horizon=reason_counts[IntentDispositionReason.INSUFFICIENT_TERMINAL_HORIZON],
        no_liquidity=reason_counts[IntentDispositionReason.NO_LIQUIDITY],
        partial_entries=counters.partial_entries,
        exit_fill_attempts=counters.exit_fill_attempts,
        partial_exits=counters.partial_exits,
        zero_fill_exits=counters.zero_fill_exits,
        funding_settlements_expected=counters.funding_settlements_expected,
        funding_settlements_observed=counters.funding_settlements_observed,
    )
    cell = CellEquityCurve(
        cell_id=cell_id,
        sleeve_id=sleeve_id,
        symbol=symbol,
        snapshots=tuple(daily_snapshots),
        initial_equity_usd=float(initial_equity_usd),
        trades=tuple(trades),
    )
    immutable_fills = tuple(fill_events)
    immutable_funding_events = tuple(funding_events)
    replay_evidence_sha256 = _replay_evidence_sha256(
        cell=cell,
        dispositions=ordered_dispositions,
        fills=immutable_fills,
        funding_events=immutable_funding_events,
        assumptions_sha256=assumptions.assumptions_sha256,
        counters=immutable_counters,
        fees_usd=fees,
        carry_cost_usd=carry,
        implementation_shortfall_usd=shortfall,
        turnover_usd=turnover,
    )
    return ManagedCellResult(
        cell=cell,
        dispositions=ordered_dispositions,
        fills=immutable_fills,
        funding_events=immutable_funding_events,
        assumptions=assumptions,
        assumptions_sha256=assumptions.assumptions_sha256,
        replay_evidence_sha256=replay_evidence_sha256,
        counters=immutable_counters,
        fees_usd=fees,
        carry_cost_usd=carry,
        implementation_shortfall_usd=shortfall,
        turnover_usd=turnover,
    )
