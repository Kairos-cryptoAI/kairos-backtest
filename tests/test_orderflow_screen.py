from __future__ import annotations

import hashlib
import json
import math
import sys
from dataclasses import asdict, replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest
from kairos_strategy.candles import Candle

import kairos_backtest.orderflow_screen as screen
from kairos_backtest.data import DatasetManifest
from kairos_backtest.orderflow_campaign import (
    DEFAULT_ORDERFLOW_PROTOCOL,
    OrderFlowCampaignEvidence,
    OrderFlowCandidate,
    orderflow_scenarios,
)
from kairos_backtest.provenance import RuntimeProvenance
from kairos_backtest.research_protocol import DataRole, ResearchPurpose
from kairos_backtest.sleeves.orderflow_volatility_expansion import OrderFlowExpansionVariant


def _environment() -> screen.OrderFlowScreenEnvironment:
    return screen.OrderFlowScreenEnvironment(
        git_head_sha="1" * 40,
        git_tree_sha="2" * 40,
        git_dirty=False,
        source_sha256="3" * 64,
        source_files=(("kairos_backtest/orderflow_screen.py", "4" * 64),),
        pyproject_sha256="5" * 64,
        uv_lock_sha256="6" * 64,
        runtime=RuntimeProvenance(
            python="3.11.0",
            implementation="CPython",
            platform="test",
            packages=(("kairos-backtest", "0.1.0"),),
        ),
    )


def _snapshot() -> screen._EnvironmentSnapshot:
    environment = _environment()
    return screen._EnvironmentSnapshot(
        git_head_sha=environment.git_head_sha,
        git_tree_sha=environment.git_tree_sha,
        source_sha256=environment.source_sha256,
        source_files=environment.source_files,
        pyproject_sha256=environment.pyproject_sha256,
        uv_lock_sha256=environment.uv_lock_sha256,
        runtime=environment.runtime,
    )


def _metric(
    *,
    log_growth: float = 0.04,
    profit_factor: float | None = 1.5,
    expectancy: float = 1.0,
    drawdown: float = 0.04,
    trades: int = 40,
) -> screen.OrderFlowMetrics:
    net_return = math.expm1(log_growth)
    return screen.OrderFlowMetrics(
        trades=trades,
        net_return=net_return,
        log_growth=math.log1p(net_return),
        profit_factor=profit_factor,
        expectancy_usd_per_trade=expectancy,
        maximum_drawdown=drawdown,
        hac_sharpe=1.0,
        fees_usd=10.0,
        shortfall_usd=4.0,
        funding_usd=2.0,
        rejection_dispositions=tuple((reason, 0) for reason in screen._rejection_inventory()),
    )


def _scenario(
    name: str,
    *,
    log_growth: float = 0.04,
    profit_factor: float | None = 1.5,
    expectancy: float = 1.0,
    drawdown: float = 0.04,
    counts: tuple[int, int, int, int, int] = (40, 40, 40, 40, 40),
    symbol_expectancies: tuple[float, float, float, float, float] = (1, 1, 1, 1, 1),
    exit_days: int = 60,
) -> screen.OrderFlowScenarioMetrics:
    per_symbol = tuple(
        screen.OrderFlowSymbolMetrics(
            symbol=symbol,
            metrics=_metric(trades=trades, expectancy=symbol_expectancy),
        )
        for symbol, trades, symbol_expectancy in zip(
            screen.SYMBOLS,
            counts,
            symbol_expectancies,
            strict=True,
        )
    )
    total = sum(counts)
    return screen.OrderFlowScenarioMetrics(
        scenario_name=name,
        per_symbol=per_symbol,
        combined=_metric(
            log_growth=log_growth,
            profit_factor=profit_factor,
            expectancy=expectancy,
            drawdown=drawdown,
            trades=total,
        ),
        distinct_utc_exit_days=exit_days,
        positive_expectancy_symbols=sum(value > 0 for value in symbol_expectancies),
        maximum_one_symbol_trade_share=max(counts) / total if total else 0.0,
    )


