from __future__ import annotations

import math
from dataclasses import replace

import pytest
from kairos_core.enums import Side
from kairos_quant.candles import Candle

from kairos_backtest.cost_risk import AllInCostModel, RiskLimits, size_and_admit
from kairos_backtest.execution import (
    ExecutionConfig,
    FundingConfig,
    FundingRateObservation,
)
from kairos_backtest.managed_evaluation import (
    IntentDispositionReason,
    ManagedEvaluationAssumptions,
    ManagedEvaluationPolicy,
    ManagedExecutionOrigin,
    ManagedFillPhase,
    evaluate_sleeve_cell,
)
from kairos_backtest.strategy_models import ExitPlan, ExitReason, SleeveIntent

_MINUTE_MS = 60_000
_TWO_DAYS = 2 * 24 * 60
_SLEEVE = "test_sleeve"
_SYMBOL = "BTCUSDT"


def candles(*, count: int = _TWO_DAYS) -> list[Candle]:
    return [
        Candle(
            symbol=_SYMBOL,
            timeframe="1m",
            open_time_ms=index * _MINUTE_MS,
            close_time_ms=(index + 1) * _MINUTE_MS - 1,
            open=100.0,
            high=100.5,
            low=99.5,
            close=100.0,
            volume=100.0,
            quote_volume=10_000.0,
            taker_buy_volume=50.0,
        )
        for index in range(count)
    ]


def with_bar(
    rows: list[Candle],
    index: int,
    *,
    open: float | None = None,
    high: float | None = None,
    low: float | None = None,
    close: float | None = None,
    volume: float | None = None,
) -> None:
    current = rows[index]
    changed_volume = current.volume if volume is None else volume
    rows[index] = replace(
        current,
        open=current.open if open is None else open,
        high=current.high if high is None else high,
        low=current.low if low is None else low,
        close=current.close if close is None else close,
        volume=changed_volume,
        quote_volume=changed_volume * (current.close if close is None else close),
        taker_buy_volume=changed_volume / 2,
    )


def intent(
    rows: list[Candle],
    decision_index: int,
    *,
    side: Side = Side.LONG,
    stop: float = 99.0,
    target: float = 102.0,
    max_hold_minutes: int = 1,
    expires_timestamp_ms: int | None = None,
    trailing_activation: float | None = None,
    trailing_distance: float | None = None,
    tag: str = "default",
) -> SleeveIntent:
    reference = rows[decision_index].close
    eligible = rows[decision_index + 1].open_time_ms
    return SleeveIntent(
        sleeve_id=_SLEEVE,
        symbol=_SYMBOL,
        side=side,
        decision_ts_ms=rows[decision_index].close_time_ms,
        entry_eligible_ts_ms=eligible,
        entry_expires_ts_ms=(eligible + _MINUTE_MS if expires_timestamp_ms is None else expires_timestamp_ms),
        reference_price=reference,
        signal_strength=0.75,
        gross_reward_bps=abs(target - reference) / reference * 10_000,
        exit_plan=ExitPlan(
            stop_price=stop,
            target_price=target,
            max_holding_ms=max_hold_minutes * _MINUTE_MS,
            trailing_activation_price=trailing_activation,
            trailing_distance=trailing_distance,
        ),
        metadata=(("tag", tag),),
    )


def zero_execution(**changes: object) -> ExecutionConfig:
    values: dict[str, object] = {
        "latency_ms": 0,
        "spread_bps": 0.0,
        "slippage_bps": 0.0,
        "fee_bps": 0.0,
        "max_volume_participation": 1.0,
        "slippage_jitter_bps": 0.0,
        "funding": FundingConfig(),
    }
    values.update(changes)
    return ExecutionConfig(**values)  # type: ignore[arg-type]


def zero_costs(**changes: float) -> AllInCostModel:
    values = {
        "fee_bps_per_side": 0.0,
        "spread_bps": 0.0,
        "slippage_bps_per_side": 0.0,
        "adverse_funding_bps": 0.0,
        "latency_bps": 0.0,
        "uncertainty_buffer_bps": 0.0,
    }
    values.update(changes)
    return AllInCostModel(**values)


