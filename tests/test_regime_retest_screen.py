from __future__ import annotations

import hashlib
import inspect
import json
import math
import sys
from dataclasses import asdict, replace
from datetime import UTC, date, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest
from kairos_quant.candles import Candle

import kairos_backtest.regime_retest_screen as screen
from kairos_backtest.data import DatasetManifest
from kairos_backtest.provenance import RuntimeProvenance
from kairos_backtest.regime_retest_campaign import (
    DEFAULT_REGIME_RETEST_PROTOCOL,
    RegimeRetestCampaignEvidence,
    RegimeRetestCandidate,
    regime_retest_scenarios,
)
from kairos_backtest.research_protocol import DataRole, ResearchPurpose


def _environment() -> screen.RegimeRetestScreenEnvironment:
    source = screen.OrderFlowScreenEnvironment(
        git_head_sha="1" * 40,
        git_tree_sha="2" * 40,
        git_dirty=False,
        source_sha256="3" * 64,
        source_files=(("kairos_backtest/regime_retest_screen.py", "4" * 64),),
        pyproject_sha256="5" * 64,
        uv_lock_sha256="6" * 64,
        runtime=RuntimeProvenance(
            python="3.11.0",
            implementation="CPython",
            platform="test",
            packages=(("kairos-backtest", "0.1.0"),),
        ),
    )
    dependency_pins = tuple(
        screen.RegimeRetestInstalledDependencyPin(
            distribution=distribution,
            import_package=screen.EXPECTED_DEPENDENCY_IMPORTS[distribution][0],
            version=version,
            url=url,
            vcs="git",
            requested_revision=commit,
            commit_id=commit,
            direct_url_sha256="7" * 64,
            source_sha256=screen.EXPECTED_DEPENDENCY_IMPORTS[distribution][1],
            source_files=screen.EXPECTED_DEPENDENCY_IMPORTS[distribution][2],
        )
        for distribution, version, url, commit in screen.EXPECTED_DEPENDENCY_SOURCES
    )
    return screen.RegimeRetestScreenEnvironment(
        source=source,
        project_root=screen._canonical_project_root().as_posix(),
        repository_remote_url="https://github.com/Kairos-cryptoAI/kairos-backtest.git",
        repository_identity=screen.EXPECTED_REPOSITORY_IDENTITY,
        repository_branch="main",
        dependency_pins=dependency_pins,
    )


def _canonical_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[Path, Path, Path, Path]:
    monkeypatch.setattr(screen, "_CANONICAL_PROJECT_ROOT", tmp_path)
    output = tmp_path / "reports" / "regime-retest-screen"
    return (
        output / "plan.json",
        output / "attempt.json",
        output / "result.json",
        output / "summary.json",
    )


def _fake_dependency_metadata(
    distribution_name: str,
    document: str | None,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    version: str | None = None,
    source_sha256: str | None = None,
) -> tuple[str, str, str]:
    rows = {row[0]: row[1:] for row in screen.EXPECTED_DEPENDENCY_SOURCES}
    expected_version, url, commit = rows[distribution_name]
    import_package, expected_source, source_files = screen.EXPECTED_DEPENDENCY_IMPORTS[distribution_name]
    package_root = tmp_path / import_package
    package_root.mkdir()
    for index in range(source_files):
        filename = "__init__.py" if index == 0 else f"module_{index}.py"
        (package_root / filename).write_text("# synthetic\n", encoding="utf-8")
    fake_distribution = SimpleNamespace(
        version=expected_version if version is None else version,
        read_text=lambda filename: document if filename == "direct_url.json" else None,
        locate_file=lambda package: package_root if package == import_package else tmp_path / package,
    )
    monkeypatch.setattr(
        screen.importlib.metadata,
        "distribution",
        lambda requested: (
            fake_distribution if requested == distribution_name else pytest.fail("unexpected distribution")
        ),
    )
    monkeypatch.setattr(
        screen.importlib,
        "import_module",
        lambda requested: (
            SimpleNamespace(__file__=str(package_root / "__init__.py"))
            if requested == import_package
            else pytest.fail("unexpected import")
        ),
    )
    monkeypatch.setattr(
        screen,
        "source_fingerprint",
        lambda root: (
            (expected_source if source_sha256 is None else source_sha256)
            if root == package_root
            else pytest.fail("unexpected source root")
        ),
    )
    return url, commit, expected_source


def _direction(direction, trades: int) -> screen.RegimeRetestDirectionMetrics:
    return screen.RegimeRetestDirectionMetrics(
        direction=direction,
        trades=trades,
        profit_factor=1.2,
        expectancy_usd_per_trade=1.0,
    )


def _metric(
    *,
    trades: int,
    long_trades: int | None = None,
    log_growth: float = 0.02,
    profit_factor: float | None = 1.2,
    expectancy: float = 1.0,
    reference_gross: float = 500.0,
    drawdown: float = 0.03,
    sharpe: float | None = 1.0,
) -> screen.RegimeRetestMetrics:
    longs = trades // 2 if long_trades is None else long_trades
    shorts = trades - longs
    net_return = math.expm1(log_growth)
    return screen.RegimeRetestMetrics(
        trades=trades,
        net_return=net_return,
        log_growth=math.log1p(net_return),
        profit_factor=profit_factor,
        expectancy_usd_per_trade=expectancy,
        reference_gross_pnl_usd=reference_gross,
        maximum_drawdown=drawdown,
        hac_sharpe=sharpe,
        fees_usd=10.0,
        shortfall_usd=5.0,
        funding_usd=1.0,
        directions=(
            _direction(screen.Side.LONG, longs),
            _direction(screen.Side.SHORT, shorts),
        ),
        rejection_dispositions=tuple((reason, 0) for reason in screen._rejection_inventory()),
    )


