from __future__ import annotations

from dataclasses import FrozenInstanceError, replace

import pytest
from kairos_core.enums import Side
from kairos_quant.candles import Candle

from kairos_backtest.barrier_engine import ManagedPosition
from kairos_backtest.strategy_models import ExitPlan, ExitReason, SleeveIntent


def candle(
    index: int,
    *,
    open: float = 100.0,
    high: float = 104.0,
    low: float = 96.0,
    close: float = 101.0,
    symbol: str = "BTCUSDT",
    timeframe: str = "1m",
) -> Candle:
    open_time = index * 60_000
    return Candle(
        symbol=symbol,
        timeframe=timeframe,
        open_time_ms=open_time,
        close_time_ms=open_time + 59_999,
        open=open,
        high=high,
        low=low,
        close=close,
        volume=100.0,
    )


def intent(
    side: Side = Side.LONG,
    *,
    trailing: bool = False,
    max_holding_ms: int = 600_000,
) -> SleeveIntent:
    if side is Side.LONG:
        plan = ExitPlan(
            stop_price=90.0,
            target_price=120.0,
            max_holding_ms=max_holding_ms,
            trailing_activation_price=105.0 if trailing else None,
            trailing_distance=5.0 if trailing else None,
        )
    else:
        plan = ExitPlan(
            stop_price=110.0,
            target_price=80.0,
            max_holding_ms=max_holding_ms,
            trailing_activation_price=95.0 if trailing else None,
            trailing_distance=5.0 if trailing else None,
        )
    return SleeveIntent(
        sleeve_id="trend",
        symbol="BTCUSDT",
        side=side,
        decision_ts_ms=0,
        entry_eligible_ts_ms=0,
        entry_expires_ts_ms=0,
        reference_price=100.0,
        signal_strength=0.7,
        gross_reward_bps=2_000.0,
        exit_plan=plan,
    )


def position(
    side: Side = Side.LONG,
    *,
    trailing: bool = False,
    max_holding_ms: int = 600_000,
) -> ManagedPosition:
    return ManagedPosition(
        intent(side, trailing=trailing, max_holding_ms=max_holding_ms),
        entry_timestamp_ms=0,
        entry_price=100.0,
        quantity=1.0,
        entry_fee_usd=0.2,
        entry_implementation_shortfall_usd=0.1,
    )


def fill_all(
    position: ManagedPosition,
    decision_price: float,
    *,
    open: float,
    high: float,
    low: float,
    close: float,
):
    decision = position.on_candle(candle(0, open=open, high=high, low=low, close=close))
    assert decision is not None
    result = position.apply_exit_fill(
        decision,
        fill_timestamp_ms=decision.decision_timestamp_ms,
        fill_price=decision_price,
        filled_quantity=decision.requested_quantity,
        fee_usd=0.3,
        implementation_shortfall_usd=0.2,
    )
    assert result.trade_record is not None
    return decision, result.trade_record


def test_first_candle_must_start_at_entry_and_entry_barrier_is_active() -> None:
    managed = position()
    with pytest.raises(ValueError, match="first managed candle"):
        managed.on_candle(candle(1))

    decision, record = fill_all(managed, 90.0, open=100.0, high=102.0, low=85.0, close=95.0)

    assert decision.exit_reason is ExitReason.STOP_LOSS
    assert decision.decision_timestamp_ms == 59_999
    assert record.maximum_adverse_excursion_usd == 10.0
    assert record.maximum_favorable_excursion_usd == 0.0


@pytest.mark.parametrize(
    ("side", "gap_open", "expected_reference", "fill_price", "expected_reason"),
    [
        (Side.LONG, 85.0, 85.0, 84.5, ExitReason.STOP_LOSS),
        (Side.SHORT, 115.0, 115.0, 115.5, ExitReason.STOP_LOSS),
        (Side.LONG, 125.0, 120.0, 120.0, ExitReason.TAKE_PROFIT),
        (Side.SHORT, 75.0, 80.0, 80.0, ExitReason.TAKE_PROFIT),
    ],
)
def test_gap_policy_is_adverse_for_stops_and_never_improves_targets(
    side: Side,
    gap_open: float,
    expected_reference: float,
    fill_price: float,
    expected_reason: ExitReason,
) -> None:
    managed = position(side)
    assert managed.on_candle(candle(0)) is None
    second = candle(
        1,
        open=gap_open,
        high=max(gap_open, 101.0),
        low=min(gap_open, 99.0),
        close=gap_open,
    )

    decision = managed.on_candle(second)

    assert decision is not None
    assert decision.reference_price == expected_reference
    assert decision.exit_reason is expected_reason
    result = managed.apply_exit_fill(
        decision,
        fill_timestamp_ms=second.open_time_ms,
        fill_price=fill_price,
        filled_quantity=1.0,
    )
    assert result.fully_closed is True


