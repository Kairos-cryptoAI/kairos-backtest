import hashlib
import math
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest
from kairos_core.enums import Side

import kairos_backtest.promotion_v2 as promotion_v2
from kairos_backtest.portfolio import CellEquityCurve, DailyCellSnapshot, synchronize_cells
from kairos_backtest.promotion_v2 import (
    CSCV_ALGORITHM_SHA256,
    SELECTION_RULE_SHA256,
    CandidateFreeze,
    ExperimentPlan,
    FrozenTrialRegistryEvidence,
    InputIntentInventory,
    NestedFoldProtocol,
    NestedOOSFoldEvidence,
    ParameterPlateauEvidence,
    PromotionEvidenceV2,
    PromotionProtocolEvidence,
    PromotionTarget,
    ScenarioPerformance,
    ScenarioRunFingerprint,
    TerminalHoldoutEvidence,
    evaluate_offline_to_shadow,
)
from kairos_backtest.research_protocol import DataRole, DataWindow, ResearchProtocol
from kairos_backtest.robustness import DailyReturnSeries, ParameterOutcome, SynchronousTrialMatrix
from kairos_backtest.strategy_models import ExitPlan, ExitReason, SleeveIntent, TradeRecord
from kairos_backtest.trial_registry import (
    FailureClass,
    RegistryIntegrityError,
    RegistryOutcome,
    TrialFingerprints,
    TrialRegistry,
)

_SELECTION_START = date(2019, 9, 1)
_SELECTION_END = date(2021, 5, 1)
_NESTED_WINDOW_START = date(2021, 6, 1)
_NESTED_WINDOW_END = date(2025, 1, 1)
_NESTED_START = date(2023, 1, 1)
_HOLDOUT_START = date(2025, 1, 1)
_HOLDOUT_END = date(2026, 1, 1)
_FOLD_DAYS = 46
_SELECTION_BLOCK_DAYS = 76
_FOLDS = 8
_TRIALS = 9
_SPECS = (
    ("trend", "BTCUSDT"),
    ("trend", "ETHUSDT"),
    ("range", "SOLUSDT"),
    ("range", "XRPUSDT"),
    ("macro", "ADAUSDT"),
    ("macro", "BNBUSDT"),
)


def _digest(label: str) -> str:
    return hashlib.sha256(label.encode()).hexdigest()


def _dates(start: date, count: int) -> tuple[date, ...]:
    return tuple(start + timedelta(days=index) for index in range(count))


def _nested_series() -> tuple[DailyReturnSeries, ...]:
    values: list[float] = []
    for fold in range(_FOLDS):
        block = [0.0] * _SELECTION_BLOCK_DAYS
        for index in range(8):
            position = ((2 * index + 1) * _SELECTION_BLOCK_DAYS) // 16
            block[position] = -0.001 if fold < 2 else 0.002
        values.extend(block)
    dates = _dates(_SELECTION_START, len(values))
    assert dates == _dates(_SELECTION_START, (_SELECTION_END - _SELECTION_START).days)
    selected = tuple(values)
    return tuple(
        DailyReturnSeries(dates, tuple(value - trial * 0.00000001 for value in selected))
        for trial in range(_TRIALS)
    )


def _experiment_plan() -> ExperimentPlan:
    fold_protocol = NestedFoldProtocol(
        protocol_name="expanding-nested-v1",
        train_origin=_NESTED_WINDOW_START,
        first_test_start=_NESTED_START,
        fold_count=_FOLDS,
        minimum_train_days=365,
        test_days=_FOLD_DAYS,
        minimum_test_trades=50,
        purge_days=2,
        cscv_blocks=8,
        cscv_performance_measure="sharpe",
        cscv_algorithm_sha256=CSCV_ALGORITHM_SHA256,
        minimum_candidate_log_growth=0.0,
        selection_rule_sha256=SELECTION_RULE_SHA256,
    )
    research = ResearchProtocol(
        protocol_name="promotion-v2-test",
        universe=tuple(symbol for _sleeve, symbol in _SPECS),
        windows=(
            DataWindow("selection", _SELECTION_START, _SELECTION_END, DataRole.SELECTION),
            DataWindow(
                "nested",
                _NESTED_WINDOW_START,
                _NESTED_WINDOW_END,
                DataRole.ROBUSTNESS,
            ),
            DataWindow("terminal", _HOLDOUT_START, _HOLDOUT_END, DataRole.BLIND),
        ),
        max_trials=_TRIALS,
        maximum_holding_ms=60_000,
        maximum_label_horizon_ms=60_000,
        maximum_execution_latency_ms=1_000,
        warmup_ms=0,
    )
    return ExperimentPlan(
        research_protocol=research,
        nested_folds=fold_protocol,
        nested_evaluation_window_name="nested",
        final_trial_window_name="selection",
        terminal_holdout_window_name="terminal",
        final_trial_config_sha256s=tuple(_digest(f"config:{trial}") for trial in range(_TRIALS)),
        nested_trial_config_sha256s=tuple(_digest(f"config:{trial}") for trial in range(2)),
    )


def _protocol(
    plan: ExperimentPlan,
    registry: FrozenTrialRegistryEvidence,
) -> PromotionProtocolEvidence:
    frozen_at = datetime(2024, 12, 1, tzinfo=UTC)
    candidate_freeze = CandidateFreeze.capture_selection(
        plan,
        registry,
        candidate_commit="a" * 40,
        frozen_at=frozen_at,
    )
    research = replace(
        plan.research_protocol,
        candidate_commit="a" * 40,
        parameter_set_sha256=registry.selected_trial.fingerprints.config_sha256,
        frozen_at=frozen_at,
    )
    return PromotionProtocolEvidence(
        plan,
        research,
        candidate_freeze,
        datetime(2026, 1, 1, tzinfo=UTC),
    )


