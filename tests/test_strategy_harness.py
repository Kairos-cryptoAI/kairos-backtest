from dataclasses import replace

import pytest
from kairos_core.enums import OrderSide, Side

from kairos_backtest.evaluation import evaluate
from kairos_backtest.execution import (
    ExecutionConfig,
    FillSimulator,
    FundingConfig,
    FundingRateObservation,
)
from kairos_backtest.fixtures import causal_momentum_signals, synthetic_regime_candles
from kairos_backtest.harness import (
    EvaluationScenario,
    evaluate_sensitivity,
    evaluate_walk_forward,
    evaluate_window,
)
from kairos_backtest.metrics import calculate_metrics
from kairos_backtest.readiness import evaluate_promotion, promotion_data_quality_reasons
from kairos_backtest.strategy import StrategySignal
from kairos_backtest.walk_forward import split_walk_forward


def test_causal_fixture_is_unchanged_when_only_future_data_changes():
    source = synthetic_regime_candles(count=600, seed=7)
    cutoff = 400
    cutoff_timestamp = source[cutoff].close_time_ms
    original = [
        signal for signal in causal_momentum_signals(source) if signal.timestamp_ms <= cutoff_timestamp
    ]
    changed = list(source)
    for index in range(cutoff + 1, len(changed)):
        row = changed[index]
        changed[index] = replace(
            row,
            open=row.open * 3,
            high=row.high * 3,
            low=row.low * 3,
            close=row.close * 3,
        )
    mutated = [
        signal for signal in causal_momentum_signals(changed) if signal.timestamp_ms <= cutoff_timestamp
    ]

    assert mutated == original


def test_fill_after_signal_uses_configured_latency_without_future_bar_clamp():
    candle = synthetic_regime_candles(count=120)[10]
    config = ExecutionConfig(spread_bps=20, slippage_bps=20)
    timestamp = candle.open_time_ms + 250

    fill = FillSimulator(config).fill(
        candle,
        OrderSide.BUY,
        1,
        available_volume=candle.volume,
        timestamp_ms=timestamp,
        reference_price=candle.open,
    )

    assert fill.timestamp_ms == timestamp
    assert fill.reference_price == candle.open
    assert fill.implementation_shortfall_usd > 0
    assert fill.price > candle.open


@pytest.mark.parametrize(("latency_ms", "expected_index"), [(0, 11), (250, 12)])
def test_evaluation_executes_at_first_open_not_before_eligibility(latency_ms, expected_index):
    source = synthetic_regime_candles(count=120, seed=41)
    signals = [StrategySignal(source[10].close_time_ms, Side.LONG, 1.0, ("entry",))]

    result = evaluate(
        source,
        signals,
        initial_equity=10_000,
        execution=ExecutionConfig(
            latency_ms=latency_ms,
            fee_bps=0,
            spread_bps=0,
            slippage_bps=0,
            max_volume_participation=1,
        ),
    )

    assert result.first_fill_timestamp_ms == source[expected_index].open_time_ms
    assert result.first_fill_timestamp_ms >= source[10].close_time_ms + latency_ms


def test_evaluation_volume_capacity_uses_only_the_preceding_closed_candle():
    source = synthetic_regime_candles(count=120, seed=43)
    source[10] = replace(source[10], volume=2.0, taker_buy_volume=1.0)
    source[11] = replace(source[11], volume=1_000_000_000.0, taker_buy_volume=500_000_000.0)
    signals = [StrategySignal(source[10].close_time_ms, Side.LONG, 1.0, ("entry",))]

    result = evaluate(
        source,
        signals,
        initial_equity=10_000,
        execution=ExecutionConfig(
            latency_ms=0,
            fee_bps=0,
            spread_bps=0,
            slippage_bps=0,
            max_volume_participation=0.01,
        ),
    )

    assert result.first_fill_timestamp_ms == source[11].open_time_ms
    assert result.turnover_usd < 10
    assert result.partial_fill_count >= 1
    assert result.fill_ratio_pct < 100


def test_evaluation_rejects_gaps_instead_of_using_future_marks_or_stale_liquidity():
    source = synthetic_regime_candles(count=120, seed=59)
    gapped = source[:20] + source[21:]

    with pytest.raises(ValueError, match="contiguous"):
        evaluate(
            gapped,
            [],
            initial_equity=10_000,
            execution=ExecutionConfig(),
        )


def test_incomplete_terminal_liquidation_fails_instead_of_marking_residual_position():
    source = synthetic_regime_candles(count=120, seed=13)
    source[-1] = replace(source[-1], volume=0.0, taker_buy_volume=0.0)
    signals = [StrategySignal(source[10].close_time_ms, Side.LONG, 1.0, ("entry",))]

    with pytest.raises(ValueError, match="terminal liquidation"):
        evaluate(
            source,
            signals,
            initial_equity=10_000,
            execution=ExecutionConfig(),
        )

    flagged = evaluate(
        source,
        signals,
        initial_equity=10_000,
        execution=ExecutionConfig(),
        allow_incomplete_terminal=True,
    )
    readiness = evaluate_promotion((flagged,), (flagged, flagged))

    assert flagged.terminal_liquidation_complete is False
    assert flagged.terminal_residual_quantity > 0
    assert flagged.terminal_residual_notional_usd > 0
    assert "incomplete_oos_terminal_liquidation" in readiness.reasons
    assert "incomplete_sensitivity_terminal_liquidation" in readiness.reasons


