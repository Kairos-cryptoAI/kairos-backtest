from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from datetime import UTC, date, datetime
from typing import Any

import pytest
from kairos_core.enums import Side
from kairos_strategy.candles import Candle

import kairos_backtest.regime_retest_campaign as campaign
import kairos_backtest.sleeves.regime_retest_reclaim as regime_sleeve
from kairos_backtest.scenarios import SYMBOLS
from kairos_backtest.sleeves.regime_retest_reclaim import (
    RegimeRetestGenerationCounters,
    RegimeRetestGenerationEvidence,
    RegimeRetestReclaimVariant,
    RegimeRetestSetupEvent,
    RegimeRetestSetupEventType,
    RegimeVetoRetestReclaimConfig,
)
from kairos_backtest.strategy_models import ExitPlan, SleeveIntent

_MINUTE_MS = 60_000
_DAY_MS = 24 * 60 * _MINUTE_MS


def _utc_ms(day: date) -> int:
    return int(datetime.combine(day, datetime.min.time(), UTC).timestamp() * 1_000)


def _candles(symbol: str, start: date, days: int) -> list[Candle]:
    start_ms = _utc_ms(start)
    return [
        Candle(
            symbol=symbol,
            timeframe="1m",
            open_time_ms=start_ms + index * _MINUTE_MS,
            close_time_ms=start_ms + (index + 1) * _MINUTE_MS - 1,
            open=100.0,
            high=100.5,
            low=99.5,
            close=100.0,
            volume=100.0,
            quote_volume=10_000.0,
            taker_buy_volume=50.0,
        )
        for index in range(days * 24 * 60)
    ]


def _intent(
    row: Candle,
    *,
    side: Side,
    tag: str,
    config: RegimeVetoRetestReclaimConfig,
) -> SleeveIntent:
    eligible = row.close_time_ms + 1
    stop = 99.0 if side is Side.LONG else 101.0
    target = 103.0 if side is Side.LONG else 97.0
    max_hold_bars = config.long_max_hold_bars if side is Side.LONG else config.short_max_hold_bars
    return SleeveIntent(
        sleeve_id=campaign.REGIME_RETEST_SLEEVE_ID,
        symbol=row.symbol,
        side=side,
        decision_ts_ms=row.close_time_ms,
        entry_eligible_ts_ms=eligible,
        entry_expires_ts_ms=(row.close_time_ms + config.intent_valid_bars * 5 * _MINUTE_MS),
        reference_price=row.close,
        signal_strength=0.75,
        gross_reward_bps=300.0,
        exit_plan=ExitPlan(
            stop_price=stop,
            target_price=target,
            max_holding_ms=max_hold_bars * 5 * _MINUTE_MS,
        ),
        metadata=(
            ("atr", "1"),
            ("boundary", "100"),
            ("config_sha256", config.fingerprint),
            ("phase", tag),
            ("strategy_version", campaign.REGIME_RETEST_SLEEVE_ID),
            ("trigger_ts_ms", str(row.close_time_ms - 5 * _MINUTE_MS)),
            ("variant", config.variant.value),
        ),
    )


def _generation_evidence(
    intents: list[SleeveIntent],
    config: RegimeVetoRetestReclaimConfig,
) -> RegimeRetestGenerationEvidence:
    events: list[RegimeRetestSetupEvent] = []
    lifecycle = (
        RegimeRetestSetupEventType.STRUCTURAL_BREAKOUT_CANDIDATE,
        RegimeRetestSetupEventType.ARMED_SETUP,
        RegimeRetestSetupEventType.STRUCTURAL_RECLAIM,
        RegimeRetestSetupEventType.EMITTED_INTENT,
    )
    for intent in intents:
        intent_metadata = dict(intent.metadata)
        trigger_ts_ms = int(intent_metadata["trigger_ts_ms"])
        setup_id = regime_sleeve._setup_id(
            symbol=intent.symbol,
            side=intent.side,
            trigger_ts_ms=trigger_ts_ms,
            boundary=float(intent_metadata["boundary"]),
            atr=float(intent_metadata["atr"]),
            config_sha256=config.fingerprint,
        )
        for event_type in lifecycle:
            is_trigger_event = event_type in {
                RegimeRetestSetupEventType.STRUCTURAL_BREAKOUT_CANDIDATE,
                RegimeRetestSetupEventType.ARMED_SETUP,
            }
            events.append(
                RegimeRetestSetupEvent(
                    sequence=len(events),
                    event_type=event_type,
                    setup_id=setup_id,
                    symbol=intent.symbol,
                    side=intent.side,
                    decision_ts_ms=(trigger_ts_ms if is_trigger_event else intent.decision_ts_ms),
                    trigger_ts_ms=trigger_ts_ms,
                    metadata=(
                        (
                            ("atr", intent_metadata["atr"]),
                            ("boundary", intent_metadata["boundary"]),
                        )
                        if is_trigger_event
                        else (("elapsed_bars", "1"),)
                    ),
                    intent_id=(
                        intent.intent_id if event_type is RegimeRetestSetupEventType.EMITTED_INTENT else None
                    ),
                )
            )

    def counters(side: Side) -> RegimeRetestGenerationCounters:
        emitted = sum(intent.side is side for intent in intents)
        return RegimeRetestGenerationCounters(
            structural_breakout_candidates=emitted,
            armed_setups=emitted,
            structural_reclaims=emitted,
            emitted_intents=emitted,
        )

    immutable_events = tuple(events)
    long_counters = counters(Side.LONG)
    short_counters = counters(Side.SHORT)
    return RegimeRetestGenerationEvidence(
        config_sha256=config.fingerprint,
        variant=config.variant,
        intents=tuple(intents),
        events=immutable_events,
        long_counters=long_counters,
        short_counters=short_counters,
        total_counters=long_counters + short_counters,
        setup_inventory_sha256=regime_sleeve._inventory_sha256(
            immutable_events,
            setup_only=True,
        ),
        outcome_inventory_sha256=regime_sleeve._inventory_sha256(
            immutable_events,
            setup_only=False,
        ),
    )


