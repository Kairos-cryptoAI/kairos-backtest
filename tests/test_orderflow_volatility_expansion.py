from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, replace

import pytest
from kairos_core.enums import Side
from kairos_quant.candles import Candle

from kairos_backtest.sleeves.orderflow_volatility_expansion import (
    OrderFlowExpansionVariant,
    OrderFlowVolatilityExpansionConfig,
    generate_orderflow_volatility_expansion_intents,
)

_FIVE_MINUTES_MS = 5 * 60 * 1_000


def _directional_taker_volume(volume: float, directional_imbalance: float, side: Side) -> float:
    signed_imbalance = directional_imbalance if side is Side.LONG else -directional_imbalance
    return volume * (1 + signed_imbalance) / 2


def _canonical_number(value: float) -> str:
    normalized = 0.0 if value == 0 else value
    return format(normalized, ".17g")


def _independent_prior_wilder_atr(rows: list[Candle], period: int, signal_index: int) -> float:
    """Test oracle following the written Wilder recurrence, not production code."""

    true_ranges: list[float] = []
    for index, row in enumerate(rows[:signal_index]):
        if index == 0:
            true_ranges.append(row.high - row.low)
        else:
            previous_close = rows[index - 1].close
            true_ranges.append(
                max(
                    row.high - row.low,
                    abs(row.high - previous_close),
                    abs(row.low - previous_close),
                )
            )
    atr = sum(true_ranges[:period]) / period
    for true_range in true_ranges[period:]:
        atr = (atr * (period - 1) + true_range) / period
    return atr


def _flow_profile(variant: OrderFlowExpansionVariant) -> tuple[float, float, float, float]:
    if variant is OrderFlowExpansionVariant.IMPULSE:
        return 0.0, 0.0, 0.0, 0.20
    if variant is OrderFlowExpansionVariant.PERSISTENCE:
        return 0.0, 0.12, 0.12, 0.12
    return -0.10, -0.10, -0.10, 0.20


def _five_minute_source(
    side: Side,
    variant: OrderFlowExpansionVariant,
    *,
    future_bars: int = 0,
) -> list[Candle]:
    flow = _flow_profile(variant)
    rows: list[Candle] = []
    for index in range(72):
        candle_range = 1.5 if index >= 66 else 2.0
        volume = 100.0
        directional_imbalance = flow[index - 69] if index >= 69 else 0.0
        rows.append(
            Candle(
                symbol="BTCUSDT",
                timeframe="5m",
                open_time_ms=index * _FIVE_MINUTES_MS,
                close_time_ms=(index + 1) * _FIVE_MINUTES_MS - 1,
                open=100.0,
                high=100.0 + candle_range / 2,
                low=100.0 - candle_range / 2,
                close=100.0,
                volume=volume,
                quote_volume=volume * 100,
                taker_buy_volume=_directional_taker_volume(
                    volume,
                    directional_imbalance,
                    side,
                ),
            )
        )

    signal_volume = 150.0
    if side is Side.LONG:
        opened, high, low, closed = 100.0, 103.0, 100.0, 102.25
    else:
        opened, high, low, closed = 100.0, 100.0, 97.0, 97.75
    rows.append(
        Candle(
            symbol="BTCUSDT",
            timeframe="5m",
            open_time_ms=72 * _FIVE_MINUTES_MS,
            close_time_ms=73 * _FIVE_MINUTES_MS - 1,
            open=opened,
            high=high,
            low=low,
            close=closed,
            volume=signal_volume,
            quote_volume=signal_volume * closed,
            taker_buy_volume=_directional_taker_volume(signal_volume, flow[-1], side),
        )
    )

    for offset in range(future_bars):
        index = 73 + offset
        rows.append(
            Candle(
                symbol="BTCUSDT",
                timeframe="5m",
                open_time_ms=index * _FIVE_MINUTES_MS,
                close_time_ms=(index + 1) * _FIVE_MINUTES_MS - 1,
                open=closed,
                high=closed + 0.5,
                low=closed - 0.5,
                close=closed,
                volume=100,
                quote_volume=100 * closed,
                taker_buy_volume=50,
            )
        )
    return rows


