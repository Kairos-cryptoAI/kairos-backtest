from __future__ import annotations

from dataclasses import replace

import pytest
from kairos_core.enums import Side
from kairos_quant.candles import Candle

from kairos_backtest.sleeves.trend_pullback_reclaim import (
    PullbackDepthVariant,
    TrendPullbackReclaimConfig,
    _feature_hash,
    _number,
    _segmented_ema,
    _wilder_atr,
    generate_trend_pullback_reclaim_intents,
)

_FIVE_MINUTES_MS = 5 * 60 * 1_000


def _five_minute_source(
    side: Side,
    depth_atr: float,
    *,
    count: int = 960,
    signal_index: int | None = None,
) -> tuple[list[Candle], int]:
    direction = 1 if side is Side.LONG else -1
    signal = count - 1 if signal_index is None else signal_index
    if not 900 <= signal < count:
        raise ValueError("the fixture needs a warmed-up signal inside its source")
    start_price = 100.0 if side is Side.LONG else 200.0
    rows: list[Candle] = []
    previous_close = start_price
    for index in range(count):
        opened = previous_close
        closed = opened + direction * 0.02
        rows.append(
            Candle(
                symbol="BTCUSDT",
                timeframe="5m",
                open_time_ms=index * _FIVE_MINUTES_MS,
                close_time_ms=(index + 1) * _FIVE_MINUTES_MS - 1,
                open=opened,
                high=max(opened, closed) + 0.4,
                low=min(opened, closed) - 0.4,
                close=closed,
                volume=50,
                quote_volume=50 * closed,
                taker_buy_volume=25,
            )
        )
        previous_close = closed

    previous_index = signal - 1
    ema20 = float(_segmented_ema(rows, 20, _FIVE_MINUTES_MS)[previous_index])
    previous = rows[previous_index]
    for _ in range(20):
        previous_atr = float(_wilder_atr(rows, 14)[previous_index])
        if side is Side.LONG:
            rows[previous_index] = replace(
                previous,
                high=previous.close + 0.005,
                low=ema20 - depth_atr * previous_atr,
            )
        else:
            rows[previous_index] = replace(
                previous,
                high=ema20 + depth_atr * previous_atr,
                low=previous.close - 0.005,
            )
    return rows, signal


def _to_one_minute(rows_5m: list[Candle]) -> list[Candle]:
    rows_1m: list[Candle] = []
    for row in rows_5m:
        for minute in range(5):
            fraction_open = minute / 5
            fraction_close = (minute + 1) / 5
            opened = row.open + (row.close - row.open) * fraction_open
            closed = row.open + (row.close - row.open) * fraction_close
            timestamp = row.open_time_ms + minute * 60_000
            rows_1m.append(
                Candle(
                    symbol=row.symbol,
                    timeframe="1m",
                    open_time_ms=timestamp,
                    close_time_ms=timestamp + 59_999,
                    open=opened,
                    high=row.high if minute == 0 else max(opened, closed),
                    low=row.low if minute == 0 else min(opened, closed),
                    close=closed,
                    volume=10,
                    quote_volume=10 * closed,
                    taker_buy_volume=5,
                )
            )
    return rows_1m


def _source(
    side: Side,
    depth_atr: float,
    *,
    count: int = 960,
    signal_index: int | None = None,
) -> tuple[list[Candle], int]:
    rows, signal = _five_minute_source(
        side,
        depth_atr,
        count=count,
        signal_index=signal_index,
    )
    return _to_one_minute(rows), signal


def _closed_rows(
    closes: list[float],
    *,
    start_time_ms: int = 0,
    high_offsets: list[float] | None = None,
    low_offsets: list[float] | None = None,
) -> list[Candle]:
    highs = high_offsets or [0.0] * len(closes)
    lows = low_offsets or [0.0] * len(closes)
    return [
        Candle(
            symbol="BTCUSDT",
            timeframe="5m",
            open_time_ms=start_time_ms + index * _FIVE_MINUTES_MS,
            close_time_ms=start_time_ms + (index + 1) * _FIVE_MINUTES_MS - 1,
            open=close,
            high=close + highs[index],
            low=close - lows[index],
            close=close,
            volume=1,
        )
        for index, close in enumerate(closes)
    ]


def test_ema_matches_an_independent_sma_seeded_known_vector_and_resets_after_gap():
    first = _closed_rows([10.0, 11.0, 12.0, 13.0])
    second = _closed_rows(
        [20.0, 22.0, 24.0, 26.0],
        start_time_ms=first[-1].close_time_ms + 1 + _FIVE_MINUTES_MS,
    )

    values = _segmented_ema(first + second, period=3, interval_ms=_FIVE_MINUTES_MS)

    assert values[:4].tolist() == pytest.approx([float("nan"), float("nan"), 11.0, 12.0], nan_ok=True)
    assert values[4:].tolist() == pytest.approx([float("nan"), float("nan"), 22.0, 24.0], nan_ok=True)


