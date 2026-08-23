from dataclasses import replace

import pytest
from kairos_core.enums import OrderSide, Side
from kairos_quant.replay import ReplayPoint, ReplayResult
from kairos_strategy.candles import Candle

import kairos_backtest.engine as engine_module
from kairos_backtest import ExecutionConfig, FillSimulator, ReplayClock, run_backtest, split_walk_forward
from kairos_backtest.execution import SimulatedFill, TradeLedger


def candles(count: int = 80) -> list[Candle]:
    result = []
    for index in range(count):
        close = 100 + index * 0.2 + (1 if index % 5 else -1)
        result.append(
            Candle(
                symbol="BTCUSDT",
                timeframe="1m",
                open_time_ms=index * 60_000,
                close_time_ms=index * 60_000 + 59_999,
                open=close - 0.1,
                high=close + 1,
                low=close - 1,
                close=close,
                volume=100,
                quote_volume=10_000,
            )
        )
    return result


def test_clock_cannot_move_backwards():
    clock = ReplayClock()
    clock.advance_to(100)
    try:
        clock.advance_to(99)
    except ValueError:
        pass
    else:
        raise AssertionError("clock accepted backward time")


def test_fill_is_partial_and_costs_are_adverse():
    candle = candles(1)[0]
    fill = FillSimulator(ExecutionConfig(max_volume_participation=0.01), seed=7).fill(
        candle, OrderSide.BUY, 10, available_volume=candle.volume
    )
    assert fill.filled_quantity == 1
    assert fill.price >= candle.open
    assert fill.fee_usd > 0


def test_fill_jitter_cannot_turn_costs_favorable_and_close_reference_is_supported():
    candle = candles(1)[0]
    simulator = FillSimulator(
        ExecutionConfig(spread_bps=0, slippage_bps=0, slippage_jitter_bps=50),
        seed=1,
    )

    buy = simulator.fill(candle, OrderSide.BUY, 1, available_volume=candle.volume)
    sell = simulator.fill(
        candle,
        OrderSide.SELL,
        1,
        available_volume=candle.volume,
        timestamp_ms=candle.close_time_ms,
        reference_price=candle.close,
    )

    assert buy.price >= candle.open
    assert sell.price <= candle.close


def test_trade_ledger_accounts_for_entry_exit_fees_and_realized_pnl():
    ledger = TradeLedger()
    ledger.apply(SimulatedFill(0, OrderSide.BUY, 1, 1, 100, 0.1))
    ledger.apply(SimulatedFill(60_000, OrderSide.SELL, 1, 1, 110, 0.11))

    assert ledger.position == 0
    assert ledger.closed_trade_pnls == [pytest.approx(9.79)]


def test_backtest_is_reproducible():
    data = candles()
    config = ExecutionConfig(slippage_jitter_bps=1)
    first = run_backtest(data, seed=42, execution=config)
    second = run_backtest(data, seed=42, execution=config)
    assert first == second
    assert first.manifest.candles_sha256
    assert first.metrics.max_drawdown >= 0


def test_backtest_canonicalizes_input_order_and_records_boundaries():
    data = candles()
    execution = ExecutionConfig(slippage_jitter_bps=1)

    ordered = run_backtest(data, seed=42, execution=execution)
    reversed_input = run_backtest(reversed(data), seed=42, execution=execution)

    assert ordered == reversed_input
    assert ordered.manifest.actual_start_ms == data[0].open_time_ms
    assert ordered.manifest.actual_end_ms == data[-1].close_time_ms


def test_backtest_seed_changes_stochastic_execution_only_when_requested():
    data = candles()
    execution = ExecutionConfig(slippage_jitter_bps=2)

    first = run_backtest(data, seed=1, execution=execution)
    second = run_backtest(data, seed=2, execution=execution)

    assert first.fills != second.fills
    assert all(fill.timestamp_ms % 60_000 == 0 for fill in first.fills[:-1])


def test_backtest_never_uses_last_candle_as_initial_liquidity(monkeypatch):
    data = candles(2)
    monkeypatch.setattr(
        engine_module,
        "replay_candles",
        lambda _candles: ReplayResult((ReplayPoint(-1, Side.LONG),)),
    )

    report = engine_module.run_backtest(
        data,
        execution=ExecutionConfig(latency_ms=0, max_volume_participation=1),
    )

    assert report.fills[0].timestamp_ms == data[1].open_time_ms


def test_backtest_rejects_an_incomplete_terminal_liquidation(monkeypatch):
    data = candles(2)
    data[-1] = replace(data[-1], volume=0.0)
    monkeypatch.setattr(
        engine_module,
        "replay_candles",
        lambda _candles: ReplayResult((ReplayPoint(-1, Side.LONG),)),
    )

    with pytest.raises(ValueError, match="terminal liquidation"):
        engine_module.run_backtest(
            data,
            execution=ExecutionConfig(latency_ms=0, max_volume_participation=1),
        )


def test_walk_forward_has_no_train_test_overlap():
    folds = split_walk_forward(list(range(100)), train_size=40, test_size=10)
    assert len(folds) == 6
    assert all(fold.train_end == fold.test_start for fold in folds)
    assert all(fold.train_end <= fold.test_start for fold in folds)
