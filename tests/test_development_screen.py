from __future__ import annotations

import json
import math
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest
from kairos_strategy.candles import Candle

import kairos_backtest.development_screen as screen
from kairos_backtest.data import DatasetManifest
from kairos_backtest.development_campaign import (
    DEFAULT_DEVELOPMENT_PROTOCOL,
    DevelopmentCampaignEvidence,
    DevelopmentCandidate,
    development_scenarios,
)
from kairos_backtest.research_protocol import DataRole, ResearchPurpose
from kairos_backtest.sleeves.trend_pullback_reclaim import (
    PullbackDepthVariant,
    TrendPullbackReclaimConfig,
)


def _metric(
    *,
    log_growth: float = 0.05,
    profit_factor: float | None = 2.0,
    expectancy: float = 1.0,
    drawdown: float = 0.1,
    trades: int = 100,
) -> screen.ScreenMetrics:
    return screen.ScreenMetrics(
        trades=trades,
        net_return=math.expm1(log_growth),
        log_growth=math.log1p(math.expm1(log_growth)),
        profit_factor=profit_factor,
        expectancy_usd_per_trade=expectancy,
        maximum_drawdown=drawdown,
        hac_sharpe=1.25,
        fees_usd=10.0,
        shortfall_usd=4.0,
        funding_usd=2.0,
        rejection_dispositions=tuple((reason, 0) for reason in screen._rejection_inventory()),
    )


def _scenario(
    name: str,
    *,
    log_growth: float = 0.05,
    drawdown: float = 0.1,
    pullback_trades: int = 100,
) -> screen.ScenarioScreenMetrics:
    sleeves = tuple(
        screen.SleeveScreenMetrics(
            sleeve_id=sleeve_id,
            metrics=_metric(
                log_growth=log_growth if sleeve_id == screen.PULLBACK_SLEEVE_ID else 0.05,
                drawdown=drawdown if sleeve_id == screen.PULLBACK_SLEEVE_ID else 0.1,
                trades=pullback_trades if sleeve_id == screen.PULLBACK_SLEEVE_ID else 20,
            ),
        )
        for sleeve_id in screen.SLEEVE_IDS
    )
    return screen.ScenarioScreenMetrics(
        scenario_name=name,
        per_sleeve=sleeves,
        combined=_metric(log_growth=log_growth, drawdown=drawdown, trades=pullback_trades + 40),
    )


def _summary(
    *,
    baseline_log: float = 0.05,
    stress_log: float = 0.04,
    stress_drawdown: float = 0.1,
    pullback_trades: int = 100,
) -> tuple[screen.ScenarioScreenMetrics, ...]:
    return (
        _scenario(
            "baseline",
            log_growth=baseline_log,
            drawdown=0.08,
            pullback_trades=pullback_trades,
        ),
        _scenario(
            "stress",
            log_growth=stress_log,
            drawdown=stress_drawdown,
            pullback_trades=pullback_trades,
        ),
    )


def _fake_evidence(candidate: DevelopmentCandidate) -> DevelopmentCampaignEvidence:
    evidence = object.__new__(DevelopmentCampaignEvidence)
    values = {
        "candidate": candidate,
        "protocol": DEFAULT_DEVELOPMENT_PROTOCOL,
        "protocol_name": DEFAULT_DEVELOPMENT_PROTOCOL.protocol_name,
        "protocol_sha256": DEFAULT_DEVELOPMENT_PROTOCOL.fingerprint(),
        "window_name": screen.WINDOW_NAME,
        "role": DataRole.RESEARCH,
        "purpose": ResearchPurpose.FIT,
        "generation_start": screen.GENERATION_START,
        "evaluation_start": screen.EVALUATION_START,
        "evaluation_end": screen.EVALUATION_END,
        "requested_initial_equity_usd": screen.INITIAL_EQUITY_USD,
        "cell_initial_equity_usd": screen.INITIAL_EQUITY_USD / 15,
        "datasets": (),
        "scenarios": tuple(
            SimpleNamespace(scenario=scenario) for scenario in development_scenarios(candidate)
        ),
        "seed": screen.BASE_SEED,
    }
    for name, value in values.items():
        object.__setattr__(evidence, name, value)
    return evidence


