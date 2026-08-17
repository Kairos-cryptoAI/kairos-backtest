from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, replace

import pytest
from kairos_core.enums import Side
from kairos_quant.candles import Candle

from kairos_backtest.sleeves.regime_retest_reclaim import (
    RegimeRetestGenerationCounters,
    RegimeRetestGenerationEvidence,
    RegimeRetestReclaimVariant,
    RegimeRetestSetupEvent,
    RegimeRetestSetupEventType,
    RegimeVetoRetestReclaimConfig,
    generate_regime_veto_retest_reclaim_evidence,
    generate_regime_veto_retest_reclaim_intents,
)

_MINUTE_MS = 60_000
_FIVE_MINUTES_MS = 5 * _MINUTE_MS
_HOUR_MS = 60 * _MINUTE_MS
_TREND_HOURS = 73


def _taker_volume(volume: float, directional_imbalance: float, side: Side) -> float:
    raw = directional_imbalance if side is Side.LONG else -directional_imbalance
    return volume * (1 + raw) / 2


def _trend_rows(regime_side: Side) -> list[Candle]:
    offsets = (0.0, 2.0, -2.0, 1.8, -1.8, 1.5, -1.5, 1.0, -1.0, 0.7, -0.3, 0.5)
    direction = 1 if regime_side is Side.LONG else -1
    offset_scale = 1.0 if regime_side is Side.LONG else 1.5
    rows: list[Candle] = []
    previous_close = 200.0 if regime_side is Side.LONG else 300.0
    for hour in range(_TREND_HOURS):
        base = (200.0 + 0.5 * hour) if regime_side is Side.LONG else (300.0 - 0.5 * hour)
        for slot, raw_offset in enumerate(offsets):
            close = base + direction * raw_offset * offset_scale
            opened = previous_close
            high = max(opened, close) + 0.20
            low = min(opened, close) - 0.20
            index = hour * 12 + slot
            rows.append(
                Candle(
                    symbol="BTCUSDT",
                    timeframe="5m",
                    open_time_ms=index * _FIVE_MINUTES_MS,
                    close_time_ms=(index + 1) * _FIVE_MINUTES_MS - 1,
                    open=opened,
                    high=high,
                    low=low,
                    close=close,
                    volume=100.0,
                    quote_volume=100.0 * close,
                    taker_buy_volume=50.0,
                )
            )
            previous_close = close
    return rows


def _independent_prior_atr(rows: list[Candle], period: int, signal_index: int) -> float:
    true_ranges: list[float] = []
    for index, row in enumerate(rows[:signal_index]):
        if index == 0:
            true_ranges.append(row.high - row.low)
        else:
            prior_close = rows[index - 1].close
            true_ranges.append(
                max(
                    row.high - row.low,
                    abs(row.high - prior_close),
                    abs(row.low - prior_close),
                )
            )
    atr = sum(true_ranges[:period]) / period
    for true_range in true_ranges[period:]:
        atr = (atr * (period - 1) + true_range) / period
    return atr


def _append_trigger_and_reclaims(
    rows: list[Candle],
    signal_side: Side,
    variant: RegimeRetestReclaimVariant,
    *,
    reclaim_offset: int = 1,
    first_reclaim_flow: float | None = None,
    invalidate: bool = False,
) -> list[Candle]:
    signal_index = len(rows)
    atr = _independent_prior_atr(rows, 24, signal_index)
    prior = rows[-12:]
    boundary = max(row.high for row in prior) if signal_side is Side.LONG else min(row.low for row in prior)
    prior_close = rows[-1].close
    if signal_side is Side.LONG:
        close = boundary + 0.50 * atr
        opened = prior_close
        low = min(opened, close) - 0.20 * atr
        high = close + 0.05 * atr
    else:
        close = boundary - 0.50 * atr
        opened = prior_close
        high = max(opened, close) + 0.40 * atr
        low = close - 0.05 * atr
    trigger_volume = 150.0
    rows.append(
        Candle(
            symbol="BTCUSDT",
            timeframe="5m",
            open_time_ms=signal_index * _FIVE_MINUTES_MS,
            close_time_ms=(signal_index + 1) * _FIVE_MINUTES_MS - 1,
            open=opened,
            high=high,
            low=low,
            close=close,
            volume=trigger_volume,
            quote_volume=trigger_volume * close,
            taker_buy_volume=_taker_volume(trigger_volume, 0.0, signal_side),
        )
    )

    for offset in range(1, reclaim_offset):
        index = signal_index + offset
        if signal_side is Side.LONG:
            opened = boundary + 0.40 * atr
            high = boundary + 0.45 * atr
            low = boundary + 0.30 * atr
            close = boundary + 0.35 * atr
        else:
            opened = boundary - 0.40 * atr
            high = boundary - 0.30 * atr
            low = boundary - 0.45 * atr
            close = boundary - 0.35 * atr
        rows.append(
            Candle(
                symbol="BTCUSDT",
                timeframe="5m",
                open_time_ms=index * _FIVE_MINUTES_MS,
                close_time_ms=(index + 1) * _FIVE_MINUTES_MS - 1,
                open=opened,
                high=high,
                low=low,
                close=close,
                volume=100.0,
                quote_volume=100.0 * close,
                taker_buy_volume=50.0,
            )
        )

    index = signal_index + reclaim_offset
    if invalidate:
        if signal_side is Side.LONG:
            opened = boundary
            high = boundary + 0.10 * atr
            low = boundary - 0.40 * atr
            close = boundary - 0.30 * atr
        else:
            opened = boundary
            high = boundary + 0.30 * atr
            low = boundary - 0.10 * atr
            close = boundary + 0.25 * atr
    elif signal_side is Side.LONG:
        opened = boundary + 0.10 * atr
        high = boundary + 0.25 * atr
        low = boundary - 0.10 * atr
        close = boundary + 0.20 * atr
    else:
        opened = boundary - 0.10 * atr
        high = boundary + 0.10 * atr
        low = boundary - 0.25 * atr
        close = boundary - 0.20 * atr

    if first_reclaim_flow is not None:
        flow = first_reclaim_flow
    elif variant is RegimeRetestReclaimVariant.FLOW_REACCELERATION:
        flow = 0.20
    elif variant is RegimeRetestReclaimVariant.ABSORPTION_RECLAIM:
        flow = -0.15
    else:
        flow = 0.0
    volume = 150.0 if variant is RegimeRetestReclaimVariant.ABSORPTION_RECLAIM else 100.0
    rows.append(
        Candle(
            symbol="BTCUSDT",
            timeframe="5m",
            open_time_ms=index * _FIVE_MINUTES_MS,
            close_time_ms=(index + 1) * _FIVE_MINUTES_MS - 1,
            open=opened,
            high=high,
            low=low,
            close=close,
            volume=volume,
            quote_volume=volume * close,
            taker_buy_volume=_taker_volume(volume, flow, signal_side),
        )
    )
    return rows