def test_wilder_atr_matches_an_independent_true_range_known_vector_and_resets_after_gap():
    first = _closed_rows(
        [10.0, 10.0, 10.0, 10.0],
        high_offsets=[1.0, 4.0, 6.0, 8.0],
        low_offsets=[1.0, 0.0, 0.0, 0.0],
    )
    second = _closed_rows(
        [20.0, 20.0, 20.0, 20.0],
        start_time_ms=first[-1].close_time_ms + 1 + _FIVE_MINUTES_MS,
        high_offsets=[1.0, 2.0, 2.0, 4.0],
        low_offsets=[1.0, 0.0, 0.0, 0.0],
    )

    values = _wilder_atr(first + second, period=3)

    assert values[:4].tolist() == pytest.approx(
        [float("nan"), float("nan"), 4.0, 16.0 / 3.0],
        nan_ok=True,
    )
    assert values[4:].tolist() == pytest.approx(
        [float("nan"), float("nan"), 2.0, 8.0 / 3.0],
        nan_ok=True,
    )


@pytest.mark.parametrize(
    ("side", "variant", "depth"),
    [
        (Side.LONG, PullbackDepthVariant.SHALLOW, 0.25),
        (Side.LONG, PullbackDepthVariant.MEDIUM, 0.75),
        (Side.LONG, PullbackDepthVariant.DEEP, 1.25),
        (Side.SHORT, PullbackDepthVariant.SHALLOW, 0.25),
        (Side.SHORT, PullbackDepthVariant.MEDIUM, 0.75),
        (Side.SHORT, PullbackDepthVariant.DEEP, 1.25),
    ],
)
def test_real_one_minute_aggregation_emits_mutually_exclusive_bounded_intent(
    side,
    variant,
    depth,
):
    source, signal_index = _source(side, depth)
    matching = generate_trend_pullback_reclaim_intents(
        source,
        TrendPullbackReclaimConfig(depth_variant=variant),
    )

    assert len(matching) == 1
    intent = matching[0]
    metadata = dict(intent.metadata)
    risk = abs(intent.reference_price - intent.exit_plan.stop_price)
    reward = abs(intent.exit_plan.target_price - intent.reference_price)
    assert intent.side is side
    assert intent.sleeve_id == "trend_pullback_reclaim_v1"
    assert intent.decision_ts_ms == (signal_index + 1) * _FIVE_MINUTES_MS - 1
    assert intent.entry_eligible_ts_ms == intent.decision_ts_ms + 1
    assert intent.entry_expires_ts_ms == intent.decision_ts_ms + _FIVE_MINUTES_MS
    assert intent.exit_plan.max_holding_ms == 3 * 60 * 60 * 1_000
    assert intent.exit_plan.trailing_activation_price is None
    assert intent.exit_plan.trailing_distance is None
    assert reward / risk == pytest.approx(2.0)
    assert 0.5 <= float(metadata["risk_atr"]) <= 2.0
    assert float(metadata["depth_atr"]) == pytest.approx(depth)
    assert metadata["depth_variant"] == variant.value
    assert len(metadata["config_sha256"]) == 64
    assert len(metadata["feature_hash"]) == 64
    assert len(intent.intent_id) == 64

    for other in PullbackDepthVariant:
        if other is variant:
            continue
        assert not generate_trend_pullback_reclaim_intents(
            source,
            TrendPullbackReclaimConfig(depth_variant=other),
        )


@pytest.mark.parametrize(
    ("depth", "owner"),
    [
        (0.0, PullbackDepthVariant.SHALLOW),
        (0.5, PullbackDepthVariant.MEDIUM),
        (1.0, PullbackDepthVariant.DEEP),
        (1.5, PullbackDepthVariant.DEEP),
    ],
)
def test_shared_depth_boundaries_have_exactly_one_owner(depth, owner):
    owners = [variant for variant in PullbackDepthVariant if variant.contains(depth)]

    assert owners == [owner]


@pytest.mark.parametrize("depth", [-0.0001, 1.5001, float("nan"), True])
def test_unsupported_depths_have_no_owner(depth):
    assert not any(variant.contains(depth) for variant in PullbackDepthVariant)


def test_stop_distance_bounds_fail_closed_on_both_sides():
    source, _ = _source(Side.LONG, 0.75)
    config = TrendPullbackReclaimConfig(depth_variant=PullbackDepthVariant.MEDIUM)
    baseline = generate_trend_pullback_reclaim_intents(source, config)
    assert len(baseline) == 1
    risk_atr = float(dict(baseline[0].metadata)["risk_atr"])

    too_tight = replace(
        config,
        minimum_stop_distance_atr=risk_atr + 0.01,
    )
    too_wide = replace(
        config,
        maximum_stop_distance_atr=risk_atr - 0.01,
    )

    assert not generate_trend_pullback_reclaim_intents(source, too_tight)
    assert not generate_trend_pullback_reclaim_intents(source, too_wide)