def _install_lightweight_data(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, int]]:
    generated_by_symbol: dict[str, list[Candle]] = {}
    evaluated_by_symbol: dict[str, list[Candle]] = {}
    calls: list[tuple[str, int]] = []
    boundary_ms = _utc_ms(campaign.REGIME_RETEST_EVALUATION_START)

    for symbol in SYMBOLS:
        evaluation = _candles(symbol, campaign.REGIME_RETEST_EVALUATION_START, 2)
        warmup = Candle(
            symbol=symbol,
            timeframe="1m",
            open_time_ms=boundary_ms - _MINUTE_MS,
            close_time_ms=boundary_ms - 1,
            open=100.0,
            high=100.5,
            low=99.5,
            close=100.0,
            volume=0.0,
            quote_volume=0.0,
            taker_buy_volume=0.0,
        )
        generated_by_symbol[symbol] = [warmup, *evaluation]
        evaluated_by_symbol[symbol] = evaluation

    def fake_slice(
        _rows: list[Candle],
        *,
        symbol: str,
        generation_start: date,
        evaluation_start: date,
        evaluation_end: date,
    ) -> tuple[list[Candle], list[Candle], campaign.RegimeRetestDatasetEvidence]:
        assert generation_start == campaign.REGIME_RETEST_GENERATION_START
        assert evaluation_start == campaign.REGIME_RETEST_EVALUATION_START
        assert evaluation_end == campaign.REGIME_RETEST_EVALUATION_END
        digest = hashlib.sha256(f"regime-retest-dataset:{symbol}".encode()).hexdigest()
        warmup_candles = (evaluation_start - generation_start).days * 24 * 60
        evaluation_candles = (evaluation_end - evaluation_start).days * 24 * 60
        dataset_evidence_sha256 = campaign._dataset_evidence_sha256(
            symbol=symbol,
            generation_start=generation_start,
            evaluation_start=evaluation_start,
            evaluation_end=evaluation_end,
            warmup_candles=warmup_candles,
            evaluation_candles=evaluation_candles,
            warmup_zero_volume_candles=1,
            evaluation_zero_volume_candles=0,
            candles_sha256=digest,
        )
        return (
            generated_by_symbol[symbol],
            evaluated_by_symbol[symbol],
            campaign.RegimeRetestDatasetEvidence(
                symbol=symbol,
                generation_start=generation_start,
                evaluation_start=evaluation_start,
                evaluation_end=evaluation_end,
                warmup_candles=warmup_candles,
                evaluation_candles=evaluation_candles,
                warmup_zero_volume_candles=1,
                evaluation_zero_volume_candles=0,
                candles_sha256=digest,
                dataset_evidence_sha256=dataset_evidence_sha256,
            ),
        )

    def fake_generator(
        rows: list[Candle],
        config: RegimeVetoRetestReclaimConfig,
    ) -> RegimeRetestGenerationEvidence:
        calls.append((rows[0].symbol, len(rows)))
        return _generation_evidence(
            [
                _intent(rows[0], side=Side.LONG, tag="warmup", config=config),
                _intent(rows[5], side=Side.SHORT, tag="evaluation", config=config),
            ],
            config,
        )

    monkeypatch.setattr(campaign, "_slice_symbol", fake_slice)
    monkeypatch.setattr(
        campaign,
        "generate_regime_veto_retest_reclaim_evidence",
        fake_generator,
    )
    return calls


def _dummy_universe() -> dict[str, list[Candle]]:
    return {symbol: [_candles(symbol, campaign.REGIME_RETEST_EVALUATION_START, 1)[0]] for symbol in SYMBOLS}


def _run_lightweight(monkeypatch: pytest.MonkeyPatch) -> campaign.RegimeRetestCampaignEvidence:
    _install_lightweight_data(monkeypatch)
    return campaign.run_regime_retest_campaign(_dummy_universe())