def _summary(
    *,
    baseline_log: float = 0.04,
    stress_log: float = 0.03,
    stress_drawdown: float = 0.04,
    counts: tuple[int, int, int, int, int] = (40, 40, 40, 40, 40),
    symbol_expectancies: tuple[float, float, float, float, float] = (1, 1, 1, 1, 1),
    exit_days: int = 60,
) -> tuple[screen.OrderFlowScenarioMetrics, ...]:
    return (
        _scenario(
            "baseline",
            log_growth=baseline_log,
            counts=counts,
            symbol_expectancies=symbol_expectancies,
            exit_days=exit_days,
        ),
        _scenario(
            "stress",
            log_growth=stress_log,
            drawdown=stress_drawdown,
            counts=counts,
            symbol_expectancies=symbol_expectancies,
            exit_days=exit_days,
        ),
    )


def _fake_evidence(candidate: OrderFlowCandidate) -> OrderFlowCampaignEvidence:
    evidence = object.__new__(OrderFlowCampaignEvidence)
    values = {
        "candidate": candidate,
        "protocol": DEFAULT_ORDERFLOW_PROTOCOL,
        "protocol_name": DEFAULT_ORDERFLOW_PROTOCOL.protocol_name,
        "protocol_sha256": DEFAULT_ORDERFLOW_PROTOCOL.fingerprint(),
        "window_name": screen.WINDOW_NAME,
        "role": DataRole.RESEARCH,
        "purpose": ResearchPurpose.FIT,
        "generation_start": screen.GENERATION_START,
        "evaluation_start": screen.EVALUATION_START,
        "evaluation_end": screen.EVALUATION_END,
        "requested_initial_equity_usd": screen.INITIAL_EQUITY_USD,
        "cell_initial_equity_usd": screen.INITIAL_EQUITY_USD / len(screen.SYMBOLS),
        "datasets": (),
        "scenarios": tuple(SimpleNamespace(scenario=scenario) for scenario in orderflow_scenarios(candidate)),
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
    summaries: dict[OrderFlowExpansionVariant, tuple[screen.OrderFlowScenarioMetrics, ...]],
    fail_on_call: int | None = None,
    mutate_plan: bool = False,
    summary_path: Path | None = None,
) -> tuple[list[dict[str, object]], list[tuple[object, ...]], list[bool]]:
    loader_calls: list[tuple[object, ...]] = []
    loader_modes: list[bool] = []

    class FakeLoader:
        def __init__(self, _cache_dir: Path, *, allow_download: bool = True) -> None:
            loader_modes.append(allow_download)

        def load(self, symbol: str, start, end, interval: str):
            loader_calls.append((symbol, start, end, interval))
            return cast(list[Candle], [object()]), object()

    campaign_calls: list[dict[str, object]] = []

    def fake_campaign(candles_by_symbol, **kwargs):
        assert plan_path.exists()
        assert not result_path.exists()
        assert summary_path is None or not summary_path.exists()
        campaign_calls.append({"candles_id": id(candles_by_symbol), **kwargs})
        if mutate_plan and len(campaign_calls) == 1:
            plan_path.write_text("tampered\n", encoding="utf-8")
        if fail_on_call is not None and len(campaign_calls) == fail_on_call:
            raise RuntimeError("synthetic campaign failure")
        return _fake_evidence(cast(OrderFlowCandidate, kwargs["candidate"]))

    def fake_summary(evidence: OrderFlowCampaignEvidence):
        return summaries[evidence.candidate.config.variant]

    def fake_to_dict(evidence: OrderFlowCampaignEvidence) -> dict[str, object]:
        return {
            "candidate": evidence.candidate.to_dict(),
            "evaluation_end_exclusive": evidence.evaluation_end.isoformat(),
            "evaluation_start": evidence.evaluation_start.isoformat(),
            "full_orderflow_campaign_evidence": True,
        }

    monkeypatch.setattr(screen, "_capture_clean_environment", _environment)
    monkeypatch.setattr(screen, "_assert_environment_stable", lambda _expected: None)
    monkeypatch.setattr(screen, "BinanceArchiveLoader", FakeLoader)
    monkeypatch.setattr(screen, "_validate_complete_cached_slice", lambda *_args: None)
    monkeypatch.setattr(screen, "run_orderflow_campaign", fake_campaign)
    monkeypatch.setattr(screen, "_summarize_campaign", fake_summary)
    monkeypatch.setattr(OrderFlowCampaignEvidence, "to_dict", fake_to_dict)
    return campaign_calls, loader_calls, loader_modes