def _to_one_minute(rows: list[Candle]) -> list[Candle]:
    result: list[Candle] = []
    for row in rows:
        for minute in range(5):
            opened = row.open + (row.close - row.open) * minute / 5
            closed = row.open + (row.close - row.open) * (minute + 1) / 5
            timestamp = row.open_time_ms + minute * 60_000
            result.append(
                Candle(
                    symbol=row.symbol,
                    timeframe="1m",
                    open_time_ms=timestamp,
                    close_time_ms=timestamp + 59_999,
                    open=opened,
                    high=row.high if minute == 0 else max(opened, closed),
                    low=row.low if minute == 0 else min(opened, closed),
                    close=closed,
                    volume=row.volume / 5,
                    quote_volume=row.quote_volume / 5,
                    taker_buy_volume=row.taker_buy_volume / 5,
                )
            )
    return result


def _source(
    side: Side,
    variant: OrderFlowExpansionVariant,
    *,
    future_bars: int = 0,
) -> list[Candle]:
    return _to_one_minute(_five_minute_source(side, variant, future_bars=future_bars))


@pytest.mark.parametrize("side", [Side.LONG, Side.SHORT])
@pytest.mark.parametrize("variant", list(OrderFlowExpansionVariant))
def test_exact_common_and_flow_thresholds_emit_one_bounded_next_open_intent(side, variant):
    source = _source(side, variant)
    config = OrderFlowVolatilityExpansionConfig(variant=variant)

    intents = generate_orderflow_volatility_expansion_intents(source, config)

    assert len(intents) == 1
    intent = intents[0]
    metadata = dict(intent.metadata)
    risk = abs(intent.reference_price - intent.exit_plan.stop_price)
    reward = abs(intent.exit_plan.target_price - intent.reference_price)
    assert intent.sleeve_id == "orderflow_volatility_expansion_v1"
    assert intent.side is side
    assert intent.decision_ts_ms == 73 * _FIVE_MINUTES_MS - 1
    assert intent.entry_eligible_ts_ms == 73 * _FIVE_MINUTES_MS
    assert intent.entry_expires_ts_ms == 74 * _FIVE_MINUTES_MS - 1
    assert intent.exit_plan.max_holding_ms == 12 * _FIVE_MINUTES_MS
    assert intent.exit_plan.trailing_activation_price is None
    assert intent.exit_plan.trailing_distance is None
    assert reward / risk == pytest.approx(3.0)
    assert float(metadata["compression_ratio"]) == pytest.approx(0.75)
    assert float(metadata["range_expansion"]) == pytest.approx(1.5)
    assert float(metadata["volume_surprise"]) == pytest.approx(1.5)
    assert float(metadata["close_location"]) == pytest.approx(0.75)
    directional_imbalance = _flow_profile(variant)[-1]
    expected_raw_imbalance = directional_imbalance if side is Side.LONG else -directional_imbalance
    assert float(metadata["current_imbalance"]) == pytest.approx(expected_raw_imbalance)
    assert metadata["assigned_variant"] == variant.value
    assert metadata["variant"] == variant.value
    assert len(metadata["config_sha256"]) == 64
    assert len(metadata["feature_hash"]) == 64
    assert len(intent.intent_id) == 64
    assert 0 <= intent.signal_strength <= 1