def test_evaluation_fails_explicitly_when_execution_costs_make_equity_insolvent():
    source = synthetic_regime_candles(count=120, seed=47)
    signals = [StrategySignal(source[10].close_time_ms, Side.LONG, 1.0, ("entry",))]

    with pytest.raises(ValueError, match="insolvent"):
        evaluate(
            source,
            signals,
            initial_equity=10_000,
            execution=ExecutionConfig(
                latency_ms=0,
                fee_bps=20_000,
                spread_bps=0,
                slippage_bps=0,
                max_volume_participation=1,
            ),
        )


def test_metrics_reject_nonpositive_equity_instead_of_skipping_the_period():
    with pytest.raises(ValueError, match="finite positive"):
        calculate_metrics([100.0, 0.0, 50.0], [])


def test_funding_unavailable_is_explicit_and_assumed_funding_is_adverse():
    source = synthetic_regime_candles(count=600, seed=3)
    signals = [StrategySignal(source[10].close_time_ms, Side.LONG, 0.5, ("fixture",))]
    unavailable = evaluate(
        source,
        signals,
        initial_equity=10_000,
        execution=ExecutionConfig(funding=FundingConfig()),
    )
    assumed = evaluate(
        source,
        signals,
        initial_equity=10_000,
        execution=ExecutionConfig(
            funding=FundingConfig(
                rate_8h_bps=1.0,
                source="assumed_adverse_evedex_example",
                evidence="assumed",
            )
        ),
    )

    assert unavailable.funding_source == "unavailable"
    assert unavailable.funding_usd == 0
    assert assumed.funding_source == "assumed_adverse_evedex_example"
    assert assumed.funding_usd > 0
    assert assumed.final_equity < unavailable.final_equity


def test_hourly_settlement_precedes_a_latency_delayed_boundary_entry():
    source = synthetic_regime_candles(count=120, seed=5)
    signals = [StrategySignal(source[59].close_time_ms, Side.LONG, 0.5, ("boundary_entry",))]

    result = evaluate(
        source,
        signals,
        initial_equity=10_000,
        execution=ExecutionConfig(
            funding=FundingConfig(
                rate_8h_bps=5,
                source="assumed_adverse_stress",
                evidence="assumed",
            )
        ),
    )

    assert result.funding_observations_expected == 1
    assert result.funding_usd == 0


def test_cost_and_funding_stress_is_monotonic_on_identical_signals():
    source = synthetic_regime_candles(count=1_200, seed=11)
    signals = causal_momentum_signals(source)
    baseline = EvaluationScenario(
        "baseline",
        ExecutionConfig(
            fee_bps=4.5,
            spread_bps=2,
            slippage_bps=2,
            funding=FundingConfig(
                rate_8h_bps=1,
                source="assumed_adverse_evedex_example",
                evidence="assumed",
            ),
        ),
    )
    stress = EvaluationScenario(
        "stress",
        ExecutionConfig(
            fee_bps=4.5,
            spread_bps=8,
            slippage_bps=8,
            funding=FundingConfig(
                rate_8h_bps=5,
                source="assumed_adverse_stress",
                evidence="assumed",
            ),
        ),
    )

    results = evaluate_sensitivity(source, signals, (baseline, stress))

    assert results[1].result.final_equity <= results[0].result.final_equity
    assert results[1].result.implementation_shortfall_usd >= results[0].result.implementation_shortfall_usd
    assert results[1].result.funding_usd >= results[0].result.funding_usd


def test_walk_forward_has_purge_and_test_pnl_uses_only_test_rows():
    source = synthetic_regime_candles(count=1_000, seed=17)
    signals = causal_momentum_signals(source)
    folds = split_walk_forward(source, train_size=400, test_size=150, step=150, purge_size=10)
    results = evaluate_walk_forward(
        source,
        signals,
        train_size=400,
        test_size=150,
        purge_size=10,
        execution=ExecutionConfig(),
    )

    assert results
    assert len(results) == len(folds)
    assert all(result.fold.test_start - result.fold.train_end == 10 for result in results)
    assert all(result.fold.train_end <= result.fold.test_start for result in results)
    assert all(result.result.funding_source == "unavailable" for result in results)


def test_evaluation_window_uses_warmup_state_without_pre_window_pnl():
    source = synthetic_regime_candles(count=600, seed=29)
    signals = [StrategySignal(source[100].close_time_ms, Side.LONG, 0.5, ("warmup",))]

    result = evaluate_window(
        source,
        signals,
        start_index=300,
        end_index=500,
        initial_equity=10_000,
        execution=ExecutionConfig(),
    )

    assert result.exposure_pct == 99.5
    assert result.market_periods == 200
    assert result.exposed_periods == 199
    assert result.benchmark_return_pct == pytest.approx((source[499].close / source[300].open - 1) * 100)


