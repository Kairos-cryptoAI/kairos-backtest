from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from typing import Any

import pytest
from kairos_core.enums import Side
from kairos_strategy.candles import Candle

import kairos_backtest.development_campaign as campaign
from kairos_backtest.cost_risk import AllInCostModel
from kairos_backtest.execution import FundingConfig
from kairos_backtest.research_protocol import (
    DEVELOPMENT_WINDOWS,
    ResearchProtocol,
    ResearchPurpose,
)
from kairos_backtest.scenarios import BASELINE, SYMBOLS
from kairos_backtest.strategy_models import ExitPlan, SleeveIntent

_MINUTE_MS = 60_000
_DAY_MS = 24 * 60 * _MINUTE_MS
_TREND = "trend_breakout_v1"
_RANGE = "range_mean_reversion_v1"
_PULLBACK = "trend_pullback_reclaim_v1"


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


def _universe(start: date, days: int) -> dict[str, list[Candle]]:
    return {symbol: _candles(symbol, start, days) for symbol in SYMBOLS}


def _protocol(*, warmup_days: int = 1) -> ResearchProtocol:
    return ResearchProtocol(
        protocol_name="synthetic-development-test",
        universe=SYMBOLS,
        windows=DEVELOPMENT_WINDOWS,
        max_trials=24,
        maximum_holding_ms=4 * 60 * 60 * 1_000,
        maximum_label_horizon_ms=4 * 60 * 60 * 1_000,
        maximum_execution_latency_ms=500,
        warmup_ms=warmup_days * _DAY_MS,
    )


def _intent(row: Candle, sleeve_id: str, tag: str) -> SleeveIntent:
    eligible = row.close_time_ms + 1
    return SleeveIntent(
        sleeve_id=sleeve_id,
        symbol=row.symbol,
        side=Side.LONG,
        decision_ts_ms=row.close_time_ms,
        entry_eligible_ts_ms=eligible,
        entry_expires_ts_ms=eligible + _MINUTE_MS,
        reference_price=row.close,
        signal_strength=0.75,
        gross_reward_bps=300.0,
        exit_plan=ExitPlan(
            stop_price=99.0,
            target_price=103.0,
            max_holding_ms=_MINUTE_MS,
        ),
        metadata=(("phase", tag),),
    )


def _install_causal_generators(
    monkeypatch: pytest.MonkeyPatch,
    *,
    evaluation_start: date,
) -> list[tuple[str, int, int]]:
    calls: list[tuple[str, int, int]] = []
    boundary_ms = _utc_ms(evaluation_start)

    def factory(sleeve_id: str) -> Callable[[list[Candle], object], list[SleeveIntent]]:
        def generate(rows: list[Candle], _config: object) -> list[SleeveIntent]:
            calls.append((sleeve_id, rows[0].open_time_ms, len(rows)))
            warmup = next(row for row in rows if row.close_time_ms == boundary_ms - 1)
            first_evaluation = next(row for row in rows if row.open_time_ms == boundary_ms)
            return [
                _intent(warmup, sleeve_id, "warmup"),
                _intent(first_evaluation, sleeve_id, "evaluation"),
            ]

        return generate

    monkeypatch.setattr(
        campaign,
        "generate_trend_breakout_intents",
        factory(_TREND),
    )
    monkeypatch.setattr(
        campaign,
        "generate_range_mean_reversion_intents",
        factory(_RANGE),
    )
    monkeypatch.setattr(
        campaign,
        "generate_trend_pullback_reclaim_intents",
        factory(_PULLBACK),
    )
    return calls