def test_raw_predicate_overlaps_have_one_owner_at_a_fixed_priority():
    flip_source = _source(Side.LONG, OrderFlowExpansionVariant.FLIP_RELEASE)
    flip_results = {
        variant: generate_orderflow_volatility_expansion_intents(
            flip_source,
            OrderFlowVolatilityExpansionConfig(variant=variant),
        )
        for variant in OrderFlowExpansionVariant
    }

    assert not flip_results[OrderFlowExpansionVariant.IMPULSE]
    assert not flip_results[OrderFlowExpansionVariant.PERSISTENCE]
    assert len(flip_results[OrderFlowExpansionVariant.FLIP_RELEASE]) == 1
    flip_metadata = dict(flip_results[OrderFlowExpansionVariant.FLIP_RELEASE][0].metadata)
    assert flip_metadata["flip_release_raw"] == "1"
    assert flip_metadata["impulse_raw"] == "1"
    assert flip_metadata["assigned_variant"] == "flip_release"

    persistent_rows = _five_minute_source(Side.LONG, OrderFlowExpansionVariant.PERSISTENCE)
    signal = persistent_rows[-1]
    persistent_rows[-1] = replace(
        signal,
        taker_buy_volume=_directional_taker_volume(signal.volume, 0.20, Side.LONG),
    )
    persistence_source = _to_one_minute(persistent_rows)
    persistence_results = {
        variant: generate_orderflow_volatility_expansion_intents(
            persistence_source,
            OrderFlowVolatilityExpansionConfig(variant=variant),
        )
        for variant in OrderFlowExpansionVariant
    }
    assert not persistence_results[OrderFlowExpansionVariant.IMPULSE]
    assert len(persistence_results[OrderFlowExpansionVariant.PERSISTENCE]) == 1
    assert not persistence_results[OrderFlowExpansionVariant.FLIP_RELEASE]
    persistence_metadata = dict(persistence_results[OrderFlowExpansionVariant.PERSISTENCE][0].metadata)
    assert persistence_metadata["persistence_raw"] == "1"
    assert persistence_metadata["impulse_raw"] == "1"
    assert persistence_metadata["assigned_variant"] == "persistence"


def test_flip_release_is_never_reclassified_as_an_impulse():
    source = _source(Side.SHORT, OrderFlowExpansionVariant.FLIP_RELEASE)

    assert not generate_orderflow_volatility_expansion_intents(
        source,
        OrderFlowVolatilityExpansionConfig(variant=OrderFlowExpansionVariant.IMPULSE),
    )


def test_prefix_invariance_and_incomplete_terminal_bucket():
    source = _source(Side.LONG, OrderFlowExpansionVariant.IMPULSE, future_bars=8)
    cutoff = 73 * _FIVE_MINUTES_MS - 1
    original = [
        intent
        for intent in generate_orderflow_volatility_expansion_intents(source)
        if intent.decision_ts_ms <= cutoff
    ]
    changed = list(source)
    future_start = 73 * 5
    for index in range(future_start, len(changed)):
        row = changed[index]
        changed[index] = replace(
            row,
            open=row.open * 2,
            high=row.high * 2,
            low=row.low * 2,
            close=row.close * 2,
        )
    mutated = [
        intent
        for intent in generate_orderflow_volatility_expansion_intents(changed)
        if intent.decision_ts_ms <= cutoff
    ]
    assert original
    assert mutated == original

    complete = _source(Side.LONG, OrderFlowExpansionVariant.IMPULSE)
    tail = []
    for offset in range(4):
        timestamp = len(complete) * 60_000 + offset * 60_000
        tail.append(
            Candle(
                symbol="BTCUSDT",
                timeframe="1m",
                open_time_ms=timestamp,
                close_time_ms=timestamp + 59_999,
                open=500,
                high=600,
                low=400,
                close=550,
                volume=10_000,
                quote_volume=5_500_000,
                taker_buy_volume=10_000,
            )
        )
    assert generate_orderflow_volatility_expansion_intents(complete + tail) == (
        generate_orderflow_volatility_expansion_intents(complete)
    )


def test_gap_resets_all_features_including_prior_wilder_atr():
    source = _source(Side.LONG, OrderFlowExpansionVariant.IMPULSE)
    del source[60 * 5]

    assert generate_orderflow_volatility_expansion_intents(source) == []