def evaluate(
    rows: list[Candle],
    candidates: list[SleeveIntent],
    *,
    initial_equity: float = 1_000.0,
    execution: ExecutionConfig | None = None,
    costs: AllInCostModel | None = None,
    limits: RiskLimits | None = None,
    policy: ManagedEvaluationPolicy | None = None,
    seed: int = 7,
):
    ordered = sorted(candidates, key=lambda item: (item.entry_eligible_ts_ms, item.intent_id))
    return evaluate_sleeve_cell(
        rows,
        ordered,
        cell_id="test-sleeve-btc",
        sleeve_id=_SLEEVE,
        symbol=_SYMBOL,
        initial_equity_usd=initial_equity,
        execution=execution or zero_execution(),
        costs=costs or zero_costs(),
        limits=limits or RiskLimits(maximum_notional_fraction=1.0),
        policy=policy,
        seed=seed,
    )


def test_entry_is_first_open_after_the_closed_decision_and_latency() -> None:
    rows = candles()
    candidate = intent(rows, 10)

    result = evaluate(rows, [candidate], execution=zero_execution(latency_ms=250))

    trade = result.trades[0]
    assert trade.entry_timestamp_ms == rows[12].open_time_ms
    assert trade.entry_timestamp_ms > candidate.decision_ts_ms
    assert result.dispositions[0].reason is IntentDispositionReason.ENTERED

    expired = evaluate(
        rows,
        [intent(rows, 10, expires_timestamp_ms=rows[11].open_time_ms)],
        execution=zero_execution(latency_ms=250),
    )
    assert expired.dispositions[0].reason is IntentDispositionReason.EXPIRED


def test_gap_recomputes_reward_and_rejects_stale_candidate() -> None:
    rows = candles()
    with_bar(rows, 11, open=101.99, high=102.0, low=101.5, close=101.9)
    candidate = intent(rows, 10)

    result = evaluate(rows, [candidate], costs=AllInCostModel())

    assert result.trades == ()
    assert result.dispositions[0].reason is IntentDispositionReason.REWARD_BELOW_COST_HURDLE

    outside = candles()
    with_bar(outside, 11, open=102.1, high=102.2, low=101.8, close=102.0)
    rejected = evaluate(outside, [intent(outside, 10)])
    assert rejected.dispositions[0].reason is IntentDispositionReason.ENTRY_OUTSIDE_EXIT_PLAN


def test_actual_entry_must_preserve_the_net_reward_to_risk_floor() -> None:
    rows = candles()
    with_bar(rows, 11, open=100.45, high=100.6, low=100.2, close=100.5)
    candidate = intent(rows, 10)

    result = evaluate(rows, [candidate])

    assert result.trades == ()
    assert result.dispositions[0].reason is IntentDispositionReason.REWARD_RISK_TOO_LOW


def test_overlapping_candidates_are_flat_only_and_have_one_disposition_each() -> None:
    rows = candles()
    with_bar(rows, 10, volume=300.0)
    with_bar(rows, 11, high=103.0, low=99.5, close=101.0, volume=100.0)
    candidates = [
        intent(rows, 10, max_hold_minutes=3, tag="a"),
        intent(rows, 10, max_hold_minutes=3, target=102.5, tag="b"),
    ]

    result = evaluate(rows, candidates)

    assert len(result.trades) == 1
    assert [item.reason for item in result.dispositions].count(IntentDispositionReason.ENTERED) == 1
    assert [item.reason for item in result.dispositions].count(
        IntentDispositionReason.OVERLAPPING_POSITION
    ) == 1
    assert result.counters.overlapping == 1
    assert result.counters.intents == 2


def test_expiry_is_inclusive_at_the_exact_entry_open() -> None:
    rows = candles()
    expiry = rows[11].open_time_ms
    candidate = intent(rows, 10, expires_timestamp_ms=expiry)

    result = evaluate(rows, [candidate])

    assert result.dispositions[0].scheduled_entry_timestamp_ms == expiry
    assert result.dispositions[0].reason is IntentDispositionReason.ENTERED


def test_actual_cost_and_stop_distance_determine_position_size() -> None:
    rows = candles()
    candidate = intent(rows, 10)
    costs = zero_costs(fee_bps_per_side=5.0)
    limits = RiskLimits(risk_fraction=0.01, maximum_notional_fraction=1.0)
    expected = size_and_admit(
        side=Side.LONG,
        entry_price=100.0,
        stop_price=99.0,
        target_price=102.0,
        equity_usd=10_000.0,
        costs=costs,
        limits=limits,
    )

    result = evaluate(
        rows,
        [candidate],
        initial_equity=10_000.0,
        execution=zero_execution(fee_bps=5.0),
        costs=costs,
        limits=limits,
    )

    assert result.trades[0].quantity == pytest.approx(expected.quantity)
    assert result.trades[0].initial_risk_usd == pytest.approx(expected.quantity)
    assert result.trades[0].entry_fee_usd > 0
    assert result.trades[0].exit_fee_usd > 0