def _scenario(
    name: str,
    *,
    counts: tuple[int, int, int, int, int] = (33, 33, 33, 33, 33),
    exit_days: int = 50,
    positive_symbols: int = 3,
    log_growth: float = 0.02,
    drawdown: float = 0.03,
    long_trades: int = 83,
    profit_factor: float | None = 1.2,
    expectancy: float = 1.0,
    reference_gross: float = 500.0,
    sharpe: float | None = 1.0,
) -> screen.RegimeRetestScenarioMetrics:
    symbol_expectancies = (1.0,) * positive_symbols + (0.0,) * (5 - positive_symbols)
    per_symbol = tuple(
        screen.RegimeRetestSymbolMetrics(
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
    return screen.RegimeRetestScenarioMetrics(
        scenario_name=name,
        per_symbol=per_symbol,
        combined=_metric(
            trades=total,
            long_trades=long_trades,
            log_growth=log_growth,
            profit_factor=profit_factor,
            expectancy=expectancy,
            reference_gross=reference_gross,
            drawdown=drawdown,
            sharpe=sharpe,
        ),
        distinct_utc_exit_days=exit_days,
        positive_expectancy_symbols=positive_symbols,
        maximum_one_symbol_trade_share=max(counts) / total,
    )


def _scenarios(
    *,
    log_growth: float = 0.02,
    drawdown: float = 0.03,
) -> tuple[screen.RegimeRetestScenarioMetrics, ...]:
    return (
        _scenario("baseline"),
        _scenario("stress", log_growth=log_growth, drawdown=drawdown),
    )


def _fake_evidence(candidate: RegimeRetestCandidate) -> RegimeRetestCampaignEvidence:
    generation_by_symbol: dict[str, tuple[screen.RegimeRetestGenerationEvidence, str]] = {}
    for index, symbol in enumerate(screen.SYMBOLS, start=1):
        long_counters = screen.RegimeRetestGenerationCounters(
            structural_breakout_candidates=index,
            armed_setups=index,
            structural_reclaims=index,
            emitted_intents=index,
        )
        short_counters = screen.RegimeRetestGenerationCounters(
            structural_breakout_candidates=index + 1,
            armed_setups=index + 1,
            structural_reclaims=index + 1,
            emitted_intents=index + 1,
        )
        generation = object.__new__(screen.RegimeRetestGenerationEvidence)
        generation_values = {
            "config_sha256": candidate.config.fingerprint,
            "variant": candidate.config.variant,
            "intents": (),
            "events": (),
            "long_counters": long_counters,
            "short_counters": short_counters,
            "total_counters": long_counters + short_counters,
            "setup_inventory_sha256": hashlib.sha256(f"setup:{symbol}".encode()).hexdigest(),
            "outcome_inventory_sha256": hashlib.sha256(f"outcome:{symbol}".encode()).hexdigest(),
        }
        for name, value in generation_values.items():
            object.__setattr__(generation, name, value)
        generation_sha256 = hashlib.sha256(
            f"generation:{candidate.candidate_sha256}:{symbol}".encode()
        ).hexdigest()
        generation_by_symbol[symbol] = (generation, generation_sha256)

    scenario_evidence = []
    for scenario in regime_retest_scenarios(candidate):
        cells = []
        for symbol in screen.SYMBOLS:
            generation, generation_sha256 = generation_by_symbol[symbol]
            cell = object.__new__(screen.RegimeRetestCellEvidence)
            for name, value in {
                "scenario_name": scenario.name,
                "symbol": symbol,
                "candidate_sha256": candidate.candidate_sha256,
                "generation_evidence": generation,
                "generation_evidence_sha256": generation_sha256,
            }.items():
                object.__setattr__(cell, name, value)
            cells.append(cell)
        scenario_evidence.append(SimpleNamespace(scenario=scenario, cells=tuple(cells)))

    evidence = object.__new__(RegimeRetestCampaignEvidence)
    values = {
        "candidate": candidate,
        "protocol": DEFAULT_REGIME_RETEST_PROTOCOL,
        "protocol_name": DEFAULT_REGIME_RETEST_PROTOCOL.protocol_name,
        "protocol_sha256": DEFAULT_REGIME_RETEST_PROTOCOL.fingerprint(),
        "window_name": screen.WINDOW_NAME,
        "role": DataRole.RESEARCH,
        "purpose": ResearchPurpose.FIT,
        "generation_start": screen.GENERATION_START,
        "evaluation_start": screen.EVALUATION_START,
        "evaluation_end": screen.EVALUATION_END,
        "window_rationale": screen.WINDOW_RATIONALE,
        "requested_initial_equity_usd": screen.INITIAL_EQUITY_USD,
        "cell_initial_equity_usd": screen.INITIAL_EQUITY_USD / len(screen.SYMBOLS),
        "datasets": (),
        "scenarios": tuple(scenario_evidence),
        "seed": screen.BASE_SEED,
    }
    for name, value in values.items():
        object.__setattr__(evidence, name, value)
    return evidence


def _synthetic_screen_result(monkeypatch: pytest.MonkeyPatch) -> screen.RegimeRetestScreenResult:
    plan = screen.RegimeRetestScreenPlan(environment=_environment())
    scenarios = _scenarios()
    monkeypatch.setattr(screen, "_summarize_campaign", lambda _evidence: scenarios)
    trial_results = tuple(
        screen.RegimeRetestTrialResult(
            trial=trial,
            campaign_evidence=_fake_evidence(trial.candidate),
            scenarios=scenarios,
        )
        for trial in plan.trials
    )
    plan_bytes = screen._serialized_json(plan.to_dict())
    attempt = screen._attempt_for_plan(plan, plan_bytes)
    attempt_bytes = screen._serialized_json(attempt.to_dict())
    return screen.RegimeRetestScreenResult(
        plan=plan,
        attempt=attempt,
        attempt_file_sha256=hashlib.sha256(attempt_bytes).hexdigest(),
        attempt_file_bytes=len(attempt_bytes),
        trials=trial_results,
    )


def test_plan_binds_disjoint_window_preflight_lineage_and_exact_three_trials() -> None:
    plan = screen.RegimeRetestScreenPlan(environment=_environment())

    assert plan.generation_start == date(2023, 12, 1)
    assert plan.evaluation_start == date(2024, 2, 1)
    assert plan.evaluation_end == date(2024, 7, 1)
    assert plan.warmup_days == 62
    assert plan.window_rationale == (
        "start after the known pre-existing invalid XRP minute in November 2023; no imputation"
    )
    assert plan.seed == 44
    assert screen.OPERATIONAL_HORIZON_MS == 152 * 60_000
    assert screen.DECISION_CUTOFF_EXCLUSIVE == datetime(2024, 6, 30, 21, 28, tzinfo=UTC)
    assert tuple(trial.lineage_trial_number for trial in plan.trials) == (7, 8, 9)
    assert tuple(trial.trial_id for trial in plan.trials) == (
        "STRUCTURAL",
        "FLOW_REACCELERATION",
        "ABSORPTION",
    )
    assert len({trial.candidate_sha256 for trial in plan.trials}) == 3
    payload = plan.to_dict()
    assert payload["permissions"] == {
        "live_allowed": False,
        "promotion_eligible": False,
        "shadow_allowed": False,
    }
    preflight = cast(dict[str, object], payload["archive_preflight_disclosure"])
    data = cast(dict[str, object], payload["data"])
    trial_budget = cast(dict[str, object], payload["trial_budget"])
    eligibility = cast(dict[str, object], payload["eligibility"])
    gates = cast(dict[str, object], eligibility["each_baseline_and_stress"])
    environment = cast(dict[str, object], payload["environment"])
    ledger = cast(dict[str, object], payload["one_shot_ledger"])
    assert preflight["parsed_market_values"] is False
    assert preflight["plan_precedes_first_archive_byte_access"] is False
    assert preflight["fixed_slice_generation_start"] == "2023-12-01"
    assert preflight["fixed_slice_end_exclusive"] == "2024-07-01"
    assert preflight["fixed_slice_expected_archives"] == 35
    assert preflight["fixed_slice_expected_archives_per_symbol"] == 7
    assert preflight["fixed_slice_expected_months"] == [
        "2023-12",
        "2024-01",
        "2024-02",
        "2024-03",
        "2024-04",
        "2024-05",
        "2024-06",
    ]
    assert preflight["fixed_slice_covered_by_broader_transport_audit"] is True
    assert preflight["fixed_slice_inventory_not_recomputed_without_cache_access"] is True
    assert preflight["known_data_issue"] == "pre-existing invalid XRP minute in November 2023"
    assert "no repair or imputation" in cast(str, preflight["known_data_issue_treatment"])
    assert data["terminal_embargo_ms"] == 152 * 60_000
    assert data["expected_archives"] == 35
    assert data["expected_archives_per_symbol"] == 7
    assert data["expected_archive_months"] == [
        "2023-12",
        "2024-01",
        "2024-02",
        "2024-03",
        "2024-04",
        "2024-05",
        "2024-06",
    ]
    assert data["expected_rows_per_symbol"] == 306_720
    assert data["expected_warmup_rows_per_symbol"] == 89_280
    assert data["expected_evaluation_rows_per_symbol"] == 217_440
    assert data["window_rationale"] == plan.window_rationale
    assert data["no_repair"] is True
    assert trial_budget["adaptive_rerun_allowed"] is False
    assert gates["total_closed_trades_at_least"] == 165
    assert gates["minimum_closed_trades_per_symbol"] == 17
    assert gates["distinct_utc_exit_days_at_least"] == 50
    each_direction = cast(dict[str, object], gates["each_direction"])
    assert each_direction["closed_trades_at_least"] == 50
    dependencies = cast(list[dict[str, object]], environment["dependency_pins"])
    assert [(row["distribution"], row["commit_id"]) for row in dependencies] == [
        ("kairos-core", "c2b9ba192521f9843b245e1eae8a501d408a6bfa"),
        ("kairos-quant-scouts", "c74b9853bd97597b2104b2d9c4bcd5b7c6cefb24"),
    ]
    repository = cast(dict[str, object], environment["repository"])
    assert repository["identity"] == "Kairos-cryptoAI/kairos-backtest"
    assert repository["branch"] == "main"
    assert ledger["canonical_output_directory"] == "reports/regime-retest-screen"
    assert ledger["public_output_override_allowed"] is False
    durability = cast(dict[str, object], ledger["durability"])
    assert durability["file_content_fsync"] is True
    assert durability["windows_power_loss_durability_claimed"] is False

    with pytest.raises(ValueError, match="fixed preregistered"):
        replace(plan, trials=plan.trials + (plan.trials[0],))
    with pytest.raises(ValueError, match="exact registered lineage"):
        replace(plan.trials[0], lineage_trial_number=10)
    with pytest.raises(ValueError, match="main branch"):
        replace(plan.environment, repository_branch="feature")
    with pytest.raises(ValueError, match="origin remote URL"):
        replace(plan.environment, repository_remote_url="https://github.com/attacker/fork.git")


def test_missing_origin_remote_is_not_treated_as_unavailable_optional_provenance(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_git_value(_root: Path, *arguments: str) -> str | None:
        if arguments == ("rev-parse", "--show-toplevel"):
            return tmp_path.as_posix()
        if arguments == ("config", "--get", "remote.origin.url"):
            return None
        return pytest.fail(f"unexpected Git query: {arguments}")

    monkeypatch.setattr(screen, "_git_value", fake_git_value)
    with pytest.raises(RuntimeError, match="origin remote is missing"):
        screen._read_environment_overlay(tmp_path)


@pytest.mark.parametrize("distribution_name", ("kairos-core", "kairos-quant-scouts"))
def test_installed_dependencies_require_exact_direct_url_and_frozen_source(
    distribution_name: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rows = {row[0]: row[1:] for row in screen.EXPECTED_DEPENDENCY_SOURCES}
    version, url, commit = rows[distribution_name]
    payload = screen._expected_direct_url_payload(url, commit)
    document = json.dumps(payload, separators=(",", ":"))
    _fake_dependency_metadata(distribution_name, document, tmp_path, monkeypatch)

    pin = screen._read_installed_dependency_pin(distribution_name)

    assert pin.version == version
    assert pin.url == url
    assert pin.requested_revision == commit
    assert pin.commit_id == commit
    assert pin.direct_url_sha256 == hashlib.sha256(document.encode()).hexdigest()
    assert pin.source_sha256 == screen.EXPECTED_DEPENDENCY_IMPORTS[distribution_name][1]


@pytest.mark.parametrize("distribution_name", ("kairos-core", "kairos-quant-scouts"))
@pytest.mark.parametrize(
    "defect",
    ("missing", "malformed", "duplicate", "wrong_url", "wrong_commit", "wrong_version", "source_drift"),
)
def test_dependency_provenance_defects_fail_closed(
    distribution_name: str,
    defect: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rows = {row[0]: row[1:] for row in screen.EXPECTED_DEPENDENCY_SOURCES}
    version, url, commit = rows[distribution_name]
    payload = screen._expected_direct_url_payload(url, commit)
    document: str | None = json.dumps(payload, separators=(",", ":"))
    installed_version: str | None = None
    source_sha256: str | None = None
    if defect == "missing":
        document = None
    elif defect == "malformed":
        document = "{"
    elif defect == "duplicate":
        document = (
            f'{{"url":"{url}","url":"{url}","vcs_info":'
            f'{{"vcs":"git","commit_id":"{commit}","requested_revision":"{commit}"}}}}'
        )
    elif defect == "wrong_url":
        payload["url"] = "https://github.com/attacker/replacement.git"
        document = json.dumps(payload, separators=(",", ":"))
    elif defect == "wrong_commit":
        cast(dict[str, object], payload["vcs_info"])["commit_id"] = "0" * 40
        document = json.dumps(payload, separators=(",", ":"))
    elif defect == "wrong_version":
        installed_version = f"{version}.post1"
    elif defect == "source_drift":
        source_sha256 = "0" * 64
    _fake_dependency_metadata(
        distribution_name,
        document,
        tmp_path,
        monkeypatch,
        version=installed_version,
        source_sha256=source_sha256,
    )

    with pytest.raises(RuntimeError):
        screen._read_installed_dependency_pin(distribution_name)


@pytest.mark.parametrize("defect", (None, "pyproject_pin", "lock_pin", "duplicate_lock"))
def test_dependency_declarations_and_lock_must_match_installed_trust_anchors(
    defect: str | None,
    tmp_path: Path,
) -> None:
    core_commit = screen.EXPECTED_DEPENDENCY_SOURCES[0][3]
    quant_commit = screen.EXPECTED_DEPENDENCY_SOURCES[1][3]
    pyproject = (
        '[project]\nname = "kairos-backtest"\n[tool.uv.sources]\n'
        'kairos-core = { git = "https://github.com/Kairos-cryptoAI/kairos-core.git", '
        f'rev = "{core_commit}" }}\n'
        "kairos-quant-scouts = { git = "
        '"https://github.com/Kairos-cryptoAI/kairos-quant-scouts.git", '
        f'rev = "{quant_commit}" }}\n'
    )
    lock = (
        '[[package]]\nname = "kairos-core"\nversion = "0.2.0"\nsource = { git = '
        f'"https://github.com/Kairos-cryptoAI/kairos-core.git?rev={core_commit}#{core_commit}" }}\n'
        '[[package]]\nname = "kairos-quant-scouts"\nversion = "0.1.0"\nsource = { git = '
        '"https://github.com/Kairos-cryptoAI/kairos-quant-scouts.git?rev='
        f'{quant_commit}#{quant_commit}" }}\n'
    )
    if defect == "pyproject_pin":
        pyproject = pyproject.replace(core_commit, "0" * 40, 1)
    elif defect == "lock_pin":
        lock = lock.replace(quant_commit, "0" * 40, 1)
    elif defect == "duplicate_lock":
        lock += f"""
[[package]]
name = "kairos-core"
version = "0.2.0"
source = {{ git = "https://github.com/Kairos-cryptoAI/kairos-core.git?rev={core_commit}#{core_commit}" }}
"""
    (tmp_path / "pyproject.toml").write_text(pyproject, encoding="utf-8")
    (tmp_path / "uv.lock").write_text(lock, encoding="utf-8")

    if defect is None:
        screen._assert_declared_dependency_sources(tmp_path)
    else:
        with pytest.raises(RuntimeError):
            screen._assert_declared_dependency_sources(tmp_path)


def test_generation_diagnostics_recompute_ordered_symbols_counters_and_hashes() -> None:
    trial = screen._fixed_trials()[0]
    evidence = _fake_evidence(trial.candidate)

    diagnostics = screen._condense_generation_diagnostics(evidence, trial)
    payload = diagnostics.to_dict()

    assert payload["schema_version"] == 1
    assert tuple(item.symbol for item in diagnostics.symbols) == screen.SYMBOLS
    assert diagnostics.aggregate_long_counters.emitted_intents == 15
    assert diagnostics.aggregate_short_counters.emitted_intents == 20
    assert diagnostics.aggregate_total_counters.emitted_intents == 35
    assert diagnostics.aggregate_total_counters == (
        diagnostics.aggregate_long_counters + diagnostics.aggregate_short_counters
    )
    assert diagnostics.generation_diagnostics_sha256 == screen._sha256(diagnostics._payload())
    for item in diagnostics.symbols:
        assert item.symbol_diagnostics_sha256 == screen._sha256(item._payload())
    serialized = json.dumps(payload, sort_keys=True)
    assert '"events"' not in serialized
    assert '"intents"' not in serialized


@pytest.mark.parametrize("tamper", ("scenario_sha", "candidate", "variant"))
def test_generation_diagnostics_reject_cross_scenario_candidate_and_variant_tampering(
    tamper: str,
) -> None:
    trial = screen._fixed_trials()[0]
    evidence = _fake_evidence(trial.candidate)
    baseline, stress = evidence.scenarios
    if tamper == "scenario_sha":
        object.__setattr__(stress.cells[0], "generation_evidence_sha256", "f" * 64)
        expected = "baseline and stress generation evidence"
    elif tamper == "candidate":
        for scenario in (baseline, stress):
            object.__setattr__(scenario.cells[0], "candidate_sha256", "f" * 64)
        expected = "trial candidate"
    else:
        alternate_variant = screen.TRIAL_VARIANTS[1]
        object.__setattr__(baseline.cells[0].generation_evidence, "variant", alternate_variant)
        expected = "candidate/variant"

    with pytest.raises(ValueError, match=expected):
        screen._condense_generation_diagnostics(evidence, trial)


def test_generation_diagnostics_reject_counter_aggregate_tampering() -> None:
    trial = screen._fixed_trials()[0]
    diagnostics = screen._condense_generation_diagnostics(_fake_evidence(trial.candidate), trial)
    zero = screen.RegimeRetestGenerationCounters()

    with pytest.raises(ValueError, match="symbol generation total"):
        replace(diagnostics.symbols[0], total_counters=zero)
    with pytest.raises(ValueError, match="aggregate counters"):
        replace(diagnostics, aggregate_total_counters=zero)


def test_screen_result_rejects_coordinated_forged_winner_and_trial_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result = _synthetic_screen_result(monkeypatch)
    forged_winner = SimpleNamespace(
        trial=result.plan.trials[0],
        eligible=True,
        worst_scenario_log_growth=999.0,
        stress_maximum_drawdown=-999.0,
    )
    forged_trials = (
        cast(screen.RegimeRetestTrialResult, forged_winner),
        result.trials[1],
        result.trials[2],
    )

    with pytest.raises(TypeError, match="exact RegimeRetestTrialResult"):
        replace(result, trials=forged_trials)
    with pytest.raises(TypeError, match="exact immutable tuple"):
        replace(
            result,
            trials=cast(tuple[screen.RegimeRetestTrialResult, ...], list(result.trials)),
        )
    with pytest.raises(ValueError, match="exactly three trials"):
        replace(result, trials=result.trials[:2])
    with pytest.raises(ValueError, match="ordering and identity"):
        replace(result, trials=tuple(reversed(result.trials)))

    cloned_trial = replace(result.trials[0].trial)
    assert cloned_trial == result.trials[0].trial
    assert cloned_trial is not result.trials[0].trial
    cloned_result_trial = replace(result.trials[0], trial=cloned_trial)
    with pytest.raises(ValueError, match="ordering and identity"):
        replace(result, trials=(cloned_result_trial, *result.trials[1:]))


def test_screen_result_recomputes_plan_and_attempt_file_commitments(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result = _synthetic_screen_result(monkeypatch)
    plan_bytes = screen._serialized_json(result.plan.to_dict())
    attempt_bytes = screen._serialized_json(result.attempt.to_dict())
    assert result.attempt.plan_file_sha256 == hashlib.sha256(plan_bytes).hexdigest()
    assert result.attempt.plan_file_bytes == len(plan_bytes)
    assert result.attempt_file_sha256 == hashlib.sha256(attempt_bytes).hexdigest()
    assert result.attempt_file_bytes == len(attempt_bytes)

    for forged_attempt in (
        replace(result.attempt, plan_file_sha256="f" * 64),
        replace(result.attempt, plan_file_bytes=result.attempt.plan_file_bytes + 1),
    ):
        forged_attempt_bytes = screen._serialized_json(forged_attempt.to_dict())
        with pytest.raises(ValueError, match="plan-file commitment"):
            replace(
                result,
                attempt=forged_attempt,
                attempt_file_sha256=hashlib.sha256(forged_attempt_bytes).hexdigest(),
                attempt_file_bytes=len(forged_attempt_bytes),
            )

    with pytest.raises(ValueError, match="attempt-file commitment"):
        replace(result, attempt_file_sha256="0" * 64)
    with pytest.raises(ValueError, match="attempt-file commitment"):
        replace(result, attempt_file_bytes=result.attempt_file_bytes + 1)


@pytest.mark.parametrize(
    ("scenarios", "failure"),
    [
        ((_scenario("baseline", log_growth=0.0), _scenario("stress")), "baseline_log_growth_not_positive"),
        (
            (_scenario("baseline", profit_factor=1.0), _scenario("stress")),
            "baseline_profit_factor_not_above_one",
        ),
        ((_scenario("baseline", expectancy=0.0), _scenario("stress")), "baseline_expectancy_not_positive"),
        (
            (_scenario("baseline", reference_gross=0.0), _scenario("stress")),
            "baseline_reference_gross_pnl_not_positive",
        ),
        ((_scenario("baseline", sharpe=0.0), _scenario("stress")), "baseline_hac_sharpe_not_positive"),
        (
            (_scenario("baseline", counts=(32, 33, 33, 33, 33)), _scenario("stress")),
            "baseline_closed_trades_below_minimum",
        ),
        (
            (_scenario("baseline", counts=(16, 38, 37, 37, 37)), _scenario("stress")),
            "baseline_BTCUSDT_closed_trades_below_minimum",
        ),
        (
            (_scenario("baseline", exit_days=49), _scenario("stress")),
            "baseline_distinct_utc_exit_days_below_minimum",
        ),
        (
            (_scenario("baseline", positive_symbols=2), _scenario("stress")),
            "baseline_positive_expectancy_symbols_below_minimum",
        ),
        (
            (_scenario("baseline", counts=(83, 21, 21, 20, 20)), _scenario("stress")),
            "baseline_one_symbol_trade_share_above_maximum",
        ),
        (
            (_scenario("baseline", long_trades=49), _scenario("stress")),
            "baseline_long_closed_trades_below_minimum",
        ),
        (
            (
                _scenario("baseline", counts=(51, 51, 51, 51, 50), long_trades=127),
                _scenario("stress"),
            ),
            "stress_trade_retention_below_minimum",
        ),
        (
            (_scenario("baseline"), _scenario("stress", drawdown=0.0500001)),
            "stress_maximum_drawdown_above_maximum",
        ),
    ],
)
def test_every_registered_gate_is_strict(
    scenarios: tuple[screen.RegimeRetestScenarioMetrics, ...],
    failure: str,
) -> None:
    assert failure in screen._eligibility_failures(scenarios)


def test_exact_gate_boundaries_pass() -> None:
    baseline = _scenario("baseline", drawdown=0.05)
    stress = _scenario("stress", drawdown=0.05)
    assert stress.combined.trades / baseline.combined.trades >= 0.65
    assert screen._eligibility_failures((baseline, stress)) == ()


def test_each_direction_requires_count_profit_factor_and_expectancy() -> None:
    baseline = _scenario("baseline")
    long_row, short_row = baseline.combined.directions
    for changed, failure in (
        (
            replace(long_row, profit_factor=1.0),
            "baseline_long_profit_factor_not_above_one",
        ),
        (
            replace(long_row, expectancy_usd_per_trade=0.0),
            "baseline_long_expectancy_not_positive",
        ),
        (
            replace(short_row, profit_factor=1.0),
            "baseline_short_profit_factor_not_above_one",
        ),
        (
            replace(short_row, expectancy_usd_per_trade=0.0),
            "baseline_short_expectancy_not_positive",
        ),
    ):
        directions = (changed, short_row) if changed.direction is screen.Side.LONG else (long_row, changed)
        changed_metric = replace(baseline.combined, directions=directions)
        changed_baseline = replace(baseline, combined=changed_metric)
        assert failure in screen._eligibility_failures((changed_baseline, _scenario("stress")))


def test_consumed_attempt_is_written_before_cache_parse_and_survives_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan_path, attempt_path, result_path, summary_path = _canonical_paths(tmp_path, monkeypatch)
    parsed = False
    writes: list[Path] = []
    exclusive_publish = screen._publish_exclusive_bytes

    def record_write(path: Path, payload: bytes, artifact_name: str) -> None:
        writes.append(path)
        exclusive_publish(path, payload, artifact_name)

    def fail_cache(_cache_dir: Path):
        nonlocal parsed
        assert plan_path.exists()
        assert attempt_path.exists()
        attempt = json.loads(attempt_path.read_text(encoding="utf-8"))
        assert attempt["status"] == "consumed"
        assert attempt["trial_lineage"][0]["lineage_trial_number"] == 7
        parsed = True
        raise RuntimeError("synthetic parse failure")

    monkeypatch.setattr(screen, "_capture_clean_environment", _environment)
    monkeypatch.setattr(screen, "_assert_environment_stable", lambda _expected: None)
    monkeypatch.setattr(screen, "_publish_exclusive_bytes", record_write)
    monkeypatch.setattr(screen, "_load_fixed_cache", fail_cache)
    monkeypatch.setattr(screen, "_now_utc", lambda: datetime(2026, 8, 17, 20, tzinfo=UTC))

    with pytest.raises(RuntimeError, match="synthetic parse failure"):
        screen.run_regime_retest_screen(tmp_path / "cache")
    assert parsed
    assert writes == [plan_path, attempt_path]
    assert plan_path.exists() and attempt_path.exists()
    assert not result_path.exists() and not summary_path.exists()

    monkeypatch.setattr(screen, "_load_fixed_cache", lambda _path: pytest.fail("cache was reopened"))
    with pytest.raises(FileExistsError, match="one-shot attempt is unavailable"):
        screen.run_regime_retest_screen(tmp_path / "cache")


@pytest.mark.parametrize(
    ("artifact_index", "error", "attempt_exists"),
    ((0, "screen plan changed", False), (1, "attempt ledger changed", True)),
)
def test_substitution_between_exclusive_write_and_immediate_read_fails_closed(
    artifact_index: int,
    error: str,
    attempt_exists: bool,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan_path, attempt_path, result_path, summary_path = _canonical_paths(tmp_path, monkeypatch)
    target = (plan_path, attempt_path)[artifact_index]
    original_read = screen._read_artifact_bytes
    substituted = False

    def substitute_before_read(path: Path) -> bytes:
        nonlocal substituted
        if path == target and not substituted:
            path.write_bytes(b'{"substituted":true}\n')
            substituted = True
        return original_read(path)

    monkeypatch.setattr(screen, "_capture_clean_environment", _environment)
    monkeypatch.setattr(screen, "_assert_environment_stable", lambda _expected: None)
    monkeypatch.setattr(screen, "_read_artifact_bytes", substitute_before_read)
    monkeypatch.setattr(screen, "_load_fixed_cache", lambda _path: pytest.fail("cache was parsed"))

    with pytest.raises(RuntimeError, match=error):
        screen.run_regime_retest_screen(tmp_path / "cache")

    assert substituted
    assert plan_path.exists()
    assert attempt_path.exists() is attempt_exists
    assert not result_path.exists() and not summary_path.exists()


def test_environment_drift_after_plan_is_rejected_before_attempt_consumption(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan_path, attempt_path, result_path, summary_path = _canonical_paths(tmp_path, monkeypatch)
    monkeypatch.setattr(screen, "_capture_clean_environment", _environment)
    monkeypatch.setattr(
        screen,
        "_assert_environment_stable",
        lambda _expected: (_ for _ in ()).throw(RuntimeError("synthetic provenance drift")),
    )
    monkeypatch.setattr(screen, "_load_fixed_cache", lambda _path: pytest.fail("cache was parsed"))

    with pytest.raises(RuntimeError, match="synthetic provenance drift"):
        screen.run_regime_retest_screen(tmp_path / "cache")

    assert plan_path.exists()
    assert not attempt_path.exists()
    assert not result_path.exists() and not summary_path.exists()


@pytest.mark.parametrize(
    ("tampered_artifact", "error"),
    (("plan", "plan changed"), ("attempt", "attempt ledger changed")),
)
def test_attempt_and_plan_tampering_prevent_result_publication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    tampered_artifact: str,
    error: str,
) -> None:
    plan_path, attempt_path, result_path, summary_path = _canonical_paths(tmp_path, monkeypatch)

    monkeypatch.setattr(screen, "_capture_clean_environment", _environment)
    monkeypatch.setattr(screen, "_assert_environment_stable", lambda _expected: None)
    monkeypatch.setattr(screen, "_load_fixed_cache", lambda _path: {})

    def mutate_then_fail(_candles, **_kwargs):
        target = plan_path if tampered_artifact == "plan" else attempt_path
        target.write_text("tampered\n", encoding="utf-8")
        return _fake_evidence(cast(RegimeRetestCandidate, _kwargs["candidate"]))

    monkeypatch.setattr(screen, "run_regime_retest_campaign", mutate_then_fail)
    monkeypatch.setattr(screen, "_summarize_campaign", lambda _evidence: _scenarios())
    monkeypatch.setattr(
        RegimeRetestCampaignEvidence,
        "to_dict",
        lambda self: {"candidate": self.candidate.to_dict()},
    )
    with pytest.raises(RuntimeError, match=error):
        screen.run_regime_retest_screen(tmp_path / "cache")
    assert attempt_path.exists()
    assert not result_path.exists() and not summary_path.exists()


def test_successful_synthetic_screen_ranks_only_eligible_trials_and_binds_attempt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan_path, attempt_path, result_path, summary_path = _canonical_paths(tmp_path, monkeypatch)
    summaries = {
        screen.TRIAL_VARIANTS[0]: _scenarios(log_growth=0.04, drawdown=0.04),
        screen.TRIAL_VARIANTS[1]: _scenarios(log_growth=0.03, drawdown=0.02),
        screen.TRIAL_VARIANTS[2]: _scenarios(log_growth=0.03, drawdown=0.02),
    }

    monkeypatch.setattr(screen, "_capture_clean_environment", _environment)
    monkeypatch.setattr(screen, "_assert_environment_stable", lambda _expected: None)
    monkeypatch.setattr(screen, "_load_fixed_cache", lambda _path: {})
    monkeypatch.setattr(
        screen,
        "run_regime_retest_campaign",
        lambda _candles, **kwargs: _fake_evidence(cast(RegimeRetestCandidate, kwargs["candidate"])),
    )
    monkeypatch.setattr(
        screen,
        "_summarize_campaign",
        lambda evidence: summaries[evidence.candidate.config.variant],
    )
    monkeypatch.setattr(
        RegimeRetestCampaignEvidence,
        "to_dict",
        lambda self: {"candidate": self.candidate.to_dict(), "synthetic": True},
    )
    monkeypatch.setattr(screen, "_now_utc", lambda: datetime(2026, 8, 17, 20, tzinfo=UTC))

    result = screen.run_regime_retest_screen(tmp_path / "cache")

    assert result.ranked_eligible_trial_ids == (
        "FLOW_REACCELERATION",
        "ABSORPTION",
        "STRUCTURAL",
    )
    assert result.selected_trial_id == "FLOW_REACCELERATION"
    attempt_bytes = attempt_path.read_bytes()
    result_doc = json.loads(result_path.read_text(encoding="utf-8"))
    summary_doc = json.loads(summary_path.read_text(encoding="utf-8"))
    assert result_doc["attempt"]["file_sha256"] == hashlib.sha256(attempt_bytes).hexdigest()
    assert summary_doc == screen.compact_regime_retest_summary(result)
    summary_trials = cast(list[dict[str, object]], summary_doc["trials"])
    assert summary_trials[0]["generation_diagnostics"] == result.trials[0].generation_diagnostics.to_dict()
    assert '"events"' not in json.dumps(summary_doc, sort_keys=True)
    assert '"intents"' not in json.dumps(summary_doc, sort_keys=True)
    assert result_doc["permissions"] == {
        "live_allowed": False,
        "promotion_eligible": False,
        "shadow_allowed": False,
    }


def test_output_paths_are_unique_and_cli_has_no_overwrite_escape_hatch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    paths = _canonical_paths(tmp_path, monkeypatch)
    assert screen._validate_output_paths() == paths
    assert tuple(inspect.signature(screen.run_regime_retest_screen).parameters) == ("cache_dir",)
    with pytest.raises(TypeError):
        screen.run_regime_retest_screen(tmp_path / "cache", tmp_path / "alternate")  # type: ignore[call-arg]

    monkeypatch.setattr(sys, "argv", ["kairos-regime-retest-screen", "--help"])
    with pytest.raises(SystemExit) as exit_info:
        screen.main()
    assert exit_info.value.code == 0
    help_text = capsys.readouterr().out
    assert "--cache-dir" in help_text
    assert "--plan-output" not in help_text
    assert "--attempt-output" not in help_text
    assert "--result-output" not in help_text
    assert "--summary-output" not in help_text
    assert "--overwrite" not in help_text


def test_in_root_symlink_or_junction_cannot_alias_the_canonical_ledger(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(screen, "_CANONICAL_PROJECT_ROOT", tmp_path)
    alias = tmp_path / "reports" / "regime-retest-screen"
    alternate = tmp_path / "alternate-ledger"
    real_resolve = Path.resolve

    def emulate_in_root_reparse_point(path: Path, strict: bool = False) -> Path:
        if path == alias:
            return alternate
        return real_resolve(path, strict=strict)

    monkeypatch.setattr(Path, "resolve", emulate_in_root_reparse_point)
    with pytest.raises(RuntimeError, match="symlink or junction"):
        screen._canonical_output_paths()


def test_synthetic_cache_validation_is_complete_checksum_bound_and_domain_strict(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    start = date(2023, 7, 1)
    end = date(2023, 7, 2)
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
    digest = hashlib.sha256(
        json.dumps([asdict(candle) for candle in candles], separators=(",", ":")).encode()
    ).hexdigest()
    manifest = DatasetManifest(
        symbol="BTCUSDT",
        interval="1m",
        requested_start=start.isoformat(),
        requested_end=end.isoformat(),
        actual_start_ms=start_ms,
        actual_end_ms=start_ms + 24 * 60 * 60_000 - 1,
        rows=len(candles),
        sha256=digest,
        files=("BTCUSDT-1m-2023-07.zip",),
        gaps=0,
        transport_verification="zip_crc_and_parsed_rows_sha256",
        checksum_status="official_sha256_verified",
        checksum_files_verified=1,
        expected_files=1,
        csv_schema="binance_futures_kline_v1_12_columns",
    )
    monkeypatch.setattr(screen, "GENERATION_START", start)
    monkeypatch.setattr(screen, "EVALUATION_END", end)

    screen._validate_complete_cached_slice("BTCUSDT", candles, manifest)
    with pytest.raises(ValueError, match="incomplete offline cache evidence"):
        screen._validate_complete_cached_slice("BTCUSDT", candles, replace(manifest, gaps=1))
    malformed = candles.copy()
    malformed[0] = replace(malformed[0])
    object.__setattr__(malformed[0], "taker_buy_volume", 11.0)
    with pytest.raises(ValueError, match="invalid OHLC/volume/quote/taker domains"):
        screen._validate_complete_cached_slice("BTCUSDT", malformed, manifest)


def test_pyproject_registers_cli_and_only_full_result_is_ignored() -> None:
    root = Path(__file__).parents[1]
    pyproject = (root / "pyproject.toml").read_text(encoding="utf-8")
    gitignore = (root / ".gitignore").read_text(encoding="utf-8")
    assert 'kairos-regime-retest-screen = "kairos_backtest.regime_retest_screen:main"' in pyproject
    assert "reports/regime-retest-screen/result.json" in gitignore
    assert "reports/regime-retest-screen/plan.json" not in gitignore
    assert "reports/regime-retest-screen/attempt.json" not in gitignore
    assert "reports/regime-retest-screen/summary.json" not in gitignore