def test_campaign_is_causal_synchronized_equal_weight_and_deterministic(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data_start = date(2021, 7, 1)
    evaluation_start = date(2021, 7, 2)
    evaluation_end = date(2021, 7, 4)
    rows = _universe(data_start, 3)
    generator_calls = _install_causal_generators(
        monkeypatch,
        evaluation_start=evaluation_start,
    )
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

    def run() -> campaign.DevelopmentCampaignEvidence:
        return campaign.run_development_campaign(
            rows,
            window_name="research",
            purpose=ResearchPurpose.FIT,
            protocol=_protocol(),
            evaluation_start=evaluation_start,
            evaluation_end=evaluation_end,
            initial_equity_usd=100_000.0,
            seed=91,
        )

    first = run()
    second = run()

    assert first == second
    assert first.to_dict() == second.to_dict()
    assert json.loads(json.dumps(first.to_dict(), allow_nan=False)) == first.to_dict()
    assert len(first.scenarios) == 2
    assert [item.scenario.name for item in first.scenarios] == ["baseline", "stress"]
    baseline_scenario, stress_scenario = [item.scenario for item in first.scenarios]
    assert baseline_scenario.execution.funding.evidence == "unavailable"
    assert baseline_scenario.costs.adverse_funding_bps == 0.0
    assert stress_scenario.execution.funding.evidence == "assumed"
    assert stress_scenario.execution.funding.source == "assumed_adverse_stress"
    assert stress_scenario.costs.adverse_funding_bps == 2.5
    assert all(len(item.cells) == 15 for item in first.scenarios)
    assert first.cell_initial_equity_usd == pytest.approx(100_000 / 15)
    assert all(item.portfolio.initial_equity_usd == pytest.approx(100_000.0) for item in first.scenarios)
    assert all(
        sum(cell.initial_equity_usd for cell in item.cells) == pytest.approx(100_000.0)
        for item in first.scenarios
    )
    cell_ids = [cell.cell_id for item in first.scenarios for cell in item.cells]
    assert len(cell_ids) == len(set(cell_ids)) == 30
    assert all(
        cell.generated_intents == 2
        and cell.warmup_intents_filtered == 1
        and cell.evaluated_intents == 1
        and cell.result.counters.intents == 1
        for item in first.scenarios
        for cell in item.cells
    )
    assert all(
        trade.intent.decision_ts_ms >= _utc_ms(evaluation_start)
        for item in first.scenarios
        for cell in item.cells
        for trade in cell.result.trades
    )
    for scenario_evidence in first.scenarios:
        scenario = scenario_evidence.scenario
        for cell in scenario_evidence.cells:
            assumptions = cell.result.assumptions
            assert assumptions.execution == scenario.execution
            assert assumptions.execution.funding == scenario.execution.funding
            assert assumptions.costs == scenario.costs
            assert assumptions.policy == scenario.policy
            assert assumptions.limits == scenario.limits == first.candidate.risk
            assert (
                assumptions.seed
                == cell.evaluation_seed
                == campaign.derive_seed(
                    first.seed,
                    "development-campaign-v2",
                    first.candidate.candidate_sha256,
                    first.window_name,
                    first.evaluation_start.isoformat(),
                    first.evaluation_end.isoformat(),
                    scenario.name,
                    cell.sleeve_id,
                    cell.symbol,
                )
            )
    assert generator_calls
    assert all(
        start_ms == _utc_ms(data_start) and count == 3 * 24 * 60 for _, start_ms, count in generator_calls
    )
    assert evaluator_calls
    assert all(
        start_ms == _utc_ms(evaluation_start) and count == 2 * 24 * 60 and intents == 1
        for start_ms, count, intents in evaluator_calls
    )
    payload = first.to_dict()
    assert payload["development_only"] is True
    assert payload["reused_data"] is True
    assert payload["out_of_sample"] is False
    assert payload["promotion_eligible"] is False

    forged_portfolio = replace(
        first.scenarios[0].portfolio,
        total_return=99.0,
        trades=999,
    )
    with pytest.raises(ValueError, match="exactly equal synchronized"):
        replace(first.scenarios[0], portfolio=forged_portfolio)
    with pytest.raises(ValueError, match="candle counts"):
        replace(first.datasets[0], evaluation_candles=1)
    with pytest.raises(ValueError, match="protocol fingerprint"):
        replace(first, protocol_sha256="0" * 64)
    with pytest.raises(ValueError, match="equal-weight allocation"):
        replace(first, cell_initial_equity_usd=first.cell_initial_equity_usd * 2)


def test_campaign_evidence_rejects_funding_or_derived_seed_relabelling(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data_start = date(2021, 7, 1)
    evaluation_start = date(2021, 7, 2)
    evaluation_end = date(2021, 7, 4)
    rows = _universe(data_start, 3)
    _install_causal_generators(monkeypatch, evaluation_start=evaluation_start)
    evidence = campaign.run_development_campaign(
        rows,
        window_name="research",
        purpose=ResearchPurpose.FIT,
        protocol=_protocol(),
        evaluation_start=evaluation_start,
        evaluation_end=evaluation_end,
        seed=91,
    )
    baseline = evidence.scenarios[0]
    cell = baseline.cells[0]
    forged_execution = replace(
        cell.result.assumptions.execution,
        funding=FundingConfig(
            rate_8h_bps=0.0,
            source="forged_assumption",
            evidence="assumed",
        ),
    )
    forged_assumptions = replace(cell.result.assumptions, execution=forged_execution)
    with pytest.raises(ValueError, match="self-check SHA-256"):
        replace(
            cell.result,
            assumptions=forged_assumptions,
            assumptions_sha256=forged_assumptions.assumptions_sha256,
        )
    with pytest.raises(ValueError, match="derived campaign seed"):
        replace(evidence, seed=evidence.seed + 1)


def test_candidate_sha_covers_every_typed_sleeve_risk_and_policy_configuration() -> None:
    original = campaign.DevelopmentCandidate()
    changed_trend = campaign.DevelopmentCandidate(
        trend=replace(original.trend, donchian_lookback=original.trend.donchian_lookback + 1),
        range_mean_reversion=original.range_mean_reversion,
        risk=original.risk,
    )
    changed_range = campaign.DevelopmentCandidate(
        trend=original.trend,
        range_mean_reversion=replace(
            original.range_mean_reversion,
            vwap_lookback_bars=original.range_mean_reversion.vwap_lookback_bars + 1,
        ),
        risk=original.risk,
    )
    changed_pullback = campaign.DevelopmentCandidate(
        trend=original.trend,
        range_mean_reversion=original.range_mean_reversion,
        trend_pullback_reclaim=replace(
            original.trend_pullback_reclaim,
            max_hold_bars=original.trend_pullback_reclaim.max_hold_bars + 1,
        ),
        risk=original.risk,
    )
    changed_risk = campaign.DevelopmentCandidate(
        trend=original.trend,
        range_mean_reversion=original.range_mean_reversion,
        risk=replace(original.risk, risk_fraction=original.risk.risk_fraction / 2),
    )
    changed_policy = campaign.DevelopmentCandidate(
        trend=original.trend,
        range_mean_reversion=original.range_mean_reversion,
        risk=original.risk,
        terminal_liquidation_grace_ms=0,
    )

    fingerprints = {
        original.candidate_sha256,
        changed_trend.candidate_sha256,
        changed_range.candidate_sha256,
        changed_pullback.candidate_sha256,
        changed_risk.candidate_sha256,
        changed_policy.candidate_sha256,
    }
    assert len(fingerprints) == 6
    assert all(len(value) == 64 for value in fingerprints)
    integer_equivalent = campaign.DevelopmentCandidate(
        trend=original.trend,
        range_mean_reversion=original.range_mean_reversion,
        trend_pullback_reclaim=original.trend_pullback_reclaim,
        risk=replace(original.risk, maximum_leverage=1),
    )
    assert integer_equivalent.candidate_sha256 == original.candidate_sha256
    assert campaign._canonical_json_bytes([100, -0.0]) == campaign._canonical_json_bytes([100.0, 0.0])


def test_selected_gap_fails_closed_but_explicit_contiguous_subwindow_is_allowed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data_start = date(2021, 7, 1)
    rows = _universe(data_start, 4)
    _install_causal_generators(monkeypatch, evaluation_start=date(2021, 7, 2))
    rows["BTCUSDT"].pop(100)

    with pytest.raises(ValueError, match="gaps are not imputed"):
        campaign.run_development_campaign(
            rows,
            window_name="research",
            purpose=ResearchPurpose.FIT,
            protocol=_protocol(),
            evaluation_start=date(2021, 7, 2),
            evaluation_end=date(2021, 7, 4),
        )

    _install_causal_generators(monkeypatch, evaluation_start=date(2021, 7, 3))
    evidence = campaign.run_development_campaign(
        rows,
        window_name="research",
        purpose=ResearchPurpose.FIT,
        protocol=_protocol(),
        evaluation_start=date(2021, 7, 3),
        evaluation_end=date(2021, 7, 5),
    )

    assert evidence.generation_start == date(2021, 7, 2)
    assert all(dataset.warmup_candles == 24 * 60 for dataset in evidence.datasets)
    assert all(dataset.evaluation_candles == 2 * 24 * 60 for dataset in evidence.datasets)


def test_role_and_scenario_contracts_fail_closed() -> None:
    rows = _universe(date(2021, 7, 1), 3)

    with pytest.raises(TypeError, match="candidate must be a DevelopmentCandidate"):
        campaign.run_development_campaign(
            rows,
            window_name="research",
            purpose=ResearchPurpose.FIT,
            protocol=_protocol(),
            evaluation_start=date(2021, 7, 1),
            evaluation_end=date(2021, 7, 3),
            candidate=False,  # type: ignore[arg-type]
        )

    with pytest.raises(PermissionError, match="selection data cannot be used to fit"):
        campaign.run_development_campaign(
            rows,
            window_name="selection",
            purpose=ResearchPurpose.FIT,
            protocol=_protocol(),
            evaluation_start=date(2024, 7, 1),
            evaluation_end=date(2024, 7, 3),
        )
    with pytest.raises(PermissionError, match="cannot be used to promote"):
        campaign.run_development_campaign(
            rows,
            window_name="research",
            purpose=ResearchPurpose.PROMOTE,
            protocol=_protocol(),
            evaluation_start=date(2021, 7, 1),
            evaluation_end=date(2021, 7, 3),
        )

    candidate = campaign.DevelopmentCandidate()
    baseline, stress = campaign.development_scenarios(candidate)
    with pytest.raises(ValueError, match="ordered baseline and stress"):
        campaign.run_development_campaign(
            rows,
            window_name="research",
            purpose=ResearchPurpose.FIT,
            protocol=_protocol(),
            evaluation_start=date(2021, 7, 1),
            evaluation_end=date(2021, 7, 3),
            candidate=candidate,
            scenarios=(baseline, baseline),
        )
    with pytest.raises(ValueError, match="ordered baseline and stress"):
        campaign.run_development_campaign(
            rows,
            window_name="research",
            purpose=ResearchPurpose.FIT,
            protocol=_protocol(),
            evaluation_start=date(2021, 7, 1),
            evaluation_end=date(2021, 7, 3),
            candidate=candidate,
            scenarios=(),
        )
    forged_baseline = replace(stress, name="baseline")
    with pytest.raises(ValueError, match="fixed candidate factory"):
        campaign.run_development_campaign(
            rows,
            window_name="research",
            purpose=ResearchPurpose.FIT,
            protocol=_protocol(),
            evaluation_start=date(2021, 7, 1),
            evaluation_end=date(2021, 7, 3),
            candidate=candidate,
            scenarios=(forged_baseline, stress),
        )
    with pytest.raises(ValueError, match="exactly match execution"):
        campaign.DevelopmentScenario(
            "baseline",
            BASELINE,
            replace(
                baseline.costs,
                spread_bps=baseline.costs.spread_bps + 0.25,
            ),
            baseline.policy,
            baseline.limits,
            candidate.maximum_liquidation_horizon_ms,
        )
    with pytest.raises(ValueError, match="exactly match execution"):
        campaign.DevelopmentScenario(
            "baseline",
            BASELINE,
            replace(
                baseline.costs,
                spread_bps=baseline.costs.spread_bps - 1e-13,
            ),
            baseline.policy,
            baseline.limits,
            candidate.maximum_liquidation_horizon_ms,
        )
    assert isinstance(baseline.costs, AllInCostModel)
    assert baseline.execution.funding.evidence == "unavailable"


def test_full_fixed_window_requires_complete_input_and_never_silently_truncates() -> None:
    rows = _universe(date(2021, 7, 1), 3)

    with pytest.raises(ValueError, match="gaps are not imputed"):
        campaign.run_development_campaign(
            rows,
            window_name="research",
            purpose=ResearchPurpose.FIT,
            protocol=_protocol(),
        )


def test_evaluation_boundaries_must_be_complete_utc_days_inside_the_role() -> None:
    rows = _universe(date(2021, 7, 1), 3)

    with pytest.raises(ValueError, match="at least two complete UTC days"):
        campaign.run_development_campaign(
            rows,
            window_name="research",
            purpose=ResearchPurpose.FIT,
            protocol=_protocol(),
            evaluation_start=date(2021, 7, 2),
            evaluation_end=date(2021, 7, 3),
        )
    with pytest.raises(ValueError, match="inside its registered data role"):
        campaign.run_development_campaign(
            rows,
            window_name="research",
            purpose=ResearchPurpose.FIT,
            protocol=_protocol(),
            evaluation_start=date(2021, 6, 30),
            evaluation_end=date(2021, 7, 2),
        )


def test_default_protocol_keeps_the_three_fixed_reused_data_roles() -> None:
    assert campaign.DEFAULT_DEVELOPMENT_PROTOCOL.windows == DEVELOPMENT_WINDOWS
    assert [
        (window.name, window.start, window.end, window.role.value)
        for window in campaign.DEFAULT_DEVELOPMENT_PROTOCOL.windows
    ] == [
        ("research", date(2021, 7, 1), date(2024, 7, 1), "research"),
        ("selection", date(2024, 7, 1), date(2025, 7, 1), "selection"),
        ("robustness", date(2025, 7, 1), date(2026, 8, 1), "robustness"),
    ]
    assert timedelta(milliseconds=campaign.DEFAULT_DEVELOPMENT_PROTOCOL.warmup_ms).days == 35
    assert campaign.DEFAULT_DEVELOPMENT_PROTOCOL.max_trials == 3
