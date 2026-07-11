from kairos_core.enums import OrderSide
from kairos_quant.candles import Candle

from kairos_backtest import ExecutionConfig, FillSimulator, ReplayClock, run_backtest, split_walk_forward


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
        candle, OrderSide.BUY, 10
    )
    assert fill.filled_quantity == 1
    assert fill.price >= candle.open
    assert fill.fee_usd > 0


def test_backtest_is_reproducible():
    data = candles()
    config = ExecutionConfig(slippage_jitter_bps=1)
    first = run_backtest(data, seed=42, execution=config)
    second = run_backtest(data, seed=42, execution=config)
    assert first == second
    assert first.manifest.candles_sha256
    assert first.metrics.max_drawdown >= 0


def test_walk_forward_has_no_train_test_overlap():
    folds = split_walk_forward(list(range(100)), train_size=40, test_size=10)
    assert len(folds) == 6
    assert all(fold.train_end == fold.test_start for fold in folds)
    assert all(fold.train_end <= fold.test_start for fold in folds)
