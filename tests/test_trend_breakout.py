import math
from dataclasses import replace

import pytest
from kairos_core.enums import Side
from kairos_quant.candles import Candle

from kairos_backtest.sleeves.trend_breakout import (
    TrendBreakoutConfig,
    generate_trend_breakout_intents,
)


def _trend_candles(*, count: int = 900, drift: float = 0.0002) -> list[Candle]:
    price = 100.0
    rows: list[Candle] = []
    for index in range(count):
        opened = price
        closed = opened * (1 + drift)
        volume = 100.0
        rows.append(
            Candle(
                symbol="BTCUSDT",
                timeframe="1m",
                open_time_ms=index * 60_000,
                close_time_ms=(index + 1) * 60_000 - 1,
                open=opened,
                high=max(opened, closed),
                low=min(opened, closed),
                close=closed,
                volume=volume,
                quote_volume=volume * closed,
                taker_buy_volume=volume * (0.55 if drift > 0 else 0.45),
            )
        )
        price = closed
    return rows


@pytest.mark.parametrize(("drift", "side"), [(0.0002, Side.LONG), (-0.0002, Side.SHORT)])
def test_breakout_uses_the_current_closed_bar_but_only_the_prior_channel(drift, side):
    source = _trend_candles(drift=drift)

    intents = generate_trend_breakout_intents(source)

    assert intents
    final = intents[-1]
    assert final.decision_ts_ms == source[-1].close_time_ms
    assert final.entry_eligible_ts_ms == source[-1].close_time_ms + 1
    assert final.side is side
    assert final.sleeve_id == "trend_breakout_v1"
    # Including the current bar in its own Donchian channel would make this
    # strictly monotonic close unable to cross the threshold.
    assert final.reference_price == source[-1].close


def test_exit_geometry_edge_and_validity_are_finite_and_bounded():
    intent = generate_trend_breakout_intents(_trend_candles())[-1]
    plan = intent.exit_plan
    metadata = dict(intent.metadata)
    risk_distance = intent.reference_price - plan.stop_price
    reward_distance = plan.target_price - intent.reference_price

    assert plan.max_holding_ms == 2 * 60 * 60 * 1_000
    assert reward_distance / risk_distance == pytest.approx(2.0 / 1.25)
    assert intent.gross_reward_bps == pytest.approx(reward_distance / intent.reference_price * 10_000)
    assert intent.entry_expires_ts_ms == intent.decision_ts_ms + 5 * 60 * 1_000
    assert plan.trailing_activation_price == pytest.approx(intent.reference_price + risk_distance / 1.25)
    assert plan.trailing_distance == pytest.approx(risk_distance / 1.25)
    assert len(metadata["config_sha256"]) == 64
    assert len(metadata["feature_hash"]) == 64
    assert len(intent.intent_id) == 64
    assert all(
        math.isfinite(value)
        for value in (
            intent.reference_price,
            plan.stop_price,
            plan.target_price,
            intent.gross_reward_bps,
        )
    )


def test_intents_are_prefix_invariant_when_only_future_prices_change():
    source = _trend_candles(count=960)
    cutoff = source[899].close_time_ms
    original = [
        intent for intent in generate_trend_breakout_intents(source) if intent.decision_ts_ms <= cutoff
    ]
    changed = list(source)
    for index in range(900, len(changed)):
        row = changed[index]
        changed[index] = replace(
            row,
            open=row.open * 3,
            high=row.high * 3,
            low=row.low * 3,
            close=row.close * 3,
        )
    mutated = [
        intent for intent in generate_trend_breakout_intents(changed) if intent.decision_ts_ms <= cutoff
    ]

    assert original
    assert mutated == original


def test_incomplete_terminal_five_minute_bar_is_never_observed():
    complete = _trend_candles(count=900)
    with_incomplete_tail = _trend_candles(count=904)

    assert generate_trend_breakout_intents(with_incomplete_tail) == (
        generate_trend_breakout_intents(complete)
    )


def test_gap_resets_both_five_minute_and_hourly_feature_history():
    source = _trend_candles(count=1_800)
    gapped = source[:900] + source[901:]
    intents = generate_trend_breakout_intents(gapped)
    gap_start = source[900].open_time_ms
    # The first post-gap hourly regime requires 13 complete observations:
    # 12 changes across the fixed 12-hour lookback.
    regime_recovered = source[1_739].close_time_ms

    assert intents
    assert not [intent for intent in intents if gap_start <= intent.decision_ts_ms < regime_recovered]
    assert any(intent.decision_ts_ms >= regime_recovered for intent in intents)


def test_optional_flow_filters_use_a_closed_bar_and_prior_volume_baseline():
    source = _trend_candles()
    filtered = TrendBreakoutConfig(
        minimum_volume_surprise=2.0,
        minimum_directional_taker_share=0.70,
    )
    assert not generate_trend_breakout_intents(source, filtered)

    surprised = list(source)
    for index in range(895, 900):
        row = surprised[index]
        surprised[index] = replace(
            row,
            volume=1_000.0,
            quote_volume=1_000.0 * row.close,
            taker_buy_volume=800.0,
        )
    intents = generate_trend_breakout_intents(surprised, filtered)

    assert len(intents) == 1
    assert intents[0].decision_ts_ms == surprised[-1].close_time_ms
    assert float(dict(intents[0].metadata)["volume_surprise"]) == pytest.approx(10.0)


def test_breakout_distance_filter_rejects_marginal_channel_crosses():
    source = _trend_candles()
    baseline = generate_trend_breakout_intents(source)
    filtered = generate_trend_breakout_intents(
        source,
        TrendBreakoutConfig(minimum_breakout_atr=2.0),
    )

    assert baseline
    assert filtered == []
    assert float(dict(baseline[-1].metadata)["breakout_distance_atr"]) > 0


def test_repeated_evaluation_produces_identical_intent_hashes():
    source = _trend_candles()

    first = generate_trend_breakout_intents(source)
    second = generate_trend_breakout_intents(list(reversed(source)))

    assert first == second
    assert [intent.intent_id for intent in first] == [intent.intent_id for intent in second]


@pytest.mark.parametrize(
    "config",
    [
        TrendBreakoutConfig(donchian_lookback=1),
        TrendBreakoutConfig(minimum_abs_hourly_slope=0),
        TrendBreakoutConfig(minimum_regime_efficiency=0),
    ],
)
def test_supported_boundary_configuration_remains_finite(config):
    intents = generate_trend_breakout_intents(_trend_candles(), config)

    assert intents
    assert all(math.isfinite(intent.gross_reward_bps) for intent in intents)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"donchian_lookback": 0},
        {"atr_period": True},
        {"minimum_regime_efficiency": float("nan")},
        {"minimum_abs_hourly_slope": -0.1},
        {"minimum_breakout_atr": True},
        {"minimum_breakout_atr": 2.01},
        {"stop_atr_multiple": 0},
        {"trailing_activation_atr_multiple": None},
        {"trailing_distance_atr_multiple": True},
        {"trailing_activation_atr_multiple": 2.0},
        {"minimum_volume_surprise": 0},
        {"minimum_directional_taker_share": 0.49},
    ],
)
def test_invalid_configuration_fails_closed(kwargs):
    with pytest.raises(ValueError):
        TrendBreakoutConfig(**kwargs)


@pytest.mark.parametrize("config", [False, 0, ""])
def test_falsey_invalid_trend_config_does_not_select_defaults(config):
    with pytest.raises(ValueError, match="TrendBreakoutConfig"):
        generate_trend_breakout_intents([], config)  # type: ignore[arg-type]