def test_full_jitter_price_range_is_admitted_before_any_order_is_sent() -> None:
    rows = candles()
    candidate = intent(rows, 10, stop=99.95, target=102.0)
    execution = zero_execution(slippage_jitter_bps=10.0)

    result = evaluate(
        rows,
        [candidate],
        execution=execution,
        costs=zero_costs(slippage_bps_per_side=10.0),
        limits=RiskLimits(
            risk_fraction=0.01,
            maximum_notional_fraction=1.0,
            minimum_stop_distance_bps=10.0,
        ),
        seed=1,
    )

    assert result.dispositions[0].reason is IntentDispositionReason.STOP_TOO_TIGHT
    assert result.fills == ()
    assert result.turnover_usd == 0
    assert result.fees_usd == 0


def test_admission_cost_model_must_dominate_execution_and_holding_funding() -> None:
    rows = candles()
    candidate = intent(rows, 10)
    with pytest.raises(ValueError, match="dominate"):
        evaluate(
            rows,
            [candidate],
            execution=zero_execution(fee_bps=1.0),
            costs=zero_costs(),
        )

    across_boundary = intent(rows, 58, max_hold_minutes=2, tag="funding_cost")
    funding = FundingConfig(
        rate_8h_bps=100.0,
        source="adverse_test",
        evidence="assumed",
    )
    with pytest.raises(ValueError, match="adverse funding"):
        evaluate(
            rows,
            [across_boundary],
            execution=zero_execution(funding=funding),
            costs=zero_costs(),
        )

    one_settlement_only = zero_costs(adverse_funding_bps=12.5)
    with pytest.raises(ValueError, match="adverse funding"):
        evaluate(
            rows,
            [across_boundary],
            execution=zero_execution(funding=funding),
            costs=one_settlement_only,
            policy=ManagedEvaluationPolicy(
                application_exit_latency_ms=0,
                terminal_liquidation_grace_ms=60 * _MINUTE_MS,
            ),
        )


def test_application_exit_latency_defers_timeout_but_not_resting_target() -> None:
    rows = candles()
    timeout = intent(rows, 0, max_hold_minutes=1, tag="timeout")
    policy = ManagedEvaluationPolicy(
        application_exit_latency_ms=250,
        terminal_liquidation_grace_ms=5 * _MINUTE_MS,
    )

    delayed = evaluate(rows, [timeout], policy=policy)

    assert delayed.trades[0].entry_timestamp_ms == rows[1].open_time_ms
    assert delayed.trades[0].exit_reason is ExitReason.TIMEOUT
    assert delayed.trades[0].exit_timestamp_ms == rows[3].open_time_ms
    assert delayed.fills[-1].execution_origin is ManagedExecutionOrigin.APPLICATION_EXIT

    target_rows = candles()
    with_bar(target_rows, 1, high=102.5, low=99.5, close=101.0)
    target = evaluate(target_rows, [intent(target_rows, 0)], policy=policy)
    assert target.trades[0].exit_reason is ExitReason.TAKE_PROFIT
    assert target.trades[0].exit_timestamp_ms == target_rows[1].close_time_ms
    assert target.fills[-1].execution_origin is ManagedExecutionOrigin.RESTING_BARRIER


def test_residual_retry_latency_starts_at_the_actual_partial_attempt() -> None:
    rows = candles()
    with_bar(rows, 0, volume=1.0)
    with_bar(rows, 2, volume=0.4)
    with_bar(rows, 3, volume=1.0)
    candidate = intent(rows, 0, max_hold_minutes=1)
    policy = ManagedEvaluationPolicy(
        application_exit_latency_ms=250,
        terminal_liquidation_grace_ms=2 * _MINUTE_MS,
    )

    result = evaluate(
        rows,
        [candidate],
        initial_equity=100.0,
        limits=RiskLimits(risk_fraction=0.01, maximum_notional_fraction=1.0),
        policy=policy,
    )

    exits = [fill for fill in result.fills if fill.phase is ManagedFillPhase.EXIT]
    assert [fill.fill_timestamp_ms for fill in exits] == [
        rows[3].open_time_ms,
        rows[4].open_time_ms,
    ]
    assert [fill.filled_quantity for fill in exits] == pytest.approx([0.4, 0.6])
    assert exits[1].order_decision_timestamp_ms == exits[0].fill_timestamp_ms
    assert exits[1].execution_eligible_timestamp_ms == exits[0].fill_timestamp_ms + 250
    assert result.trades[0].exit_timestamp_ms == rows[4].open_time_ms
    assert result.trades[0].exit_timestamp_ms == (
        result.trades[0].entry_timestamp_ms
        + candidate.exit_plan.max_holding_ms
        + policy.terminal_liquidation_grace_ms
    )