def _install_fast_screen(
    monkeypatch: pytest.MonkeyPatch,
    plan_path: Path,
    result_path: Path,
    *,
    summaries: dict[PullbackDepthVariant, tuple[screen.ScenarioScreenMetrics, ...]],
    fail_on_call: int | None = None,
) -> tuple[list[dict[str, object]], list[tuple[object, ...]]]:
    loader_constructions: list[dict[str, object]] = []
    loader_calls: list[tuple[object, ...]] = []

    class FakeLoader:
        def __init__(self, cache_dir: Path, *, allow_download: bool = True) -> None:
            loader_constructions.append({"cache_dir": cache_dir, "allow_download": allow_download})

        def load(
            self,
            symbol: str,
            start,
            end,
            interval: str,
        ) -> tuple[list[Candle], object]:
            loader_calls.append((symbol, start, end, interval))
            return cast(list[Candle], [object()]), object()

    campaign_calls: list[dict[str, object]] = []

    def fake_campaign(candles_by_symbol, **kwargs):
        assert plan_path.exists()
        assert not result_path.exists()
        campaign_calls.append({"candles_id": id(candles_by_symbol), **kwargs})
        if fail_on_call is not None and len(campaign_calls) == fail_on_call:
            raise RuntimeError("synthetic campaign failure")
        return _fake_evidence(cast(DevelopmentCandidate, kwargs["candidate"]))

    def fake_summary(
        evidence: DevelopmentCampaignEvidence,
    ) -> tuple[screen.ScenarioScreenMetrics, ...]:
        return summaries[evidence.candidate.trend_pullback_reclaim.depth_variant]

    def fake_to_dict(evidence: DevelopmentCampaignEvidence) -> dict[str, object]:
        return {
            "candidate": evidence.candidate.to_dict(),
            "evaluation_end_exclusive": evidence.evaluation_end.isoformat(),
            "evaluation_start": evidence.evaluation_start.isoformat(),
            "full_development_campaign_evidence": True,
        }

    monkeypatch.setattr(screen, "BinanceArchiveLoader", FakeLoader)
    monkeypatch.setattr(screen, "_validate_complete_cached_slice", lambda *_: None)
    monkeypatch.setattr(screen, "run_development_campaign", fake_campaign)
    monkeypatch.setattr(screen, "_summarize_campaign", fake_summary)
    monkeypatch.setattr(DevelopmentCampaignEvidence, "to_dict", fake_to_dict)
    return campaign_calls, loader_calls


def test_plan_is_canonical_and_contains_exactly_the_three_bound_candidates() -> None:
    plan = screen.ExperimentScreenPlan()

    assert tuple(trial.trial_id for trial in plan.trials) == ("SHALLOW", "MEDIUM", "DEEP")
    assert tuple(trial.variant for trial in plan.trials) == screen.TRIAL_VARIANTS
    assert len({trial.candidate_sha256 for trial in plan.trials}) == 3
    assert all(trial.candidate.trend_pullback_reclaim.depth_variant is trial.variant for trial in plan.trials)
    assert plan.evaluation_start == screen.EVALUATION_START == screen.date(2023, 1, 1)
    assert plan.evaluation_end == screen.EVALUATION_END == screen.date(2023, 7, 1)
    assert plan.warmup_days == 35
    assert plan.universe == screen.SYMBOLS
    assert plan.minimum_pullback_closed_trades_per_scenario == 100
    assert plan.purpose is ResearchPurpose.FIT
    assert len(plan.environment.environment_sha256) == 64
    assert plan.environment.source_sha256 == screen.source_fingerprint()
    assert plan.to_dict()["environment"]["uv_lock_sha256"] == plan.environment.uv_lock_sha256
    assert plan.to_dict()["plan_sha256"] == screen._sha256(plan._payload())
    serialized_plan = json.loads(screen._canonical_json_bytes(plan.to_dict()))
    serialized_hash = serialized_plan.pop("plan_sha256")
    assert serialized_hash == screen._sha256(serialized_plan)

    with pytest.raises(ValueError, match="fixed preregistered"):
        replace(plan, trials=plan.trials + (plan.trials[0],))
    with pytest.raises(ValueError, match="candidate SHA-256"):
        replace(plan.trials[0], candidate_sha256="0" * 64)
    with pytest.raises(ValueError, match="differ only"):
        replace(
            plan.trials[0],
            candidate=DevelopmentCandidate(
                trend_pullback_reclaim=TrendPullbackReclaimConfig(depth_variant=PullbackDepthVariant.MEDIUM)
            ),
        )


