"""Causal OHLC barrier handling for one managed sleeve position."""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass, field, replace

from kairos_core.enums import Side
from kairos_quant.candles import Candle

from .strategy_models import ExitReason, SleeveIntent, TradeRecord


def _positive_finite(name: str, value: float) -> None:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or value <= 0
    ):
        raise ValueError(f"{name} must be finite and positive")


def _non_negative_finite(name: str, value: float) -> None:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or value < 0
    ):
        raise ValueError(f"{name} must be finite and non-negative")


def _timestamp(name: str, value: int) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{name} must be a non-negative integer")


@dataclass(frozen=True, slots=True)
class BarrierExitDecision:
    """One fill opportunity created by a barrier or a pending-exit retry."""

    intent_id: str
    decision_sequence: int
    decision_timestamp_ms: int
    valid_until_timestamp_ms: int
    reference_price: float
    requested_quantity: float
    exit_reason: ExitReason
    ambiguous_intrabar: bool
    decision_id: str = field(init=False)

    def __post_init__(self) -> None:
        if (
            len(self.intent_id) != 64
            or self.intent_id != self.intent_id.lower()
            or any(character not in "0123456789abcdef" for character in self.intent_id)
        ):
            raise ValueError("intent_id must be a lowercase SHA-256")
        if (
            isinstance(self.decision_sequence, bool)
            or not isinstance(self.decision_sequence, int)
            or self.decision_sequence <= 0
        ):
            raise ValueError("decision_sequence must be a positive integer")
        _timestamp("decision_timestamp_ms", self.decision_timestamp_ms)
        _timestamp("valid_until_timestamp_ms", self.valid_until_timestamp_ms)
        if self.valid_until_timestamp_ms < self.decision_timestamp_ms:
            raise ValueError("exit decision validity cannot end before its timestamp")
        _positive_finite("reference_price", self.reference_price)
        _positive_finite("requested_quantity", self.requested_quantity)
        if not isinstance(self.exit_reason, ExitReason):
            raise ValueError("exit_reason must be an ExitReason")
        if not isinstance(self.ambiguous_intrabar, bool):
            raise ValueError("ambiguous_intrabar must be boolean")
        object.__setattr__(self, "reference_price", float(self.reference_price))
        object.__setattr__(self, "requested_quantity", float(self.requested_quantity))
        payload = {
            "ambiguous_intrabar": self.ambiguous_intrabar,
            "decision_sequence": self.decision_sequence,
            "decision_timestamp_ms": self.decision_timestamp_ms,
            "exit_reason": self.exit_reason.value,
            "intent_id": self.intent_id,
            "reference_price": self.reference_price.hex(),
            "requested_quantity": self.requested_quantity.hex(),
            "schema": "kairos.barrier-exit-decision.v1",
            "valid_until_timestamp_ms": self.valid_until_timestamp_ms,
        }
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        object.__setattr__(self, "decision_id", hashlib.sha256(encoded).hexdigest())


@dataclass(frozen=True, slots=True)
class BarrierFillResult:
    """Validated full or partial execution of one exit decision."""

    decision: BarrierExitDecision
    fill_timestamp_ms: int
    fill_price: float
    filled_quantity: float
    fee_usd: float
    implementation_shortfall_usd: float
    remaining_quantity: float
    fully_closed: bool
    trade_record: TradeRecord | None


@dataclass(frozen=True, slots=True)
class ManagedPositionState:
    """Read-only snapshot of all information known after the latest event."""

    intent: SleeveIntent
    entry_timestamp_ms: int
    entry_price: float
    initial_quantity: float
    remaining_quantity: float
    entry_fee_usd: float
    entry_implementation_shortfall_usd: float
    accumulated_carry_cost_usd: float
    active_stop_price: float
    active_stop_reason: ExitReason
    trailing_activated: bool
    maximum_adverse_excursion_usd: float
    maximum_favorable_excursion_usd: float
    last_bar_open_time_ms: int | None
    last_bar_close_time_ms: int | None
    timeframe_duration_ms: int | None
    timeframe: str | None
    exit_pending: bool
    pending_exit_reason: ExitReason | None
    pending_ambiguous_intrabar: bool
    active_decision: BarrierExitDecision | None
    decision_sequence: int
    exit_filled_quantity: float
    exit_notional_usd: float
    exit_fee_usd: float
    exit_implementation_shortfall_usd: float
    closed: bool