def test_entry_candle_barrier_is_active_and_shares_preceding_capacity() -> None:
    rows = candles()
    with_bar(rows, 10, volume=250.0)
    with_bar(rows, 11, high=102.5, low=99.5, close=101.0, volume=100.0)
    candidate = intent(rows, 10)

    result = evaluate(
        rows,
        [candidate],
        initial_equity=10_000.0,
        limits=RiskLimits(risk_fraction=0.01, maximum_notional_fraction=1.0),
    )

    trade = result.trades[0]
    assert trade.entry_timestamp_ms == rows[11].open_time_ms
    assert trade.exit_timestamp_ms == rows[11].close_time_ms
    assert trade.exit_reason is ExitReason.TAKE_PROFIT
    assert result.counters.exit_fill_attempts == 1
    entry, exit = result.fills
    assert (entry.phase, exit.phase) == (ManagedFillPhase.ENTRY, ManagedFillPhase.EXIT)
    assert entry.capacity_before == pytest.approx(250.0)
    assert entry.capacity_after == pytest.approx(150.0)
    assert exit.capacity_before == pytest.approx(150.0)
    assert exit.capacity_after == pytest.approx(50.0)
    assert sum(fill.filled_quantity for fill in result.fills) == pytest.approx(200.0)
    assert sum(fill.filled_quantity for fill in result.fills) <= rows[10].volume


def test_ioc_entry_can_partially_fill_without_a_later_entry_retry() -> None:
    rows = candles()
    with_bar(rows, 10, volume=0.4)
    with_bar(rows, 11, volume=1.0)
    candidate = intent(rows, 10)

    result = evaluate(
        rows,
        [candidate],
        initial_equity=100.0,
        limits=RiskLimits(risk_fraction=0.01, maximum_notional_fraction=1.0),
    )

    assert result.trades[0].quantity == pytest.approx(0.4)
    assert result.dispositions[0].requested_quantity == pytest.approx(1.0)
    assert result.dispositions[0].filled_quantity == pytest.approx(0.4)
    assert result.counters.partial_entries == 1
    assert len(result.trades) == 1


def test_positive_subpicounit_fill_is_not_silently_classified_as_zero() -> None:
    rows = candles()
    scale = 10**13
    for index, row in enumerate(rows):
        rows[index] = replace(
            row,
            open=row.open * scale,
            high=row.high * scale,
            low=row.low * scale,
            close=row.close * scale,
            quote_volume=row.quote_volume * scale,
        )
    with_bar(
        rows,
        11,
        high=102.5 * scale,
        low=99.5 * scale,
        close=101.0 * scale,
    )
    candidate = intent(
        rows,
        10,
        stop=99.0 * scale,
        target=102.0 * scale,
    )

    result = evaluate(rows, [candidate], initial_equity=1_000.0)

    assert 0 < result.trades[0].quantity < 1e-12
    assert result.dispositions[0].reason is IntentDispositionReason.ENTERED
    assert result.turnover_usd > 0
    with pytest.raises(ValueError, match="cannot exceed"):
        replace(result.fills[0], requested_quantity=1e-15)


def test_zero_exit_retry_observes_latency_and_can_fill_exactly_at_deadline() -> None:
    rows = candles()
    with_bar(rows, 0, volume=1.0)
    with_bar(rows, 2, volume=0.0)
    with_bar(rows, 3, volume=1.0)
    candidate = intent(rows, 0, max_hold_minutes=1, tag="zero_retry")
    policy = ManagedEvaluationPolicy(
        application_exit_latency_ms=250,
        terminal_liquidation_grace_ms=2 * _MINUTE_MS,
    )

    result = evaluate(
        rows,
        [candidate],
        initial_equity=100.0,
        limits=RiskLimits(risk_fraction=0.01, maximum_notional_fraction=1.0),
        policy=policy,
    )

    exits = [fill for fill in result.fills if fill.phase is ManagedFillPhase.EXIT]
    assert [(fill.fill_timestamp_ms, fill.filled_quantity) for fill in exits] == [
        (rows[3].open_time_ms, 0.0),
        (rows[4].open_time_ms, 1.0),
    ]
    assert exits[1].order_decision_timestamp_ms == exits[0].fill_timestamp_ms
    assert exits[1].execution_eligible_timestamp_ms == exits[0].fill_timestamp_ms + 250
    assert result.trades[0].exit_timestamp_ms == (
        result.trades[0].entry_timestamp_ms
        + candidate.exit_plan.max_holding_ms
        + policy.terminal_liquidation_grace_ms
    )