def test_trial_result_rejects_campaign_evidence_from_another_candidate() -> None:
    plan = screen.ExperimentScreenPlan()
    shallow = plan.trials[0]
    medium_evidence = _fake_evidence(plan.trials[1].candidate)

    with pytest.raises(ValueError, match="does not match its preregistered trial"):
        screen.TrialScreenResult(
            trial=shallow,
            campaign_evidence=medium_evidence,
            scenarios=_summary(),
        )


def test_frequency_guard_has_exact_99_fail_and_100_pass_boundaries() -> None:
    below = _summary(pullback_trades=99)
    at_boundary = _summary(pullback_trades=100)

    failures = screen._eligibility_failures(below, minimum_pullback_trades=100)
    assert failures == (
        "baseline_pullback_closed_trades_below_minimum",
        "stress_pullback_closed_trades_below_minimum",
    )
    assert screen._eligibility_failures(at_boundary, minimum_pullback_trades=100) == ()


@pytest.mark.parametrize(
    ("pullback", "failure"),
    [
        (_metric(log_growth=0.0), "baseline_pullback_log_growth_not_positive"),
        (_metric(profit_factor=1.0), "baseline_pullback_profit_factor_not_above_one"),
        (_metric(expectancy=0.0), "baseline_pullback_expectancy_not_positive"),
    ],
)
def test_each_preregistered_economic_eligibility_gate_is_strict(
    pullback: screen.ScreenMetrics,
    failure: str,
) -> None:
    baseline, stress = _summary()
    baseline_sleeves = tuple(
        replace(item, metrics=pullback) if item.sleeve_id == screen.PULLBACK_SLEEVE_ID else item
        for item in baseline.per_sleeve
    )
    scenarios = (replace(baseline, per_sleeve=baseline_sleeves), stress)

    assert failure in screen._eligibility_failures(scenarios, minimum_pullback_trades=100)