@pytest.mark.parametrize("side", [Side.LONG, Side.SHORT])
def test_both_intrabar_barriers_choose_stop_and_mark_ambiguity(side: Side) -> None:
    managed = position(side)

    decision = managed.on_candle(candle(0, open=100.0, high=125.0, low=75.0, close=100.0))

    assert decision is not None
    assert decision.exit_reason is ExitReason.STOP_LOSS
    assert decision.ambiguous_intrabar is True
    stop = 90.0 if side is Side.LONG else 110.0
    assert decision.reference_price == stop
    result = managed.apply_exit_fill(
        decision,
        fill_timestamp_ms=decision.decision_timestamp_ms,
        fill_price=stop,
        filled_quantity=1.0,
    )
    record = result.trade_record
    assert record is not None
    assert record.ambiguous_intrabar is True
    assert record.maximum_adverse_excursion_usd == 10.0
    assert record.maximum_favorable_excursion_usd == 0.0


def test_long_trailing_is_calculated_at_close_for_next_bar_and_never_loosens() -> None:
    managed = position(trailing=True)
    first = candle(0, open=100.0, high=116.0, low=95.0, close=115.0)

    assert managed.on_candle(first) is None
    assert managed.state.active_stop_price == 110.0
    assert managed.state.active_stop_reason is ExitReason.TRAILING_STOP

    assert managed.on_candle(candle(1, open=112.0, high=113.0, low=111.0, close=112.0)) is None
    assert managed.state.active_stop_price == 110.0

    decision = managed.on_candle(candle(2, open=111.0, high=112.0, low=109.0, close=110.0))
    assert decision is not None
    assert decision.exit_reason is ExitReason.TRAILING_STOP
    assert decision.reference_price == 110.0


def test_short_trailing_activates_on_the_next_candle() -> None:
    managed = position(Side.SHORT, trailing=True)
    assert managed.on_candle(candle(0, open=100.0, high=105.0, low=84.0, close=85.0)) is None
    assert managed.state.active_stop_price == 90.0

    decision = managed.on_candle(candle(1, open=89.0, high=91.0, low=87.0, close=90.0))

    assert decision is not None
    assert decision.exit_reason is ExitReason.TRAILING_STOP
    assert decision.reference_price == 90.0


def test_timeout_occurs_at_first_eligible_open_without_using_intrabar_extremes() -> None:
    managed = position(max_holding_ms=120_000)
    assert managed.on_candle(candle(0)) is None
    assert managed.on_candle(candle(1)) is None

    decision = managed.on_candle(candle(2, open=102.0, high=125.0, low=80.0, close=100.0))

    assert decision is not None
    assert decision.exit_reason is ExitReason.TIMEOUT
    assert decision.decision_timestamp_ms == 120_000
    assert decision.reference_price == 102.0


