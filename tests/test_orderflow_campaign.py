from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from typing import Any

import pytest
from kairos_core.enums import Side
from kairos_strategy.candles import Candle

import kairos_backtest.orderflow_campaign as campaign
from kairos_backtest.research_protocol import ResearchProtocol
from kairos_backtest.scenarios import SYMBOLS
from kairos_backtest.sleeves.orderflow_volatility_expansion import (
    OrderFlowExpansionVariant,
    OrderFlowVolatilityExpansionConfig,
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
    tag: str,
    config: OrderFlowVolatilityExpansionConfig,
) -> SleeveIntent:
    eligible = row.close_time_ms + 1
    return SleeveIntent(
        sleeve_id=campaign.ORDERFLOW_SLEEVE_ID,
        symbol=row.symbol,
        side=Side.LONG,
        decision_ts_ms=row.close_time_ms,
        entry_eligible_ts_ms=eligible,
        entry_expires_ts_ms=(row.close_time_ms + config.intent_valid_bars * 5 * _MINUTE_MS),
        reference_price=row.close,
        signal_strength=0.75,
        gross_reward_bps=300.0,
        exit_plan=ExitPlan(
            stop_price=99.0,
            target_price=103.0,
            max_holding_ms=config.max_hold_bars * 5 * _MINUTE_MS,
        ),
        metadata=(
            ("config_sha256", config.fingerprint),
            ("phase", tag),
            ("strategy_version", campaign.ORDERFLOW_SLEEVE_ID),
            ("variant", config.variant.value),
        ),
    )


def _install_lightweight_data(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, int]]:
    generated_by_symbol: dict[str, list[Candle]] = {}
    evaluated_by_symbol: dict[str, list[Candle]] = {}
    calls: list[tuple[str, int]] = []
    boundary_ms = _utc_ms(campaign.ORDERFLOW_EVALUATION_START)

    for symbol in SYMBOLS:
        evaluation = _candles(symbol, campaign.ORDERFLOW_EVALUATION_START, 2)
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
    ) -> tuple[list[Candle], list[Candle], campaign.OrderFlowDatasetEvidence]:
        assert generation_start == campaign.ORDERFLOW_GENERATION_START
        assert evaluation_start == campaign.ORDERFLOW_EVALUATION_START
        assert evaluation_end == campaign.ORDERFLOW_EVALUATION_END
        digest = hashlib.sha256(f"dataset:{symbol}".encode()).hexdigest()
        return (
            generated_by_symbol[symbol],
            evaluated_by_symbol[symbol],
            campaign.OrderFlowDatasetEvidence(
                symbol=symbol,
                generation_start=generation_start,
                evaluation_start=evaluation_start,
                evaluation_end=evaluation_end,
                warmup_candles=35 * 24 * 60,
                evaluation_candles=(evaluation_end - evaluation_start).days * 24 * 60,
                warmup_zero_volume_candles=1,
                evaluation_zero_volume_candles=0,
                candles_sha256=digest,
            ),
        )

    def fake_generator(
        rows: list[Candle],
        config: OrderFlowVolatilityExpansionConfig,
    ) -> list[SleeveIntent]:
        symbol = rows[0].symbol
        calls.append((symbol, len(rows)))
        return [
            _intent(rows[0], "warmup", config),
            _intent(rows[5], "evaluation", config),
        ]

    monkeypatch.setattr(campaign, "_slice_symbol", fake_slice)
    monkeypatch.setattr(
        campaign,
        "generate_orderflow_volatility_expansion_intents",
        fake_generator,
    )
    return calls


def _run_lightweight(monkeypatch: pytest.MonkeyPatch) -> campaign.OrderFlowCampaignEvidence:
    _install_lightweight_data(monkeypatch)
    dummy = {symbol: [_candles(symbol, campaign.ORDERFLOW_EVALUATION_START, 1)[0]] for symbol in SYMBOLS}
    return campaign.run_orderflow_campaign(dummy)