def _fingerprints(
    plan: ExperimentPlan,
    trial: int,
    *,
    data_label: str = "nested-data",
) -> TrialFingerprints:
    return TrialFingerprints(
        protocol_sha256=plan.preregistration_sha256,
        config_sha256=_digest(f"config:{trial}"),
        code_sha256=_digest("code"),
        data_sha256=_digest(data_label),
        dependency_sha256=_digest("dependencies"),
        container_sha256=_digest("container"),
    )


def _registry_evidence(
    path: Path,
    plan: ExperimentPlan,
    series: tuple[DailyReturnSeries, ...],
    *,
    add_failure: bool = False,
) -> FrozenTrialRegistryEvidence:
    registry = TrialRegistry(path)
    for trial, returns in enumerate(series):
        if add_failure and trial == len(series) - 1:
            registry.append_failure(_fingerprints(plan, trial), FailureClass.NUMERICAL)
        else:
            registry.append_success(_fingerprints(plan, trial), returns)
    registry.finalize_selection(1)
    return FrozenTrialRegistryEvidence.capture(registry)


def _outer_test_values() -> tuple[float, ...]:
    values: list[float] = []
    for fold in range(_FOLDS):
        block = [0.0] * _FOLD_DAYS
        for index in range(8):
            position = ((2 * index + 1) * _FOLD_DAYS) // 16
            block[position] = -0.001 if fold < 2 else 0.002
        values.extend(block)
    return tuple(values)


def _nested_folds(
    plan: ExperimentPlan,
    registry_dir: Path,
) -> tuple[NestedOOSFoldEvidence, ...]:
    output: list[NestedOOSFoldEvidence] = []
    outer_values = _outer_test_values()
    train_start = plan.nested_folds.train_origin
    for index in range(_FOLDS):
        test_start = _NESTED_START + timedelta(days=index * _FOLD_DAYS)
        test_end = test_start + timedelta(days=_FOLD_DAYS)
        train_end = test_start - timedelta(days=2)
        train_dates = _dates(train_start, (train_end - train_start).days)
        train_values = tuple(0.001 if day % 3 else -0.0005 for day in range(len(train_dates)))
        inner_registry = TrialRegistry(registry_dir / f"fold-{index + 1}.jsonl")
        for trial in range(2):
            inner_registry.append_success(
                _fingerprints(plan, trial, data_label=f"fold-training-data:{index + 1}"),
                DailyReturnSeries(
                    train_dates,
                    tuple(value - trial * 0.0000001 for value in train_values),
                ),
            )
        inner_registry.finalize_selection(1)
        inner_selection = FrozenTrialRegistryEvidence.capture(inner_registry)
        start = index * _FOLD_DAYS
        end = start + _FOLD_DAYS
        output.append(
            NestedOOSFoldEvidence(
                fold_id=f"nested-oos-{index + 1:03d}",
                train_start=train_start,
                train_end_exclusive=train_end,
                test_start=test_start,
                test_end_exclusive=test_end,
                test_returns=DailyReturnSeries(
                    _dates(test_start, _FOLD_DAYS),
                    outer_values[start:end],
                ),
                test_closed_trades=60,
                selection_registry=inner_selection,
                selection_frozen_at=datetime.combine(train_end, datetime.min.time(), UTC),
                candidate_sha256=inner_selection.candidate_sha256,
                fold_protocol_sha256=plan.nested_folds.fingerprint,
            )
        )
    return tuple(output)