def test_plan_binds_clean_git_environment_fixed_data_and_exact_three_candidates() -> None:
    plan = screen.OrderFlowScreenPlan(environment=_environment())

    assert plan.version == "kairos.orderflow-development-screen.v1"
    assert tuple(trial.trial_id for trial in plan.trials) == (
        "IMPULSE",
        "PERSISTENCE",
        "FLIP_RELEASE",
    )
    assert tuple(trial.variant for trial in plan.trials) == screen.TRIAL_VARIANTS
    assert len({trial.candidate_sha256 for trial in plan.trials}) == 3
    assert all(trial.candidate.config.variant is trial.variant for trial in plan.trials)
    assert plan.generation_start == date(2022, 5, 27)
    assert plan.evaluation_start == date(2022, 7, 1)
    assert plan.evaluation_end == date(2023, 1, 1)
    assert plan.warmup_days == 35
    assert plan.universe == screen.SYMBOLS
    assert plan.role is DataRole.RESEARCH
    assert plan.purpose is ResearchPurpose.FIT
    assert plan.environment.git_dirty is False
    assert plan.to_dict()["environment"]["git"]["head_sha"] == "1" * 40
    assert plan.to_dict()["environment"]["source_files"] == [
        {"path": "kairos_backtest/orderflow_screen.py", "sha256": "4" * 64}
    ]
    assert plan.to_dict()["plan_sha256"] == screen._sha256(plan._payload())
    assert plan.to_dict()["eligibility"]["trade_count_is_ranking_objective"] is False

    with pytest.raises(ValueError, match="fixed preregistered"):
        replace(plan, trials=plan.trials + (plan.trials[0],))
    with pytest.raises(ValueError, match="exact registered"):
        replace(plan.trials[0], candidate=plan.trials[1].candidate)
    with pytest.raises(ValueError, match="git_dirty=false"):
        replace(plan.environment, git_dirty=True)


def test_environment_capture_refuses_git_dirtiness_seen_by_both_status_checks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, ...]] = []

    def fake_git(_root: Path, *arguments: str) -> str:
        calls.append(arguments)
        return " M pyproject.toml"

    monkeypatch.setattr(screen, "_git_text", fake_git)
    monkeypatch.setattr(screen, "_read_environment_snapshot", lambda _root: _snapshot())

    with pytest.raises(RuntimeError, match="dirty Git worktree"):
        screen.OrderFlowScreenEnvironment.capture()
    assert calls == [
        ("status", "--porcelain=v1", "--untracked-files=all"),
        ("status", "--porcelain=v1", "--untracked-files=all"),
    ]