def test_partial_exit_retries_preserve_pending_reason_and_shared_capacity() -> None:
    rows = candles()
    with_bar(rows, 0, volume=1.0)
    with_bar(rows, 1, volume=0.4)
    with_bar(rows, 2, open=99.0, high=99.5, low=98.5, close=99.0, volume=0.3)
    with_bar(rows, 3, open=99.0, high=99.5, low=98.5, close=99.0, volume=1.0)
    with_bar(rows, 4, open=99.0, high=99.5, low=98.5, close=99.0, volume=1.0)
    candidate = intent(rows, 0, max_hold_minutes=10)

    result = evaluate(
        rows,
        [candidate],
        initial_equity=100.0,
        limits=RiskLimits(risk_fraction=0.01, maximum_notional_fraction=1.0),
    )

    trade = result.trades[0]
    assert trade.quantity == pytest.approx(1.0)
    assert trade.exit_price == pytest.approx(99.0)
    assert trade.exit_reason is ExitReason.STOP_LOSS
    assert trade.exit_timestamp_ms == rows[4].open_time_ms
    assert result.counters.exit_fill_attempts == 3
    assert result.counters.partial_exits == 2
    assert result.counters.zero_fill_exits == 0
    exits = [fill for fill in result.fills if fill.phase is ManagedFillPhase.EXIT]
    assert [fill.filled_quantity for fill in exits] == pytest.approx([0.4, 0.3, 0.3])
    assert [fill.capacity_before for fill in exits] == pytest.approx([0.4, 0.3, 1.0])
    assert [fill.capacity_after for fill in exits] == pytest.approx([0.0, 0.0, 0.7])


@pytest.mark.parametrize("failure", ["incomplete", "gap", "nonfinite"])
def test_incomplete_gapped_or_nonfinite_daily_inputs_fail_closed(failure: str) -> None:
    rows = candles()
    if failure == "incomplete":
        rows = rows[:-1]
    elif failure == "gap":
        rows[100] = replace(
            rows[100],
            open_time_ms=rows[100].open_time_ms + _MINUTE_MS,
            close_time_ms=rows[100].close_time_ms + _MINUTE_MS,
        )
    else:
        object.__setattr__(rows[100], "volume", float("nan"))

    with pytest.raises(ValueError):
        evaluate(rows, [])


def test_insufficient_horizon_is_rejected_but_liquidity_residual_fails_terminal() -> None:
    rows = candles()
    late = intent(rows, len(rows) - 2, max_hold_minutes=1)

    rejected = evaluate(rows, [late])

    assert rejected.dispositions[0].reason is IntentDispositionReason.INSUFFICIENT_TERMINAL_HORIZON

    residual_rows = candles()
    decision_index = len(residual_rows) - 70
    with_bar(residual_rows, decision_index, volume=1.0)
    for index in range(decision_index + 1, len(residual_rows)):
        with_bar(residual_rows, index, volume=0.0)
    residual = intent(residual_rows, decision_index, max_hold_minutes=1)

    with pytest.raises(ValueError, match="bounded liquidation horizon"):
        evaluate(residual_rows, [residual], initial_equity=100.0)


def test_dispositions_remain_in_input_order_when_terminal_rejection_is_preclassified() -> None:
    rows = candles()
    early = intent(rows, 10, tag="early")
    late = intent(rows, len(rows) - 2, max_hold_minutes=1, tag="late")

    result = evaluate(rows, [early, late])

    assert [item.intent_id for item in result.dispositions] == [early.intent_id, late.intent_id]
    assert result.dispositions[0].reason is IntentDispositionReason.ENTERED
    assert result.dispositions[1].reason is IntentDispositionReason.INSUFFICIENT_TERMINAL_HORIZON