def test_screen_writes_plan_first_runs_exact_trials_and_ranks_deterministically(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan_path = tmp_path / "plan.json"
    result_path = tmp_path / "result.json"
    summaries = {
        PullbackDepthVariant.SHALLOW: _summary(baseline_log=0.05, stress_log=0.04),
        PullbackDepthVariant.MEDIUM: _summary(
            baseline_log=0.07,
            stress_log=0.06,
            stress_drawdown=0.1,
        ),
        PullbackDepthVariant.DEEP: _summary(
            baseline_log=0.07,
            stress_log=0.06,
            stress_drawdown=0.1,
        ),
    }
    campaign_calls, loader_calls = _install_fast_screen(
        monkeypatch,
        plan_path,
        result_path,
        summaries=summaries,
    )

    result = screen.run_development_screen(tmp_path / "cache", plan_path, result_path)

    assert [call[0] for call in loader_calls] == list(screen.SYMBOLS)
    assert all(call[1:] == (screen.GENERATION_START, screen.EVALUATION_END, "1m") for call in loader_calls)
    assert len(campaign_calls) == 3
    assert [
        cast(DevelopmentCandidate, call["candidate"]).trend_pullback_reclaim.depth_variant
        for call in campaign_calls
    ] == list(screen.TRIAL_VARIANTS)
    assert len({call["candles_id"] for call in campaign_calls}) == 1
    for call in campaign_calls:
        assert call["window_name"] == "research"
        assert call["purpose"] is ResearchPurpose.FIT
        assert call["protocol"] == DEFAULT_DEVELOPMENT_PROTOCOL
        assert call["evaluation_start"] == screen.EVALUATION_START
        assert call["evaluation_end"] == screen.EVALUATION_END
        assert call["initial_equity_usd"] == 100_000.0
        assert call["seed"] == 42
        candidate = cast(DevelopmentCandidate, call["candidate"])
        assert call["scenarios"] == development_scenarios(candidate)

    assert result.ranked_eligible_trial_ids == ("MEDIUM", "DEEP", "SHALLOW")
    assert result.selected_trial_id == "MEDIUM"
    plan_document = json.loads(plan_path.read_text(encoding="utf-8"))
    result_document = json.loads(result_path.read_text(encoding="utf-8"))
    assert plan_document["plan_sha256"] == result_document["plan_sha256"]
    assert result_document["trials"][0]["campaign_evidence"]["full_development_campaign_evidence"]
    assert result_document["trials"][0]["metrics"][0]["per_sleeve"]
    assert b": " not in plan_path.read_bytes()
    assert b": " not in result_path.read_bytes()


def test_outputs_are_deterministic_and_always_carry_non_promotion_flags(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    summaries = {variant: _summary() for variant in screen.TRIAL_VARIANTS}
    first_plan = tmp_path / "first-plan.json"
    first_result = tmp_path / "first-result.json"
    _install_fast_screen(
        monkeypatch,
        first_plan,
        first_result,
        summaries=summaries,
    )
    first = screen.run_development_screen(tmp_path / "cache", first_plan, first_result)

    second_plan = tmp_path / "second-plan.json"
    second_result = tmp_path / "second-result.json"
    _install_fast_screen(
        monkeypatch,
        second_plan,
        second_result,
        summaries=summaries,
    )
    second = screen.run_development_screen(tmp_path / "cache", second_plan, second_result)

    assert first_plan.read_bytes() == second_plan.read_bytes()
    assert first_result.read_bytes() == second_result.read_bytes()
    assert first.selected_trial_id == second.selected_trial_id == "SHALLOW"
    document = json.loads(first_result.read_text(encoding="utf-8"))
    assert document["classification"] == "development_diagnostics_only"
    assert document["reused_data"] is True
    assert document["out_of_sample"] is False
    assert document["promotion_eligible"] is False
    assert document["shadow_allowed"] is False
    assert document["live_allowed"] is False
    assert document["permissions"] == {
        "live_allowed": False,
        "promotion_eligible": False,
        "shadow_allowed": False,
    }


def test_ineligible_trials_produce_explicit_reject_all(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan_path = tmp_path / "plan.json"
    result_path = tmp_path / "result.json"
    summaries = {variant: _summary(pullback_trades=99) for variant in screen.TRIAL_VARIANTS}
    _install_fast_screen(
        monkeypatch,
        plan_path,
        result_path,
        summaries=summaries,
    )

    result = screen.run_development_screen(tmp_path / "cache", plan_path, result_path)

    assert result.ranked_eligible_trial_ids == ()
    assert result.selected_trial_id == screen.REJECT_ALL
    assert json.loads(result_path.read_text(encoding="utf-8"))["ranking"]["selected_trial_id"] == "REJECT_ALL"


def test_plan_survives_failure_but_no_partial_result_or_temporary_file_does(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan_path = tmp_path / "plan.json"
    result_path = tmp_path / "result.json"
    summaries = {variant: _summary() for variant in screen.TRIAL_VARIANTS}
    _install_fast_screen(
        monkeypatch,
        plan_path,
        result_path,
        summaries=summaries,
        fail_on_call=2,
    )

    with pytest.raises(RuntimeError, match="synthetic campaign failure"):
        screen.run_development_screen(tmp_path / "cache", plan_path, result_path)

    assert plan_path.exists()
    assert not result_path.exists()
    assert not tuple(tmp_path.glob("*.tmp"))
    assert not tuple(tmp_path.glob(".*.tmp"))


def test_default_is_strictly_offline_and_existing_outputs_are_refused_before_loading(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan_path = tmp_path / "plan.json"
    result_path = tmp_path / "result.json"
    constructions: list[bool] = []

    class MissingOfflineLoader:
        def __init__(self, _cache_dir: Path, *, allow_download: bool = True) -> None:
            constructions.append(allow_download)

        def load(self, *_args, **_kwargs):
            raise FileNotFoundError("cache incomplete")

    monkeypatch.setattr(screen, "BinanceArchiveLoader", MissingOfflineLoader)
    with pytest.raises(FileNotFoundError, match="cache incomplete"):
        screen.run_development_screen(tmp_path / "cache", plan_path, result_path)
    assert constructions == [False]
    assert plan_path.exists()
    assert not result_path.exists()

    with pytest.raises(FileExistsError, match="refusing to overwrite"):
        screen.run_development_screen(tmp_path / "cache", plan_path, result_path)
    assert constructions == [False]


def test_incomplete_manifest_and_rows_fail_without_imputation() -> None:
    manifest = DatasetManifest(
        symbol="BTCUSDT",
        interval="1m",
        requested_start=screen.GENERATION_START.isoformat(),
        requested_end=screen.EVALUATION_END.isoformat(),
        actual_start_ms=0,
        actual_end_ms=0,
        rows=0,
        sha256="0" * 64,
        files=(),
        gaps=1,
        transport_verification="zip_crc_and_parsed_rows_sha256",
        checksum_status="unavailable",
        checksum_files_verified=0,
        expected_files=0,
        csv_schema="binance_futures_kline_v1_12_columns",
    )

    with pytest.raises(ValueError, match="incomplete offline cache evidence"):
        screen._validate_complete_cached_slice("BTCUSDT", [], manifest)


def test_complete_shape_without_official_checksums_fails_closed() -> None:
    expected_rows = (screen.EVALUATION_END - screen.GENERATION_START).days * 24 * 60
    expected_files = len(screen.month_starts(screen.GENERATION_START, screen.EVALUATION_END))
    start_ms = int(
        screen.datetime.combine(screen.GENERATION_START, screen.datetime.min.time(), screen.UTC).timestamp()
        * 1_000
    )
    end_ms = int(
        screen.datetime.combine(screen.EVALUATION_END, screen.datetime.min.time(), screen.UTC).timestamp()
        * 1_000
    )
    manifest = DatasetManifest(
        symbol="BTCUSDT",
        interval="1m",
        requested_start=screen.GENERATION_START.isoformat(),
        requested_end=screen.EVALUATION_END.isoformat(),
        actual_start_ms=start_ms,
        actual_end_ms=end_ms - 1,
        rows=expected_rows,
        sha256="0" * 64,
        files=tuple(f"archive-{index}.zip" for index in range(expected_files)),
        gaps=0,
        transport_verification="zip_crc_and_parsed_rows_sha256",
        checksum_status="unavailable",
        checksum_files_verified=0,
        expected_files=expected_files,
        csv_schema="binance_futures_kline_v1_12_columns",
    )

    with pytest.raises(ValueError, match="verified official checksums"):
        screen._validate_complete_cached_slice("BTCUSDT", [], manifest)