def test_environment_capture_and_stability_are_double_checked_against_toctou(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    status_calls: list[tuple[str, ...]] = []

    def clean_git(_root: Path, *arguments: str) -> str:
        status_calls.append(arguments)
        return ""

    first = _snapshot()
    changed = replace(first, source_sha256="a" * 64)
    snapshots = iter((first, changed))
    monkeypatch.setattr(screen, "_git_text", clean_git)
    monkeypatch.setattr(screen, "_read_environment_snapshot", lambda _root: next(snapshots))

    with pytest.raises(RuntimeError, match="changed while it was being captured"):
        screen.OrderFlowScreenEnvironment.capture()
    assert status_calls == [
        ("status", "--porcelain=v1", "--untracked-files=all"),
        ("status", "--porcelain=v1", "--untracked-files=all"),
    ]

    status_calls.clear()
    snapshots = iter((first, first))
    screen._assert_environment_stable(_environment())
    assert len(status_calls) == 2
    assert status_calls[0] == status_calls[1]
    assert status_calls[0][-4:] == (
        "--",
        ":(glob)kairos_backtest/**/*.py",
        "pyproject.toml",
        "uv.lock",
    )


@pytest.mark.parametrize(
    ("scenarios", "failure"),
    [
        (
            (_scenario("baseline", log_growth=0.0), _scenario("stress")),
            "baseline_log_growth_not_positive",
        ),
        (
            (_scenario("baseline", profit_factor=1.0), _scenario("stress")),
            "baseline_profit_factor_not_above_one",
        ),
        (
            (_scenario("baseline", expectancy=0.0), _scenario("stress")),
            "baseline_expectancy_not_positive",
        ),
        (
            _summary(counts=(39, 40, 40, 40, 40)),
            "baseline_closed_trades_below_minimum",
        ),
        (
            _summary(counts=(19, 46, 45, 45, 45)),
            "baseline_BTCUSDT_closed_trades_below_minimum",
        ),
        (
            _summary(exit_days=59),
            "baseline_distinct_utc_exit_days_below_minimum",
        ),
        (
            _summary(symbol_expectancies=(1, 1, 0, 0, 0)),
            "baseline_positive_expectancy_symbols_below_minimum",
        ),
        (
            _summary(counts=(101, 25, 25, 25, 24)),
            "baseline_one_symbol_trade_share_above_maximum",
        ),
        (
            _summary(stress_drawdown=0.0500000001),
            "stress_maximum_drawdown_above_maximum",
        ),
    ],
)
def test_each_eligibility_gate_is_strict(
    scenarios: tuple[screen.OrderFlowScenarioMetrics, ...],
    failure: str,
) -> None:
    assert failure in screen._eligibility_failures(scenarios)


def test_eligibility_boundaries_pass_exactly_and_trade_count_is_not_ranking_objective() -> None:
    scenarios = _summary(
        counts=(100, 25, 25, 25, 25),
        symbol_expectancies=(1, 1, 1, 0, 0),
        exit_days=60,
        stress_drawdown=0.05,
    )

    assert screen._eligibility_failures(scenarios) == ()


def test_ranking_uses_worst_growth_then_stress_drawdown_then_fixed_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = screen.OrderFlowScreenPlan(environment=_environment())
    summaries = {
        OrderFlowExpansionVariant.IMPULSE: _summary(
            baseline_log=0.08,
            stress_log=0.06,
            stress_drawdown=0.04,
        ),
        OrderFlowExpansionVariant.PERSISTENCE: _summary(
            baseline_log=0.06,
            stress_log=0.06,
            stress_drawdown=0.03,
        ),
        OrderFlowExpansionVariant.FLIP_RELEASE: _summary(
            baseline_log=0.06,
            stress_log=0.06,
            stress_drawdown=0.03,
        ),
    }
    monkeypatch.setattr(
        screen,
        "_summarize_campaign",
        lambda evidence: summaries[evidence.candidate.config.variant],
    )
    trials = tuple(
        screen.OrderFlowTrialResult(
            trial=trial,
            campaign_evidence=(evidence := _fake_evidence(trial.candidate)),
            scenarios=summaries[evidence.candidate.config.variant],
        )
        for trial in plan.trials
    )

    result = screen.OrderFlowScreenResult(plan=plan, trials=trials)

    assert result.ranked_eligible_trial_ids == ("PERSISTENCE", "FLIP_RELEASE", "IMPULSE")
    assert result.selected_trial_id == "PERSISTENCE"


def test_screen_writes_plan_before_cache_runs_exact_trials_and_is_deterministic(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    summaries = {variant: _summary() for variant in screen.TRIAL_VARIANTS}
    first_plan = tmp_path / "first-plan.json"
    first_result = tmp_path / "first-result.json"
    first_summary = tmp_path / "first-summary.json"
    calls, loads, loader_modes = _install_fast_screen(
        monkeypatch,
        first_plan,
        first_result,
        summaries=summaries,
        summary_path=first_summary,
    )

    first = screen.run_orderflow_screen(
        tmp_path / "cache",
        first_plan,
        first_result,
        summary_output=first_summary,
    )

    assert loader_modes == [False]
    assert [call[0] for call in loads] == list(screen.SYMBOLS)
    assert all(call[1:] == (screen.GENERATION_START, screen.EVALUATION_END, "1m") for call in loads)
    assert [cast(OrderFlowCandidate, call["candidate"]).config.variant for call in calls] == list(
        screen.TRIAL_VARIANTS
    )
    assert len({call["candles_id"] for call in calls}) == 1
    for call in calls:
        candidate = cast(OrderFlowCandidate, call["candidate"])
        assert call["protocol"] == DEFAULT_ORDERFLOW_PROTOCOL
        assert call["initial_equity_usd"] == 100_000.0
        assert call["seed"] == 42
        assert call["scenarios"] == orderflow_scenarios(candidate)
    plan_document = json.loads(first_plan.read_text(encoding="utf-8"))
    result_document = json.loads(first_result.read_text(encoding="utf-8"))
    summary_document = json.loads(first_summary.read_text(encoding="utf-8"))
    assert plan_document["plan_version"] == screen.PLAN_VERSION
    assert result_document["schema_version"] == 1
    assert plan_document["plan_sha256"] == result_document["plan_sha256"]
    assert result_document["trials"][0]["campaign_evidence"]["full_orderflow_campaign_evidence"]
    assert result_document["permissions"] == {
        "live_allowed": False,
        "promotion_eligible": False,
        "shadow_allowed": False,
    }
    assert first.selected_trial_id == "IMPULSE"
    assert b": " not in first_plan.read_bytes()
    assert b": " not in first_result.read_bytes()
    assert b": " not in first_summary.read_bytes()
    assert summary_document == screen.compact_orderflow_summary(first)

    second_plan = tmp_path / "second-plan.json"
    second_result = tmp_path / "second-result.json"
    second_summary = tmp_path / "second-summary.json"
    _install_fast_screen(
        monkeypatch,
        second_plan,
        second_result,
        summaries=summaries,
        summary_path=second_summary,
    )
    second = screen.run_orderflow_screen(
        tmp_path / "cache",
        second_plan,
        second_result,
        summary_output=second_summary,
    )
    assert first_plan.read_bytes() == second_plan.read_bytes()
    assert first_result.read_bytes() == second_result.read_bytes()
    assert first_summary.read_bytes() == second_summary.read_bytes()
    compact = screen.compact_orderflow_summary(first)
    assert compact == screen.compact_orderflow_summary(second)
    full_result_bytes = screen._serialized_json(first.to_dict())
    assert compact["full_result"] == {
        "bytes": len(full_result_bytes),
        "schema_version": 1,
        "sha256": hashlib.sha256(full_result_bytes).hexdigest(),
    }


def test_reject_all_and_plan_survives_campaign_or_plan_drift_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rejected = {variant: _summary(counts=(39, 40, 40, 40, 40)) for variant in screen.TRIAL_VARIANTS}
    plan_path = tmp_path / "reject-plan.json"
    result_path = tmp_path / "reject-result.json"
    _install_fast_screen(monkeypatch, plan_path, result_path, summaries=rejected)
    result = screen.run_orderflow_screen(tmp_path / "cache", plan_path, result_path)
    assert result.selected_trial_id == screen.REJECT_ALL

    failure_plan = tmp_path / "failure-plan.json"
    failure_result = tmp_path / "failure-result.json"
    _install_fast_screen(
        monkeypatch,
        failure_plan,
        failure_result,
        summaries={variant: _summary() for variant in screen.TRIAL_VARIANTS},
        fail_on_call=2,
    )
    with pytest.raises(RuntimeError, match="synthetic campaign failure"):
        screen.run_orderflow_screen(tmp_path / "cache", failure_plan, failure_result)
    assert failure_plan.exists()
    assert not failure_result.exists()

    drift_plan = tmp_path / "drift-plan.json"
    drift_result = tmp_path / "drift-result.json"
    _install_fast_screen(
        monkeypatch,
        drift_plan,
        drift_result,
        summaries={variant: _summary() for variant in screen.TRIAL_VARIANTS},
        mutate_plan=True,
    )
    with pytest.raises(RuntimeError, match="plan changed"):
        screen.run_orderflow_screen(tmp_path / "cache", drift_plan, drift_result)
    assert drift_plan.read_text(encoding="utf-8") == "tampered\n"
    assert not drift_result.exists()
    assert not tuple(tmp_path.glob(".*.tmp"))


def test_default_is_offline_and_existing_outputs_are_refused_before_cache_access(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan_path = tmp_path / "plan.json"
    result_path = tmp_path / "result.json"
    constructions: list[bool] = []

    class MissingLoader:
        def __init__(self, _cache_dir: Path, *, allow_download: bool = True) -> None:
            constructions.append(allow_download)

        def load(self, *_args, **_kwargs):
            raise FileNotFoundError("cache incomplete")

    monkeypatch.setattr(screen, "_capture_clean_environment", _environment)
    monkeypatch.setattr(screen, "BinanceArchiveLoader", MissingLoader)
    with pytest.raises(FileNotFoundError, match="cache incomplete"):
        screen.run_orderflow_screen(tmp_path / "cache", plan_path, result_path)
    assert constructions == [False]
    assert plan_path.exists()
    assert not result_path.exists()

    with pytest.raises(FileExistsError, match="refusing to overwrite"):
        screen.run_orderflow_screen(tmp_path / "cache", plan_path, result_path)
    assert constructions == [False]


def test_all_output_paths_are_distinct_and_existing_summary_is_refused_without_overwrite(
    tmp_path: Path,
) -> None:
    plan_path = tmp_path / "plan.json"
    result_path = tmp_path / "result.json"
    summary_path = tmp_path / "summary.json"

    for duplicate in (
        (plan_path, result_path, plan_path),
        (plan_path, result_path, result_path),
        (plan_path, plan_path, summary_path),
    ):
        with pytest.raises(ValueError, match="must be different files"):
            screen._validate_output_paths(*duplicate, overwrite=False)

    for existing in (plan_path, result_path, summary_path):
        existing.write_text("existing\n", encoding="utf-8")
        with pytest.raises(FileExistsError, match="refusing to overwrite"):
            screen._validate_output_paths(
                plan_path,
                result_path,
                summary_path,
                overwrite=False,
            )
        existing.unlink()


def test_cli_help_documents_default_compact_summary_output(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(sys, "argv", ["kairos-orderflow-screen", "--help"])

    with pytest.raises(SystemExit) as exit_info:
        screen.main()

    assert exit_info.value.code == 0
    output = capsys.readouterr().out
    assert "--summary-output" in output

    calls: list[dict[str, object]] = []

    def fake_run(*_args, **kwargs):
        calls.append(kwargs)
        return object()

    monkeypatch.setattr(sys, "argv", ["kairos-orderflow-screen"])
    monkeypatch.setattr(screen, "run_orderflow_screen", fake_run)
    monkeypatch.setattr(screen, "_print_summary", lambda _result: None)
    assert screen.main() == 0
    assert calls == [
        {
            "summary_output": Path("reports/orderflow-screen/summary.json"),
            "overwrite": False,
        }
    ]


def test_explicit_overwrite_replaces_all_outputs_without_exposing_stale_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan_path = tmp_path / "plan.json"
    result_path = tmp_path / "result.json"
    summary_path = tmp_path / "summary.json"
    plan_path.write_text("stale plan\n", encoding="utf-8")
    result_path.write_text("stale result\n", encoding="utf-8")
    summary_path.write_text("stale summary\n", encoding="utf-8")
    summaries = {variant: _summary() for variant in screen.TRIAL_VARIANTS}
    _install_fast_screen(
        monkeypatch,
        plan_path,
        result_path,
        summaries=summaries,
        summary_path=summary_path,
    )

    result = screen.run_orderflow_screen(
        tmp_path / "cache",
        plan_path,
        result_path,
        summary_output=summary_path,
        overwrite=True,
    )

    assert result.selected_trial_id == "IMPULSE"
    assert json.loads(plan_path.read_text(encoding="utf-8"))["plan_version"] == screen.PLAN_VERSION
    assert json.loads(result_path.read_text(encoding="utf-8"))["schema_version"] == 1
    assert json.loads(summary_path.read_text(encoding="utf-8")) == screen.compact_orderflow_summary(result)


def test_overwrite_crash_after_new_plan_publish_cannot_leave_the_old_result(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan_path = tmp_path / "plan.json"
    result_path = tmp_path / "result.json"
    summary_path = tmp_path / "summary.json"
    plan_path.write_text("stale plan\n", encoding="utf-8")
    result_path.write_text("stale result\n", encoding="utf-8")
    summary_path.write_text("stale summary\n", encoding="utf-8")
    _install_fast_screen(
        monkeypatch,
        plan_path,
        result_path,
        summaries={variant: _summary() for variant in screen.TRIAL_VARIANTS},
        summary_path=summary_path,
    )
    atomic_write = screen._atomic_write_json
    fsync_directory = screen._fsync_directory
    fsync_calls: list[Path] = []

    def record_fsync(path: Path) -> None:
        fsync_calls.append(path)
        fsync_directory(path)

    def crash_after_publish(path: Path, payload: object, *, overwrite: bool) -> None:
        if path == plan_path:
            assert not result_path.exists()
            assert not summary_path.exists()
            assert fsync_calls == [result_path.parent, summary_path.parent]
        atomic_write(path, payload, overwrite=overwrite)
        if path == plan_path:
            raise RuntimeError("synthetic crash after plan publish")

    monkeypatch.setattr(screen, "_fsync_directory", record_fsync)
    monkeypatch.setattr(screen, "_atomic_write_json", crash_after_publish)

    with pytest.raises(RuntimeError, match="synthetic crash"):
        screen.run_orderflow_screen(
            tmp_path / "cache",
            plan_path,
            result_path,
            summary_output=summary_path,
            overwrite=True,
        )

    assert json.loads(plan_path.read_text(encoding="utf-8"))["plan_version"] == screen.PLAN_VERSION
    assert not result_path.exists()
    assert not summary_path.exists()
    assert fsync_calls == [result_path.parent, summary_path.parent, plan_path.parent]


def _manifest(
    *,
    start: date,
    end: date,
    candles: list[Candle],
    gaps: int = 0,
    checksum_status: str = "official_sha256_verified",
) -> DatasetManifest:
    start_ms = int(datetime.combine(start, datetime.min.time(), UTC).timestamp() * 1_000)
    end_ms = int(datetime.combine(end, datetime.min.time(), UTC).timestamp() * 1_000)
    files = tuple(f"BTCUSDT-1m-{month:%Y-%m}.zip" for month in screen.month_starts(start, end))
    digest = hashlib.sha256(
        json.dumps([asdict(candle) for candle in candles], separators=(",", ":")).encode()
    ).hexdigest()
    return DatasetManifest(
        symbol="BTCUSDT",
        interval="1m",
        requested_start=start.isoformat(),
        requested_end=end.isoformat(),
        actual_start_ms=start_ms,
        actual_end_ms=end_ms - 1,
        rows=len(candles),
        sha256=digest,
        files=files,
        gaps=gaps,
        transport_verification="zip_crc_and_parsed_rows_sha256",
        checksum_status=checksum_status,
        checksum_files_verified=len(files) if checksum_status == "official_sha256_verified" else 0,
        expected_files=len(files),
        csv_schema="binance_futures_kline_v1_12_columns",
    )


def test_cache_validation_rejects_gap_checksum_and_quote_taker_domain_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    start = date(2022, 5, 1)
    end = start + timedelta(days=1)
    start_ms = int(datetime.combine(start, datetime.min.time(), UTC).timestamp() * 1_000)
    candles = [
        Candle(
            symbol="BTCUSDT",
            timeframe="1m",
            open_time_ms=start_ms + index * 60_000,
            close_time_ms=start_ms + index * 60_000 + 59_999,
            open=100.0,
            high=101.0,
            low=99.0,
            close=100.0,
            volume=10.0,
            quote_volume=1_000.0,
            taker_buy_volume=5.0,
        )
        for index in range(24 * 60)
    ]
    monkeypatch.setattr(screen, "GENERATION_START", start)
    monkeypatch.setattr(screen, "EVALUATION_END", end)

    valid = _manifest(start=start, end=end, candles=candles)
    screen._validate_complete_cached_slice("BTCUSDT", candles, valid)

    boundary_rows = candles.copy()
    boundary_rows[0] = replace(
        boundary_rows[0],
        volume=0.0,
        quote_volume=0.0,
        taker_buy_volume=0.0,
    )
    boundary_rows[1] = replace(boundary_rows[1], quote_volume=990.0)
    boundary_rows[2] = replace(boundary_rows[2], quote_volume=1_010.0)
    screen._validate_complete_cached_slice(
        "BTCUSDT",
        boundary_rows,
        _manifest(start=start, end=end, candles=boundary_rows),
    )

    with pytest.raises(ValueError, match="incomplete offline cache evidence"):
        screen._validate_complete_cached_slice(
            "BTCUSDT",
            candles,
            replace(valid, gaps=1),
        )
    with pytest.raises(ValueError, match="official checksums and ZIP CRC"):
        screen._validate_complete_cached_slice(
            "BTCUSDT",
            candles,
            replace(
                valid,
                checksum_status="unavailable",
                checksum_files_verified=0,
            ),
        )

    malformed = candles.copy()
    malformed[0] = replace(malformed[0], quote_volume=2_000.0)
    with pytest.raises(ValueError, match="quote volume is inconsistent"):
        screen._validate_complete_cached_slice(
            "BTCUSDT",
            malformed,
            _manifest(start=start, end=end, candles=malformed),
        )

    malformed_taker = candles.copy()
    malformed_taker[0] = replace(malformed_taker[0])
    object.__setattr__(malformed_taker[0], "taker_buy_volume", 11.0)
    with pytest.raises(ValueError, match="invalid OHLC/volume/quote/taker domains"):
        screen._validate_complete_cached_slice(
            "BTCUSDT",
            malformed_taker,
            _manifest(start=start, end=end, candles=malformed_taker),
        )

    with pytest.raises(ValueError, match="parsed-row SHA-256 mismatch"):
        screen._validate_complete_cached_slice(
            "BTCUSDT",
            candles,
            replace(valid, sha256="0" * 64),
        )


def test_cache_manifest_requires_exact_ordered_unique_monthly_archive_names(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    start = date(2022, 5, 1)
    end = date(2022, 7, 1)
    start_ms = int(datetime.combine(start, datetime.min.time(), UTC).timestamp() * 1_000)
    end_ms = int(datetime.combine(end, datetime.min.time(), UTC).timestamp() * 1_000)
    expected_rows = (end - start).days * 24 * 60
    expected_files = ("BTCUSDT-1m-2022-05.zip", "BTCUSDT-1m-2022-06.zip")
    manifest = DatasetManifest(
        symbol="BTCUSDT",
        interval="1m",
        requested_start=start.isoformat(),
        requested_end=end.isoformat(),
        actual_start_ms=start_ms,
        actual_end_ms=end_ms - 1,
        rows=expected_rows,
        sha256="0" * 64,
        files=expected_files,
        gaps=0,
        transport_verification="zip_crc_and_parsed_rows_sha256",
        checksum_status="official_sha256_verified",
        checksum_files_verified=2,
        expected_files=2,
        csv_schema="binance_futures_kline_v1_12_columns",
    )
    monkeypatch.setattr(screen, "GENERATION_START", start)
    monkeypatch.setattr(screen, "EVALUATION_END", end)

    for files in (
        ("BTCUSDT-1m-2022-05.zip",),
        ("BTCUSDT-1m-2022-05.zip", "BTCUSDT-1m-2022-05.zip"),
        tuple(reversed(expected_files)),
        ("BTCUSDT-1m-2022-05.zip", "ETHUSDT-1m-2022-06.zip"),
    ):
        with pytest.raises(ValueError, match="incomplete offline cache evidence"):
            screen._validate_complete_cached_slice(
                "BTCUSDT",
                [],
                replace(manifest, files=files),
            )
