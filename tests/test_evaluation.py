from dataclasses import replace
from datetime import UTC, date, datetime, timedelta

import pytest
from kairos_strategy.candles import Candle

from kairos_backtest.cli import SEGMENT_WARMUP_DAYS, segment_warmup_start, yearly_segments
from kairos_backtest.data import month_starts
from kairos_backtest.evaluation import aggregate_results, evaluate
from kairos_backtest.execution import ExecutionConfig
from kairos_backtest.runner import full_year_bounds
from kairos_backtest.scenarios import Horizon
from kairos_backtest.strategy import generate_signals
from kairos_backtest.timeframes import build_timeframes


def candles(count: int = 48_100, symbol: str = "BTCUSDT") -> list[Candle]:
    start = int(datetime(2025, 1, 1, tzinfo=UTC).timestamp() * 1000)
    result = []
    for index in range(count):
        price = 100 + index * 0.01
        result.append(
            Candle(
                symbol=symbol,
                timeframe="1m",
                open_time_ms=start + index * 60_000,
                close_time_ms=start + (index + 1) * 60_000 - 1,
                open=price,
                high=price + 1,
                low=price - 1,
                close=price + 0.2,
                volume=10,
                quote_volume=1_000,
                taker_buy_volume=6,
            )
        )
    return result


def test_month_sequence_and_full_year_bounds():
    assert month_starts(date(2025, 11, 1), date(2026, 2, 1)) == [
        date(2025, 11, 1),
        date(2025, 12, 1),
        date(2026, 1, 1),
    ]
    assert full_year_bounds(date(2026, 7, 12)) == (
        date(2021, 1, 1),
        date(2025, 1, 1),
        date(2026, 1, 1),
    )


def test_timeframes_only_emit_complete_buckets():
    source = candles(241)
    frames = build_timeframes(source)
    assert len(frames["4h"]) == 1
    assert frames["4h"][0].close == source[239].close


def test_strategy_and_evaluation_are_deterministic():
    source = candles()
    signals = generate_signals(source)
    execution = ExecutionConfig(fee_bps=4, spread_bps=2, slippage_bps=2, latency_ms=250)
    first = evaluate(source, signals, initial_equity=10_000, execution=execution)
    second = evaluate(source, signals, initial_equity=10_000, execution=execution)
    assert first == second
    assert first.benchmark_return_pct > 0
    assert first.trades >= 1


def test_strategy_has_no_signal_before_senior_warmup():
    assert generate_signals(candles(47_999)) == []


def test_five_year_horizon_is_split_into_bounded_years():
    horizon = Horizon("5y", date(2021, 7, 1), date(2026, 7, 1))
    segments = yearly_segments(horizon)
    assert len(segments) == 5
    assert segments[0] == (date(2021, 7, 1), date(2022, 7, 1))
    assert segments[-1] == (date(2025, 7, 1), date(2026, 7, 1))
    assert segment_warmup_start(horizon, segments[0][0]) == horizon.start
    assert segment_warmup_start(horizon, segments[1][0]) == date(2022, 7, 1) - timedelta(
        days=SEGMENT_WARMUP_DAYS
    )


def test_segment_results_are_compounded():
    source = candles()
    signals = generate_signals(source)
    execution = ExecutionConfig(fee_bps=4, spread_bps=2, slippage_bps=2, latency_ms=250)
    segment = evaluate(source, signals, initial_equity=10_000, execution=execution)
    combined = aggregate_results([segment, segment], initial_equity=10_000)
    expected_growth = (1 + segment.return_pct / 100) ** 2
    assert combined.final_equity == 10_000 * expected_growth
    assert combined.trades == segment.trades * 2
    assert combined.statistics is not None
    assert combined.statistics.periods == segment.statistics.periods * 2
    assert combined.metrics.annualized_volatility == segment.metrics.annualized_volatility

    with pytest.raises(ValueError, match="initial equity"):
        aggregate_results([segment], initial_equity=0)
    with pytest.raises(ValueError, match="finite metrics"):
        aggregate_results([replace(segment, return_pct=float("nan"))], initial_equity=10_000)
    with pytest.raises(ValueError, match="growth and equity"):
        aggregate_results([replace(segment, benchmark_return_pct=-100.0)], initial_equity=10_000)