def test_funding_settles_before_same_open_entry_and_never_grants_credits() -> None:
    rows = candles()
    boundary_entry = intent(rows, 59)
    assumed = FundingConfig(
        rate_8h_bps=100.0,
        source="adverse_test",
        evidence="assumed",
    )

    entered_after_settlement = evaluate(
        rows,
        [boundary_entry],
        initial_equity=100.0,
        execution=zero_execution(funding=assumed),
        policy=ManagedEvaluationPolicy(
            application_exit_latency_ms=0,
            terminal_liquidation_grace_ms=0,
        ),
    )

    assert entered_after_settlement.trades
    assert entered_after_settlement.carry_cost_usd == 0

    historical_credit = FundingConfig(
        source="negative_rate_fixture",
        evidence="historical",
        historical_rates=(FundingRateObservation(60 * _MINUTE_MS, -100.0),),
    )
    across_boundary = intent(rows, 58, max_hold_minutes=2, tag="credit")
    no_credit = evaluate(
        rows,
        [across_boundary],
        initial_equity=100.0,
        execution=zero_execution(funding=historical_credit),
        policy=ManagedEvaluationPolicy(
            application_exit_latency_ms=0,
            terminal_liquidation_grace_ms=0,
        ),
    )
    assert no_credit.carry_cost_usd == 0
    assert no_credit.cell.closing_equity_usd[-1] == pytest.approx(100.0)


def test_funding_ledger_includes_settlement_at_terminal_deadline() -> None:
    rows = candles()
    with_bar(rows, 0, volume=1.0)
    funding = FundingConfig(
        rate_8h_bps=8.0,
        source="minute_deadline_fixture",
        evidence="assumed",
        settlement_interval_ms=_MINUTE_MS,
    )
    policy = ManagedEvaluationPolicy(
        application_exit_latency_ms=250,
        terminal_liquidation_grace_ms=_MINUTE_MS,
    )

    result = evaluate(
        rows,
        [intent(rows, 0, max_hold_minutes=1, tag="deadline_funding")],
        initial_equity=100.0,
        execution=zero_execution(funding=funding),
        costs=zero_costs(adverse_funding_bps=2.0),
        limits=RiskLimits(risk_fraction=0.01, maximum_notional_fraction=1.0),
        policy=policy,
    )

    assert [event.timestamp_ms for event in result.funding_events] == [
        rows[2].open_time_ms,
        rows[3].open_time_ms,
    ]
    assert result.trades[0].exit_timestamp_ms == rows[3].open_time_ms
    assert sum(event.charged_cost_usd for event in result.funding_events) == pytest.approx(
        result.carry_cost_usd
    )


def test_daily_snapshot_marks_open_position_with_fees_then_finishes_flat() -> None:
    rows = candles()
    with_bar(rows, 1439, open=100.0, high=101.2, low=99.5, close=101.0, volume=100.0)
    with_bar(rows, 1440, open=101.0, high=101.5, low=100.5, close=101.0, volume=100.0)
    candidate = intent(
        rows,
        1438,
        stop=98.0,
        target=103.0,
        max_hold_minutes=1,
    )

    result = evaluate(
        rows,
        [candidate],
        execution=zero_execution(fee_bps=10.0),
        costs=zero_costs(fee_bps_per_side=10.0),
    )

    first, second = result.cell.snapshots
    assert first.open_position is not None
    assert first.mark_price == 101.0
    assert first.closing_equity_usd == pytest.approx(
        1_000 + first.open_position.remaining_quantity * (101.0 - 100.0) - first.open_position.entry_fee_usd
    )
    assert second.open_position is None
    assert second.mark_price is None
    assert second.cumulative_realized_pnl_usd == pytest.approx(result.trades[0].net_pnl_usd)


def test_short_trade_and_open_daily_mark_are_directionally_correct() -> None:
    rows = candles()
    with_bar(rows, 1439, open=100.0, high=100.5, low=98.8, close=99.0)
    with_bar(rows, 1440, open=99.0, high=99.5, low=98.5, close=99.0)
    candidate = intent(
        rows,
        1438,
        side=Side.SHORT,
        stop=102.0,
        target=97.0,
        max_hold_minutes=1,
    )

    result = evaluate(rows, [candidate])

    first = result.cell.snapshots[0]
    assert first.open_position is not None
    assert first.closing_equity_usd > 1_000.0
    assert result.trades[0].intent.side is Side.SHORT
    assert result.trades[0].exit_reason is ExitReason.TIMEOUT


def test_frozen_result_rejects_entry_or_exit_ledgers_that_disagree_with_trade() -> None:
    rows = candles()
    result = evaluate(rows, [intent(rows, 10)])
    entry, *exits = result.fills
    forged_entry = replace(
        entry,
        order_decision_timestamp_ms=rows[10].open_time_ms,
        execution_eligible_timestamp_ms=rows[10].open_time_ms,
        execution_candle_open_time_ms=rows[10].open_time_ms,
        fill_timestamp_ms=rows[10].open_time_ms,
    )

    with pytest.raises(ValueError, match="entry fill evidence"):
        replace(result, fills=(forged_entry, *exits))

    final_exit = exits[-1]
    forged_exit = replace(final_exit, price=final_exit.price * 0.99)
    forged_turnover = (
        result.turnover_usd
        - final_exit.filled_quantity * final_exit.price
        + (forged_exit.filled_quantity * forged_exit.price)
    )
    with pytest.raises(ValueError, match="exit fill evidence"):
        replace(result, fills=(entry, *exits[:-1], forged_exit), turnover_usd=forged_turnover)


