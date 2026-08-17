from __future__ import annotations

from dataclasses import replace

import numpy as np
import pytest
from kairos_core.enums import Side
from kairos_quant.candles import Candle

from kairos_backtest.sleeves.range_mean_reversion import (
    RangeMeanReversionConfig,
    _rolling_prior_vwap,
    _wilder_atr,
    generate_range_mean_reversion_intents,
)


def candle(index: int, close: float, *, timeframe: str = "5m", volume: float = 10) -> Candle:
    interval = 300_000 if timeframe == "5m" else 3_600_000
    opened = index * interval
    return Candle(
        symbol="BTCUSDT",
        timeframe=timeframe,
        open_time_ms=opened,
        close_time_ms=opened + interval - 1,
        open=close,
        high=close + 1,
        low=close - 1,
        close=close,
        volume=volume,
        quote_volume=volume * close,
        taker_buy_volume=volume / 2,
    )


def one_minute_range_source(
    last_two_closes: tuple[float, float],
    *,
    five_minute_bars: int = 40,
) -> list[Candle]:
    closes = [100 + (index % 2) * 0.2 for index in range(five_minute_bars)]
    closes[-2:] = last_two_closes
    rows: list[Candle] = []
    for bar, close in enumerate(closes):
        for minute in range(5):
            index = bar * 5 + minute
            rows.append(
                Candle(
                    symbol="BTCUSDT",
                    timeframe="1m",
                    open_time_ms=index * 60_000,
                    close_time_ms=(index + 1) * 60_000 - 1,
                    open=close,
                    high=close + 0.2,
                    low=close - 0.2,
                    close=close,
                    volume=10,
                    quote_volume=10 * close,
                    taker_buy_volume=5,
                )
            )
    return rows


def permissive_config() -> RangeMeanReversionConfig:
    return RangeMeanReversionConfig(
        vwap_lookback_bars=3,
        atr_period=2,
        regime_lookback_hours=2,
        maximum_regime_efficiency=1,
        maximum_abs_hourly_slope=1,
        band_atr_multiple=0.5,
        stop_atr_multiple=1,
        max_hold_bars=6,
    )


def test_prior_vwap_excludes_the_current_decision_bar_and_resets_at_gaps():
    rows = [candle(index, 100 + index) for index in range(5)]
    values = _rolling_prior_vwap(rows, 3)

    assert np.isnan(values[2])
    assert values[3] == pytest.approx(sum(100 + index for index in range(3)) / 3)
    mutated = rows.copy()
    mutated[3] = replace(mutated[3], open=1_000, high=1_001, low=999, close=1_000)
    assert _rolling_prior_vwap(mutated, 3)[3] == values[3]

    gapped = rows[:3] + [replace(rows[3], open_time_ms=2_000_000, close_time_ms=2_299_999)]
    assert np.isnan(_rolling_prior_vwap(gapped, 3)[-1])


def test_atr_never_uses_a_pre_gap_close():
    rows = [candle(index, 100) for index in range(3)]
    rows.append(
        Candle(
            symbol="BTCUSDT",
            timeframe="5m",
            open_time_ms=2_000_000,
            close_time_ms=2_299_999,
            open=1_000,
            high=1_001,
            low=999,
            close=1_000,
            volume=10,
        )
    )

    values = _wilder_atr(rows, 2)

    assert np.isnan(values[-1])


def test_range_reentry_emits_bounded_directional_intent(monkeypatch):
    rows_5m = [candle(index, 100 + (index % 2) * 0.2) for index in range(40)]
    rows_5m[-2] = replace(rows_5m[-2], open=96, high=97, low=95, close=96)
    rows_5m[-1] = replace(rows_5m[-1], open=98, high=99, low=97, close=98)
    rows_1h = [candle(index, 100 + (index % 2) * 0.01, timeframe="1h") for index in range(4)]
    monkeypatch.setattr(
        "kairos_backtest.sleeves.range_mean_reversion.build_timeframes",
        lambda _: {"5m": rows_5m, "1h": rows_1h},
    )
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

    intents = generate_range_mean_reversion_intents([], config)

    assert len(intents) == 1
    intent = intents[0]
    assert intent.side is Side.LONG
    assert intent.entry_eligible_ts_ms == rows_5m[-1].close_time_ms + 1
    assert intent.entry_expires_ts_ms == rows_5m[-1].close_time_ms + 5 * 60 * 1_000
    assert intent.exit_plan.stop_price < intent.reference_price < intent.exit_plan.target_price
    assert intent.exit_plan.max_holding_ms == 30 * 60 * 1_000
    assert intent.gross_reward_bps > 0
    assert dict(intent.metadata)["strategy_version"] == "range_mean_reversion_v1"

    extended = generate_range_mean_reversion_intents(
        [],
        replace(
            config,
            target_atr_extension=0.5,
            minimum_gross_reward_to_risk=2.0,
        ),
    )[0]
    reward = extended.exit_plan.target_price - extended.reference_price
    risk = extended.reference_price - extended.exit_plan.stop_price
    assert extended.exit_plan.target_price > float(dict(extended.metadata)["vwap"])
    assert reward / risk >= 2.0
    assert float(dict(extended.metadata)["gross_reward_to_risk"]) == pytest.approx(reward / risk)


