from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest
from kairos_core.enums import Side

from kairos_backtest.strategy_models import ExitPlan, ExitReason, SleeveIntent, TradeRecord


def make_plan(side: Side = Side.LONG, *, trailing: bool = True) -> ExitPlan:
    if side is Side.LONG:
        stop, target, activation = 90.0, 120.0, 105.0
    else:
        stop, target, activation = 110.0, 80.0, 95.0
    return ExitPlan(
        stop_price=stop,
        target_price=target,
        max_holding_ms=120_000,
        trailing_activation_price=activation if trailing else None,
        trailing_distance=5.0 if trailing else None,
    )


def make_intent(
    side: Side = Side.LONG,
    *,
    metadata: tuple[tuple[str, str], ...] = (("regime", "trend"),),
) -> SleeveIntent:
    return SleeveIntent(
        sleeve_id="trend_breakout",
        symbol="BTCUSDT",
        side=side,
        decision_ts_ms=1_000,
        entry_eligible_ts_ms=2_000,
        entry_expires_ts_ms=3_000,
        reference_price=100.0,
        signal_strength=0.72,
        gross_reward_bps=2_000.0,
        exit_plan=make_plan(side),
        metadata=metadata,
    )


def test_intent_is_immutable_canonical_and_deterministically_identified() -> None:
    first = make_intent(metadata=(("z", "last"), ("a", "first")))
    second = make_intent(metadata=(("a", "first"), ("z", "last")))

    assert first.metadata == (("a", "first"), ("z", "last"))
    assert first.intent_id == second.intent_id
    assert len(first.intent_id) == 64
    with pytest.raises(FrozenInstanceError):
        first.signal_strength = 0.99  # type: ignore[misc]


def test_semantically_equal_numeric_representations_share_one_intent_id() -> None:
    integer_plan = ExitPlan(90, 120, 120_000, 105, 5)
    integer_intent = SleeveIntent(
        sleeve_id="trend_breakout",
        symbol="BTCUSDT",
        side=Side.LONG,
        decision_ts_ms=1_000,
        entry_eligible_ts_ms=2_000,
        entry_expires_ts_ms=3_000,
        reference_price=100,
        signal_strength=-0.0,
        gross_reward_bps=2_000,
        exit_plan=integer_plan,
    )
    float_intent = SleeveIntent(
        sleeve_id="trend_breakout",
        symbol="BTCUSDT",
        side=Side.LONG,
        decision_ts_ms=1_000,
        entry_eligible_ts_ms=2_000,
        entry_expires_ts_ms=3_000,
        reference_price=100.0,
        signal_strength=0.0,
        gross_reward_bps=2_000.0,
        exit_plan=ExitPlan(90.0, 120.0, 120_000, 105.0, 5.0),
    )

    assert integer_intent == float_intent
    assert integer_intent.intent_id == float_intent.intent_id


@pytest.mark.parametrize(
    ("activation", "distance"),
    [(105.0, None), (None, 5.0), (105.0, 0.0), (float("nan"), 5.0)],
)
def test_exit_plan_rejects_incomplete_or_non_finite_trailing_configuration(
    activation: float | None,
    distance: float | None,
) -> None:
    with pytest.raises(ValueError):
        ExitPlan(90.0, 120.0, 60_000, activation, distance)


@pytest.mark.parametrize(
    "updates",
    [
        {"side": Side.FLAT},
        {"signal_strength": float("inf")},
        {"signal_strength": 1.01},
        {"gross_reward_bps": 0.0},
        {"gross_reward_bps": 1_999.0},
        {"entry_eligible_ts_ms": 999},
        {"entry_expires_ts_ms": 1_999},
        {"sleeve_id": " trend_breakout"},
        {"symbol": "btcusdt"},
        {"metadata": (("duplicate", "a"), ("duplicate", "b"))},
    ],
)
def test_intent_validation_is_strict(updates: dict[str, object]) -> None:
    values: dict[str, object] = {
        "sleeve_id": "trend_breakout",
        "symbol": "BTCUSDT",
        "side": Side.LONG,
        "decision_ts_ms": 1_000,
        "entry_eligible_ts_ms": 2_000,
        "entry_expires_ts_ms": 3_000,
        "reference_price": 100.0,
        "signal_strength": 0.7,
        "gross_reward_bps": 2_000.0,
        "exit_plan": make_plan(),
    }
    values.update(updates)

    with pytest.raises(ValueError):
        SleeveIntent(**values)  # type: ignore[arg-type]


def test_directional_barriers_and_trailing_activation_are_validated() -> None:
    with pytest.raises(ValueError, match="long exits"):
        SleeveIntent(
            sleeve_id="trend",
            symbol="BTCUSDT",
            side=Side.LONG,
            decision_ts_ms=0,
            entry_eligible_ts_ms=0,
            entry_expires_ts_ms=60_000,
            reference_price=100.0,
            signal_strength=0.5,
            gross_reward_bps=1.0,
            exit_plan=ExitPlan(105.0, 120.0, 60_000),
        )

    with pytest.raises(ValueError, match="short trailing activation"):
        SleeveIntent(
            sleeve_id="trend",
            symbol="BTCUSDT",
            side=Side.SHORT,
            decision_ts_ms=0,
            entry_eligible_ts_ms=0,
            entry_expires_ts_ms=60_000,
            reference_price=100.0,
            signal_strength=0.5,
            gross_reward_bps=1.0,
            exit_plan=ExitPlan(110.0, 80.0, 60_000, 105.0, 5.0),
        )


@pytest.mark.parametrize(
    ("side", "exit_price", "expected_gross"),
    [(Side.LONG, 110.0, 20.0), (Side.SHORT, 90.0, 20.0)],
)
def test_trade_record_pnl_identity_does_not_double_subtract_shortfall(
    side: Side,
    exit_price: float,
    expected_gross: float,
) -> None:
    record = TradeRecord(
        intent=make_intent(side),
        entry_timestamp_ms=2_000,
        exit_timestamp_ms=3_000,
        entry_price=100.0,
        exit_price=exit_price,
        quantity=2.0,
        exit_reason=ExitReason.TAKE_PROFIT,
        entry_fee_usd=1.0,
        exit_fee_usd=2.0,
        carry_cost_usd=0.5,
        implementation_shortfall_usd=7.0,
        maximum_adverse_excursion_usd=4.0,
        maximum_favorable_excursion_usd=22.0,
    )

    assert record.gross_pnl_usd == pytest.approx(expected_gross)
    assert record.total_cost_usd == pytest.approx(3.5)
    assert record.net_pnl_usd == pytest.approx(expected_gross - 3.5)
    assert record.implementation_shortfall_usd == 7.0
    assert record.r_multiple == pytest.approx(record.net_pnl_usd / record.initial_risk_usd)


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -1.0])
def test_trade_record_rejects_invalid_costs(value: float) -> None:
    with pytest.raises(ValueError):
        TradeRecord(
            intent=make_intent(),
            entry_timestamp_ms=2_000,
            exit_timestamp_ms=3_000,
            entry_price=100.0,
            exit_price=105.0,
            quantity=1.0,
            exit_reason=ExitReason.TIMEOUT,
            entry_fee_usd=value,
        )