def test_partial_protective_fill_keeps_exit_pending_and_retries_without_new_barriers() -> None:
    managed = position()
    first = managed.on_candle(candle(0, open=100.0, high=121.0, low=99.0, close=110.0))
    assert first is not None
    assert first.exit_reason is ExitReason.TAKE_PROFIT

    partial = managed.apply_exit_fill(
        first,
        fill_timestamp_ms=first.decision_timestamp_ms,
        fill_price=120.0,
        filled_quantity=0.4,
        fee_usd=0.1,
        implementation_shortfall_usd=0.05,
    )
    assert partial.fully_closed is False
    assert partial.remaining_quantity == pytest.approx(0.6)
    assert partial.trade_record is None
    assert managed.state.exit_pending is True

    retry_bar = candle(1, open=85.0, high=130.0, low=80.0, close=90.0)
    retry = managed.on_candle(retry_bar, carry_cost_usd=0.5)
    assert retry is not None
    assert retry.exit_reason is ExitReason.TAKE_PROFIT
    assert retry.reference_price == 85.0
    assert retry.requested_quantity == pytest.approx(0.6)
    with pytest.raises(ValueError, match="stale"):
        managed.apply_exit_fill(
            first,
            fill_timestamp_ms=first.decision_timestamp_ms,
            fill_price=120.0,
            filled_quantity=0.1,
        )

    final = managed.apply_exit_fill(
        retry,
        fill_timestamp_ms=retry.decision_timestamp_ms,
        fill_price=84.0,
        filled_quantity=0.6,
        fee_usd=0.2,
        implementation_shortfall_usd=0.15,
    )
    record = final.trade_record
    assert final.fully_closed is True
    assert record is not None
    assert record.exit_price == pytest.approx(98.4)
    assert record.exit_fee_usd == pytest.approx(0.3)
    assert record.carry_cost_usd == 0.5
    assert record.implementation_shortfall_usd == pytest.approx(0.3)
    assert record.maximum_adverse_excursion_usd == pytest.approx(9.0)
    assert managed.state.closed is True


@pytest.mark.parametrize(
    "changes",
    [
        {"reference_price": 1_000.0},
        {"valid_until_timestamp_ms": 120_000},
        {"requested_quantity": 0.5},
        {"exit_reason": ExitReason.TAKE_PROFIT},
        {"ambiguous_intrabar": True},
    ],
)
def test_exit_decision_payload_cannot_be_forged_while_reusing_active_state(changes) -> None:
    managed = position()
    decision = managed.on_candle(candle(0, open=100.0, high=101.0, low=89.0, close=95.0))
    assert decision is not None
    forged = replace(decision, **changes)

    assert forged.decision_id != decision.decision_id
    with pytest.raises(ValueError, match="stale"):
        managed.apply_exit_fill(
            forged,
            fill_timestamp_ms=decision.decision_timestamp_ms,
            fill_price=90.0,
            filled_quantity=0.5,
        )


def test_exit_decision_and_state_snapshots_are_immutable() -> None:
    managed = position()
    decision = managed.on_candle(candle(0, open=100.0, high=101.0, low=89.0, close=95.0))
    assert decision is not None

    with pytest.raises(FrozenInstanceError):
        decision.reference_price = 1.0  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        managed.state.exit_pending = False  # type: ignore[misc]


def test_candles_must_be_symbol_timeframe_and_chronology_consistent() -> None:
    managed = position()
    assert managed.on_candle(candle(0)) is None
    with pytest.raises(ValueError, match="overlap"):
        managed.on_candle(candle(0))
    with pytest.raises(ValueError, match="timeframe"):
        managed.on_candle(candle(1, timeframe="5m"))

    other = position()
    with pytest.raises(ValueError, match="symbol"):
        other.on_candle(candle(0, symbol="ETHUSDT"))


def test_overlapping_or_variable_duration_candles_and_unaligned_timeout_fail_closed() -> None:
    overlapping = position()
    assert overlapping.on_candle(candle(0)) is None
    with pytest.raises(ValueError, match="overlap"):
        overlapping.on_candle(replace(candle(1), open_time_ms=30_000, close_time_ms=89_999))

    duration = position()
    assert duration.on_candle(candle(0)) is None
    with pytest.raises(ValueError, match="duration"):
        duration.on_candle(replace(candle(1), close_time_ms=89_999))

    unaligned = position(max_holding_ms=90_000)
    with pytest.raises(ValueError, match="align"):
        unaligned.on_candle(candle(0))


def test_a_real_gap_is_allowed_and_pending_exit_retries_at_the_gap_open() -> None:
    managed = position()
    first = managed.on_candle(candle(0, open=100.0, high=121.0, low=99.0, close=110.0))
    assert first is not None
    partial = managed.apply_exit_fill(
        first,
        fill_timestamp_ms=first.decision_timestamp_ms,
        fill_price=120.0,
        filled_quantity=0.4,
    )
    assert partial.fully_closed is False

    retry = managed.on_candle(candle(3, open=95.0, high=96.0, low=94.0, close=95.0))

    assert retry is not None
    assert retry.decision_timestamp_ms == 180_000
    assert retry.reference_price == 95.0
    assert retry.requested_quantity == pytest.approx(0.6)