def test_campaign_is_fixed_equal_weight_causal_and_deterministic(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    generator_calls = _install_lightweight_data(monkeypatch)
    evaluator_calls: list[tuple[int, int, int]] = []
    real_evaluator = campaign.evaluate_sleeve_cell

    def recording_evaluator(
        candles_1m: list[Candle],
        intents: list[SleeveIntent],
        **kwargs: Any,
    ) -> Any:
        evaluator_calls.append((candles_1m[0].open_time_ms, len(candles_1m), len(intents)))
        return real_evaluator(candles_1m, intents, **kwargs)

    monkeypatch.setattr(campaign, "evaluate_sleeve_cell", recording_evaluator)
    first = campaign.run_regime_retest_campaign(_dummy_universe())
    second = campaign.run_regime_retest_campaign(_dummy_universe())

    assert first == second
    assert first.to_dict() == second.to_dict()
    assert json.loads(json.dumps(first.to_dict(), allow_nan=False)) == first.to_dict()
    assert (
        first.generation_start,
        first.evaluation_start,
        first.evaluation_end,
    ) == (date(2023, 12, 1), date(2024, 2, 1), date(2024, 7, 1))
    assert first.window_rationale == campaign.REGIME_RETEST_WINDOW_RATIONALE
    assert first.window_name == "research"
    assert first.role.value == "research"
    assert first.purpose.value == "fit"
    assert first.protocol.max_trials == 3
    assert first.protocol.warmup_ms == 62 * _DAY_MS
    assert first.protocol.maximum_holding_ms == 9_000_000
    assert first.protocol.maximum_label_horizon_ms == 9_060_000
    assert first.protocol.maximum_execution_latency_ms == 500
    assert first.protocol.purge_ms == 9_060_500
    assert first.seed == 44
    assert first.requested_initial_equity_usd == 100_000.0
    assert first.cell_initial_equity_usd == 20_000.0
    assert len(first.scenarios) == 2
    assert tuple(item.scenario.name for item in first.scenarios) == ("baseline", "stress")
    assert all(len(item.cells) == 5 for item in first.scenarios)
    assert sum(len(item.cells) for item in first.scenarios) == 10
    assert all(item.portfolio.initial_equity_usd == 100_000.0 for item in first.scenarios)
    assert all(
        tuple((cell.sleeve_id, cell.symbol) for cell in item.cells)
        == tuple((campaign.REGIME_RETEST_SLEEVE_ID, symbol) for symbol in SYMBOLS)
        for item in first.scenarios
    )
    assert all(
        cell.generated_intents == 2
        and cell.warmup_intents_filtered == 1
        and cell.evaluated_intents == 1
        and cell.terminal_embargo_intents_filtered == 0
        and len(cell.warmup_intent_ids) == 1
        and len(cell.evaluated_intent_ids) == 1
        and cell.terminal_embargo_intent_ids == ()
        and cell.evaluated_intent_ids == tuple(item.intent.intent_id for item in cell.result.dispositions)
        for item in first.scenarios
        for cell in item.cells
    )
    assert all(dataset.warmup_zero_volume_candles == 1 for dataset in first.datasets)
    assert len(generator_calls) == 10
    assert all(count == 2 * 24 * 60 + 1 for _, count in generator_calls)
    assert all(
        sum(called_symbol == symbol for called_symbol, _ in generator_calls) == 2 for symbol in SYMBOLS
    )
    assert len(evaluator_calls) == 20
    assert all(
        start == _utc_ms(campaign.REGIME_RETEST_EVALUATION_START) and candles == 2 * 24 * 60 and intents == 1
        for start, candles, intents in evaluator_calls
    )

    baseline, stress = first.scenarios
    for baseline_cell, stress_cell in zip(baseline.cells, stress.cells, strict=True):
        assert baseline_cell.generated_intents_sha256 == stress_cell.generated_intents_sha256
        assert baseline_cell.warmup_intents_sha256 == stress_cell.warmup_intents_sha256
        assert baseline_cell.evaluated_intents_sha256 == stress_cell.evaluated_intents_sha256
        assert baseline_cell.terminal_embargo_intents_sha256 == stress_cell.terminal_embargo_intents_sha256
        assert baseline_cell.generation_evidence is stress_cell.generation_evidence
        assert baseline_cell.generation_evidence_sha256 == stress_cell.generation_evidence_sha256
        assert baseline_cell.generation_evidence.config_sha256 == first.candidate.config.fingerprint
        assert baseline_cell.generation_evidence.total_counters.emitted_intents == 2
        assert len(baseline_cell.generation_evidence.events) == 8
        dataset = next(item for item in first.datasets if item.symbol == baseline_cell.symbol)
        for scenario_name, cell in (
            ("baseline", baseline_cell),
            ("stress", stress_cell),
        ):
            assert cell.evaluation_seed == campaign.derive_seed(
                44,
                "regime-veto-retest-campaign-v1",
                first.protocol_sha256,
                first.candidate.candidate_sha256,
                dataset.candles_sha256,
                dataset.dataset_evidence_sha256,
                cell.generated_intents_sha256,
                cell.generation_evidence_sha256,
                "research",
                "2024-02-01",
                "2024-07-01",
                scenario_name,
                campaign.REGIME_RETEST_SLEEVE_ID,
                cell.symbol,
            )

    payload = first.to_dict()
    assert payload["development_only"] is True
    assert payload["reused_data"] is True
    assert payload["out_of_sample"] is False
    assert payload["data"]["external_dataset_attestation_verified"] is False
    assert first.external_dataset_attestation_verified is False
    assert payload["permissions"] == {
        "live_allowed": False,
        "promotion_eligible": False,
        "shadow_allowed": False,
    }
    assert payload["horizons"] == {
        "candidate_maximum_holding_ms": 5_400_000,
        "candidate_maximum_liquidation_horizon_ms": 9_000_000,
        "maximum_label_horizon_ms": 9_060_000,
        "operational_horizon_ms": 9_120_000,
        "purge_ms": 9_060_500,
    }
    first_cell_payload = payload["scenarios"][0]["cells"][0]
    assert len(first_cell_payload["generation_evidence"]["events"]) == 8
    assert (
        first_cell_payload["generation_evidence_sha256"]
        == first.scenarios[0].cells[0].generation_evidence_sha256
    )


def test_candidate_hash_schema_binds_exactly_three_frozen_variants() -> None:
    candidates = tuple(
        campaign.RegimeRetestCandidate(
            config=RegimeVetoRetestReclaimConfig(variant=variant),
        )
        for variant in campaign.REGIME_RETEST_VARIANTS
    )
    original = candidates[0]

    assert tuple(item.config.variant for item in candidates) == campaign.REGIME_RETEST_VARIANTS
    assert len({item.candidate_sha256 for item in candidates}) == 3
    assert all(len(item.candidate_sha256) == 64 for item in candidates)
    assert original.parameter_dict()["schema"] == "kairos.regime-veto-retest-candidate.v1"
    assert original.parameter_dict()["family"] == "regime_veto_retest_reclaim"
    assert original.parameter_dict()["sleeve_id"] == campaign.REGIME_RETEST_SLEEVE_ID
    assert set(original.parameter_dict()) == {
        "config",
        "family",
        "risk",
        "schema",
        "sleeve_id",
        "terminal_liquidation_grace_ms",
    }
    assert original.risk.risk_fraction == 0.0025
    assert original.risk.maximum_notional_fraction == 0.25
    assert original.risk.maximum_leverage == 1.0
    assert original.risk.minimum_net_reward_to_risk == 1.25
    assert campaign._canonical_json_bytes([100, -0.0]) == campaign._canonical_json_bytes([100.0, 0.0])

    with pytest.raises(ValueError, match="exact frozen variant"):
        campaign.RegimeRetestCandidate(
            config=replace(
                original.config,
                long_minimum_volume_surprise=original.config.long_minimum_volume_surprise + 0.1,
            )
        )
    with pytest.raises(ValueError, match="exact frozen variant"):
        campaign.RegimeRetestCandidate(
            config=replace(original.config, long_max_hold_bars=17),
        )
    with pytest.raises(ValueError, match="risk limits are frozen"):
        campaign.RegimeRetestCandidate(
            risk=replace(original.risk, risk_fraction=0.001),
        )
    with pytest.raises(ValueError, match="grace must be exactly 60"):
        campaign.RegimeRetestCandidate(terminal_liquidation_grace_ms=0)


def test_canonical_hash_rejects_the_internal_float_marker_at_every_depth() -> None:
    marker = {"__float_hex__": (1.5).hex()}

    with pytest.raises(ValueError, match="reserved marker keys"):
        campaign._canonical_json_bytes(marker)
    with pytest.raises(ValueError, match="reserved marker keys"):
        campaign._canonical_json_bytes({"nested": [marker]})

    assert campaign._canonical_json_bytes(1.5) != campaign._canonical_json_bytes("0x1.8p+0")


def test_scenarios_are_exact_bounded_and_stress_dominates() -> None:
    candidate = campaign.RegimeRetestCandidate()
    baseline, stress = campaign.regime_retest_scenarios(candidate)

    assert candidate.maximum_holding_ms == 90 * _MINUTE_MS
    assert candidate.maximum_liquidation_horizon_ms == 150 * _MINUTE_MS
    assert candidate.maximum_label_horizon_ms == 151 * _MINUTE_MS
    assert candidate.operational_horizon_ms == 152 * _MINUTE_MS
    assert all(item.maximum_liquidation_horizon_ms == 150 * _MINUTE_MS for item in (baseline, stress))
    assert all(item.policy.terminal_liquidation_grace_ms == 60 * _MINUTE_MS for item in (baseline, stress))
    assert baseline.execution.funding.evidence == "unavailable"
    assert baseline.costs.adverse_funding_bps == 0.0
    assert stress.execution.funding.evidence == "assumed"
    assert stress.execution.funding.source == "assumed_adverse_stress"
    assert stress.costs.adverse_funding_bps == 1.875

    baseline_cost = (
        baseline.execution.latency_ms,
        baseline.execution.spread_bps,
        baseline.execution.slippage_bps + baseline.execution.slippage_jitter_bps,
        baseline.execution.fee_bps,
        -baseline.execution.max_volume_participation,
        baseline.costs.adverse_funding_bps,
    )
    stress_cost = (
        stress.execution.latency_ms,
        stress.execution.spread_bps,
        stress.execution.slippage_bps + stress.execution.slippage_jitter_bps,
        stress.execution.fee_bps,
        -stress.execution.max_volume_participation,
        stress.costs.adverse_funding_bps,
    )
    assert all(stressed >= base for base, stressed in zip(baseline_cost, stress_cost, strict=True))


@pytest.mark.parametrize(
    ("side", "holding_ms"),
    ((Side.LONG, 90 * _MINUTE_MS), (Side.SHORT, 60 * _MINUTE_MS)),
)
def test_generated_intents_are_bound_to_side_horizon_cadence_and_metadata(
    side: Side,
    holding_ms: int,
) -> None:
    candidate = campaign.RegimeRetestCandidate()
    signal_bar = _candles("BTCUSDT", campaign.REGIME_RETEST_EVALUATION_START, 1)[4]
    valid = _intent(signal_bar, side=side, tag="evaluation", config=candidate.config)

    assert valid.exit_plan.max_holding_ms == holding_ms
    campaign._validate_generated_intent(valid, candidate=candidate, symbol="BTCUSDT")

    with pytest.raises(ValueError, match="holding bound"):
        campaign._validate_generated_intent(
            replace(
                valid,
                exit_plan=replace(valid.exit_plan, max_holding_ms=holding_ms - _MINUTE_MS),
            ),
            candidate=candidate,
            symbol="BTCUSDT",
        )
    with pytest.raises(ValueError, match="metadata"):
        metadata = tuple(
            (key, "0" * 64 if key == "config_sha256" else value) for key, value in valid.metadata
        )
        campaign._validate_generated_intent(
            replace(valid, metadata=metadata),
            candidate=candidate,
            symbol="BTCUSDT",
        )
    with pytest.raises(ValueError, match="five-minute open"):
        campaign._validate_generated_intent(
            replace(valid, entry_eligible_ts_ms=valid.entry_eligible_ts_ms + _MINUTE_MS),
            candidate=candidate,
            symbol="BTCUSDT",
        )
    with pytest.raises(ValueError, match="expiry"):
        campaign._validate_generated_intent(
            replace(valid, entry_expires_ts_ms=valid.entry_expires_ts_ms + _MINUTE_MS),
            candidate=candidate,
            symbol="BTCUSDT",
        )


def test_terminal_operational_embargo_is_committed_but_not_evaluated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_lightweight_data(monkeypatch)
    evaluated_counts: list[int] = []
    real_evaluator = campaign.evaluate_sleeve_cell
    terminal_entry_ms = _utc_ms(campaign.REGIME_RETEST_EVALUATION_END) - 150 * _MINUTE_MS

    def generator(
        rows: list[Candle],
        config: RegimeVetoRetestReclaimConfig,
    ) -> RegimeRetestGenerationEvidence:
        terminal_row = Candle(
            symbol=rows[0].symbol,
            timeframe="1m",
            open_time_ms=terminal_entry_ms - _MINUTE_MS,
            close_time_ms=terminal_entry_ms - 1,
            open=100.0,
            high=100.5,
            low=99.5,
            close=100.0,
            volume=100.0,
            quote_volume=10_000.0,
            taker_buy_volume=50.0,
        )
        return _generation_evidence(
            [
                _intent(rows[0], side=Side.LONG, tag="warmup", config=config),
                _intent(rows[5], side=Side.SHORT, tag="evaluation", config=config),
                _intent(terminal_row, side=Side.LONG, tag="terminal", config=config),
            ],
            config,
        )

    def evaluator(
        candles_1m: list[Candle],
        intents: list[SleeveIntent],
        **kwargs: Any,
    ) -> Any:
        evaluated_counts.append(len(intents))
        return real_evaluator(candles_1m, intents, **kwargs)

    monkeypatch.setattr(campaign, "generate_regime_veto_retest_reclaim_evidence", generator)
    monkeypatch.setattr(campaign, "evaluate_sleeve_cell", evaluator)
    evidence = campaign.run_regime_retest_campaign(_dummy_universe())

    assert evaluated_counts == [1] * 10
    empty_sha256 = campaign._intent_inventory_sha256([])
    for scenario in evidence.scenarios:
        for cell in scenario.cells:
            assert cell.generated_intents == 3
            assert cell.warmup_intents_filtered == 1
            assert cell.evaluated_intents == 1
            assert cell.terminal_embargo_intents_filtered == 1
            assert cell.terminal_embargo_intents_sha256 != empty_sha256
            assert cell.generated_intents_sha256 == campaign._generated_intents_sha256(
                warmup_intents=cell.warmup_intents_filtered,
                warmup_intents_sha256=cell.warmup_intents_sha256,
                evaluated_intents=cell.evaluated_intents,
                evaluated_intents_sha256=cell.evaluated_intents_sha256,
                terminal_embargo_intents=cell.terminal_embargo_intents_filtered,
                terminal_embargo_intents_sha256=cell.terminal_embargo_intents_sha256,
            )


def test_campaign_rejects_duplicate_generated_intent_ids_before_evaluation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_lightweight_data(monkeypatch)
    evaluator_called = False

    def duplicate_generator(
        rows: list[Candle],
        config: RegimeVetoRetestReclaimConfig,
    ) -> RegimeRetestGenerationEvidence:
        intent = _intent(rows[5], side=Side.LONG, tag="duplicate", config=config)
        return _generation_evidence([intent, intent], config)

    def forbidden_evaluator(*args: object, **kwargs: object) -> object:
        nonlocal evaluator_called
        evaluator_called = True
        raise AssertionError("duplicate inventory must fail before evaluation")

    monkeypatch.setattr(
        campaign,
        "generate_regime_veto_retest_reclaim_evidence",
        duplicate_generator,
    )
    monkeypatch.setattr(campaign, "evaluate_sleeve_cell", forbidden_evaluator)

    with pytest.raises(
        ValueError,
        match="causally ordered|one-to-one identity|unique intent IDs",
    ):
        campaign.run_regime_retest_campaign(_dummy_universe())
    assert evaluator_called is False


def test_evidence_rejects_grid_capital_seed_inventory_and_replay_forgery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    evidence = _run_lightweight(monkeypatch)
    baseline, stress = evidence.scenarios

    with pytest.raises(ValueError, match="exactly five"):
        replace(baseline, cells=baseline.cells[:-1])
    with pytest.raises(ValueError, match="exactly equal synchronized"):
        replace(
            baseline,
            portfolio=replace(baseline.portfolio, total_return=99.0, trades=999),
        )
    with pytest.raises(ValueError, match="five-way allocation"):
        replace(evidence, cell_initial_equity_usd=10_000.0)
    with pytest.raises(ValueError, match="fixed at 44"):
        replace(evidence, seed=43)
    with pytest.raises(ValueError, match="frozen regime/retest screen"):
        replace(evidence, evaluation_end=date(2024, 6, 1))
    with pytest.raises(ValueError, match="protocol fingerprint"):
        replace(evidence, protocol_sha256="0" * 64)

    original_stress_cell = stress.cells[0]
    changed_embargo_count = original_stress_cell.terminal_embargo_intents_filtered + 1
    with pytest.raises(ValueError, match="count does not match"):
        replace(
            original_stress_cell,
            generated_intents=original_stress_cell.generated_intents + 1,
            terminal_embargo_intents_filtered=changed_embargo_count,
            generated_intents_sha256=campaign._generated_intents_sha256(
                warmup_intents=original_stress_cell.warmup_intents_filtered,
                warmup_intents_sha256=original_stress_cell.warmup_intents_sha256,
                evaluated_intents=original_stress_cell.evaluated_intents,
                evaluated_intents_sha256=original_stress_cell.evaluated_intents_sha256,
                terminal_embargo_intents=changed_embargo_count,
                terminal_embargo_intents_sha256=(original_stress_cell.terminal_embargo_intents_sha256),
            ),
        )

    forged_terminal_entry_ms = _utc_ms(campaign.REGIME_RETEST_EVALUATION_END) - 150 * _MINUTE_MS
    forged_terminal_row = Candle(
        symbol=original_stress_cell.symbol,
        timeframe="1m",
        open_time_ms=forged_terminal_entry_ms - _MINUTE_MS,
        close_time_ms=forged_terminal_entry_ms - 1,
        open=100.0,
        high=100.5,
        low=99.5,
        close=100.0,
        volume=100.0,
        quote_volume=10_000.0,
        taker_buy_volume=50.0,
    )
    forged_terminal_intent = _intent(
        forged_terminal_row,
        side=Side.LONG,
        tag="coordinated-forgery",
        config=evidence.candidate.config,
    )
    forged_terminal_ids = (forged_terminal_intent.intent_id,)
    forged_terminal_sha256 = campaign._intent_ids_sha256(forged_terminal_ids)
    forged_generation_evidence = _generation_evidence(
        [*original_stress_cell.generation_evidence.intents, forged_terminal_intent],
        evidence.candidate.config,
    )

    def forge_filtered_inventory(
        cell: campaign.RegimeRetestCellEvidence,
    ) -> campaign.RegimeRetestCellEvidence:
        forged_count = cell.terminal_embargo_intents_filtered + 1
        generated_intents_sha256 = campaign._generated_intents_sha256(
            warmup_intents=cell.warmup_intents_filtered,
            warmup_intents_sha256=cell.warmup_intents_sha256,
            evaluated_intents=cell.evaluated_intents,
            evaluated_intents_sha256=cell.evaluated_intents_sha256,
            terminal_embargo_intents=forged_count,
            terminal_embargo_intents_sha256=forged_terminal_sha256,
        )
        generation_evidence_sha256 = campaign._generation_evidence_sha256(
            generation_evidence=forged_generation_evidence,
            candidate_sha256=cell.candidate_sha256,
            dataset_sha256=cell.dataset_sha256,
            dataset_evidence_sha256=cell.dataset_evidence_sha256,
            symbol=cell.symbol,
            warmup_intent_ids=cell.warmup_intent_ids,
            evaluated_intent_ids=cell.evaluated_intent_ids,
            terminal_embargo_intent_ids=forged_terminal_ids,
        )
        return replace(
            cell,
            generated_intents=cell.generated_intents + 1,
            terminal_embargo_intents_filtered=forged_count,
            terminal_embargo_intent_ids=forged_terminal_ids,
            terminal_embargo_intents_sha256=forged_terminal_sha256,
            generated_intents_sha256=generated_intents_sha256,
            generation_evidence=forged_generation_evidence,
            generation_evidence_sha256=generation_evidence_sha256,
        )

    coordinated_forged_scenarios = tuple(
        replace(scenario, cells=(forge_filtered_inventory(scenario.cells[0]), *scenario.cells[1:]))
        for scenario in evidence.scenarios
    )
    with pytest.raises(ValueError, match="derived campaign seed"):
        replace(evidence, scenarios=coordinated_forged_scenarios)

    original_generation_evidence = original_stress_cell.generation_evidence
    last_event = original_generation_evidence.events[-1]
    diagnostics_setup_id = regime_sleeve._setup_id(
        symbol=original_stress_cell.symbol,
        side=Side.LONG,
        trigger_ts_ms=last_event.decision_ts_ms,
        boundary=100.0,
        atr=1.0,
        config_sha256=original_generation_evidence.config_sha256,
    )
    tampered_events = (
        *original_generation_evidence.events,
        RegimeRetestSetupEvent(
            sequence=len(original_generation_evidence.events),
            event_type=RegimeRetestSetupEventType.STRUCTURAL_BREAKOUT_CANDIDATE,
            setup_id=diagnostics_setup_id,
            symbol=original_stress_cell.symbol,
            side=Side.LONG,
            decision_ts_ms=last_event.decision_ts_ms,
            trigger_ts_ms=last_event.decision_ts_ms,
            metadata=(("atr", "1"), ("boundary", "100")),
        ),
        RegimeRetestSetupEvent(
            sequence=len(original_generation_evidence.events) + 1,
            event_type=RegimeRetestSetupEventType.REGIME_REJECT,
            setup_id=diagnostics_setup_id,
            symbol=original_stress_cell.symbol,
            side=Side.LONG,
            decision_ts_ms=last_event.decision_ts_ms,
            trigger_ts_ms=last_event.decision_ts_ms,
            metadata=(("reason", "coordinated-tamper"),),
        ),
    )
    tampered_long_counters = regime_sleeve._counters_from_events(tampered_events, Side.LONG)
    tampered_short_counters = regime_sleeve._counters_from_events(tampered_events, Side.SHORT)
    tampered_generation_evidence = RegimeRetestGenerationEvidence(
        config_sha256=original_generation_evidence.config_sha256,
        variant=original_generation_evidence.variant,
        intents=original_generation_evidence.intents,
        events=tampered_events,
        long_counters=tampered_long_counters,
        short_counters=tampered_short_counters,
        total_counters=tampered_long_counters + tampered_short_counters,
        setup_inventory_sha256=regime_sleeve._inventory_sha256(
            tampered_events,
            setup_only=True,
        ),
        outcome_inventory_sha256=regime_sleeve._inventory_sha256(
            tampered_events,
            setup_only=False,
        ),
    )

    def forge_diagnostics(
        cell: campaign.RegimeRetestCellEvidence,
    ) -> campaign.RegimeRetestCellEvidence:
        binding_sha256 = campaign._generation_evidence_sha256(
            generation_evidence=tampered_generation_evidence,
            candidate_sha256=cell.candidate_sha256,
            dataset_sha256=cell.dataset_sha256,
            dataset_evidence_sha256=cell.dataset_evidence_sha256,
            symbol=cell.symbol,
            warmup_intent_ids=cell.warmup_intent_ids,
            evaluated_intent_ids=cell.evaluated_intent_ids,
            terminal_embargo_intent_ids=cell.terminal_embargo_intent_ids,
        )
        return replace(
            cell,
            generation_evidence=tampered_generation_evidence,
            generation_evidence_sha256=binding_sha256,
        )

    coordinated_diagnostics_tamper = tuple(
        replace(scenario, cells=(forge_diagnostics(scenario.cells[0]), *scenario.cells[1:]))
        for scenario in evidence.scenarios
    )
    with pytest.raises(ValueError, match="derived campaign seed"):
        replace(evidence, scenarios=coordinated_diagnostics_tamper)

    duplicate_id = original_stress_cell.warmup_intent_ids[0]
    duplicate_ids = (duplicate_id,)
    duplicate_sha256 = campaign._intent_ids_sha256(duplicate_ids)
    with pytest.raises(ValueError, match="unique across every role partition"):
        replace(
            original_stress_cell,
            generated_intents=original_stress_cell.generated_intents + 1,
            terminal_embargo_intents_filtered=1,
            terminal_embargo_intent_ids=duplicate_ids,
            terminal_embargo_intents_sha256=duplicate_sha256,
            generated_intents_sha256=campaign._generated_intents_sha256(
                warmup_intents=original_stress_cell.warmup_intents_filtered,
                warmup_intents_sha256=original_stress_cell.warmup_intents_sha256,
                evaluated_intents=original_stress_cell.evaluated_intents,
                evaluated_intents_sha256=original_stress_cell.evaluated_intents_sha256,
                terminal_embargo_intents=1,
                terminal_embargo_intents_sha256=duplicate_sha256,
            ),
        )

    with pytest.raises(ValueError, match="candidate fingerprint"):
        replace(
            evidence,
            candidate=campaign.RegimeRetestCandidate(
                config=RegimeVetoRetestReclaimConfig(
                    variant=RegimeRetestReclaimVariant.FLOW_REACCELERATION,
                )
            ),
        )
    with pytest.raises(ValueError, match="evidence commitment"):
        replace(evidence.datasets[0], candles_sha256="0" * 64)

    first_dataset, second_dataset, *remaining_datasets = evidence.datasets
    tampered_zero_count = first_dataset.warmup_zero_volume_candles + 1
    tampered_zero_commitment = campaign._dataset_evidence_sha256(
        symbol=first_dataset.symbol,
        generation_start=first_dataset.generation_start,
        evaluation_start=first_dataset.evaluation_start,
        evaluation_end=first_dataset.evaluation_end,
        warmup_candles=first_dataset.warmup_candles,
        evaluation_candles=first_dataset.evaluation_candles,
        warmup_zero_volume_candles=tampered_zero_count,
        evaluation_zero_volume_candles=first_dataset.evaluation_zero_volume_candles,
        candles_sha256=first_dataset.candles_sha256,
    )
    tampered_zero_dataset = replace(
        first_dataset,
        warmup_zero_volume_candles=tampered_zero_count,
        dataset_evidence_sha256=tampered_zero_commitment,
    )
    with pytest.raises(ValueError, match="dataset evidence fingerprint"):
        replace(
            evidence,
            datasets=(tampered_zero_dataset, second_dataset, *remaining_datasets),
        )

    def forge_zero_count_cell(
        cell: campaign.RegimeRetestCellEvidence,
    ) -> campaign.RegimeRetestCellEvidence:
        if cell.symbol != first_dataset.symbol:
            return cell
        generation_binding = campaign._generation_evidence_sha256(
            generation_evidence=cell.generation_evidence,
            candidate_sha256=cell.candidate_sha256,
            dataset_sha256=cell.dataset_sha256,
            dataset_evidence_sha256=tampered_zero_commitment,
            symbol=cell.symbol,
            warmup_intent_ids=cell.warmup_intent_ids,
            evaluated_intent_ids=cell.evaluated_intent_ids,
            terminal_embargo_intent_ids=cell.terminal_embargo_intent_ids,
        )
        return replace(
            cell,
            dataset_evidence_sha256=tampered_zero_commitment,
            generation_evidence_sha256=generation_binding,
        )

    coordinated_zero_scenarios = tuple(
        replace(
            scenario,
            cells=tuple(forge_zero_count_cell(cell) for cell in scenario.cells),
        )
        for scenario in evidence.scenarios
    )
    with pytest.raises(ValueError, match="derived campaign seed"):
        replace(
            evidence,
            datasets=(tampered_zero_dataset, second_dataset, *remaining_datasets),
            scenarios=coordinated_zero_scenarios,
        )

    swapped_hashes = {
        first_dataset.symbol: second_dataset.candles_sha256,
        second_dataset.symbol: first_dataset.candles_sha256,
    }

    def forge_dataset_hash(
        dataset: campaign.RegimeRetestDatasetEvidence,
        candles_sha256: str,
    ) -> campaign.RegimeRetestDatasetEvidence:
        commitment = campaign._dataset_evidence_sha256(
            symbol=dataset.symbol,
            generation_start=dataset.generation_start,
            evaluation_start=dataset.evaluation_start,
            evaluation_end=dataset.evaluation_end,
            warmup_candles=dataset.warmup_candles,
            evaluation_candles=dataset.evaluation_candles,
            warmup_zero_volume_candles=dataset.warmup_zero_volume_candles,
            evaluation_zero_volume_candles=dataset.evaluation_zero_volume_candles,
            candles_sha256=candles_sha256,
        )
        return replace(
            dataset,
            candles_sha256=candles_sha256,
            dataset_evidence_sha256=commitment,
        )

    coordinated_datasets = (
        forge_dataset_hash(first_dataset, swapped_hashes[first_dataset.symbol]),
        forge_dataset_hash(second_dataset, swapped_hashes[second_dataset.symbol]),
        *remaining_datasets,
    )
    coordinated_dataset_evidence_hashes = {
        dataset.symbol: dataset.dataset_evidence_sha256 for dataset in coordinated_datasets
    }

    def forge_cell_dataset(
        cell: campaign.RegimeRetestCellEvidence,
    ) -> campaign.RegimeRetestCellEvidence:
        dataset_sha256 = swapped_hashes.get(cell.symbol, cell.dataset_sha256)
        dataset_evidence_sha256 = coordinated_dataset_evidence_hashes[cell.symbol]
        generation_evidence_sha256 = campaign._generation_evidence_sha256(
            generation_evidence=cell.generation_evidence,
            candidate_sha256=cell.candidate_sha256,
            dataset_sha256=dataset_sha256,
            dataset_evidence_sha256=dataset_evidence_sha256,
            symbol=cell.symbol,
            warmup_intent_ids=cell.warmup_intent_ids,
            evaluated_intent_ids=cell.evaluated_intent_ids,
            terminal_embargo_intent_ids=cell.terminal_embargo_intent_ids,
        )
        return replace(
            cell,
            dataset_sha256=dataset_sha256,
            dataset_evidence_sha256=dataset_evidence_sha256,
            generation_evidence_sha256=generation_evidence_sha256,
        )

    coordinated_scenarios = tuple(
        replace(
            scenario,
            cells=tuple(forge_cell_dataset(cell) for cell in scenario.cells),
        )
        for scenario in evidence.scenarios
    )
    with pytest.raises(ValueError, match="derived campaign seed"):
        replace(
            evidence,
            datasets=coordinated_datasets,
            scenarios=coordinated_scenarios,
        )

    cell = baseline.cells[0]
    with pytest.raises(ValueError, match="partition hash"):
        replace(cell, evaluated_intents_sha256="0" * 64)
    original_evaluated_intent = cell.generation_evidence.intents[1]
    forged_evaluated_intent = replace(
        original_evaluated_intent,
        metadata=tuple(
            (key, "forged-evaluated" if key == "phase" else value)
            for key, value in original_evaluated_intent.metadata
        ),
    )
    forged_evaluated_ids = (forged_evaluated_intent.intent_id,)
    forged_evaluated_sha256 = campaign._intent_ids_sha256(forged_evaluated_ids)
    forged_evaluated_evidence = _generation_evidence(
        [cell.generation_evidence.intents[0], forged_evaluated_intent],
        evidence.candidate.config,
    )
    forged_evaluated_generated_sha256 = campaign._generated_intents_sha256(
        warmup_intents=cell.warmup_intents_filtered,
        warmup_intents_sha256=cell.warmup_intents_sha256,
        evaluated_intents=cell.evaluated_intents,
        evaluated_intents_sha256=forged_evaluated_sha256,
        terminal_embargo_intents=cell.terminal_embargo_intents_filtered,
        terminal_embargo_intents_sha256=cell.terminal_embargo_intents_sha256,
    )
    forged_evaluated_evidence_sha256 = campaign._generation_evidence_sha256(
        generation_evidence=forged_evaluated_evidence,
        candidate_sha256=cell.candidate_sha256,
        dataset_sha256=cell.dataset_sha256,
        dataset_evidence_sha256=cell.dataset_evidence_sha256,
        symbol=cell.symbol,
        warmup_intent_ids=cell.warmup_intent_ids,
        evaluated_intent_ids=forged_evaluated_ids,
        terminal_embargo_intent_ids=cell.terminal_embargo_intent_ids,
    )
    with pytest.raises(ValueError, match="managed dispositions"):
        replace(
            cell,
            evaluated_intent_ids=forged_evaluated_ids,
            evaluated_intents_sha256=forged_evaluated_sha256,
            generated_intents_sha256=forged_evaluated_generated_sha256,
            generation_evidence=forged_evaluated_evidence,
            generation_evidence_sha256=forged_evaluated_evidence_sha256,
        )
    with pytest.raises(ValueError, match="exact role partitions"):
        replace(cell, generation_evidence=forged_evaluated_evidence)
    with pytest.raises(ValueError, match="exact campaign binding"):
        replace(cell, generation_evidence_sha256="0" * 64)
    with pytest.raises(ValueError, match="exact role partition"):
        replace(cell, generated_intents_sha256="0" * 64)
    with pytest.raises(ValueError, match="result seed"):
        replace(cell, evaluation_seed=cell.evaluation_seed + 1)


def test_run_rejects_protocol_seed_capital_universe_and_scenario_mutation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_lightweight_data(monkeypatch)
    dummy = _dummy_universe()
    candidate = campaign.RegimeRetestCandidate()
    baseline, stress = campaign.regime_retest_scenarios(candidate)

    with pytest.raises(ValueError, match="seed is fixed at 44"):
        campaign.run_regime_retest_campaign(dummy, seed=43)
    with pytest.raises(ValueError, match="equity is fixed"):
        campaign.run_regime_retest_campaign(dummy, initial_equity_usd=90_000.0)
    with pytest.raises(ValueError, match="exactly the fixed five-symbol"):
        campaign.run_regime_retest_campaign(
            {key: value for key, value in dummy.items() if key != SYMBOLS[-1]}
        )
    with pytest.raises(ValueError, match="ordered baseline and stress"):
        campaign.run_regime_retest_campaign(
            dummy,
            candidate=candidate,
            scenarios=(stress, baseline),
        )
    with pytest.raises(ValueError, match="frozen v1 research protocol"):
        campaign.run_regime_retest_campaign(
            dummy,
            protocol=replace(campaign.DEFAULT_REGIME_RETEST_PROTOCOL, max_trials=4),
        )
    with pytest.raises(TypeError, match="RegimeRetestCandidate"):
        campaign.run_regime_retest_campaign(dummy, candidate=False)  # type: ignore[arg-type]


def test_dataset_slice_accepts_zero_volume_but_never_imputes_gaps() -> None:
    generation_start = date(2023, 1, 1)
    evaluation_start = date(2023, 1, 2)
    evaluation_end = date(2023, 1, 4)
    rows = _candles("BTCUSDT", generation_start, 3)
    rows[17] = replace(
        rows[17],
        volume=0.0,
        quote_volume=0.0,
        taker_buy_volume=0.0,
    )

    selected, evaluation, evidence = campaign._slice_symbol(
        rows,
        symbol="BTCUSDT",
        generation_start=generation_start,
        evaluation_start=evaluation_start,
        evaluation_end=evaluation_end,
    )

    assert len(selected) == 3 * 24 * 60
    assert len(evaluation) == 2 * 24 * 60
    assert evidence.warmup_zero_volume_candles == 1
    assert evidence.evaluation_zero_volume_candles == 0
    assert len(evidence.candles_sha256) == 64

    zero_rows = [replace(row, volume=0.0, quote_volume=0.0, taker_buy_volume=0.0) for row in selected]
    assert campaign.generate_regime_veto_retest_reclaim_evidence(zero_rows).intents == ()

    rows.pop(100)
    with pytest.raises(ValueError, match="gaps are not imputed"):
        campaign._slice_symbol(
            rows,
            symbol="BTCUSDT",
            generation_start=generation_start,
            evaluation_start=evaluation_start,
            evaluation_end=evaluation_end,
        )


def test_dataset_evidence_counts_and_hashes_fail_closed() -> None:
    digest = hashlib.sha256(b"dataset").hexdigest()
    warmup_candles = 62 * 24 * 60
    evaluation_candles = (
        (campaign.REGIME_RETEST_EVALUATION_END - campaign.REGIME_RETEST_EVALUATION_START).days * 24 * 60
    )
    dataset_evidence_sha256 = campaign._dataset_evidence_sha256(
        symbol="BTCUSDT",
        generation_start=campaign.REGIME_RETEST_GENERATION_START,
        evaluation_start=campaign.REGIME_RETEST_EVALUATION_START,
        evaluation_end=campaign.REGIME_RETEST_EVALUATION_END,
        warmup_candles=warmup_candles,
        evaluation_candles=evaluation_candles,
        warmup_zero_volume_candles=0,
        evaluation_zero_volume_candles=0,
        candles_sha256=digest,
    )
    valid = campaign.RegimeRetestDatasetEvidence(
        symbol="BTCUSDT",
        generation_start=campaign.REGIME_RETEST_GENERATION_START,
        evaluation_start=campaign.REGIME_RETEST_EVALUATION_START,
        evaluation_end=campaign.REGIME_RETEST_EVALUATION_END,
        warmup_candles=warmup_candles,
        evaluation_candles=evaluation_candles,
        warmup_zero_volume_candles=0,
        evaluation_zero_volume_candles=0,
        candles_sha256=digest,
        dataset_evidence_sha256=dataset_evidence_sha256,
    )

    with pytest.raises(ValueError, match="candle counts"):
        replace(valid, evaluation_candles=1)
    with pytest.raises(TypeError, match="must be an integer"):
        replace(valid, warmup_candles=float(valid.warmup_candles))
    with pytest.raises(TypeError, match="must be an integer"):
        replace(valid, evaluation_candles=True)
    with pytest.raises(ValueError, match="in-range integer"):
        replace(valid, warmup_zero_volume_candles=valid.warmup_candles + 1)
    with pytest.raises(ValueError, match="evidence commitment"):
        replace(valid, warmup_zero_volume_candles=1)
    with pytest.raises(ValueError, match="lowercase SHA-256"):
        replace(valid, candles_sha256=digest.upper())
    with pytest.raises(ValueError, match="evidence commitment"):
        replace(valid, dataset_evidence_sha256="0" * 64)


@pytest.mark.parametrize(
    ("field_name", "replacement"),
    (
        ("symbol", "ETHUSDT"),
        ("timeframe", "5m"),
        ("open_time_ms", _utc_ms(date(2023, 1, 1)) + 1),
        ("close_time_ms", _utc_ms(date(2023, 1, 1)) + _MINUTE_MS),
        ("open", 100.1),
        ("high", 100.6),
        ("low", 99.4),
        ("close", 100.1),
        ("volume", 101.0),
        ("quote_volume", 10_001.0),
        ("taker_buy_volume", 51.0),
    ),
    ids=(
        "symbol",
        "timeframe",
        "open_time_ms",
        "close_time_ms",
        "open",
        "high",
        "low",
        "close",
        "volume",
        "quote_volume",
        "taker_buy_volume",
    ),
)
def test_dataset_hash_is_sensitive_to_every_candle_field(
    field_name: str,
    replacement: object,
) -> None:
    original = _candles("BTCUSDT", date(2023, 1, 1), 1)[0]
    changed = replace(original, **{field_name: replacement})

    assert campaign._dataset_sha256([changed]) != campaign._dataset_sha256([original])


def test_fixed_protocol_is_unfrozen_reused_research_only() -> None:
    protocol = campaign.DEFAULT_REGIME_RETEST_PROTOCOL

    assert protocol.universe == SYMBOLS
    assert protocol.max_trials == 3
    assert protocol.candidate_commit is None
    assert protocol.parameter_set_sha256 is None
    assert protocol.frozen_at is None
    assert protocol.is_frozen is False
    assert protocol.window("research").role.value == "research"
    assert campaign.REGIME_RETEST_GENERATION_START == date(2023, 12, 1)
    assert campaign.REGIME_RETEST_EVALUATION_START == date(2024, 2, 1)
    assert (campaign.REGIME_RETEST_EVALUATION_START - campaign.REGIME_RETEST_GENERATION_START).days == 62
    assert campaign.REGIME_RETEST_EVALUATION_END == date(2024, 7, 1)
    assert "invalid XRP minute in November 2023" in campaign.REGIME_RETEST_WINDOW_RATIONALE