def _schedule(trades: int = 84) -> tuple[tuple[int, float], ...]:
    losses = 16
    output = tuple((2 + index * 3, -1.0) for index in range(losses))
    winners = trades - losses
    winning_days = (_HOLDOUT_END - _HOLDOUT_START).days - 70
    output += tuple((70 + ((2 * index + 1) * winning_days) // (2 * winners), 2.0) for index in range(winners))
    return tuple(sorted(output))


def _trade_pair(
    sleeve_id: str,
    symbol: str,
    *,
    day_index: int,
    ordinal: int,
    pnl: float,
) -> tuple[TradeRecord, TradeRecord]:
    timestamp = int(
        datetime.combine(_HOLDOUT_START + timedelta(days=day_index), datetime.min.time(), UTC).timestamp()
        * 1_000
    )
    intent = SleeveIntent(
        sleeve_id=sleeve_id,
        symbol=symbol,
        side=Side.LONG,
        decision_ts_ms=timestamp,
        entry_eligible_ts_ms=timestamp + 1,
        entry_expires_ts_ms=timestamp + 1,
        reference_price=100,
        signal_strength=0.5,
        gross_reward_bps=2_000,
        exit_plan=ExitPlan(90, 120, 1),
        metadata=(("ordinal", str(ordinal)),),
    )

    def closed(realized: float) -> TradeRecord:
        return TradeRecord(
            intent,
            timestamp + 1,
            timestamp + 2,
            100,
            100 + realized,
            1,
            ExitReason.TIMEOUT,
        )

    return closed(pnl), closed(pnl * 0.6)


def _curve(
    cell_id: str,
    sleeve_id: str,
    symbol: str,
    trades: tuple[TradeRecord, ...],
) -> CellEquityCurve:
    snapshots: list[DailyCellSnapshot] = []
    for day in _dates(_HOLDOUT_START, (_HOLDOUT_END - _HOLDOUT_START).days):
        realized = sum(
            trade.net_pnl_usd
            for trade in trades
            if datetime.fromtimestamp(trade.exit_timestamp_ms / 1_000, UTC).date() <= day
        )
        snapshots.append(DailyCellSnapshot(day, 10_000 + realized, realized))
    return CellEquityCurve(cell_id, sleeve_id, symbol, tuple(snapshots), 10_000, trades)


def _terminal(
    candidate_freeze: CandidateFreeze,
) -> TerminalHoldoutEvidence:
    baseline_cells: list[CellEquityCurve] = []
    stress_cells: list[CellEquityCurve] = []
    intents: list[SleeveIntent] = []
    for sleeve_id, symbol in _SPECS:
        baseline_trades: list[TradeRecord] = []
        stress_trades: list[TradeRecord] = []
        for ordinal, (day_index, pnl) in enumerate(_schedule()):
            baseline, stress = _trade_pair(
                sleeve_id,
                symbol,
                day_index=day_index,
                ordinal=ordinal,
                pnl=pnl,
            )
            baseline_trades.append(baseline)
            stress_trades.append(stress)
            intents.append(baseline.intent)
        cell_id = f"{sleeve_id}-{symbol.lower()}"
        baseline_cells.append(_curve(cell_id, sleeve_id, symbol, tuple(baseline_trades)))
        stress_cells.append(_curve(cell_id, sleeve_id, symbol, tuple(stress_trades)))
    baseline_curves = tuple(baseline_cells)
    stress_curves = tuple(stress_cells)
    inventory = InputIntentInventory(tuple(sorted(intents, key=lambda intent: intent.intent_id)))
    common = {
        "data_sha256": _digest("holdout-data"),
        "code_sha256": _digest("code"),
        "candidate_sha256": candidate_freeze.candidate_freeze_sha256,
        "input_intent_inventory_sha256": inventory.fingerprint,
    }
    return TerminalHoldoutEvidence(
        baseline_curves,
        stress_curves,
        inventory,
        inventory,
        ScenarioRunFingerprint(
            **common,
            execution_config_sha256=_digest("baseline-execution"),
            cost_config_sha256=_digest("baseline-cost"),
            output_artifact_sha256=promotion_v2._cells_artifact_sha256(baseline_curves),
        ),
        ScenarioRunFingerprint(
            **common,
            execution_config_sha256=_digest("stress-execution"),
            cost_config_sha256=_digest("stress-cost"),
            output_artifact_sha256=promotion_v2._cells_artifact_sha256(stress_curves),
        ),
    )


def _plateau(matrix: SynchronousTrialMatrix) -> ParameterPlateauEvidence:
    outcomes = tuple(
        ParameterOutcome(
            trial_id,
            sum(math.log1p(value) for value in series.returns),
            sum(math.log1p(value) for value in series.returns) * 0.8,
            2.0,
        )
        for trial_id, series in zip(matrix.trial_ids, matrix.series, strict=True)
    )
    return ParameterPlateauEvidence(outcomes[0], outcomes[1:])


def _evidence(path: Path, *, add_failure: bool = False) -> PromotionEvidenceV2:
    plan = _experiment_plan()
    series = _nested_series()
    registry = _registry_evidence(path, plan, series, add_failure=add_failure)
    protocol = _protocol(plan, registry)
    matrix = SynchronousTrialMatrix(
        tuple(f"trial:{index}" for index in range(1, len(series) + 1)),
        series,
    )
    return PromotionEvidenceV2(
        registry,
        protocol,
        matrix,
        _nested_folds(plan, path.parent),
        _plateau(matrix),
        _terminal(protocol.candidate_freeze),
    )


@pytest.fixture(scope="module")
def valid_evidence(tmp_path_factory: pytest.TempPathFactory) -> PromotionEvidenceV2:
    return _evidence(tmp_path_factory.mktemp("promotion") / "trials.jsonl")


def _patched_scenarios(
    monkeypatch: pytest.MonkeyPatch,
    evidence: PromotionEvidenceV2,
    baseline: ScenarioPerformance,
    stress: ScenarioPerformance,
) -> None:
    def scenario(cells: tuple[CellEquityCurve, ...]) -> ScenarioPerformance:
        if cells is evidence.terminal_holdout.baseline_cells:
            return baseline
        if cells is evidence.terminal_holdout.stress_cells:
            return stress
        raise AssertionError("unexpected cell inventory")

    monkeypatch.setattr(promotion_v2, "_scenario", scenario)


def _scenario_result(
    growth: float,
    *,
    profit_factor: float = 2.0,
    sharpe: float = 2.0,
    sortino: float | None = 2.0,
    calmar: float | None = 2.0,
    drawdown: float = 0.05,
) -> ScenarioPerformance:
    return ScenarioPerformance(
        365,
        500,
        growth,
        math.log1p(growth),
        profit_factor,
        sharpe,
        sortino,
        calmar,
        drawdown,
    )


def _rebuild_cell(cell: CellEquityCurve, trades: tuple[TradeRecord, ...]) -> CellEquityCurve:
    return _curve(cell.cell_id, cell.sleeve_id, cell.symbol, trades)


def _with_stress_cells(
    holdout: TerminalHoldoutEvidence,
    cells: tuple[CellEquityCurve, ...],
) -> TerminalHoldoutEvidence:
    return replace(
        holdout,
        stress_cells=cells,
        stress_run=replace(
            holdout.stress_run,
            output_artifact_sha256=promotion_v2._cells_artifact_sha256(cells),
        ),
    )


def test_complete_local_evidence_is_diagnostic_and_fails_closed_without_external_proof(
    valid_evidence: PromotionEvidenceV2,
):
    report = evaluate_offline_to_shadow(valid_evidence)

    assert report.target is None
    assert not report.shadow_allowed
    assert not report.live_allowed
    assert report.reasons == (
        "external_nested_oos_attestation_unavailable",
        "unsealed_parameter_plateau_stress_evidence",
    )
    assert report.profitable_nested_fold_fraction == 0.75
    assert report.parameter_plateau.stable
    assert report.registry_head_hash == valid_evidence.registry.sealed_anchor.head_hash
    assert report.candidate_sha256 == (valid_evidence.protocol.candidate_freeze.candidate_freeze_sha256)
    assert report.dsr is not None and report.dsr.probability >= 0.95
    assert report.cscv is not None and report.cscv.pbo <= 0.05


def test_registry_snapshot_matrix_selection_and_anchor_are_exactly_linked(
    valid_evidence: PromotionEvidenceV2,
):
    snapshot = valid_evidence.registry.verify()

    assert snapshot.frozen
    assert snapshot.selection == valid_evidence.registry.final_selection
    assert snapshot.sealed_anchor == valid_evidence.registry.sealed_anchor
    assert snapshot.sealed_anchor is not None
    assert snapshot.sealed_anchor.head_hash == snapshot.head_hash
    assert valid_evidence.trial_matrix.trial_ids == tuple(
        f"trial:{trial.trial_id}" for trial in snapshot.trials
    )
    assert valid_evidence.trial_matrix.series == tuple(trial.daily_returns for trial in snapshot.trials)


def test_two_stage_identity_has_no_post_selection_hash_cycle(
    valid_evidence: PromotionEvidenceV2,
):
    plan = valid_evidence.protocol.experiment_plan
    freeze = valid_evidence.protocol.candidate_freeze
    snapshot = valid_evidence.registry.verify()

    assert not plan.research_protocol.is_frozen
    assert all(trial.fingerprints.protocol_sha256 == plan.preregistration_sha256 for trial in snapshot.trials)
    assert freeze.experiment_plan_sha256 == plan.preregistration_sha256
    assert freeze.registry_head_hash == snapshot.head_hash
    assert freeze.selected_config_sha256 == valid_evidence.registry.selected_trial.fingerprints.config_sha256
    assert freeze.candidate_freeze_sha256 != plan.preregistration_sha256
    assert (
        replace(
            freeze,
            frozen_at=freeze.frozen_at + timedelta(seconds=1),
        ).candidate_freeze_sha256
        != freeze.candidate_freeze_sha256
    )


def test_promotion_evidence_hash_binds_blind_authorization_claim(
    valid_evidence: PromotionEvidenceV2,
):
    changed = replace(
        valid_evidence.protocol,
        blind_authorized_at=valid_evidence.protocol.blind_authorized_at + timedelta(seconds=1),
    )

    assert changed.evidence_sha256 != valid_evidence.protocol.evidence_sha256


def test_candidate_freeze_is_rechecked_against_exact_registry(
    valid_evidence: PromotionEvidenceV2,
):
    forged = replace(
        valid_evidence.protocol.candidate_freeze,
        registry_head_hash=_digest("other-registry-head"),
    )
    forged_protocol = replace(valid_evidence.protocol, candidate_freeze=forged)

    with pytest.raises(ValueError, match="exact sealed registry selection"):
        replace(valid_evidence, protocol=forged_protocol)

    with pytest.raises(ValueError, match="candidate commit"):
        replace(
            valid_evidence.protocol,
            research_protocol=replace(
                valid_evidence.protocol.research_protocol,
                candidate_commit="b" * 40,
            ),
        )


def test_fold_and_cscv_layout_cannot_be_changed_after_trials(
    valid_evidence: PromotionEvidenceV2,
):
    plan = valid_evidence.protocol.experiment_plan
    shifted_schedule = replace(
        plan.nested_folds,
        first_test_start=plan.nested_folds.first_test_start + timedelta(days=1),
    )
    shifted_plan = replace(plan, nested_folds=shifted_schedule)
    assert shifted_plan.preregistration_sha256 != plan.preregistration_sha256
    with pytest.raises(ValueError, match="does not bind the experiment plan"):
        replace(valid_evidence.protocol, experiment_plan=shifted_plan)

    changed_cscv = replace(plan.nested_folds, cscv_blocks=10)
    changed_plan = replace(plan, nested_folds=changed_cscv)
    assert changed_plan.preregistration_sha256 != plan.preregistration_sha256
    with pytest.raises(ValueError, match="does not bind the experiment plan"):
        CandidateFreeze.capture_selection(
            changed_plan,
            valid_evidence.registry,
            candidate_commit="a" * 40,
            frozen_at=valid_evidence.protocol.candidate_freeze.frozen_at,
        )
    with pytest.raises(ValueError, match="unsupported CSCV algorithm"):
        replace(plan.nested_folds, cscv_algorithm_sha256=_digest("different-CSCV"))


def test_candidate_freeze_rejects_optional_stopping_and_wrong_rule_result(tmp_path: Path):
    plan = _experiment_plan()
    stopped = TrialRegistry(tmp_path / "stopped.jsonl")
    stopped.append_success(_fingerprints(plan, 0), _nested_series()[0])
    stopped.finalize_selection(1)
    with pytest.raises(ValueError, match="exact predeclared trial inventory"):
        CandidateFreeze.capture_selection(
            plan,
            FrozenTrialRegistryEvidence.capture(stopped),
            candidate_commit="a" * 40,
            frozen_at=datetime(2024, 12, 1, tzinfo=UTC),
        )

    small_plan = replace(
        plan,
        research_protocol=replace(plan.research_protocol, max_trials=2),
        final_trial_config_sha256s=plan.final_trial_config_sha256s[:2],
    )
    dates = _dates(_SELECTION_START, (_SELECTION_END - _SELECTION_START).days)
    wrong = TrialRegistry(tmp_path / "wrong-selection.jsonl")
    wrong.append_success(
        _fingerprints(small_plan, 0),
        DailyReturnSeries(dates, tuple(0.0001 for _ in dates)),
    )
    wrong.append_success(
        _fingerprints(small_plan, 1),
        DailyReturnSeries(dates, tuple(0.0002 for _ in dates)),
    )
    wrong.finalize_selection(1)
    with pytest.raises(ValueError, match="predeclared selection rule"):
        CandidateFreeze.capture_selection(
            small_plan,
            FrozenTrialRegistryEvidence.capture(wrong),
            candidate_commit="a" * 40,
            frozen_at=datetime(2024, 12, 1, tzinfo=UTC),
        )

    false_rejection = TrialRegistry(tmp_path / "false-rejection.jsonl")
    for trial, value in enumerate((0.0001, 0.0002)):
        false_rejection.append_success(
            _fingerprints(small_plan, trial),
            DailyReturnSeries(dates, tuple(value for _ in dates)),
        )
    false_rejection.finalize_rejection()
    with pytest.raises(ValueError, match="REJECT_ALL conflicts"):
        CandidateFreeze.capture_reject_all(
            small_plan,
            FrozenTrialRegistryEvidence.capture(false_rejection),
            candidate_commit="a" * 40,
            code_sha256=_digest("code"),
            data_sha256=_digest("nested-data"),
            dependency_sha256=_digest("dependencies"),
            container_sha256=_digest("container"),
            frozen_at=datetime(2024, 12, 1, tzinfo=UTC),
        )


def test_nested_schedule_cannot_escape_registered_data_role():
    plan = _experiment_plan()
    outside = replace(
        plan.nested_folds,
        train_origin=_NESTED_WINDOW_START - timedelta(days=1),
    )

    with pytest.raises(ValueError, match="registered robustness window"):
        replace(plan, nested_folds=outside)


def test_reject_all_freeze_is_sealed_without_inventing_a_candidate(tmp_path: Path):
    plan = _experiment_plan()
    registry = TrialRegistry(tmp_path / "reject-all.jsonl")
    for trial in range(_TRIALS):
        registry.append_failure(_fingerprints(plan, trial), FailureClass.STRATEGY)
    rejection = registry.finalize_rejection()
    frozen = FrozenTrialRegistryEvidence.capture(registry)
    candidate_freeze = CandidateFreeze.capture_reject_all(
        plan,
        frozen,
        candidate_commit="a" * 40,
        code_sha256=_digest("code"),
        data_sha256=_digest("nested-data"),
        dependency_sha256=_digest("dependencies"),
        container_sha256=_digest("container"),
        frozen_at=datetime(2024, 12, 1, tzinfo=UTC),
    )

    assert frozen.outcome is RegistryOutcome.REJECT_ALL
    assert frozen.final_selection is None
    assert frozen.final_rejection == rejection
    assert candidate_freeze.outcome is RegistryOutcome.REJECT_ALL
    assert candidate_freeze.registry_terminal_sha256 == rejection.rejection_sha256
    assert candidate_freeze.selected_trial_id is None
    assert candidate_freeze.selected_config_sha256 is None
    with pytest.raises(ValueError, match="no candidate"):
        _ = frozen.candidate_sha256
    with pytest.raises(ValueError, match="selected candidate"):
        PromotionProtocolEvidence(
            plan,
            replace(
                plan.research_protocol,
                candidate_commit="a" * 40,
                parameter_set_sha256=_digest("config:0"),
                frozen_at=candidate_freeze.frozen_at,
            ),
            candidate_freeze,
            datetime(2026, 1, 1, tzinfo=UTC),
        )


def test_registry_with_any_failed_trial_is_rejected(tmp_path: Path):
    with pytest.raises(ValueError, match="failed trials"):
        _evidence(tmp_path / "failed.jsonl", add_failure=True)


def test_incomplete_or_reordered_trial_inventory_is_rejected(
    valid_evidence: PromotionEvidenceV2,
):
    matrix = valid_evidence.trial_matrix
    incomplete = SynchronousTrialMatrix(matrix.trial_ids[:-1], matrix.series[:-1])
    with pytest.raises(ValueError, match="complete frozen registry inventory"):
        replace(valid_evidence, trial_matrix=incomplete)

    reordered = SynchronousTrialMatrix(
        (matrix.trial_ids[1], matrix.trial_ids[0], *matrix.trial_ids[2:]),
        (matrix.series[1], matrix.series[0], *matrix.series[2:]),
    )
    with pytest.raises(ValueError, match="complete frozen registry inventory"):
        replace(valid_evidence, trial_matrix=reordered)


def test_forged_selection_or_anchor_linkage_is_rejected(valid_evidence: PromotionEvidenceV2):
    assert valid_evidence.registry.final_selection is not None
    forged_selection = replace(
        valid_evidence.registry.final_selection,
        candidate_sha256="f" * 64,
    )
    with pytest.raises(ValueError, match="inconsistent"):
        replace(valid_evidence.registry, final_selection=forged_selection)

    forged_anchor = replace(valid_evidence.registry.sealed_anchor, head_hash="e" * 64)
    with pytest.raises(ValueError, match="inconsistent"):
        replace(valid_evidence.registry, sealed_anchor=forged_anchor)


def test_registry_is_reverified_when_promotion_decision_is_made(tmp_path: Path):
    evidence = _evidence(tmp_path / "reverify.jsonl")
    TrialRegistry(evidence.registry.registry_path).anchor_path.unlink()

    with pytest.raises(RegistryIntegrityError, match="missing its sealed anchor"):
        evaluate_offline_to_shadow(evidence)


def test_each_nested_selection_registry_is_reverified_at_decision_time(tmp_path: Path):
    evidence = _evidence(tmp_path / "nested-reverify.jsonl")
    nested_path = evidence.nested_oos_folds[0].selection_registry.registry_path
    TrialRegistry(nested_path).anchor_path.unlink()

    with pytest.raises(RegistryIntegrityError, match="missing its sealed anchor"):
        evaluate_offline_to_shadow(evidence)


def test_nested_oos_is_canonical_predeclared_purged_and_separate_from_holdout(
    valid_evidence: PromotionEvidenceV2,
):
    folds = valid_evidence.nested_oos_folds
    protocol = valid_evidence.protocol.nested_folds
    selection = valid_evidence.protocol.final_trial_window
    holdout = valid_evidence.protocol.terminal_holdout_window

    assert tuple(fold.fold_id for fold in folds) == tuple(f"nested-oos-{index:03d}" for index in range(1, 9))
    assert all(fold.test_days == protocol.test_days for fold in folds)
    assert all(
        (
            fold.train_start,
            fold.train_end_exclusive,
            fold.test_start,
            fold.test_end_exclusive,
        )
        == protocol.expected_boundaries(index)
        for index, fold in enumerate(folds)
    )
    assert all(
        fold.train_end_exclusive + timedelta(days=protocol.purge_days) == fold.test_start for fold in folds
    )
    assert len({fold.selection_registry.registry_path for fold in folds}) == len(folds)
    assert all(fold.candidate_sha256 == fold.selection_registry.candidate_sha256 for fold in folds)
    assert set(valid_evidence.trial_matrix.series[0].dates).isdisjoint(
        day for fold in folds for day in fold.test_returns.dates
    )
    assert valid_evidence.trial_matrix.series[0].dates == _dates(
        selection.start,
        (selection.end - selection.start).days,
    )
    assert selection.end + timedelta(days=protocol.purge_days) <= folds[0].test_start
    assert folds[-1].test_end_exclusive + timedelta(days=protocol.purge_days) < holdout.start
    assert synchronize_cells(valid_evidence.terminal_holdout.baseline_cells).dates == _dates(
        holdout.start,
        (holdout.end - holdout.start).days,
    )


def test_arbitrary_fold_repartition_missing_fold_and_wrong_purge_are_rejected(
    valid_evidence: PromotionEvidenceV2,
):
    first = valid_evidence.nested_oos_folds[0]
    extended_returns = DailyReturnSeries(
        (*first.test_returns.dates, first.test_end_exclusive),
        (*first.test_returns.returns, 0.0),
    )
    with pytest.raises(ValueError, match="absolute predeclared schedule"):
        replace(
            valid_evidence,
            nested_oos_folds=(
                replace(
                    first,
                    test_end_exclusive=first.test_end_exclusive + timedelta(days=1),
                    test_returns=extended_returns,
                ),
                *valid_evidence.nested_oos_folds[1:],
            ),
        )
    with pytest.raises(ValueError, match="fold count"):
        replace(valid_evidence, nested_oos_folds=valid_evidence.nested_oos_folds[:-1])
    with pytest.raises(ValueError, match="absolute predeclared schedule"):
        replace(
            valid_evidence,
            nested_oos_folds=(
                replace(first, train_end_exclusive=first.train_end_exclusive - timedelta(days=1)),
                *valid_evidence.nested_oos_folds[1:],
            ),
        )


def test_non_blind_terminal_window_cannot_be_promotion_evidence():
    plan = _experiment_plan()
    research = replace(
        plan.research_protocol,
        windows=(
            DataWindow("selection", _SELECTION_START, _SELECTION_END, DataRole.SELECTION),
            DataWindow(
                "nested",
                _NESTED_WINDOW_START,
                _NESTED_WINDOW_END,
                DataRole.ROBUSTNESS,
            ),
            DataWindow("terminal", _HOLDOUT_START, _HOLDOUT_END, DataRole.ROBUSTNESS),
        ),
    )
    with pytest.raises(ValueError, match="blind data window"):
        replace(plan, research_protocol=research)


def test_registered_selection_and_terminal_windows_cannot_overlap():
    plan = _experiment_plan()

    with pytest.raises(ValueError, match="must not overlap"):
        replace(
            plan.research_protocol,
            windows=(
                DataWindow("selection", date(2024, 1, 1), date(2025, 7, 1), DataRole.SELECTION),
                DataWindow("terminal", _HOLDOUT_START, _HOLDOUT_END, DataRole.BLIND),
            ),
        )


def test_baseline_and_stress_require_common_provenance_but_distinct_execution_and_cost(
    valid_evidence: PromotionEvidenceV2,
):
    holdout = valid_evidence.terminal_holdout
    with pytest.raises(ValueError, match="distinct execution"):
        replace(
            holdout,
            stress_run=replace(
                holdout.stress_run,
                execution_config_sha256=holdout.baseline_run.execution_config_sha256,
            ),
        )
    with pytest.raises(ValueError, match="distinct cost"):
        replace(
            holdout,
            stress_run=replace(
                holdout.stress_run,
                cost_config_sha256=holdout.baseline_run.cost_config_sha256,
            ),
        )
    with pytest.raises(ValueError, match="common provenance"):
        replace(
            holdout,
            stress_run=replace(holdout.stress_run, data_sha256=_digest("other-data")),
        )
    with pytest.raises(ValueError, match="genuinely distinct"):
        replace(holdout, stress_cells=holdout.baseline_cells)


def test_scenario_output_fingerprint_binds_exact_curves_snapshots_and_trades(
    valid_evidence: PromotionEvidenceV2,
):
    holdout = valid_evidence.terminal_holdout

    with pytest.raises(ValueError, match="output artifact fingerprint"):
        replace(
            holdout,
            stress_run=replace(
                holdout.stress_run,
                output_artifact_sha256=_digest("forged-output"),
            ),
        )


def test_complete_input_inventory_not_executed_subset_is_compared(
    valid_evidence: PromotionEvidenceV2,
):
    holdout = valid_evidence.terminal_holdout
    first = holdout.stress_cells[0]
    reduced = _rebuild_cell(first, first.trades[:49])
    changed_holdout = _with_stress_cells(holdout, (reduced, *holdout.stress_cells[1:]))
    changed = replace(valid_evidence, terminal_holdout=changed_holdout)

    report = evaluate_offline_to_shadow(changed)

    assert changed_holdout.baseline_input_intents == changed_holdout.stress_input_intents
    assert changed_holdout.baseline_input_intents.fingerprint == (
        changed_holdout.stress_run.input_intent_inventory_sha256
    )
    assert f"stress_cell_insufficient_trades:{first.cell_id}" in report.reasons
    assert "insufficient_stress_closed_trades" in report.reasons


def test_changed_or_misdeclared_input_inventory_is_rejected(
    valid_evidence: PromotionEvidenceV2,
):
    holdout = valid_evidence.terminal_holdout
    reduced = InputIntentInventory(holdout.stress_input_intents.intents[:-1])
    with pytest.raises(ValueError, match="exact same input intent"):
        replace(holdout, stress_input_intents=reduced)
    with pytest.raises(ValueError, match="does not match"):
        replace(
            holdout,
            stress_run=replace(
                holdout.stress_run,
                input_intent_inventory_sha256=_digest("wrong-inventory"),
            ),
        )


def test_stress_cell_sleeve_survival_and_concentration_are_gated(
    valid_evidence: PromotionEvidenceV2,
):
    holdout = valid_evidence.terminal_holdout
    concentrated: list[CellEquityCurve] = []
    for cell in holdout.stress_cells:
        multiplier = 3.0 if cell.sleeve_id == "trend" else 0.1
        trades = tuple(
            replace(
                trade,
                exit_price=trade.entry_price + (trade.exit_price - trade.entry_price) * multiplier,
            )
            for trade in cell.trades
        )
        concentrated.append(_rebuild_cell(cell, trades))
    evidence = replace(
        valid_evidence,
        terminal_holdout=_with_stress_cells(holdout, tuple(concentrated)),
    )

    report = evaluate_offline_to_shadow(evidence)

    assert "excessive_stress_sleeve_profit_concentration" in report.reasons
    assert all(not reason.startswith("excessive_baseline") for reason in report.reasons)


def test_strict_concentration_comparison_rejects_any_value_above_boundary(
    monkeypatch: pytest.MonkeyPatch,
    valid_evidence: PromotionEvidenceV2,
):
    original = promotion_v2.synchronize_cells

    def synchronized(cells: tuple[CellEquityCurve, ...]):  # type: ignore[no-untyped-def]
        result = original(cells)
        if cells is valid_evidence.terminal_holdout.stress_cells:
            return replace(
                result,
                maximum_sleeve_profit_contribution=math.nextafter(0.40, math.inf),
            )
        return result

    monkeypatch.setattr(promotion_v2, "synchronize_cells", synchronized)

    report = evaluate_offline_to_shadow(valid_evidence)

    assert "excessive_stress_sleeve_profit_concentration" in report.reasons


def test_all_numeric_policy_boundaries_are_inclusive_but_wrong_side_is_strict(
    monkeypatch: pytest.MonkeyPatch,
    valid_evidence: PromotionEvidenceV2,
):
    baseline = _scenario_result(
        0.20,
        profit_factor=1.25,
        sharpe=1.0,
        sortino=1.25,
        calmar=1.0,
        drawdown=0.12,
    )
    stress = _scenario_result(
        0.10,
        profit_factor=1.10,
        sharpe=0.5,
        sortino=None,
        calmar=None,
        drawdown=0.15,
    )
    _patched_scenarios(monkeypatch, valid_evidence, baseline, stress)
    exact = evaluate_offline_to_shadow(valid_evidence)
    assert exact.target is None
    assert not exact.shadow_allowed
    assert "weak_baseline_profit_factor" not in exact.reasons

    below = replace(baseline, profit_factor=math.nextafter(1.25, -math.inf))
    _patched_scenarios(monkeypatch, valid_evidence, below, stress)
    rejected = evaluate_offline_to_shadow(valid_evidence)
    assert "weak_baseline_profit_factor" in rejected.reasons


def test_stress_retention_uses_compounded_log_growth_not_simple_return_ratio(
    monkeypatch: pytest.MonkeyPatch,
    valid_evidence: PromotionEvidenceV2,
):
    baseline = _scenario_result(1.0)
    log_passes_simple_fails = _scenario_result(0.45)
    _patched_scenarios(monkeypatch, valid_evidence, baseline, log_passes_simple_fails)

    passing = evaluate_offline_to_shadow(valid_evidence)

    assert 0.45 / 1.0 < 0.50
    assert math.log1p(0.45) / math.log1p(1.0) > 0.50
    assert "insufficient_stress_log_growth_retention" not in passing.reasons

    log_fails = _scenario_result(0.40)
    _patched_scenarios(monkeypatch, valid_evidence, baseline, log_fails)
    rejected = evaluate_offline_to_shadow(valid_evidence)
    assert "insufficient_stress_log_growth_retention" in rejected.reasons


def test_dsr_and_cscv_results_are_recomputed_and_formula_checked(
    monkeypatch: pytest.MonkeyPatch,
    valid_evidence: PromotionEvidenceV2,
):
    original = evaluate_offline_to_shadow(valid_evidence)
    assert original.dsr is not None and original.cscv is not None
    forged_dsr = replace(
        original.dsr,
        probability=math.nextafter(original.dsr.probability, 0.0),
    )
    monkeypatch.setattr(promotion_v2, "deflated_sharpe_ratio", lambda *_: forged_dsr)
    invalid_dsr = evaluate_offline_to_shadow(valid_evidence)
    assert "dsr_evidence_invalid" in invalid_dsr.reasons
    assert invalid_dsr.dsr is None

    monkeypatch.undo()
    forged_cscv = replace(original.cscv, pbo=math.nextafter(original.cscv.pbo, 1.0))
    monkeypatch.setattr(promotion_v2, "cscv_pbo", lambda *_args, **_kwargs: forged_cscv)
    invalid_cscv = evaluate_offline_to_shadow(valid_evidence)
    assert "cscv_evidence_invalid" in invalid_cscv.reasons
    assert invalid_cscv.cscv is None


def test_parameter_plateau_is_required_complete_recomputed_and_stable(
    valid_evidence: PromotionEvidenceV2,
):
    boundary = replace(valid_evidence.parameter_plateau, selected_on_boundary=True)
    rejected = evaluate_offline_to_shadow(replace(valid_evidence, parameter_plateau=boundary))
    assert "unstable_parameter_plateau" in rejected.reasons
    assert "parameter_plateau:selected_on_search_boundary" in rejected.reasons

    incomplete = replace(
        valid_evidence.parameter_plateau,
        neighbors=valid_evidence.parameter_plateau.neighbors[:-1],
    )
    with pytest.raises(ValueError, match="every registered trial"):
        replace(valid_evidence, parameter_plateau=incomplete)

    forged_selected = replace(
        valid_evidence.parameter_plateau.selected,
        baseline_log_growth=math.nextafter(
            valid_evidence.parameter_plateau.selected.baseline_log_growth,
            math.inf,
        ),
    )
    with pytest.raises(ValueError, match="recomputed"):
        replace(
            valid_evidence,
            parameter_plateau=replace(valid_evidence.parameter_plateau, selected=forged_selected),
        )


def test_report_attestation_prevents_hand_construction_or_posthoc_authorization(
    valid_evidence: PromotionEvidenceV2,
):
    report = evaluate_offline_to_shadow(valid_evidence)
    assert not report.shadow_allowed

    with pytest.raises(ValueError, match="external signed evidence verifier"):
        replace(report, target=PromotionTarget.SHADOW, reasons=())

    with pytest.raises(ValueError, match="attestation"):
        replace(report, target=None, reasons=("posthoc-forgery",))
    with pytest.raises(ValueError, match="verified evaluator"):
        replace(report, _factory_token=object())


def test_nonfinite_and_missing_typed_evidence_fails_closed(valid_evidence: PromotionEvidenceV2):
    with pytest.raises(ValueError, match="growth"):
        ScenarioPerformance(365, 500, math.nan, math.nan, 2.0, 2.0, 2.0, 2.0, 0.1)
    with pytest.raises(ValueError, match="fingerprints"):
        replace(valid_evidence.terminal_holdout.stress_run, cost_config_sha256="not-a-sha")
    with pytest.raises(TypeError, match="trial_matrix"):
        replace(valid_evidence, trial_matrix=None)  # type: ignore[arg-type]


def test_reason_order_is_deterministic_for_repeated_rejection(
    valid_evidence: PromotionEvidenceV2,
):
    boundary = replace(valid_evidence.parameter_plateau, selected_on_boundary=True)
    evidence = replace(valid_evidence, parameter_plateau=boundary)

    first = evaluate_offline_to_shadow(evidence)
    second = evaluate_offline_to_shadow(evidence)

    assert first.reasons == second.reasons
    assert len(first.reasons) == len(set(first.reasons))