def test_timeout_origin_cannot_be_relabelled_as_a_resting_barrier() -> None:
    rows = candles()
    result = evaluate(rows, [intent(rows, 0, max_hold_minutes=1)])
    timeout = result.fills[-1]
    assert timeout.execution_origin is ManagedExecutionOrigin.APPLICATION_EXIT
    assert timeout.exit_reason is ExitReason.TIMEOUT
    assert timeout.exit_decision is not None

    forged = replace(
        timeout,
        execution_origin=ManagedExecutionOrigin.RESTING_BARRIER,
        order_decision_timestamp_ms=timeout.fill_timestamp_ms,
        execution_eligible_timestamp_ms=timeout.fill_timestamp_ms,
    )
    with pytest.raises(ValueError, match="resting barrier"):
        replace(result, fills=(*result.fills[:-1], forged))


def test_stop_and_trailing_fills_retain_their_barrier_decisions() -> None:
    stop_rows = candles()
    with_bar(stop_rows, 2, open=98.5, high=99.0, low=98.0, close=98.5)
    stopped = evaluate(stop_rows, [intent(stop_rows, 0, max_hold_minutes=10)])
    stop_fill = stopped.fills[-1]
    assert stop_fill.execution_origin is ManagedExecutionOrigin.RESTING_BARRIER
    assert stop_fill.exit_reason is ExitReason.STOP_LOSS
    assert stop_fill.exit_decision is not None
    assert stop_fill.exit_decision.decision_id

    trailing_rows = candles()
    with_bar(trailing_rows, 1, high=101.5, low=99.5, close=101.2)
    with_bar(trailing_rows, 2, open=100.0, high=100.3, low=99.8, close=100.0)
    trailed = evaluate(
        trailing_rows,
        [
            intent(
                trailing_rows,
                0,
                target=103.0,
                max_hold_minutes=10,
                trailing_activation=101.0,
                trailing_distance=1.0,
            )
        ],
    )
    trailing_fill = trailed.fills[-1]
    assert trailing_fill.execution_origin is ManagedExecutionOrigin.RESTING_BARRIER
    assert trailing_fill.exit_reason is ExitReason.TRAILING_STOP
    assert trailing_fill.exit_decision is not None


def test_replay_evidence_rejects_seed_risk_or_cost_relabelling() -> None:
    rows = candles()
    result = evaluate(rows, [intent(rows, 10)])
    assert result.replay_verified is True
    assert result.external_attestation_verified is False

    changed_seed = replace(result.assumptions, seed=result.assumptions.seed + 1)
    with pytest.raises(ValueError, match="self-check SHA-256"):
        replace(
            result,
            assumptions=changed_seed,
            assumptions_sha256=changed_seed.assumptions_sha256,
        )

    changed_risk = replace(
        result.assumptions,
        limits=replace(result.assumptions.limits, risk_fraction=1e-6),
    )
    with pytest.raises(ValueError, match="risk sizing"):
        replace(
            result,
            assumptions=changed_risk,
            assumptions_sha256=changed_risk.assumptions_sha256,
        )

    changed_costs = replace(
        result.assumptions,
        costs=replace(result.assumptions.costs, uncertainty_buffer_bps=500.0),
    )
    with pytest.raises(ValueError, match="admission rejection"):
        replace(
            result,
            assumptions=changed_costs,
            assumptions_sha256=changed_costs.assumptions_sha256,
        )


def test_frozen_assumptions_bind_policy_execution_and_timing_evidence() -> None:
    rows = candles()
    policy = ManagedEvaluationPolicy(
        application_exit_latency_ms=250,
        terminal_liquidation_grace_ms=5 * _MINUTE_MS,
    )
    result = evaluate(rows, [intent(rows, 0, max_hold_minutes=1)], policy=policy)

    assert result.policy == policy
    assert result.assumptions.assumptions_sha256 == result.assumptions_sha256
    assert len(result.assumptions_sha256) == 64

    forged_policy = ManagedEvaluationPolicy(
        application_exit_latency_ms=2 * _MINUTE_MS,
        terminal_liquidation_grace_ms=2 * _MINUTE_MS,
    )
    forged_assumptions = replace(result.assumptions, policy=forged_policy)
    with pytest.raises(ValueError, match="sealed SHA-256"):
        replace(result, assumptions=forged_assumptions)
    with pytest.raises(ValueError, match="application exit timing"):
        replace(
            result,
            assumptions=forged_assumptions,
            assumptions_sha256=forged_assumptions.assumptions_sha256,
        )
    with pytest.raises(TypeError):
        replace(result, policy=forged_policy)