def test_invalid_funding_assumptions_fail_closed():
    with pytest.raises(ValueError, match="non-negative"):
        FundingConfig(rate_8h_bps=-1, source="invalid", evidence="assumed")
    with pytest.raises(ValueError, match="explicit source"):
        FundingConfig(rate_8h_bps=1, source="unavailable", evidence="assumed")


def test_promotion_gate_blocks_negative_or_incomplete_evidence():
    source = synthetic_regime_candles(count=1_200, seed=19)
    signals = causal_momentum_signals(source)
    unavailable = evaluate(
        source,
        signals,
        initial_equity=10_000,
        execution=ExecutionConfig(funding=FundingConfig()),
    )
    stress = evaluate(
        source,
        signals,
        initial_equity=10_000,
        execution=ExecutionConfig(
            spread_bps=8,
            slippage_bps=8,
            funding=FundingConfig(
                rate_8h_bps=5,
                source="assumed_adverse_stress",
                evidence="assumed",
            ),
        ),
    )

    readiness = evaluate_promotion((unavailable,), (unavailable, stress))

    assert readiness.status == "needs_revision"
    assert readiness.real_api_allowed is False
    assert "historical_funding_unavailable" in readiness.reasons
    assert "dataset_audit_unavailable" in readiness.reasons


def test_promotion_data_audit_is_required_by_default():
    assert promotion_data_quality_reasons(()) == ("dataset_audit_unavailable",)


def test_promotion_gate_rejects_every_nonfinite_metric_input():
    source = synthetic_regime_candles(count=1_200, seed=53)
    signals = causal_momentum_signals(source)
    result = evaluate(
        source,
        signals,
        initial_equity=10_000,
        execution=ExecutionConfig(),
    )
    invalid = replace(
        result,
        final_equity=float("nan"),
        return_pct=float("nan"),
        benchmark_return_pct=float("nan"),
        funding_evidence="historical",
        funding_coverage_pct=float("nan"),
        metrics=replace(
            result.metrics,
            expectancy=float("nan"),
            max_drawdown=float("nan"),
        ),
    )

    readiness = evaluate_promotion((invalid,), (result, invalid))

    assert readiness.real_api_allowed is False
    assert "invalid_oos_metrics" in readiness.reasons
    assert "invalid_sensitivity_metrics" in readiness.reasons
    assert "non_positive_oos_return" in readiness.reasons
    assert "non_positive_oos_expectancy" in readiness.reasons
    assert "oos_drawdown_limit_exceeded" in readiness.reasons
    assert "oos_benchmark_underperformance" in readiness.reasons
    assert "historical_funding_unavailable" in readiness.reasons
    assert "insufficient_sensitivity_evidence" in readiness.reasons

    assert result.statistics is not None
    inconsistent = replace(
        result,
        statistics=replace(result.statistics, return_squares_sum=-1.0),
    )
    inconsistent_readiness = evaluate_promotion(
        (inconsistent,),
        (inconsistent, inconsistent),
    )
    assert "invalid_oos_metrics" in inconsistent_readiness.reasons
    assert "invalid_sensitivity_metrics" in inconsistent_readiness.reasons


def test_assumed_funding_is_not_accepted_as_historical_for_promotion():
    source = synthetic_regime_candles(count=1_200, seed=23)
    signals = causal_momentum_signals(source)
    assumed = evaluate(
        source,
        signals,
        initial_equity=10_000,
        execution=ExecutionConfig(
            funding=FundingConfig(
                rate_8h_bps=1,
                source="historical_looking_but_assumed",
                evidence="assumed",
            )
        ),
    )

    readiness = evaluate_promotion((assumed,), (assumed, assumed))

    assert "historical_funding_unavailable" in readiness.reasons


def test_only_timestamped_complete_observations_satisfy_historical_funding_gate():
    source = synthetic_regime_candles(count=1_200, seed=31)
    signals = causal_momentum_signals(source)
    interval = 60 * 60 * 1000
    timestamps = range(interval, source[-1].close_time_ms + 1, interval)
    observations = tuple(FundingRateObservation(timestamp, 1.0) for timestamp in timestamps)
    historical = evaluate(
        source,
        signals,
        initial_equity=10_000,
        execution=ExecutionConfig(
            funding=FundingConfig(
                source="evedex_hourly_export_fixture",
                evidence="historical",
                historical_rates=observations,
            )
        ),
    )

    assert historical.funding_evidence == "historical"
    assert historical.funding_coverage_pct == 100
    readiness = evaluate_promotion((historical,), (historical, historical))
    assert "historical_funding_unavailable" not in readiness.reasons

    partial = evaluate(
        source,
        signals,
        initial_equity=10_000,
        execution=ExecutionConfig(
            funding=FundingConfig(
                source="incomplete_evedex_hourly_export_fixture",
                evidence="historical",
                historical_rates=observations[:-1],
            )
        ),
    )
    assert partial.funding_coverage_pct < 100
    assert "historical_funding_unavailable" in evaluate_promotion((partial,), (partial, partial)).reasons