def test_prefix_invariance_and_incomplete_terminal_bucket():
    source, signal_index = _source(Side.LONG, 0.75, count=996, signal_index=960)
    cutoff = (signal_index + 1) * _FIVE_MINUTES_MS - 1
    original = [
        intent
        for intent in generate_trend_pullback_reclaim_intents(source)
        if intent.decision_ts_ms <= cutoff
    ]
    changed = list(source)
    future_start = (signal_index + 1) * 5
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
        for intent in generate_trend_pullback_reclaim_intents(changed)
        if intent.decision_ts_ms <= cutoff
    ]
    assert original
    assert mutated == original

    final_source, _ = _source(Side.LONG, 0.75)
    tail = []
    for offset in range(4):
        timestamp = len(final_source) * 60_000 + offset * 60_000
        tail.append(
            Candle(
                symbol="BTCUSDT",
                timeframe="1m",
                open_time_ms=timestamp,
                close_time_ms=timestamp + 59_999,
                open=500,
                high=501,
                low=499,
                close=500,
                volume=10,
            )
        )
    assert generate_trend_pullback_reclaim_intents(final_source + tail) == (
        generate_trend_pullback_reclaim_intents(final_source)
    )


def test_gap_resets_features_and_cannot_reuse_a_stale_hourly_regime():
    source, _ = _source(Side.LONG, 0.75)
    # A one-minute hole six hours before the signal invalidates both aggregate
    # segments. Neither the 72-hour hourly EMA nor the 50-bar five-minute EMA
    # is allowed to retain pre-gap state.
    del source[-6 * 60]

    assert generate_trend_pullback_reclaim_intents(source) == []


def test_signal_recovers_only_after_complete_post_gap_feature_warmup():
    source, signal_index = _source(Side.LONG, 0.75, count=1_200)
    del source[6 * 60]

    intents = generate_trend_pullback_reclaim_intents(source)

    assert len(intents) == 1
    assert intents[0].decision_ts_ms == (signal_index + 1) * _FIVE_MINUTES_MS - 1


def test_numeric_evidence_normalizes_signed_zero_and_has_a_known_canonical_hash():
    expected = "7c361a64817f0c03cfe5adafd656b1dfa390c2230cd37e9786f505f2f8e1693b"
    positive_zero = {"decision_ts_ms": 1, "value": _number(0.0)}
    negative_zero = {"decision_ts_ms": 1, "value": _number(-0.0)}

    assert positive_zero == negative_zero == {"decision_ts_ms": 1, "value": "0"}
    assert _feature_hash(positive_zero) == expected
    assert _feature_hash(negative_zero) == expected
    assert _feature_hash({"decision_ts_ms": 1, "value": _number(1.0)}) != expected


def test_config_and_feature_changes_are_reflected_in_hashes():
    source, _ = _source(Side.LONG, 0.75)
    baseline_config = TrendPullbackReclaimConfig()
    changed_config = replace(baseline_config, target_reward_to_risk=2.25)

    baseline = generate_trend_pullback_reclaim_intents(source, baseline_config)
    changed = generate_trend_pullback_reclaim_intents(source, changed_config)

    assert baseline_config.fingerprint == "d4f3a3797521aa255bef8de4943d76cd89a8288fea2afd536a9e5039e98494e2"
    assert changed_config.fingerprint != baseline_config.fingerprint
    assert len(baseline) == len(changed) == 1
    assert dict(changed[0].metadata)["feature_hash"] != dict(baseline[0].metadata)["feature_hash"]
    assert changed[0].intent_id != baseline[0].intent_id


def test_repeated_evaluation_produces_identical_feature_and_intent_hashes():
    source, _ = _source(Side.SHORT, 0.75)

    first = generate_trend_pullback_reclaim_intents(source)
    second = generate_trend_pullback_reclaim_intents(list(reversed(source)))

    assert first == second
    assert [dict(intent.metadata)["feature_hash"] for intent in first] == [
        dict(intent.metadata)["feature_hash"] for intent in second
    ]
    assert [intent.intent_id for intent in first] == [intent.intent_id for intent in second]


@pytest.mark.parametrize(
    "changes",
    [
        {"depth_variant": "medium"},
        {"hourly_fast_ema_period": True},
        {"hourly_fast_ema_period": 72},
        {"hourly_slow_ema_period": 24},
        {"hourly_rising_lookback": 0},
        {"minimum_hourly_efficiency": 1.01},
        {"reclaim_ema_period": 50},
        {"atr_period": 0},
        {"maximum_reclaim_extension_atr": float("nan")},
        {"stop_buffer_atr": 0},
        {"minimum_stop_distance_atr": 2.01},
        {"maximum_stop_distance_atr": 0.49},
        {"target_reward_to_risk": True},
    ],
)
def test_invalid_configuration_fails_closed(changes):
    with pytest.raises(ValueError):
        TrendPullbackReclaimConfig(**changes)


@pytest.mark.parametrize("invalid", [False, 0, "", object()])
def test_generator_rejects_invalid_config_objects_instead_of_using_defaults(invalid):
    source, _ = _source(Side.LONG, 0.75)

    with pytest.raises(ValueError, match="TrendPullbackReclaimConfig or None"):
        generate_trend_pullback_reclaim_intents(source, invalid)
