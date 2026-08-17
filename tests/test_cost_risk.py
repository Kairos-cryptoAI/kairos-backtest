import pytest
from kairos_core.enums import Side

from kairos_backtest.cost_risk import (
    AdmissionReason,
    AllInCostModel,
    RiskLimits,
    size_and_admit,
)


def test_round_trip_cost_model_counts_both_sides_and_full_spread():
    costs = AllInCostModel(
        fee_bps_per_side=4.5,
        spread_bps=2,
        slippage_bps_per_side=1,
        adverse_funding_bps=1,
        latency_bps=0.5,
        uncertainty_buffer_bps=2,
    )

    assert costs.estimated_round_trip_bps == 16.5


def test_reward_must_strictly_exceed_the_cost_hurdle():
    costs = AllInCostModel(uncertainty_buffer_bps=0)
    hurdle = costs.estimated_round_trip_bps

    rejected = size_and_admit(
        side=Side.LONG,
        entry_price=100,
        stop_price=99,
        target_price=100 * (1 + hurdle / 10_000),
        equity_usd=10_000,
        costs=costs,
        limits=RiskLimits(minimum_net_reward_to_risk=0.000001),
    )
    accepted = size_and_admit(
        side=Side.LONG,
        entry_price=100,
        stop_price=99,
        target_price=100 * (1 + (hurdle + 1.0) / 10_000),
        equity_usd=10_000,
        costs=costs,
        limits=RiskLimits(minimum_net_reward_to_risk=0.000001),
    )

    assert rejected.reason is AdmissionReason.REWARD_BELOW_COST_HURDLE
    assert rejected.quantity == 0
    assert accepted.accepted is True


def test_net_reward_to_risk_must_cover_costs_on_both_outcomes():
    costs = AllInCostModel(
        fee_bps_per_side=4.5,
        spread_bps=2,
        slippage_bps_per_side=2,
        uncertainty_buffer_bps=2,
    )

    rejected = size_and_admit(
        side=Side.LONG,
        entry_price=100,
        stop_price=99,
        target_price=100.5,
        equity_usd=10_000,
        costs=costs,
    )
    accepted = size_and_admit(
        side=Side.LONG,
        entry_price=100,
        stop_price=99,
        target_price=102,
        equity_usd=10_000,
        costs=costs,
    )

    assert rejected.reason is AdmissionReason.REWARD_RISK_TOO_LOW
    assert rejected.net_reward_to_risk < 1.25
    assert accepted.accepted is True
    assert accepted.net_reward_to_risk >= 1.25


def test_net_reward_to_risk_does_not_accept_a_value_just_below_the_boundary():
    costs = AllInCostModel(
        fee_bps_per_side=0,
        spread_bps=0,
        slippage_bps_per_side=0,
        uncertainty_buffer_bps=0,
    )
    result = size_and_admit(
        side=Side.LONG,
        entry_price=100,
        stop_price=99,
        target_price=101.2499999999999,
        equity_usd=10_000,
        costs=costs,
        limits=RiskLimits(minimum_net_reward_to_risk=1.25),
    )

    assert result.net_reward_to_risk < 1.25
    assert result.reason is AdmissionReason.REWARD_RISK_TOO_LOW


def test_position_size_includes_costs_in_the_stop_loss_budget():
    result = size_and_admit(
        side=Side.LONG,
        entry_price=100,
        stop_price=99,
        target_price=102,
        equity_usd=10_000,
        costs=AllInCostModel(
            fee_bps_per_side=5,
            spread_bps=0,
            slippage_bps_per_side=0,
            uncertainty_buffer_bps=0,
        ),
        limits=RiskLimits(risk_fraction=0.01, maximum_notional_fraction=1),
    )

    assert result.accepted is True
    assert result.risk_budget_usd == 100
    assert result.quantity == pytest.approx(100 / 1.1)
    assert result.estimated_loss_at_stop_usd == pytest.approx(100)


def test_notional_cap_can_reduce_risk_below_the_budget():
    result = size_and_admit(
        side=Side.SHORT,
        entry_price=100,
        stop_price=101,
        target_price=98,
        equity_usd=10_000,
        costs=AllInCostModel(uncertainty_buffer_bps=0),
        limits=RiskLimits(
            risk_fraction=0.10,
            maximum_notional_fraction=0.10,
            maximum_leverage=1,
        ),
    )

    assert result.notional_usd == pytest.approx(1_000)
    assert result.estimated_loss_at_stop_usd < result.risk_budget_usd