def test_assumptions_hash_canonicalizes_equivalent_numeric_values() -> None:
    integer_typed = ManagedEvaluationAssumptions(
        execution=ExecutionConfig(
            latency_ms=0,
            spread_bps=2,
            slippage_bps=1,
            fee_bps=4,
            max_volume_participation=1,
            slippage_jitter_bps=-0.0,
            funding=FundingConfig(
                rate_8h_bps=-0.0,
                source="explicit_zero",
                evidence="assumed",
            ),
        ),
        costs=AllInCostModel(
            fee_bps_per_side=4,
            spread_bps=2,
            slippage_bps_per_side=1,
            adverse_funding_bps=-0.0,
            latency_bps=-0.0,
            uncertainty_buffer_bps=2,
        ),
        limits=RiskLimits(maximum_leverage=1),
        policy=ManagedEvaluationPolicy(
            application_exit_latency_ms=0,
            terminal_liquidation_grace_ms=0,
        ),
        seed=7,
    )
    float_typed = ManagedEvaluationAssumptions(
        execution=ExecutionConfig(
            latency_ms=0,
            spread_bps=2.0,
            slippage_bps=1.0,
            fee_bps=4.0,
            max_volume_participation=1.0,
            slippage_jitter_bps=0.0,
            funding=FundingConfig(
                rate_8h_bps=0.0,
                source="explicit_zero",
                evidence="assumed",
            ),
        ),
        costs=AllInCostModel(
            fee_bps_per_side=4.0,
            spread_bps=2.0,
            slippage_bps_per_side=1.0,
            adverse_funding_bps=0.0,
            latency_bps=0.0,
            uncertainty_buffer_bps=2.0,
        ),
        limits=RiskLimits(maximum_leverage=1.0),
        policy=ManagedEvaluationPolicy(
            application_exit_latency_ms=0,
            terminal_liquidation_grace_ms=0,
        ),
        seed=7,
    )

    assert integer_typed == float_typed
    assert integer_typed.assumptions_sha256 == float_typed.assumptions_sha256
    changed = replace(
        float_typed,
        execution=replace(float_typed.execution, spread_bps=math.nextafter(2.0, math.inf)),
    )
    assert changed.assumptions_sha256 != float_typed.assumptions_sha256


def test_intent_identity_order_and_typed_expiry_fail_closed() -> None:
    rows = candles()
    first = intent(rows, 10, tag="a")
    second = intent(rows, 10, target=102.5, tag="b")
    ordered = sorted([first, second], key=lambda item: (item.entry_eligible_ts_ms, item.intent_id))

    def evaluate_raw(candidates: list[SleeveIntent]):
        return evaluate_sleeve_cell(
            rows,
            candidates,
            cell_id="test-sleeve-btc",
            sleeve_id=_SLEEVE,
            symbol=_SYMBOL,
            initial_equity_usd=1_000.0,
            execution=zero_execution(),
            costs=zero_costs(),
            limits=RiskLimits(maximum_notional_fraction=1.0),
        )

    with pytest.raises(ValueError, match="sorted"):
        evaluate_raw(list(reversed(ordered)))
    with pytest.raises(ValueError, match="unique"):
        evaluate_raw([first, first])

    corrupted = intent(rows, 10, tag="corrupted")
    object.__setattr__(corrupted, "entry_expires_ts_ms", True)
    with pytest.raises(TypeError, match="typed integers"):
        evaluate_raw([corrupted])


def test_seeded_execution_is_fully_deterministic() -> None:
    rows = candles()
    candidate = intent(rows, 10)
    execution = zero_execution(
        spread_bps=2.0,
        slippage_bps=1.0,
        fee_bps=4.5,
        slippage_jitter_bps=2.0,
    )
    costs = zero_costs(
        spread_bps=2.0,
        slippage_bps_per_side=3.0,
        fee_bps_per_side=4.5,
    )

    first = evaluate(rows, [candidate], execution=execution, costs=costs, seed=123)
    second = evaluate(rows, [candidate], execution=execution, costs=costs, seed=123)

    assert second == first