def test_campaign_is_fixed_causal_equal_weight_and_deterministic(
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
    dummy = {symbol: [_candles(symbol, campaign.ORDERFLOW_EVALUATION_START, 1)[0]] for symbol in SYMBOLS}

    first = campaign.run_orderflow_campaign(dummy)
    second = campaign.run_orderflow_campaign(dummy)

    assert first == second
    assert first.to_dict() == second.to_dict()
    assert json.loads(json.dumps(first.to_dict(), allow_nan=False)) == first.to_dict()
    assert (
        first.generation_start,
        first.evaluation_start,
        first.evaluation_end,
    ) == (
        date(2022, 5, 27),
        date(2022, 7, 1),
        date(2023, 1, 1),
    )
    assert first.window_name == "research"
    assert first.role.value == "research"
    assert first.purpose.value == "fit"
    assert first.protocol.max_trials == 3
    assert first.protocol.warmup_ms == 35 * _DAY_MS
    assert first.protocol.maximum_holding_ms == 2 * 60 * _MINUTE_MS
    assert first.protocol.maximum_label_horizon_ms == 2 * 60 * _MINUTE_MS
    assert first.seed == 42
    assert first.requested_initial_equity_usd == 100_000.0
    assert first.cell_initial_equity_usd == 20_000.0
    assert len(first.scenarios) == 2
    assert tuple(item.scenario.name for item in first.scenarios) == ("baseline", "stress")
    assert all(len(item.cells) == 5 for item in first.scenarios)
    assert sum(len(item.cells) for item in first.scenarios) == 10
    assert all(item.portfolio.initial_equity_usd == 100_000.0 for item in first.scenarios)
    assert all(
        tuple((cell.sleeve_id, cell.symbol) for cell in item.cells)
        == tuple((campaign.ORDERFLOW_SLEEVE_ID, symbol) for symbol in SYMBOLS)
        for item in first.scenarios
    )
    assert all(
        cell.generated_intents == 2 and cell.warmup_intents_filtered == 1 and cell.evaluated_intents == 1
        for item in first.scenarios
        for cell in item.cells
    )
    assert all(dataset.warmup_zero_volume_candles == 1 for dataset in first.datasets)
    assert len(generator_calls) == 10
    assert all(count == 2 * 24 * 60 + 1 for _, count in generator_calls)
    assert len(evaluator_calls) == 20
    assert all(
        start == _utc_ms(campaign.ORDERFLOW_EVALUATION_START) and candles == 2 * 24 * 60 and intents == 1
        for start, candles, intents in evaluator_calls
    )

    baseline, stress = first.scenarios
    for baseline_cell, stress_cell in zip(baseline.cells, stress.cells, strict=True):
        assert baseline_cell.generated_intents_sha256 == stress_cell.generated_intents_sha256
        assert baseline_cell.warmup_intents_sha256 == stress_cell.warmup_intents_sha256
        assert baseline_cell.evaluated_intents_sha256 == stress_cell.evaluated_intents_sha256
        assert (
            baseline_cell.generated_intents,
            baseline_cell.evaluated_intents,
            baseline_cell.warmup_intents_filtered,
        ) == (
            stress_cell.generated_intents,
            stress_cell.evaluated_intents,
            stress_cell.warmup_intents_filtered,
        )
    for scenario_evidence in first.scenarios:
        for cell in scenario_evidence.cells:
            assert cell.evaluation_seed == campaign.derive_seed(
                42,
                "orderflow-campaign-v1",
                first.protocol_sha256,
                first.candidate.candidate_sha256,
                first.datasets[SYMBOLS.index(cell.symbol)].candles_sha256,
                "research",
                "2022-07-01",
                "2023-01-01",
                scenario_evidence.scenario.name,
                campaign.ORDERFLOW_SLEEVE_ID,
                cell.symbol,
            )
            assert cell.result.assumptions.seed == cell.evaluation_seed
            assert cell.result.assumptions.execution == scenario_evidence.scenario.execution
            assert cell.result.assumptions.costs == scenario_evidence.scenario.costs
            assert cell.result.assumptions.policy == scenario_evidence.scenario.policy
            assert cell.result.assumptions.limits == first.candidate.risk

    payload = first.to_dict()
    assert payload["development_only"] is True
    assert payload["reused_data"] is True
    assert payload["out_of_sample"] is False
    assert payload["promotion_eligible"] is False
    assert payload["permissions"] == {
        "live_allowed": False,
        "promotion_eligible": False,
        "shadow_allowed": False,
    }


def test_candidate_hash_schema_covers_complete_config_risk_and_policy() -> None:
    original = campaign.OrderFlowCandidate()
    changed_variant = campaign.OrderFlowCandidate(
        config=replace(original.config, variant=OrderFlowExpansionVariant.PERSISTENCE),
        risk=original.risk,
    )
    changed_feature = campaign.OrderFlowCandidate(
        config=replace(
            original.config,
            minimum_volume_surprise=original.config.minimum_volume_surprise + 0.1,
        ),
        risk=original.risk,
    )
    changed_risk = campaign.OrderFlowCandidate(
        config=original.config,
        risk=replace(original.risk, risk_fraction=original.risk.risk_fraction / 2),
    )

    assert original.parameter_dict()["schema"] == "kairos.orderflow-candidate.v1"
    assert original.parameter_dict()["family"] == "orderflow_volatility_expansion"
    assert original.parameter_dict()["sleeve_id"] == campaign.ORDERFLOW_SLEEVE_ID
    assert set(original.parameter_dict()) == {
        "config",
        "family",
        "risk",
        "schema",
        "sleeve_id",
        "terminal_liquidation_grace_ms",
    }
    assert (
        len(
            {
                original.candidate_sha256,
                changed_variant.candidate_sha256,
                changed_feature.candidate_sha256,
                changed_risk.candidate_sha256,
            }
        )
        == 4
    )
    assert all(
        len(value) == 64
        for value in (
            original.candidate_sha256,
            changed_variant.candidate_sha256,
            changed_feature.candidate_sha256,
            changed_risk.candidate_sha256,
        )
    )
    integer_equivalent = campaign.OrderFlowCandidate(
        config=original.config,
        risk=replace(original.risk, maximum_leverage=1),
    )
    assert integer_equivalent.candidate_sha256 == original.candidate_sha256
    assert campaign._canonical_json_bytes([100, -0.0]) == campaign._canonical_json_bytes([100.0, 0.0])

    with pytest.raises(ValueError, match="maximum hold must be exactly 60"):
        campaign.OrderFlowCandidate(config=replace(original.config, max_hold_bars=11))
    with pytest.raises(ValueError, match="grace must be exactly 60"):
        campaign.OrderFlowCandidate(terminal_liquidation_grace_ms=0)


def test_scenarios_are_exact_bounded_and_stress_dominates() -> None:
    candidate = campaign.OrderFlowCandidate()
    baseline, stress = campaign.orderflow_scenarios(candidate)

    assert candidate.maximum_holding_ms == 60 * _MINUTE_MS
    assert candidate.maximum_liquidation_horizon_ms == 120 * _MINUTE_MS
    assert all(item.maximum_liquidation_horizon_ms == 120 * _MINUTE_MS for item in (baseline, stress))
    assert all(item.policy.terminal_liquidation_grace_ms == 60 * _MINUTE_MS for item in (baseline, stress))
    assert baseline.execution.funding.evidence == "unavailable"
    assert baseline.costs.adverse_funding_bps == 0.0
    assert stress.execution.funding.evidence == "assumed"
    assert stress.execution.funding.source == "assumed_adverse_stress"
    assert stress.costs.adverse_funding_bps == 1.25

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


def test_generator_output_is_bound_to_candidate_horizon_cadence_and_metadata() -> None:
    candidate = campaign.OrderFlowCandidate()
    signal_bar = _candles("BTCUSDT", campaign.ORDERFLOW_EVALUATION_START, 1)[4]
    valid = _intent(signal_bar, "evaluation", candidate.config)

    campaign._validate_generated_intent(
        valid,
        candidate=candidate,
        symbol="BTCUSDT",
    )

    with pytest.raises(ValueError, match="holding bound"):
        campaign._validate_generated_intent(
            replace(
                valid,
                exit_plan=replace(
                    valid.exit_plan,
                    max_holding_ms=valid.exit_plan.max_holding_ms - _MINUTE_MS,
                ),
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
            replace(
                valid,
                entry_eligible_ts_ms=valid.entry_eligible_ts_ms + _MINUTE_MS,
            ),
            candidate=candidate,
            symbol="BTCUSDT",
        )


def test_evidence_rejects_grid_capital_seed_inventory_and_replay_forgery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    evidence = _run_lightweight(monkeypatch)
    baseline, stress = evidence.scenarios

    with pytest.raises(ValueError, match="exactly five"):
        replace(baseline, cells=baseline.cells[:-1])

    forged_portfolio = replace(baseline.portfolio, total_return=99.0, trades=999)
    with pytest.raises(ValueError, match="exactly equal synchronized"):
        replace(baseline, portfolio=forged_portfolio)

    with pytest.raises(ValueError, match="five-way allocation"):
        replace(evidence, cell_initial_equity_usd=10_000.0)
    with pytest.raises(ValueError, match="fixed at 42"):
        replace(evidence, seed=43)
    with pytest.raises(ValueError, match="fixed research screen"):
        replace(evidence, evaluation_end=date(2022, 12, 1))
    with pytest.raises(ValueError, match="protocol fingerprint"):
        replace(evidence, protocol_sha256="0" * 64)

    original_stress_cell = stress.cells[0]
    changed_warmup_count = original_stress_cell.warmup_intents_filtered + 1
    stress_first = replace(
        original_stress_cell,
        generated_intents=original_stress_cell.generated_intents + 1,
        warmup_intents_filtered=changed_warmup_count,
        generated_intents_sha256=campaign._generated_intents_sha256(
            warmup_intents=changed_warmup_count,
            warmup_intents_sha256=original_stress_cell.warmup_intents_sha256,
            evaluated_intents=original_stress_cell.evaluated_intents,
            evaluated_intents_sha256=original_stress_cell.evaluated_intents_sha256,
        ),
    )
    changed_stress = replace(stress, cells=(stress_first, *stress.cells[1:]))
    with pytest.raises(ValueError, match="same intent inventory"):
        replace(evidence, scenarios=(baseline, changed_stress))

    with pytest.raises(ValueError, match="candidate fingerprint"):
        replace(
            evidence,
            candidate=campaign.OrderFlowCandidate(
                config=replace(
                    evidence.candidate.config,
                    variant=OrderFlowExpansionVariant.FLIP_RELEASE,
                )
            ),
        )
    with pytest.raises(ValueError, match="dataset fingerprint"):
        forged_dataset = replace(evidence.datasets[0], candles_sha256="0" * 64)
        replace(evidence, datasets=(forged_dataset, *evidence.datasets[1:]))

    first_dataset, second_dataset, *remaining_datasets = evidence.datasets
    swapped_hashes = {
        first_dataset.symbol: second_dataset.candles_sha256,
        second_dataset.symbol: first_dataset.candles_sha256,
    }
    coordinated_datasets = (
        replace(first_dataset, candles_sha256=swapped_hashes[first_dataset.symbol]),
        replace(second_dataset, candles_sha256=swapped_hashes[second_dataset.symbol]),
        *remaining_datasets,
    )
    coordinated_scenarios = tuple(
        replace(
            scenario,
            cells=tuple(
                replace(
                    cell,
                    dataset_sha256=swapped_hashes.get(cell.symbol, cell.dataset_sha256),
                )
                for cell in scenario.cells
            ),
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
    with pytest.raises(ValueError, match="managed dispositions"):
        replace(cell, evaluated_intents_sha256="0" * 64)
    with pytest.raises(ValueError, match="exact role partition"):
        replace(cell, generated_intents_sha256="0" * 64)
    with pytest.raises(ValueError, match="result seed"):
        replace(cell, evaluation_seed=cell.evaluation_seed + 1)


def test_run_rejects_protocol_seed_capital_universe_and_scenario_mutation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_lightweight_data(monkeypatch)
    dummy = {symbol: [_candles(symbol, campaign.ORDERFLOW_EVALUATION_START, 1)[0]] for symbol in SYMBOLS}
    candidate = campaign.OrderFlowCandidate()
    baseline, stress = campaign.orderflow_scenarios(candidate)

    with pytest.raises(ValueError, match="seed is fixed at 42"):
        campaign.run_orderflow_campaign(dummy, seed=41)
    with pytest.raises(ValueError, match="equity is fixed"):
        campaign.run_orderflow_campaign(dummy, initial_equity_usd=90_000.0)
    with pytest.raises(ValueError, match="exactly the fixed five-symbol"):
        campaign.run_orderflow_campaign({key: value for key, value in dummy.items() if key != SYMBOLS[-1]})
    with pytest.raises(ValueError, match="ordered baseline and stress"):
        campaign.run_orderflow_campaign(dummy, candidate=candidate, scenarios=(stress, baseline))

    mutated_protocol = replace(campaign.DEFAULT_ORDERFLOW_PROTOCOL, max_trials=4)
    with pytest.raises(ValueError, match="fixed v1 research protocol"):
        campaign.run_orderflow_campaign(dummy, protocol=mutated_protocol)
    with pytest.raises(TypeError, match="OrderFlowCandidate"):
        campaign.run_orderflow_campaign(dummy, candidate=False)  # type: ignore[arg-type]


def test_dataset_slice_accepts_zero_volume_but_never_imputes_gaps() -> None:
    generation_start = date(2022, 1, 1)
    evaluation_start = date(2022, 1, 2)
    evaluation_end = date(2022, 1, 4)
    rows = _candles("BTCUSDT", generation_start, 3)
    zero = replace(
        rows[17],
        volume=0.0,
        quote_volume=0.0,
        taker_buy_volume=0.0,
    )
    rows[17] = zero

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
    valid = campaign.OrderFlowDatasetEvidence(
        symbol="BTCUSDT",
        generation_start=campaign.ORDERFLOW_GENERATION_START,
        evaluation_start=campaign.ORDERFLOW_EVALUATION_START,
        evaluation_end=campaign.ORDERFLOW_EVALUATION_END,
        warmup_candles=35 * 24 * 60,
        evaluation_candles=(campaign.ORDERFLOW_EVALUATION_END - campaign.ORDERFLOW_EVALUATION_START).days
        * 24
        * 60,
        warmup_zero_volume_candles=0,
        evaluation_zero_volume_candles=0,
        candles_sha256=digest,
    )

    with pytest.raises(ValueError, match="candle counts"):
        replace(valid, evaluation_candles=1)
    with pytest.raises(ValueError, match="in-range integer"):
        replace(valid, warmup_zero_volume_candles=valid.warmup_candles + 1)
    with pytest.raises(ValueError, match="lowercase SHA-256"):
        replace(valid, candles_sha256=digest.upper())


@pytest.mark.parametrize(
    ("field_name", "replacement"),
    (
        ("symbol", "ETHUSDT"),
        ("timeframe", "5m"),
        ("open_time_ms", _utc_ms(date(2022, 1, 1)) + 1),
        ("close_time_ms", _utc_ms(date(2022, 1, 1)) + _MINUTE_MS),
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
    original = _candles("BTCUSDT", date(2022, 1, 1), 1)[0]
    changed = replace(original, **{field_name: replacement})

    assert campaign._dataset_sha256([changed]) != campaign._dataset_sha256([original])


def test_fixed_protocol_is_unfrozen_reused_research_only() -> None:
    protocol: ResearchProtocol = campaign.DEFAULT_ORDERFLOW_PROTOCOL

    assert protocol.universe == SYMBOLS
    assert protocol.max_trials == 3
    assert protocol.candidate_commit is None
    assert protocol.parameter_set_sha256 is None
    assert protocol.frozen_at is None
    assert protocol.is_frozen is False
    assert protocol.window("research").role.value == "research"
    assert campaign.ORDERFLOW_GENERATION_START == (campaign.ORDERFLOW_EVALUATION_START - timedelta(days=35))