def test_zero_volume_bar_resets_state_and_requires_a_full_72_bar_restart():
    rows = _five_minute_source(Side.LONG, OrderFlowExpansionVariant.IMPULSE)
    rows[0] = replace(
        rows[0],
        volume=0,
        quote_volume=0,
        taker_buy_volume=0,
    )
    assert generate_orderflow_volatility_expansion_intents(_to_one_minute(rows)) == []

    zero = rows[0]
    shifted = [
        replace(
            row,
            open_time_ms=row.open_time_ms + _FIVE_MINUTES_MS,
            close_time_ms=row.close_time_ms + _FIVE_MINUTES_MS,
        )
        for row in _five_minute_source(Side.LONG, OrderFlowExpansionVariant.IMPULSE)
    ]
    recovered = generate_orderflow_volatility_expansion_intents(_to_one_minute([zero, *shifted]))

    assert len(recovered) == 1
    assert recovered[0].decision_ts_ms == 74 * _FIVE_MINUTES_MS - 1


def test_signal_uses_prior_atr_and_prior_only_medians():
    rows = _five_minute_source(Side.LONG, OrderFlowExpansionVariant.IMPULSE)
    prior_atr = _independent_prior_wilder_atr(rows, 24, signal_index=72)
    intent = generate_orderflow_volatility_expansion_intents(_to_one_minute(rows))[0]
    metadata = dict(intent.metadata)

    assert abs(intent.reference_price - intent.exit_plan.stop_price) == pytest.approx(1.25 * prior_atr)
    assert float(metadata["atr_bps"]) == pytest.approx(prior_atr / rows[-2].close * 10_000)
    assert float(metadata["range_expansion"]) == pytest.approx(3.0 / 2.0)
    assert float(metadata["volume_surprise"]) == pytest.approx(150.0 / 100.0)


def test_prior_atr_bps_boundary_is_mirrored_for_long_and_short_signals():
    expected: list[float] = []
    for side in (Side.LONG, Side.SHORT):
        rows = _five_minute_source(side, OrderFlowExpansionVariant.IMPULSE)
        prior_atr = _independent_prior_wilder_atr(rows, 24, signal_index=72)
        atr_bps = prior_atr / rows[-2].close * 10_000
        expected.append(atr_bps)
        config = OrderFlowVolatilityExpansionConfig(
            minimum_atr_bps=atr_bps,
            maximum_atr_bps=atr_bps,
        )

        intents = generate_orderflow_volatility_expansion_intents(_to_one_minute(rows), config)

        assert len(intents) == 1
        assert float(dict(intents[0].metadata)["atr_bps"]) == pytest.approx(atr_bps)
    assert expected[0] == pytest.approx(expected[1])


def test_zero_denominators_and_nonfinite_evidence_fail_closed():
    rows = _five_minute_source(Side.LONG, OrderFlowExpansionVariant.IMPULSE)
    for index in range(48, 72):
        rows[index] = replace(
            rows[index],
            volume=0.0,
            quote_volume=0.0,
            taker_buy_volume=0.0,
        )
    assert generate_orderflow_volatility_expansion_intents(_to_one_minute(rows)) == []

    with pytest.raises(ValueError):
        OrderFlowVolatilityExpansionConfig(minimum_range_expansion=float("nan"))


def test_config_and_feature_changes_are_reflected_in_hashes():
    source = _source(Side.LONG, OrderFlowExpansionVariant.IMPULSE)
    baseline_config = OrderFlowVolatilityExpansionConfig()
    changed_config = replace(baseline_config, target_reward_to_risk=3.25)
    baseline = generate_orderflow_volatility_expansion_intents(source, baseline_config)
    changed = generate_orderflow_volatility_expansion_intents(source, changed_config)

    assert len(baseline) == len(changed) == 1
    assert baseline_config.fingerprint != changed_config.fingerprint
    assert dict(baseline[0].metadata)["feature_hash"] != dict(changed[0].metadata)["feature_hash"]
    assert baseline[0].intent_id != changed[0].intent_id

    rows = _five_minute_source(Side.LONG, OrderFlowExpansionVariant.IMPULSE)
    signal = rows[-1]
    rows[-1] = replace(
        signal,
        close=102.30,
        quote_volume=signal.volume * 102.30,
    )
    feature_changed = generate_orderflow_volatility_expansion_intents(_to_one_minute(rows))
    assert len(feature_changed) == 1
    assert dict(feature_changed[0].metadata)["feature_hash"] != dict(baseline[0].metadata)["feature_hash"]