@pytest.mark.parametrize(
    ("last_two", "side"),
    [((96.0, 98.0), Side.LONG), ((104.0, 102.0), Side.SHORT)],
)
def test_real_one_minute_aggregation_emits_long_and_short_geometry(last_two, side):
    source = one_minute_range_source(last_two)

    intents = generate_range_mean_reversion_intents(source, permissive_config())

    assert len(intents) == 1
    intent = intents[0]
    assert intent.side is side
    assert intent.gross_reward_bps == pytest.approx(
        abs(intent.exit_plan.target_price - intent.reference_price) / intent.reference_price * 10_000
    )
    assert intent.entry_expires_ts_ms == intent.decision_ts_ms + 5 * 60 * 1_000
    assert len(intent.intent_id) == 64


def test_incomplete_future_bucket_and_later_prices_cannot_change_prior_intents():
    source = one_minute_range_source((96, 98))
    original = generate_range_mean_reversion_intents(source, permissive_config())
    assert original
    tail: list[Candle] = []
    for offset in range(4):
        index = len(source) + offset
        tail.append(
            Candle(
                symbol="BTCUSDT",
                timeframe="1m",
                open_time_ms=index * 60_000,
                close_time_ms=(index + 1) * 60_000 - 1,
                open=500,
                high=501,
                low=499,
                close=500,
                volume=10,
            )
        )

    with_incomplete_future = generate_range_mean_reversion_intents(
        source + tail,
        permissive_config(),
    )

    assert with_incomplete_future == original


def test_trending_hourly_regime_rejects_range_entry(monkeypatch):
    rows_5m = [candle(index, 100 + (index % 2) * 0.2) for index in range(40)]
    rows_5m[-2] = replace(rows_5m[-2], open=96, high=97, low=95, close=96)
    rows_5m[-1] = replace(rows_5m[-1], open=98, high=99, low=97, close=98)
    rows_1h = [candle(index, 100 + index * 10, timeframe="1h") for index in range(4)]
    monkeypatch.setattr(
        "kairos_backtest.sleeves.range_mean_reversion.build_timeframes",
        lambda _: {"5m": rows_5m, "1h": rows_1h},
    )

    intents = generate_range_mean_reversion_intents(
        [],
        RangeMeanReversionConfig(
            vwap_lookback_bars=3,
            atr_period=2,
            regime_lookback_hours=2,
            maximum_regime_efficiency=0.2,
            maximum_abs_hourly_slope=0.001,
            band_atr_multiple=0.5,
        ),
    )

    assert intents == []


@pytest.mark.parametrize(
    "changes",
    [
        {"vwap_lookback_bars": True},
        {"atr_period": 0},
        {"maximum_regime_efficiency": 1.1},
        {"maximum_regime_efficiency": True},
        {"maximum_abs_hourly_slope": float("nan")},
        {"band_atr_multiple": 0},
        {"stop_atr_multiple": True},
        {"target_atr_extension": 2.01},
        {"minimum_gross_reward_to_risk": True},
        {"minimum_gross_reward_to_risk": 0.99},
    ],
)
def test_invalid_range_configuration_fails_closed(changes):
    with pytest.raises(ValueError):
        RangeMeanReversionConfig(**changes)


@pytest.mark.parametrize("config", [False, 0, ""])
def test_falsey_invalid_range_config_does_not_select_defaults(config):
    with pytest.raises(ValueError, match="RangeMeanReversionConfig"):
        generate_range_mean_reversion_intents([], config)  # type: ignore[arg-type]


def test_pre_gap_hourly_regime_cannot_authorize_a_new_five_minute_segment(monkeypatch):
    old = [candle(index, 100 + (index % 2) * 0.2) for index in range(10)]
    offset = 20_000_000
    new = [
        replace(
            candle(index, 100 + (index % 2) * 0.2),
            open_time_ms=offset + index * 300_000,
            close_time_ms=offset + (index + 1) * 300_000 - 1,
        )
        for index in range(10)
    ]
    new[-2] = replace(new[-2], open=96, high=97, low=95, close=96)
    new[-1] = replace(new[-1], open=98, high=99, low=97, close=98)
    rows_1h = [candle(index, 100 + (index % 2) * 0.01, timeframe="1h") for index in range(4)]
    monkeypatch.setattr(
        "kairos_backtest.sleeves.range_mean_reversion.build_timeframes",
        lambda _: {"5m": old + new, "1h": rows_1h},
    )

    intents = generate_range_mean_reversion_intents(
        [],
        RangeMeanReversionConfig(
            vwap_lookback_bars=3,
            atr_period=2,
            regime_lookback_hours=2,
            maximum_regime_efficiency=1,
            maximum_abs_hourly_slope=1,
            band_atr_multiple=0.5,
        ),
    )

    assert intents == []