@pytest.mark.parametrize(
    ("side", "stop", "target", "reason"),
    [
        (Side.LONG, 100, 102, AdmissionReason.INVALID_STOP_SIDE),
        (Side.SHORT, 100, 98, AdmissionReason.INVALID_STOP_SIDE),
        (Side.LONG, 99, 99, AdmissionReason.INVALID_TARGET_SIDE),
        (Side.SHORT, 101, 101, AdmissionReason.INVALID_TARGET_SIDE),
        (Side.LONG, 99.95, 102, AdmissionReason.STOP_TOO_TIGHT),
        (Side.SHORT, 106, 98, AdmissionReason.STOP_TOO_WIDE),
    ],
)
def test_invalid_or_out_of_policy_stops_are_rejected(side, stop, target, reason):
    result = size_and_admit(
        side=side,
        entry_price=100,
        stop_price=stop,
        target_price=target,
        equity_usd=10_000,
    )

    assert result.accepted is False
    assert result.reason is reason
    assert result.quantity == 0


@pytest.mark.parametrize(
    "changes",
    [
        {"entry_price": float("nan")},
        {"stop_price": float("inf")},
        {"target_price": -1},
        {"equity_usd": 0},
        {"side": Side.FLAT},
    ],
)
def test_nonfinite_negative_or_nondirectional_inputs_fail_closed(changes):
    inputs = {
        "side": Side.LONG,
        "entry_price": 100,
        "stop_price": 99,
        "target_price": 102,
        "equity_usd": 10_000,
    }
    inputs.update(changes)

    with pytest.raises(ValueError):
        size_and_admit(**inputs)


def test_risk_policy_rejects_boolean_and_inverted_bounds():
    with pytest.raises(ValueError, match="finite"):
        RiskLimits(risk_fraction=True)
    with pytest.raises(ValueError, match="below"):
        RiskLimits(minimum_stop_distance_bps=100, maximum_stop_distance_bps=99)
    with pytest.raises(ValueError, match="reward-to-risk"):
        RiskLimits(minimum_net_reward_to_risk=0)


@pytest.mark.parametrize("side", ["LONG", "SHORT"])
def test_raw_serialized_sides_cannot_bypass_directional_checks(side):
    with pytest.raises(ValueError, match="directional"):
        size_and_admit(
            side=side,
            entry_price=100,
            stop_price=101,
            target_price=99,
            equity_usd=10_000,
        )


def test_reward_is_recomputed_from_the_actual_entry_after_a_gap():
    result = size_and_admit(
        side=Side.LONG,
        entry_price=101.90,
        stop_price=99,
        target_price=102,
        equity_usd=10_000,
    )

    assert result.gross_reward_bps == pytest.approx((102 / 101.90 - 1) * 10_000)
    assert result.reason is AdmissionReason.REWARD_BELOW_COST_HURDLE


def test_short_stop_costs_are_valued_on_the_higher_exit_notional():
    costs = AllInCostModel(
        fee_bps_per_side=5,
        spread_bps=0,
        slippage_bps_per_side=0,
        uncertainty_buffer_bps=0,
    )
    result = size_and_admit(
        side=Side.SHORT,
        entry_price=100,
        stop_price=110,
        target_price=90,
        equity_usd=10_000,
        costs=costs,
        limits=RiskLimits(
            risk_fraction=0.01,
            maximum_stop_distance_bps=2_000,
            minimum_net_reward_to_risk=0.5,
        ),
    )

    assert result.estimated_loss_at_stop_usd <= result.risk_budget_usd
    assert result.quantity == pytest.approx(100 / (10 + 110 * 10 / 10_000))


def test_cost_and_risk_parameters_canonicalize_equivalent_numeric_types():
    integer_costs = AllInCostModel(
        fee_bps_per_side=4,
        spread_bps=2,
        slippage_bps_per_side=1,
        adverse_funding_bps=-0.0,
        latency_bps=0,
        uncertainty_buffer_bps=2,
    )
    float_costs = AllInCostModel(
        fee_bps_per_side=4.0,
        spread_bps=2.0,
        slippage_bps_per_side=1.0,
        adverse_funding_bps=0.0,
        latency_bps=0.0,
        uncertainty_buffer_bps=2.0,
    )

    assert integer_costs == float_costs
    assert isinstance(integer_costs.fee_bps_per_side, float)
    assert integer_costs.adverse_funding_bps == 0.0
    assert RiskLimits(maximum_leverage=1) == RiskLimits(maximum_leverage=1.0)


@pytest.mark.parametrize(
    ("argument", "message"),
    [("costs", "AllInCostModel"), ("limits", "RiskLimits")],
)
def test_falsey_invalid_admission_dependencies_do_not_select_defaults(argument, message):
    inputs = {
        "side": Side.LONG,
        "entry_price": 100,
        "stop_price": 99,
        "target_price": 103,
        "equity_usd": 10_000,
        argument: False,
    }

    with pytest.raises(TypeError, match=message):
        size_and_admit(**inputs)  # type: ignore[arg-type]