class ManagedPosition:
    """Stateful, single-position barrier engine.

    The first candle must open exactly at the entry timestamp, which prevents
    pre-entry OHLC extremes from leaking into the result.  :meth:`on_candle`
    creates an exit decision but never guesses execution costs.  The caller
    finalizes an actual (possibly partial) fill with :meth:`apply_exit_fill`.
    Once an exit is pending, subsequent candles only create retry decisions;
    barriers and trailing updates can no longer reverse the exit request.
    """

    __slots__ = ("_state",)

    def __init__(
        self,
        intent: SleeveIntent,
        *,
        entry_timestamp_ms: int,
        entry_price: float,
        quantity: float,
        entry_fee_usd: float = 0.0,
        entry_implementation_shortfall_usd: float = 0.0,
    ) -> None:
        if not isinstance(intent, SleeveIntent):
            raise ValueError("intent must be a SleeveIntent")
        _timestamp("entry_timestamp_ms", entry_timestamp_ms)
        if entry_timestamp_ms < intent.entry_eligible_ts_ms:
            raise ValueError("entry_timestamp_ms predates intent eligibility")
        if entry_timestamp_ms > intent.entry_expires_ts_ms:
            raise ValueError("entry_timestamp_ms is later than intent expiry")
        _positive_finite("entry_price", entry_price)
        _positive_finite("quantity", quantity)
        _non_negative_finite("entry_fee_usd", entry_fee_usd)
        _non_negative_finite(
            "entry_implementation_shortfall_usd",
            entry_implementation_shortfall_usd,
        )
        plan = intent.exit_plan
        if intent.side is Side.LONG and not plan.stop_price < entry_price < plan.target_price:
            raise ValueError("long entry price must remain between its stop and target")
        if intent.side is Side.SHORT and not plan.target_price < entry_price < plan.stop_price:
            raise ValueError("short entry price must remain between its target and stop")
        self._state = ManagedPositionState(
            intent=intent,
            entry_timestamp_ms=entry_timestamp_ms,
            entry_price=entry_price,
            initial_quantity=quantity,
            remaining_quantity=quantity,
            entry_fee_usd=entry_fee_usd,
            entry_implementation_shortfall_usd=entry_implementation_shortfall_usd,
            accumulated_carry_cost_usd=0.0,
            active_stop_price=plan.stop_price,
            active_stop_reason=ExitReason.STOP_LOSS,
            trailing_activated=False,
            maximum_adverse_excursion_usd=0.0,
            maximum_favorable_excursion_usd=0.0,
            last_bar_open_time_ms=None,
            last_bar_close_time_ms=None,
            timeframe_duration_ms=None,
            timeframe=None,
            exit_pending=False,
            pending_exit_reason=None,
            pending_ambiguous_intrabar=False,
            active_decision=None,
            decision_sequence=0,
            exit_filled_quantity=0.0,
            exit_notional_usd=0.0,
            exit_fee_usd=0.0,
            exit_implementation_shortfall_usd=0.0,
            closed=False,
        )

    @property
    def state(self) -> ManagedPositionState:
        return self._state

    def on_candle(
        self,
        candle: Candle,
        *,
        carry_cost_usd: float = 0.0,
    ) -> BarrierExitDecision | None:
        """Consume one completed candle and optionally request an exit fill.

        A stop gap uses the adverse open.  A target gap retains the target price
        and receives no favorable improvement.  A simultaneous unordered
        intrabar stop and target resolves to the stop and is marked ambiguous.
        """
        self._validate_candle(candle)
        _non_negative_finite("carry_cost_usd", carry_cost_usd)
        state = replace(
            self._state,
            accumulated_carry_cost_usd=(self._state.accumulated_carry_cost_usd + carry_cost_usd),
            last_bar_open_time_ms=candle.open_time_ms,
            last_bar_close_time_ms=candle.close_time_ms,
            timeframe_duration_ms=candle.close_time_ms - candle.open_time_ms + 1,
            timeframe=candle.timeframe,
            active_decision=None,
        )

        if state.exit_pending:
            state = self._update_excursions(state, candle.open, candle.open)
            reason = state.pending_exit_reason
            if reason is None:  # defensive guard for corrupted state
                raise RuntimeError("pending exit lost its reason")
            return self._request_exit(
                state,
                candle,
                timestamp_ms=candle.open_time_ms,
                reference_price=candle.open,
                reason=reason,
                ambiguous=state.pending_ambiguous_intrabar,
            )

        if self._stop_crossed(candle.open, state.active_stop_price):
            state = self._update_excursions(state, candle.open, candle.open)
            return self._request_exit(
                state,
                candle,
                timestamp_ms=candle.open_time_ms,
                reference_price=candle.open,
                reason=state.active_stop_reason,
                ambiguous=False,
            )
        if self._target_crossed(candle.open):
            state = self._update_excursions(state, candle.open, candle.open)
            return self._request_exit(
                state,
                candle,
                timestamp_ms=candle.open_time_ms,
                reference_price=state.intent.exit_plan.target_price,
                reason=ExitReason.TAKE_PROFIT,
                ambiguous=False,
            )

        deadline = state.entry_timestamp_ms + state.intent.exit_plan.max_holding_ms
        if candle.open_time_ms >= deadline:
            state = self._update_excursions(state, candle.open, candle.open)
            return self._request_exit(
                state,
                candle,
                timestamp_ms=candle.open_time_ms,
                reference_price=candle.open,
                reason=ExitReason.TIMEOUT,
                ambiguous=False,
            )

        stop_hit = self._bar_hits_stop(candle, state.active_stop_price)
        target_hit = self._bar_hits_target(candle)
        if stop_hit:
            state = self._update_excursions(
                state,
                state.active_stop_price,
                state.active_stop_price,
            )
            return self._request_exit(
                state,
                candle,
                timestamp_ms=candle.close_time_ms,
                reference_price=state.active_stop_price,
                reason=state.active_stop_reason,
                ambiguous=target_hit,
            )
        if target_hit:
            target = state.intent.exit_plan.target_price
            state = self._update_excursions(state, target, target)
            return self._request_exit(
                state,
                candle,
                timestamp_ms=candle.close_time_ms,
                reference_price=target,
                reason=ExitReason.TAKE_PROFIT,
                ambiguous=False,
            )

        state = self._update_excursions(state, candle.low, candle.high)
        self._state = self._update_trailing_at_close(state, candle.close)
        return None

    def apply_exit_fill(
        self,
        decision: BarrierExitDecision,
        *,
        fill_timestamp_ms: int,
        fill_price: float,
        filled_quantity: float,
        fee_usd: float = 0.0,
        implementation_shortfall_usd: float = 0.0,
    ) -> BarrierFillResult:
        """Apply an actual protective fill, retaining residual exit-pending state."""
        state = self._state
        if state.closed or not state.exit_pending:
            raise RuntimeError("managed position has no pending exit")
        if state.active_decision is None or decision != state.active_decision:
            raise ValueError("exit decision is stale or does not belong to this position")
        if decision.intent_id != state.intent.intent_id:
            raise ValueError("exit decision does not belong to this intent")
        _timestamp("fill_timestamp_ms", fill_timestamp_ms)
        if not decision.decision_timestamp_ms <= fill_timestamp_ms <= decision.valid_until_timestamp_ms:
            raise ValueError("exit fill timestamp falls outside decision validity")
        _positive_finite("fill_price", fill_price)
        _positive_finite("filled_quantity", filled_quantity)
        if filled_quantity > state.remaining_quantity:
            raise ValueError("exit fill exceeds remaining position quantity")
        _non_negative_finite("fee_usd", fee_usd)
        _non_negative_finite("implementation_shortfall_usd", implementation_shortfall_usd)
        if state.intent.side is Side.LONG and fill_price > decision.reference_price:
            raise ValueError("long exit fill cannot improve on its decision reference")
        if state.intent.side is Side.SHORT and fill_price < decision.reference_price:
            raise ValueError("short exit fill cannot improve on its decision reference")

        remaining = state.remaining_quantity - filled_quantity
        state = replace(
            state,
            remaining_quantity=remaining,
            exit_filled_quantity=state.exit_filled_quantity + filled_quantity,
            exit_notional_usd=state.exit_notional_usd + filled_quantity * fill_price,
            exit_fee_usd=state.exit_fee_usd + fee_usd,
            exit_implementation_shortfall_usd=(
                state.exit_implementation_shortfall_usd + implementation_shortfall_usd
            ),
            active_decision=None,
        )
        record: TradeRecord | None = None
        if remaining == 0:
            average_exit_price = state.exit_notional_usd / state.exit_filled_quantity
            record = TradeRecord(
                intent=state.intent,
                entry_timestamp_ms=state.entry_timestamp_ms,
                exit_timestamp_ms=fill_timestamp_ms,
                entry_price=state.entry_price,
                exit_price=average_exit_price,
                quantity=state.initial_quantity,
                exit_reason=decision.exit_reason,
                entry_fee_usd=state.entry_fee_usd,
                exit_fee_usd=state.exit_fee_usd,
                carry_cost_usd=state.accumulated_carry_cost_usd,
                implementation_shortfall_usd=(
                    state.entry_implementation_shortfall_usd + state.exit_implementation_shortfall_usd
                ),
                maximum_adverse_excursion_usd=state.maximum_adverse_excursion_usd,
                maximum_favorable_excursion_usd=state.maximum_favorable_excursion_usd,
                ambiguous_intrabar=state.pending_ambiguous_intrabar,
            )
            state = replace(state, exit_pending=False, closed=True)
        self._state = state
        return BarrierFillResult(
            decision=decision,
            fill_timestamp_ms=fill_timestamp_ms,
            fill_price=fill_price,
            filled_quantity=filled_quantity,
            fee_usd=fee_usd,
            implementation_shortfall_usd=implementation_shortfall_usd,
            remaining_quantity=remaining,
            fully_closed=remaining == 0,
            trade_record=record,
        )

    def _validate_candle(self, candle: Candle) -> None:
        state = self._state
        if state.closed:
            raise RuntimeError("managed position is already closed")
        if not isinstance(candle, Candle):
            raise ValueError("candle must be a Candle")
        if candle.symbol != state.intent.symbol:
            raise ValueError("candle symbol does not match the managed position")
        if state.last_bar_open_time_ms is None and candle.open_time_ms != state.entry_timestamp_ms:
            raise ValueError("first managed candle must begin exactly at entry_timestamp_ms")
        duration_ms = candle.close_time_ms - candle.open_time_ms + 1
        if state.last_bar_open_time_ms is None and state.intent.exit_plan.max_holding_ms % duration_ms:
            raise ValueError("max_holding_ms must align with the managed candle duration")
        if state.last_bar_close_time_ms is not None and candle.open_time_ms <= state.last_bar_close_time_ms:
            raise ValueError("managed candles must not overlap")
        if state.timeframe_duration_ms is not None and duration_ms != state.timeframe_duration_ms:
            raise ValueError("managed candle duration cannot change")
        if state.timeframe is not None and candle.timeframe != state.timeframe:
            raise ValueError("managed position candle timeframe cannot change")

    def _request_exit(
        self,
        state: ManagedPositionState,
        candle: Candle,
        *,
        timestamp_ms: int,
        reference_price: float,
        reason: ExitReason,
        ambiguous: bool,
    ) -> BarrierExitDecision:
        sequence = state.decision_sequence + 1
        decision = BarrierExitDecision(
            intent_id=state.intent.intent_id,
            decision_sequence=sequence,
            decision_timestamp_ms=timestamp_ms,
            valid_until_timestamp_ms=candle.close_time_ms,
            reference_price=reference_price,
            requested_quantity=state.remaining_quantity,
            exit_reason=reason,
            ambiguous_intrabar=ambiguous,
        )
        self._state = replace(
            state,
            exit_pending=True,
            pending_exit_reason=reason,
            pending_ambiguous_intrabar=(state.pending_ambiguous_intrabar or ambiguous),
            active_decision=decision,
            decision_sequence=sequence,
        )
        return decision

    def _stop_crossed(self, price: float, stop: float) -> bool:
        if self._state.intent.side is Side.LONG:
            return price <= stop
        return price >= stop

    def _target_crossed(self, price: float) -> bool:
        target = self._state.intent.exit_plan.target_price
        if self._state.intent.side is Side.LONG:
            return price >= target
        return price <= target

    def _bar_hits_stop(self, candle: Candle, stop: float) -> bool:
        if self._state.intent.side is Side.LONG:
            return candle.low <= stop
        return candle.high >= stop

    def _bar_hits_target(self, candle: Candle) -> bool:
        target = self._state.intent.exit_plan.target_price
        if self._state.intent.side is Side.LONG:
            return candle.high >= target
        return candle.low <= target

    @staticmethod
    def _update_excursions(
        state: ManagedPositionState,
        observed_low: float,
        observed_high: float,
    ) -> ManagedPositionState:
        quantity = state.remaining_quantity
        if state.intent.side is Side.LONG:
            adverse = max(0.0, (state.entry_price - observed_low) * quantity)
            favorable = max(0.0, (observed_high - state.entry_price) * quantity)
        else:
            adverse = max(0.0, (observed_high - state.entry_price) * quantity)
            favorable = max(0.0, (state.entry_price - observed_low) * quantity)
        return replace(
            state,
            maximum_adverse_excursion_usd=max(state.maximum_adverse_excursion_usd, adverse),
            maximum_favorable_excursion_usd=max(
                state.maximum_favorable_excursion_usd,
                favorable,
            ),
        )

    def _update_trailing_at_close(
        self,
        state: ManagedPositionState,
        close: float,
    ) -> ManagedPositionState:
        plan = state.intent.exit_plan
        activation = plan.trailing_activation_price
        distance = plan.trailing_distance
        if activation is None or distance is None:
            return state
        activated = state.trailing_activated
        if state.intent.side is Side.LONG:
            activated = activated or close >= activation
            candidate = close - distance
            improves = candidate > state.active_stop_price
        else:
            activated = activated or close <= activation
            candidate = close + distance
            improves = candidate < state.active_stop_price
        if not activated:
            return state
        if not math.isfinite(candidate) or candidate <= 0:
            raise ValueError("computed trailing stop must be finite and positive")
        if not improves:
            return replace(state, trailing_activated=True)
        return replace(
            state,
            active_stop_price=candidate,
            active_stop_reason=ExitReason.TRAILING_STOP,
            trailing_activated=True,
        )


# Composition code can use the explicit engine name without obscuring that one
# instance owns exactly one managed position.
BarrierEngine = ManagedPosition
