from datetime import date

from kairos_core.enums import Side
from kairos_quant.candles import Candle

from kairos_backtest.data import month_starts
from kairos_backtest.evaluation import evaluate
from kairos_backtest.scenarios import BASELINE, default_horizons
from kairos_backtest.strategy import Signal
from kairos_backtest.timeframes import aggregate


def candles(count=300):
    return [
        Candle(
            "BTCUSDT",
            "1m",
            i * 60_000,
            (i + 1) * 60_000 - 1,
            100 + i / 10,
            101 + i / 10,
            99 + i / 10,
            100.5 + i / 10,
            100,
            10_000,
            55,
        )
        for i in range(count)
    ]


def test_month_ranges_and_horizons_are_deterministic():
    assert len(month_starts(date(2025, 1, 1), date(2025, 4, 1))) == 3
    horizons = default_horizons(date(2026, 7, 12))
    assert horizons[0].start == date(2021, 7, 1)
    assert horizons[1].start == date(2025, 7, 1)


def test_aggregation_uses_complete_closed_buckets():
    result = aggregate(candles(10), "5m")
    assert len(result) == 2
    assert result[0].volume == 500
    assert result[0].close == candles()[4].close


def test_evaluation_is_reproducible_and_charges_costs():
    data = candles()
    signals = [
        Signal(data[10].close_time_ms, Side.LONG, 1.0, ("test",)),
        Signal(data[200].close_time_ms, Side.FLAT, 0.0, ("exit",)),
    ]
    first = evaluate(data, signals, initial_equity=10_000, execution=BASELINE)
    second = evaluate(data, signals, initial_equity=10_000, execution=BASELINE)
    assert first == second
    assert first.fees_usd > 0
    assert first.turnover_usd > 0