def _five_minute_source(
    side: Side,
    variant: RegimeRetestReclaimVariant,
    *,
    regime_side: Side | None = None,
    reclaim_offset: int = 1,
    first_reclaim_flow: float | None = None,
    invalidate: bool = False,
) -> list[Candle]:
    rows = _trend_rows(side if regime_side is None else regime_side)
    return _append_trigger_and_reclaims(
        rows,
        side,
        variant,
        reclaim_offset=reclaim_offset,
        first_reclaim_flow=first_reclaim_flow,
        invalidate=invalidate,
    )


def _to_one_minute(rows: list[Candle]) -> list[Candle]:
    result: list[Candle] = []
    for row in rows:
        for minute in range(5):
            opened = row.open + (row.close - row.open) * minute / 5
            closed = row.open + (row.close - row.open) * (minute + 1) / 5
            timestamp = row.open_time_ms + minute * _MINUTE_MS
            result.append(
                Candle(
                    symbol=row.symbol,
                    timeframe="1m",
                    open_time_ms=timestamp,
                    close_time_ms=timestamp + _MINUTE_MS - 1,
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
    variant: RegimeRetestReclaimVariant,
    *,
    regime_side: Side | None = None,
    reclaim_offset: int = 1,
    first_reclaim_flow: float | None = None,
    invalidate: bool = False,
) -> list[Candle]:
    return _to_one_minute(
        _five_minute_source(
            side,
            variant,
            regime_side=regime_side,
            reclaim_offset=reclaim_offset,
            first_reclaim_flow=first_reclaim_flow,
            invalidate=invalidate,
        )
    )


def _active_side_counters(**changes: int) -> RegimeRetestGenerationCounters:
    values = {
        "structural_breakout_candidates": 72,
        "regime_rejects": 70,
        "expansion_rejects": 1,
        "armed_setups": 1,
        "boundary_failures": 0,
        "overextensions": 0,
        "expiries": 0,
        "structural_reclaims": 0,
        "flow_mismatches": 0,
        "risk_geometry_rejects": 0,
        "emitted_intents": 0,
        "state_resets": 0,
        "pending_setups": 0,
    }
    values.update(changes)
    return RegimeRetestGenerationCounters(**values)


def _terminal_source(
    side: Side,
    outcome: RegimeRetestSetupEventType,
) -> tuple[list[Candle], RegimeVetoRetestReclaimConfig]:
    variant = (
        RegimeRetestReclaimVariant.FLOW_REACCELERATION
        if outcome is RegimeRetestSetupEventType.FLOW_MISMATCH
        else RegimeRetestReclaimVariant.STRUCTURAL_RECLAIM
    )
    config = RegimeVetoRetestReclaimConfig(variant=variant)
    if outcome is RegimeRetestSetupEventType.BOUNDARY_FAILURE:
        return _source(side, variant, invalidate=True), config
    if outcome is RegimeRetestSetupEventType.EXPIRY:
        return _source(side, variant, reclaim_offset=4), config
    if outcome is RegimeRetestSetupEventType.FLOW_MISMATCH:
        return _source(side, variant, first_reclaim_flow=0.0), config

    rows = _five_minute_source(side, variant)
    trigger, reclaim = rows[-2:]
    atr = _independent_prior_atr(rows[: _TREND_HOURS * 12], 24, _TREND_HOURS * 12)
    if outcome is RegimeRetestSetupEventType.OVEREXTENSION:
        if side is Side.LONG:
            reclaim = replace(reclaim, high=trigger.high + 0.80 * atr)
        else:
            reclaim = replace(reclaim, low=trigger.low - 0.60 * atr)
    elif outcome is RegimeRetestSetupEventType.RISK_GEOMETRY_REJECT:
        if side is Side.LONG:
            boundary = reclaim.close - 0.20 * atr
            close = boundary + 0.05 * atr
            reclaim = replace(
                reclaim,
                open=boundary + 0.01 * atr,
                high=boundary + 0.10 * atr,
                low=boundary - 0.10 * atr,
                close=close,
                quote_volume=reclaim.volume * close,
            )
        else:
            boundary = reclaim.close + 0.20 * atr
            close = boundary - 0.11 * atr
            reclaim = replace(
                reclaim,
                open=boundary - 0.01 * atr,
                high=boundary + 0.05 * atr,
                low=boundary - 0.15 * atr,
                close=close,
                quote_volume=reclaim.volume * close,
            )
    else:
        raise AssertionError(f"unsupported terminal outcome: {outcome}")
    rows[-1] = reclaim
    return _to_one_minute(rows), config


def _reset_source(kind: str) -> list[Candle]:
    rows = _five_minute_source(
        Side.LONG,
        RegimeRetestReclaimVariant.STRUCTURAL_RECLAIM,
    )
    if kind == "zero_volume":
        rows[-1] = replace(
            rows[-1],
            volume=0.0,
            quote_volume=0.0,
            taker_buy_volume=0.0,
        )
    elif kind == "gap":
        rows[-1] = replace(
            rows[-1],
            open_time_ms=rows[-1].open_time_ms + _FIVE_MINUTES_MS,
            close_time_ms=rows[-1].close_time_ms + _FIVE_MINUTES_MS,
        )
    else:
        raise AssertionError(f"unsupported reset kind: {kind}")
    return _to_one_minute(rows)


def _independent_event_sha256(event: RegimeRetestSetupEvent) -> str:
    payload = event.to_dict()
    del payload["event_sha256"]
    canonical = json.dumps(
        payload,
        allow_nan=False,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    return hashlib.sha256(b"kairos.regime-retest-setup-event.v1\0" + canonical).hexdigest()


def _independent_inventory_sha256(
    events: tuple[RegimeRetestSetupEvent, ...],
    *,
    setup_only: bool,
) -> str:
    domain = (
        b"kairos.regime-retest-setup-inventory.v1\0"
        if setup_only
        else b"kairos.regime-retest-outcome-inventory.v1\0"
    )
    digest = hashlib.sha256(domain)
    for event in events:
        if setup_only and (event.event_type is not RegimeRetestSetupEventType.STRUCTURAL_BREAKOUT_CANDIDATE):
            continue
        digest.update(
            json.dumps(
                event.to_dict(),
                allow_nan=False,
                ensure_ascii=True,
                separators=(",", ":"),
                sort_keys=True,
            ).encode()
        )
        digest.update(b"\n")
    return digest.hexdigest()


def _resequence_and_rehash(
    events: list[RegimeRetestSetupEvent],
) -> tuple[RegimeRetestSetupEvent, ...]:
    return tuple(replace(event, sequence=sequence, event_sha256="") for sequence, event in enumerate(events))


@pytest.mark.parametrize("side", [Side.LONG, Side.SHORT])
@pytest.mark.parametrize("variant", list(RegimeRetestReclaimVariant))
def test_each_frozen_variant_emits_one_bounded_side_specific_intent(side, variant):
    source = _source(side, variant)
    config = RegimeVetoRetestReclaimConfig(variant=variant)

    intents = generate_regime_veto_retest_reclaim_intents(source, config)

    assert len(intents) == 1
    intent = intents[0]
    metadata = dict(intent.metadata)
    risk = abs(intent.reference_price - intent.exit_plan.stop_price)
    reward = abs(intent.exit_plan.target_price - intent.reference_price)
    reclaim_index = _TREND_HOURS * 12 + 1
    assert intent.sleeve_id == "regime_veto_retest_reclaim_v1"
    assert intent.side is side
    assert intent.decision_ts_ms == (reclaim_index + 1) * _FIVE_MINUTES_MS - 1
    assert intent.entry_eligible_ts_ms == (reclaim_index + 1) * _FIVE_MINUTES_MS
    assert intent.entry_expires_ts_ms == (reclaim_index + 2) * _FIVE_MINUTES_MS - 1
    assert intent.exit_plan.max_holding_ms == (18 if side is Side.LONG else 12) * _FIVE_MINUTES_MS
    assert reward / risk == pytest.approx(2.50 if side is Side.LONG else 2.25)
    assert metadata["variant"] == variant.value
    assert metadata["strategy_version"] == "regime_veto_retest_reclaim_v1"
    assert metadata["retest_bars_elapsed"] == "1"
    assert len(metadata["config_sha256"]) == 64
    assert len(metadata["feature_hash"]) == 64
    assert config.maximum_holding_ms == 90 * _MINUTE_MS


@pytest.mark.parametrize(
    ("side", "variant", "setup_sha256", "outcome_sha256"),
    [
        (
            Side.LONG,
            RegimeRetestReclaimVariant.STRUCTURAL_RECLAIM,
            "b0affac89ddacd6808fa31f45e50472b440da79aa1968a2d7d6be42b4fdf177b",
            "d6ffdd830afa6c6c1a5b053dbdb698b60826b85bcc4df6249177b47e2790f358",
        ),
        (
            Side.LONG,
            RegimeRetestReclaimVariant.FLOW_REACCELERATION,
            "96d7f5d8758c85aa89a62f4c8a57eb42bc2e76ed55ba0de9990410aa696ea9f5",
            "9c1f6c11a4559e39cdd5ae7222b662458ae9b4b0b97aae532f68bb5be1447990",
        ),
        (
            Side.LONG,
            RegimeRetestReclaimVariant.ABSORPTION_RECLAIM,
            "c0125c0ebb1d9694b2f0c1db770214aa847fff004556c4e75ccb1e2ed4c7f44d",
            "8e398e69702cbf5b17d5fc052fc8b15397842c81cbe6285f680bcd1fc07bb728",
        ),
        (
            Side.SHORT,
            RegimeRetestReclaimVariant.STRUCTURAL_RECLAIM,
            "e2141ed39f628be835ab68e1f3aaa87b009b029a525ef22f946378afd5ec05c5",
            "0e2c764ae9edee222b161a7e9b436bca7604c1dbf240a9fa3b85c8c8bd774a06",
        ),
        (
            Side.SHORT,
            RegimeRetestReclaimVariant.FLOW_REACCELERATION,
            "3d2a253afd1a2f54d167ef45cafbc9505facc27ea9cf49cad7a8acef5072401b",
            "c0cbaf95692c1f0b93f5e63a28ff85f7b7affacdd1437a8bbaef78113e3e4ecb",
        ),
        (
            Side.SHORT,
            RegimeRetestReclaimVariant.ABSORPTION_RECLAIM,
            "1447e1335fd24cb8b4a3e00a757f7923cbbb280161a224de2effa7f01560e08c",
            "a94f959f372d2265bc05dba0f20a158a9e59b460a533f05655f653c4d9b7b8ad",
        ),
    ],
)
def test_evidence_has_exact_side_counts_and_frozen_inventory_hashes(
    side,
    variant,
    setup_sha256,
    outcome_sha256,
):
    config = RegimeVetoRetestReclaimConfig(variant=variant)

    evidence = generate_regime_veto_retest_reclaim_evidence(_source(side, variant), config)

    expected = _active_side_counters(structural_reclaims=1, emitted_intents=1)
    assert isinstance(evidence, RegimeRetestGenerationEvidence)
    assert evidence.config_sha256 == config.fingerprint
    assert evidence.variant is variant
    assert evidence.long_counters == (expected if side is Side.LONG else RegimeRetestGenerationCounters())
    assert evidence.short_counters == (expected if side is Side.SHORT else RegimeRetestGenerationCounters())
    assert evidence.total_counters == expected
    assert evidence.setup_inventory_sha256 == setup_sha256
    assert evidence.outcome_inventory_sha256 == outcome_sha256
    assert [event.event_type for event in evidence.events[-4:]] == [
        RegimeRetestSetupEventType.STRUCTURAL_BREAKOUT_CANDIDATE,
        RegimeRetestSetupEventType.ARMED_SETUP,
        RegimeRetestSetupEventType.STRUCTURAL_RECLAIM,
        RegimeRetestSetupEventType.EMITTED_INTENT,
    ]
    assert list(evidence.intents) == generate_regime_veto_retest_reclaim_intents(
        _source(side, variant),
        config,
    )


@pytest.mark.parametrize("side", [Side.LONG, Side.SHORT])
@pytest.mark.parametrize(
    ("outcome", "counter_changes"),
    [
        (RegimeRetestSetupEventType.BOUNDARY_FAILURE, {"boundary_failures": 1}),
        (RegimeRetestSetupEventType.OVEREXTENSION, {"overextensions": 1}),
        (RegimeRetestSetupEventType.EXPIRY, {"expiries": 1}),
        (
            RegimeRetestSetupEventType.FLOW_MISMATCH,
            {"structural_reclaims": 1, "flow_mismatches": 1},
        ),
        (
            RegimeRetestSetupEventType.RISK_GEOMETRY_REJECT,
            {"structural_reclaims": 1, "risk_geometry_rejects": 1},
        ),
    ],
)
def test_terminal_diagnostics_reconcile_exactly_by_side(side, outcome, counter_changes):
    source, config = _terminal_source(side, outcome)

    evidence = generate_regime_veto_retest_reclaim_evidence(source, config)

    expected = _active_side_counters(**counter_changes)
    assert evidence.intents == ()
    assert evidence.long_counters == (expected if side is Side.LONG else RegimeRetestGenerationCounters())
    assert evidence.short_counters == (expected if side is Side.SHORT else RegimeRetestGenerationCounters())
    assert evidence.total_counters == expected
    assert evidence.events[-1].event_type is outcome


def test_regime_and_expansion_rejects_are_distinct_exact_outcomes():
    evidence = generate_regime_veto_retest_reclaim_evidence(
        _source(Side.LONG, RegimeRetestReclaimVariant.STRUCTURAL_RECLAIM)
    )
    regime_rejects = [
        event for event in evidence.events if event.event_type is RegimeRetestSetupEventType.REGIME_REJECT
    ]
    expansion_rejects = [
        event for event in evidence.events if event.event_type is RegimeRetestSetupEventType.EXPANSION_REJECT
    ]

    assert len(regime_rejects) == evidence.long_counters.regime_rejects == 70
    assert len(expansion_rejects) == evidence.long_counters.expansion_rejects == 1
    assert {dict(event.metadata)["reason"] for event in regime_rejects} <= {
        "missing_strict_prior_hour",
        "hourly_veto",
    }
    assert {dict(event.metadata)["reason"] for event in expansion_rejects} == {"frozen_expansion_thresholds"}


def test_event_and_inventory_hashes_match_an_independent_canonical_oracle():
    evidence = generate_regime_veto_retest_reclaim_evidence(
        _source(Side.LONG, RegimeRetestReclaimVariant.STRUCTURAL_RECLAIM)
    )

    assert all(event.event_sha256 == _independent_event_sha256(event) for event in evidence.events)
    assert evidence.setup_inventory_sha256 == _independent_inventory_sha256(
        evidence.events,
        setup_only=True,
    )
    assert evidence.outcome_inventory_sha256 == _independent_inventory_sha256(
        evidence.events,
        setup_only=False,
    )


def test_generation_evidence_and_event_hashes_fail_closed_when_tampered():
    evidence = generate_regime_veto_retest_reclaim_evidence(
        _source(Side.LONG, RegimeRetestReclaimVariant.STRUCTURAL_RECLAIM)
    )

    with pytest.raises(ValueError, match="partition"):
        RegimeRetestGenerationCounters(structural_breakout_candidates=1)
    with pytest.raises(ValueError, match="total counters"):
        replace(evidence, total_counters=RegimeRetestGenerationCounters())
    with pytest.raises(ValueError, match="canonical event inventory"):
        replace(evidence, outcome_inventory_sha256="0" * 64)
    with pytest.raises(ValueError, match="canonical payload"):
        replace(evidence.events[-1], metadata=(*evidence.events[-1].metadata, ("tampered", "1")))


def test_short_diagnostics_reject_an_adversarially_linked_long_intent():
    short_evidence = generate_regime_veto_retest_reclaim_evidence(
        _source(Side.SHORT, RegimeRetestReclaimVariant.STRUCTURAL_RECLAIM)
    )
    long_evidence = generate_regime_veto_retest_reclaim_evidence(
        _source(Side.LONG, RegimeRetestReclaimVariant.STRUCTURAL_RECLAIM)
    )
    events = list(short_evidence.events)
    events[-1] = replace(
        events[-1],
        intent_id=long_evidence.intents[0].intent_id,
        event_sha256="",
    )
    immutable_events = tuple(events)

    with pytest.raises(ValueError, match="emitted event identity"):
        replace(
            short_evidence,
            intents=long_evidence.intents,
            events=immutable_events,
            outcome_inventory_sha256=_independent_inventory_sha256(
                immutable_events,
                setup_only=False,
            ),
        )


def test_emitted_event_rejects_an_intent_with_a_different_decision_timestamp():
    evidence = generate_regime_veto_retest_reclaim_evidence(
        _source(Side.LONG, RegimeRetestReclaimVariant.STRUCTURAL_RECLAIM)
    )
    delayed = generate_regime_veto_retest_reclaim_evidence(
        _source(
            Side.LONG,
            RegimeRetestReclaimVariant.STRUCTURAL_RECLAIM,
            reclaim_offset=2,
        )
    )
    assert evidence.events[-1].setup_id == delayed.events[-1].setup_id
    assert evidence.intents[0].decision_ts_ms != delayed.intents[0].decision_ts_ms
    events = list(evidence.events)
    events[-1] = replace(
        events[-1],
        intent_id=delayed.intents[0].intent_id,
        event_sha256="",
    )
    immutable_events = tuple(events)

    with pytest.raises(ValueError, match="emitted event identity"):
        replace(
            evidence,
            intents=delayed.intents,
            events=immutable_events,
            outcome_inventory_sha256=_independent_inventory_sha256(
                immutable_events,
                setup_only=False,
            ),
        )


def test_setup_lifecycle_rejects_a_terminal_with_a_mismatched_setup_id():
    evidence = generate_regime_veto_retest_reclaim_evidence(
        _source(Side.LONG, RegimeRetestReclaimVariant.STRUCTURAL_RECLAIM)
    )
    events = list(evidence.events)
    events[-1] = replace(events[-1], setup_id="f" * 64, event_sha256="")

    with pytest.raises(ValueError, match="invalid order|begin with its structural candidate"):
        replace(evidence, events=tuple(events))

    original_setup_id = evidence.events[-1].setup_id
    noncanonical_lifecycle = tuple(
        replace(event, setup_id="e" * 64, event_sha256="") if event.setup_id == original_setup_id else event
        for event in evidence.events
    )
    with pytest.raises(ValueError, match="setup_id does not match"):
        replace(evidence, events=noncanonical_lifecycle)


def test_setup_lifecycle_rejects_cross_side_state_transitions():
    evidence = generate_regime_veto_retest_reclaim_evidence(
        _source(Side.LONG, RegimeRetestReclaimVariant.STRUCTURAL_RECLAIM)
    )
    events = list(evidence.events)
    events[-2] = replace(events[-2], side=Side.SHORT, event_sha256="")

    with pytest.raises(ValueError, match="changed symbol, side, or trigger timestamp"):
        replace(evidence, events=tuple(events))


def test_setup_lifecycle_rejects_reclaim_and_admission_in_reverse_order():
    evidence = generate_regime_veto_retest_reclaim_evidence(
        _source(Side.LONG, RegimeRetestReclaimVariant.STRUCTURAL_RECLAIM)
    )
    events = list(evidence.events)
    events[-2], events[-1] = events[-1], events[-2]
    reordered = _resequence_and_rehash(events)

    with pytest.raises(ValueError, match="invalid order"):
        replace(evidence, events=reordered)


def test_setup_lifecycle_rejects_duplicate_terminal_outcomes():
    source, config = _terminal_source(
        Side.LONG,
        RegimeRetestSetupEventType.BOUNDARY_FAILURE,
    )
    evidence = generate_regime_veto_retest_reclaim_evidence(source, config)
    events = [*evidence.events, evidence.events[-1]]
    duplicated = _resequence_and_rehash(events)

    with pytest.raises(ValueError, match="duplicate terminal"):
        replace(evidence, events=duplicated)


def test_state_reset_requires_armed_transition_exact_reason_and_timestamp_semantics():
    evidence = generate_regime_veto_retest_reclaim_evidence(_reset_source("gap"))
    reset = evidence.events[-1]

    wrong_reason = tuple(
        [*evidence.events[:-1], replace(reset, metadata=(("reason", "manual"),), event_sha256="")]
    )
    with pytest.raises(ValueError, match="reason must be exactly"):
        replace(evidence, events=wrong_reason)

    wrong_timestamp = tuple(
        [
            *evidence.events[:-1],
            replace(
                reset,
                decision_ts_ms=reset.decision_ts_ms + _FIVE_MINUTES_MS - 1,
                event_sha256="",
            ),
        ]
    )
    with pytest.raises(ValueError, match="next observed bucket open"):
        replace(evidence, events=wrong_timestamp)

    without_armed = _resequence_and_rehash(
        [event for event in evidence.events if event.event_type is not RegimeRetestSetupEventType.ARMED_SETUP]
    )
    with pytest.raises(ValueError, match="candidate must resolve once"):
        replace(evidence, events=without_armed)


def test_empty_generation_evidence_is_frozen_and_domain_separated():
    evidence = generate_regime_veto_retest_reclaim_evidence([])

    assert evidence.intents == ()
    assert evidence.events == ()
    assert evidence.long_counters == RegimeRetestGenerationCounters()
    assert evidence.short_counters == RegimeRetestGenerationCounters()
    assert evidence.total_counters == RegimeRetestGenerationCounters()
    assert (
        evidence.setup_inventory_sha256
        == hashlib.sha256(b"kairos.regime-retest-setup-inventory.v1\0").hexdigest()
    )
    assert (
        evidence.outcome_inventory_sha256
        == hashlib.sha256(b"kairos.regime-retest-outcome-inventory.v1\0").hexdigest()
    )


def test_config_fingerprint_matches_canonical_json_and_changes_candidate_identity():
    baseline = RegimeVetoRetestReclaimConfig()
    changed = replace(baseline, variant=RegimeRetestReclaimVariant.FLOW_REACCELERATION)
    expected = hashlib.sha256(
        json.dumps(
            asdict(baseline),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        ).encode()
    ).hexdigest()

    assert baseline.fingerprint == expected
    assert baseline.fingerprint != changed.fingerprint


@pytest.mark.parametrize(
    ("side", "variant", "flow"),
    [
        (Side.LONG, RegimeRetestReclaimVariant.FLOW_REACCELERATION, 0.099),
        (Side.SHORT, RegimeRetestReclaimVariant.FLOW_REACCELERATION, 0.149),
        (Side.LONG, RegimeRetestReclaimVariant.ABSORPTION_RECLAIM, -0.049),
        (Side.SHORT, RegimeRetestReclaimVariant.ABSORPTION_RECLAIM, -0.099),
    ],
)
def test_flow_variants_fail_closed_just_outside_their_current_bar_threshold(side, variant, flow):
    source = _source(side, variant, first_reclaim_flow=flow)

    assert (
        generate_regime_veto_retest_reclaim_intents(
            source,
            RegimeVetoRetestReclaimConfig(variant=variant),
        )
        == []
    )


def test_opposing_hourly_regime_vetoes_both_sides():
    for side in (Side.LONG, Side.SHORT):
        opposite = Side.SHORT if side is Side.LONG else Side.LONG
        source = _source(
            side,
            RegimeRetestReclaimVariant.STRUCTURAL_RECLAIM,
            regime_side=opposite,
        )

        assert generate_regime_veto_retest_reclaim_intents(source) == []


@pytest.mark.parametrize("side", [Side.LONG, Side.SHORT])
def test_third_bar_reclaim_is_included_but_fourth_bar_is_expired(side):
    variant = RegimeRetestReclaimVariant.STRUCTURAL_RECLAIM
    included = generate_regime_veto_retest_reclaim_intents(_source(side, variant, reclaim_offset=3))
    expired = generate_regime_veto_retest_reclaim_intents(_source(side, variant, reclaim_offset=4))

    assert len(included) == 1
    assert dict(included[0].metadata)["retest_bars_elapsed"] == "3"
    assert expired == []


@pytest.mark.parametrize("side", [Side.LONG, Side.SHORT])
def test_boundary_failure_has_precedence_over_reclaim_and_closes_setup(side):
    variant = RegimeRetestReclaimVariant.STRUCTURAL_RECLAIM
    source = _source(side, variant, invalidate=True)

    assert generate_regime_veto_retest_reclaim_intents(source) == []


def test_first_structural_reclaim_owns_setup_even_when_flow_variant_mismatches():
    variant = RegimeRetestReclaimVariant.FLOW_REACCELERATION
    rows = _five_minute_source(Side.LONG, variant, first_reclaim_flow=0.0)
    first = rows[-1]
    atr = _independent_prior_atr(rows[: _TREND_HOURS * 12], 24, _TREND_HOURS * 12)
    boundary = first.close - 0.20 * atr
    index = len(rows)
    rows.append(
        replace(
            first,
            open_time_ms=index * _FIVE_MINUTES_MS,
            close_time_ms=(index + 1) * _FIVE_MINUTES_MS - 1,
            open=boundary + 0.10 * atr,
            high=boundary + 0.25 * atr,
            low=boundary - 0.10 * atr,
            close=boundary + 0.20 * atr,
            taker_buy_volume=_taker_volume(first.volume, 0.20, Side.LONG),
        )
    )

    assert (
        generate_regime_veto_retest_reclaim_intents(
            _to_one_minute(rows),
            RegimeVetoRetestReclaimConfig(variant=variant),
        )
        == []
    )


@pytest.mark.parametrize("kind", ["gap", "zero_volume"])
def test_gap_or_zero_volume_source_row_resets_five_minute_and_hourly_state(kind):
    source = _source(Side.LONG, RegimeRetestReclaimVariant.STRUCTURAL_RECLAIM)
    index = (_TREND_HOURS - 2) * 60
    if kind == "gap":
        del source[index]
    else:
        source[index] = replace(
            source[index],
            volume=0.0,
            quote_volume=0.0,
            taker_buy_volume=0.0,
        )

    assert generate_regime_veto_retest_reclaim_intents(source) == []


def test_future_and_incomplete_current_hour_cannot_change_a_prior_intent():
    variant = RegimeRetestReclaimVariant.STRUCTURAL_RECLAIM
    prefix = _source(Side.LONG, variant)
    original = generate_regime_veto_retest_reclaim_intents(prefix)
    assert len(original) == 1
    changed = list(prefix)
    next_timestamp = changed[-1].close_time_ms + 1
    for offset in range(50):
        timestamp = next_timestamp + offset * _MINUTE_MS
        changed.append(
            Candle(
                symbol="BTCUSDT",
                timeframe="1m",
                open_time_ms=timestamp,
                close_time_ms=timestamp + _MINUTE_MS - 1,
                open=500.0,
                high=600.0,
                low=400.0,
                close=450.0,
                volume=10_000.0,
                quote_volume=4_500_000.0,
                taker_buy_volume=0.0,
            )
        )

    mutated = [
        intent
        for intent in generate_regime_veto_retest_reclaim_intents(changed)
        if intent.decision_ts_ms <= original[0].decision_ts_ms
    ]
    assert mutated == original


def test_future_rows_cannot_change_prior_diagnostic_events_or_their_prefix_hashes():
    variant = RegimeRetestReclaimVariant.STRUCTURAL_RECLAIM
    prefix = _source(Side.LONG, variant)
    original = generate_regime_veto_retest_reclaim_evidence(prefix)
    changed = list(prefix)
    next_timestamp = changed[-1].close_time_ms + 1
    for offset in range(50):
        timestamp = next_timestamp + offset * _MINUTE_MS
        changed.append(
            Candle(
                symbol="BTCUSDT",
                timeframe="1m",
                open_time_ms=timestamp,
                close_time_ms=timestamp + _MINUTE_MS - 1,
                open=500.0,
                high=600.0,
                low=400.0,
                close=450.0,
                volume=10_000.0,
                quote_volume=4_500_000.0,
                taker_buy_volume=0.0,
            )
        )

    extended = generate_regime_veto_retest_reclaim_evidence(changed)
    cutoff = original.events[-1].decision_ts_ms
    prior_events = tuple(event for event in extended.events if event.decision_ts_ms <= cutoff)
    prior_intents = tuple(intent for intent in extended.intents if intent.decision_ts_ms <= cutoff)

    assert prior_events == original.events
    assert prior_intents == original.intents
    assert _independent_inventory_sha256(prior_events, setup_only=True) == (original.setup_inventory_sha256)
    assert _independent_inventory_sha256(prior_events, setup_only=False) == (
        original.outcome_inventory_sha256
    )


@pytest.mark.parametrize(
    ("kind", "expected_hash"),
    [
        ("gap", "7b33143df704a0421745c8d43f1499af627bc15ace45931ec75ab28f9b3ae732"),
        ("zero_volume", "8c37a765250917f5aa9f48c6d2514349f4cf16dcb0ac8eedc24955ca80af00fa"),
    ],
)
def test_gap_resets_are_exact_and_order_invariant(kind, expected_hash):
    source = _reset_source(kind)

    evidence = generate_regime_veto_retest_reclaim_evidence(source)
    reversed_evidence = generate_regime_veto_retest_reclaim_evidence(list(reversed(source)))

    assert evidence == reversed_evidence
    assert evidence.long_counters == _active_side_counters(state_resets=1)
    assert evidence.short_counters == RegimeRetestGenerationCounters()
    assert evidence.intents == ()
    assert evidence.events[-1].event_type is RegimeRetestSetupEventType.STATE_RESET
    assert dict(evidence.events[-1].metadata) == {"reason": kind}
    assert evidence.outcome_inventory_sha256 == expected_hash


def test_post_gap_values_cannot_retroactively_change_the_reset_inventory():
    source = _reset_source("gap")
    gap_open = source[-1].open_time_ms - 4 * _MINUTE_MS
    changed = [
        replace(
            row,
            open=1_000.0,
            high=1_200.0,
            low=800.0,
            close=900.0,
            quote_volume=row.volume * 900.0,
        )
        if row.open_time_ms >= gap_open
        else row
        for row in source
    ]

    assert generate_regime_veto_retest_reclaim_evidence(changed) == (
        generate_regime_veto_retest_reclaim_evidence(source)
    )


def test_reversed_input_is_deterministic():
    source = _source(
        Side.SHORT,
        RegimeRetestReclaimVariant.FLOW_REACCELERATION,
    )
    config = RegimeVetoRetestReclaimConfig(variant=RegimeRetestReclaimVariant.FLOW_REACCELERATION)

    assert generate_regime_veto_retest_reclaim_intents(source, config) == (
        generate_regime_veto_retest_reclaim_intents(list(reversed(source)), config)
    )


def test_sleeves_and_v2_export_the_frozen_public_api():
    from kairos_backtest import sleeves, v2

    assert sleeves.RegimeRetestGenerationCounters is RegimeRetestGenerationCounters
    assert sleeves.RegimeRetestGenerationEvidence is RegimeRetestGenerationEvidence
    assert sleeves.RegimeRetestReclaimVariant is RegimeRetestReclaimVariant
    assert sleeves.RegimeRetestSetupEvent is RegimeRetestSetupEvent
    assert sleeves.RegimeRetestSetupEventType is RegimeRetestSetupEventType
    assert sleeves.RegimeVetoRetestReclaimConfig is RegimeVetoRetestReclaimConfig
    assert (
        sleeves.generate_regime_veto_retest_reclaim_evidence is generate_regime_veto_retest_reclaim_evidence
    )
    assert sleeves.generate_regime_veto_retest_reclaim_intents is generate_regime_veto_retest_reclaim_intents
    assert v2.RegimeRetestGenerationCounters is RegimeRetestGenerationCounters
    assert v2.RegimeRetestGenerationEvidence is RegimeRetestGenerationEvidence
    assert v2.RegimeRetestReclaimVariant is RegimeRetestReclaimVariant
    assert v2.RegimeRetestSetupEvent is RegimeRetestSetupEvent
    assert v2.RegimeRetestSetupEventType is RegimeRetestSetupEventType
    assert v2.RegimeVetoRetestReclaimConfig is RegimeVetoRetestReclaimConfig
    assert v2.generate_regime_veto_retest_reclaim_evidence is generate_regime_veto_retest_reclaim_evidence
    assert v2.generate_regime_veto_retest_reclaim_intents is generate_regime_veto_retest_reclaim_intents


@pytest.mark.parametrize(
    "changes",
    [
        {"variant": "structural_reclaim"},
        {"baseline_lookback": True},
        {"breakout_lookback": 0},
        {"hourly_fast_ema_period": 72},
        {"setup_window_bars": 0},
        {"long_minimum_hourly_efficiency": 1.01},
        {"short_maximum_hourly_slope_atr": 0.0},
        {"long_minimum_atr_bps": 251.0},
        {"short_minimum_atr_bps": 201.0},
        {"long_minimum_breakout_extension_atr": 1.01},
        {"short_minimum_reclaim_extension_atr": 0.51},
        {"long_maximum_absorption_imbalance": 0.0},
        {"short_maximum_absorption_imbalance": float("nan")},
        {"long_minimum_risk_atr": 1.26},
        {"short_target_reward_to_risk": 0.0},
        {"long_max_hold_bars": 0},
    ],
)
def test_invalid_configuration_fails_closed(changes):
    with pytest.raises(ValueError):
        RegimeVetoRetestReclaimConfig(**changes)


@pytest.mark.parametrize("invalid", [False, 0, "", object()])
def test_generator_rejects_invalid_config_objects(invalid):
    with pytest.raises(ValueError, match="RegimeVetoRetestReclaimConfig or None"):
        generate_regime_veto_retest_reclaim_intents([], invalid)  # type: ignore[arg-type]