def test_config_and_feature_hashes_match_an_independent_canonical_json_oracle():
    rows = _five_minute_source(Side.LONG, OrderFlowExpansionVariant.IMPULSE)
    config = OrderFlowVolatilityExpansionConfig()
    intent = generate_orderflow_volatility_expansion_intents(_to_one_minute(rows), config)[0]
    metadata = dict(intent.metadata)
    config_json = json.dumps(
        asdict(config),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode()
    config_sha256 = hashlib.sha256(config_json).hexdigest()
    prior_atr = _independent_prior_wilder_atr(rows, 24, signal_index=72)
    reference = 102.25
    risk = 1.25 * prior_atr
    persistence_imbalance = 30.0 / 350.0
    payload: dict[str, str | int] = {
        "assigned_variant": "impulse",
        "atr_bps": _canonical_number(prior_atr / 100.0 * 10_000),
        "body_fraction": _canonical_number(0.75),
        "close_location": _canonical_number(0.75),
        "compression_ratio": _canonical_number(0.75),
        "config_sha256": config_sha256,
        "current_imbalance": _canonical_number(0.20),
        "current_directional_imbalance": _canonical_number(0.20),
        "decision_ts_ms": 73 * _FIVE_MINUTES_MS - 1,
        "flip_prior_directional_imbalance": _canonical_number(0.0),
        "flip_release_raw": 0,
        "impulse_raw": 1,
        "persistence_directional_bars": 1,
        "persistence_directional_imbalance": _canonical_number(persistence_imbalance),
        "persistence_raw": 0,
        "prior_directional_imbalance": _canonical_number(0.0),
        "range_expansion": _canonical_number(1.5),
        "reference_price": _canonical_number(reference),
        "side": Side.LONG.value,
        "stop_price": _canonical_number(reference - risk),
        "strategy_version": "orderflow_volatility_expansion_v1",
        "symbol": "BTCUSDT",
        "target_price": _canonical_number(reference + 3 * risk),
        "volume_surprise": _canonical_number(1.5),
    }
    expected_feature_hash = hashlib.sha256(
        json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        ).encode()
    ).hexdigest()

    assert config.fingerprint == config_sha256
    assert metadata["config_sha256"] == config_sha256
    assert metadata["feature_hash"] == expected_feature_hash
    assert metadata["flip_prior_directional_imbalance"] == "0"


def test_reversed_input_is_deterministic():
    source = _source(Side.SHORT, OrderFlowExpansionVariant.PERSISTENCE)
    config = OrderFlowVolatilityExpansionConfig(variant=OrderFlowExpansionVariant.PERSISTENCE)

    first = generate_orderflow_volatility_expansion_intents(source, config)
    second = generate_orderflow_volatility_expansion_intents(list(reversed(source)), config)

    assert first == second
    assert [dict(intent.metadata)["feature_hash"] for intent in first] == [
        dict(intent.metadata)["feature_hash"] for intent in second
    ]


