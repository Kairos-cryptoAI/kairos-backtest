from __future__ import annotations

from kairos_strategy.candles import Candle
from kairos_strategy.runtime import (
    candle_to_closed_bar,
    canonical_intent_batch_bytes,
    generate_runtime_strategy_intents,
)
from kairos_strategy.sleeves import RangeMeanReversionConfig

from kairos_backtest.parity import generate_research_intents


def _frozen_stream() -> tuple[Candle, ...]:
    closes = [100 + (index % 2) * 0.2 for index in range(40)]
    closes[-2:] = [96.0, 98.0]
    return tuple(
        Candle(
            symbol="BTCUSDT",
            timeframe="1m",
            open_time_ms=index * 60_000,
            close_time_ms=(index + 1) * 60_000 - 1,
            open=closes[index // 5],
            high=closes[index // 5] + 0.2,
            low=closes[index // 5] - 0.2,
            close=closes[index // 5],
            volume=10.0,
            quote_volume=10.0 * closes[index // 5],
            taker_buy_volume=5.0,
            taker_buy_quote_volume=5.0 * closes[index // 5],
        )
        for index in range(200)
    )


def test_backtest_adapter_and_runtime_wrapper_are_byte_for_byte_identical():
    config = RangeMeanReversionConfig(
        vwap_lookback_bars=3,
        atr_period=2,
        regime_lookback_hours=2,
        maximum_regime_efficiency=1,
        maximum_abs_hourly_slope=1,
        band_atr_multiple=0.5,
        stop_atr_multiple=1,
        max_hold_bars=6,
    )
    stream = _frozen_stream()

    research = generate_research_intents("range_mean_reversion_v1", stream, config)
    runtime = generate_runtime_strategy_intents(
        "range_mean_reversion_v1",
        tuple(candle_to_closed_bar(candle) for candle in reversed(stream)),
        config,
    )

    assert research
    assert [intent.intent_id for intent in research] == [intent.intent_id for intent in runtime]
    assert canonical_intent_batch_bytes(research) == canonical_intent_batch_bytes(runtime)