@pytest.mark.parametrize(
    "boundary",
    [
        "compression",
        "range_expansion",
        "volume_surprise",
        "close_location",
        "body_fraction",
        "atr_below",
        "atr_above",
    ],
)
def test_just_outside_each_common_boundary_is_rejected(boundary):
    rows = _five_minute_source(Side.LONG, OrderFlowExpansionVariant.IMPULSE)
    signal = rows[-1]
    if boundary == "compression":
        for index in range(66, 72):
            rows[index] = replace(rows[index], high=100.7501, low=99.2499)
    elif boundary == "range_expansion":
        rows[-1] = replace(signal, high=102.999)
    elif boundary == "volume_surprise":
        volume = 149.9
        rows[-1] = replace(
            signal,
            volume=volume,
            quote_volume=volume * signal.close,
            taker_buy_volume=_directional_taker_volume(volume, 0.20, Side.LONG),
        )
    elif boundary == "close_location":
        rows[-1] = replace(signal, close=102.249, quote_volume=signal.volume * 102.249)
    elif boundary == "body_fraction":
        rows[-1] = replace(signal, open=101.201)
    else:
        scale = 0.1 if boundary == "atr_below" else 1.5
        rows = [
            replace(
                row,
                open=100 + (row.open - 100) * scale,
                high=100 + (row.high - 100) * scale,
                low=100 + (row.low - 100) * scale,
                close=100 + (row.close - 100) * scale,
                quote_volume=row.volume * (100 + (row.close - 100) * scale),
            )
            for row in rows
        ]

    assert generate_orderflow_volatility_expansion_intents(_to_one_minute(rows)) == []


@pytest.mark.parametrize("variant", list(OrderFlowExpansionVariant))
def test_just_outside_each_flow_boundary_is_rejected(variant):
    rows = _five_minute_source(Side.LONG, variant)
    if variant is OrderFlowExpansionVariant.IMPULSE:
        signal = rows[-1]
        rows[-1] = replace(
            signal,
            taker_buy_volume=_directional_taker_volume(signal.volume, 0.199, Side.LONG),
        )
    elif variant is OrderFlowExpansionVariant.PERSISTENCE:
        signal = rows[-1]
        rows[-1] = replace(
            signal,
            taker_buy_volume=_directional_taker_volume(signal.volume, 0.119, Side.LONG),
        )
    else:
        for index in range(69, 72):
            row = rows[index]
            rows[index] = replace(
                row,
                taker_buy_volume=_directional_taker_volume(row.volume, -0.099, Side.LONG),
            )

    config = OrderFlowVolatilityExpansionConfig(variant=variant)
    assert generate_orderflow_volatility_expansion_intents(_to_one_minute(rows), config) == []


@pytest.mark.parametrize(
    "changes",
    [
        {"variant": "impulse"},
        {"baseline_lookback": True},
        {"baseline_lookback": 0},
        {"compression_short_lookback": 72},
        {"compression_long_lookback": 23},
        {"atr_period": 0},
        {"persistence_minimum_directional_bars": 4},
        {"maximum_compression_ratio": 1.01},
        {"minimum_range_expansion": 0.99},
        {"minimum_volume_surprise": 0.99},
        {"minimum_close_location": 1.01},
        {"minimum_body_fraction": 0.0},
        {"minimum_atr_bps": 251},
        {"maximum_atr_bps": 24},
        {"minimum_impulse_directional_imbalance": 1.01},
        {"minimum_persistence_directional_imbalance": True},
        {"maximum_flip_prior_directional_imbalance": 0.0},
        {"maximum_flip_prior_directional_imbalance": -1.01},
        {"minimum_flip_current_imbalance": float("inf")},
        {"stop_atr_multiple": 0},
        {"target_reward_to_risk": True},
        {"max_hold_bars": 0},
    ],
)
def test_invalid_configuration_fails_closed(changes):
    with pytest.raises(ValueError):
        OrderFlowVolatilityExpansionConfig(**changes)


@pytest.mark.parametrize("invalid", [False, 0, "", object()])
def test_generator_rejects_invalid_config_objects(invalid):
    with pytest.raises(ValueError, match="OrderFlowVolatilityExpansionConfig or None"):
        generate_orderflow_volatility_expansion_intents([], invalid)  # type: ignore[arg-type]
