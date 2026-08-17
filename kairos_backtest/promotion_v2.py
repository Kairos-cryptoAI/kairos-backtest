"""Fail-closed, registry-bound offline-to-shadow promotion for strategy v2."""

from __future__ import annotations

import hashlib
import hmac
import json
import math
from dataclasses import asdict, dataclass, field, is_dataclass
from datetime import date, datetime, timedelta
from enum import Enum, StrEnum
from pathlib import Path
from statistics import NormalDist, fmean, pvariance

from .portfolio import CellEquityCurve, PortfolioEvidence, synchronize_cells
from .research_protocol import DataRole, DataWindow, ResearchProtocol, ResearchPurpose
from .robustness import (
    CSCVResult,
    DailyReturnSeries,
    DeflatedSharpeResult,
    ParameterOutcome,
    ParameterPlateauPolicy,
    ParameterPlateauReport,
    PerformanceMeasure,
    SynchronousTrialMatrix,
    cscv_pbo,
    deflated_sharpe_ratio,
    hac_sharpe,
    non_annualized_sharpe,
    parameter_plateau_report,
)
from .strategy_models import SleeveIntent, TradeRecord
from .trial_registry import (
    RegistryOutcome,
    RegistrySnapshot,
    RejectionRecord,
    SealedRegistryAnchor,
    SelectionRecord,
    TrialRecord,
    TrialRegistry,
    TrialStatus,
)

_DAY_MS = 24 * 60 * 60 * 1_000
SELECTION_RULE_SHA256 = hashlib.sha256(
    b"kairos.selection.max-log-growth-above-floor.lowest-trial-id-tiebreak:v1"
).hexdigest()
CSCV_ALGORITHM_SHA256 = hashlib.sha256(b"kairos.robustness.cscv_pbo.contiguous-equal-blocks:v1").hexdigest()


def _finite(value: object) -> bool:
    return not isinstance(value, bool) and isinstance(value, (int, float)) and math.isfinite(value)


def _is_sha256(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and value == value.lower()
        and all(character in "0123456789abcdef" for character in value)
    )


def _is_git_sha(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 40
        and value == value.lower()
        and all(character in "0123456789abcdef" for character in value)
    )


def _canonical_sha256(value: object) -> str:
    encoded = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def _canonical_value(value: object) -> object:
    """Convert immutable evidence into a stable, JSON-safe representation."""

    if isinstance(value, Enum):
        return value.value
    if isinstance(value, datetime | date):
        return value.isoformat()
    if is_dataclass(value) and not isinstance(value, type):
        return _canonical_value(asdict(value))
    if isinstance(value, dict):
        if any(not isinstance(key, str) for key in value):
            raise TypeError("canonical evidence mappings require string keys")
        return {key: _canonical_value(item) for key, item in value.items()}
    if isinstance(value, tuple | list):
        return [_canonical_value(item) for item in value]
    if value is None or isinstance(value, str | bool | int):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("canonical evidence cannot contain non-finite floats")
        return value
    raise TypeError(f"unsupported canonical evidence type: {type(value).__name__}")


def _cells_artifact_sha256(cells: tuple[CellEquityCurve, ...]) -> str:
    """Bind a scenario declaration to its exact curves, snapshots and trades."""

    return _canonical_sha256({"cells": _canonical_value(cells), "schema": 1})


def _date_range(start: date, end_exclusive: date) -> tuple[date, ...]:
    return tuple(start + timedelta(days=index) for index in range((end_exclusive - start).days))


def _trial_key(trial_id: int) -> str:
    return f"trial:{trial_id}"


def _log_growth(series: DailyReturnSeries) -> float:
    result = sum(math.log1p(value) for value in series.returns)
    if not math.isfinite(result):
        raise ValueError("daily return log growth must be finite")
    return result


def _rule_selected_trial(
    snapshot: RegistrySnapshot,
    protocol: NestedFoldProtocol,
) -> TrialRecord | None:
    """Execute the only supported, versioned candidate-selection rule."""

    candidates = tuple(
        (trial, _log_growth(trial.daily_returns))
        for trial in snapshot.trials
        if trial.status is TrialStatus.SUCCESS
        and trial.daily_returns is not None
        and _log_growth(trial.daily_returns) >= protocol.minimum_candidate_log_growth
    )
    if not candidates:
        return None
    return max(candidates, key=lambda item: (item[1], -item[0].trial_id))[0]


@dataclass(frozen=True, slots=True)
class OfflineToShadowPolicy:
    minimum_evaluation_days: int = 365
    minimum_closed_trades: int = 500
    minimum_nested_folds: int = 6
    minimum_nested_train_days: int = 365
    minimum_nested_test_days: int = 30
    minimum_nested_test_trades: int = 50
    minimum_profitable_fold_fraction: float = 0.75
    minimum_baseline_profit_factor: float = 1.25
    minimum_baseline_hac_sharpe: float = 1.0
    minimum_baseline_sortino: float = 1.25
    minimum_baseline_calmar: float = 1.0
    maximum_baseline_drawdown: float = 0.12
    minimum_stress_profit_factor: float = 1.10
    minimum_stress_hac_sharpe: float = 0.5
    maximum_stress_drawdown: float = 0.15
    minimum_stress_log_growth_retention: float = 0.50
    minimum_dsr_probability: float = 0.95
    maximum_cscv_pbo: float = 0.05
    maximum_cscv_loss_probability: float = 0.10
    minimum_cell_trades: int = 50
    minimum_cell_profit_factor: float = 1.0
    minimum_sleeve_trades: int = 100
    minimum_sleeve_profit_factor: float = 1.10
    minimum_active_sleeves: int = 3
    minimum_active_symbols: int = 3
    maximum_profit_contribution: float = 0.40

    def __post_init__(self) -> None:
        integer_fields = (
            self.minimum_evaluation_days,
            self.minimum_closed_trades,
            self.minimum_nested_folds,
            self.minimum_nested_train_days,
            self.minimum_nested_test_days,
            self.minimum_nested_test_trades,
            self.minimum_cell_trades,
            self.minimum_sleeve_trades,
            self.minimum_active_sleeves,
            self.minimum_active_symbols,
        )
        if any(
            isinstance(value, bool) or not isinstance(value, int) or value <= 0 for value in integer_fields
        ):
            raise ValueError("promotion count thresholds must be positive integers")
        non_negative = (
            self.minimum_baseline_profit_factor,
            self.minimum_baseline_hac_sharpe,
            self.minimum_baseline_sortino,
            self.minimum_baseline_calmar,
            self.minimum_stress_profit_factor,
            self.minimum_stress_hac_sharpe,
            self.minimum_cell_profit_factor,
            self.minimum_sleeve_profit_factor,
        )
        if any(not _finite(value) or value < 0 for value in non_negative):
            raise ValueError("promotion metric thresholds must be finite and non-negative")
        fractions = (
            self.minimum_profitable_fold_fraction,
            self.maximum_baseline_drawdown,
            self.maximum_stress_drawdown,
            self.minimum_stress_log_growth_retention,
            self.minimum_dsr_probability,
            self.maximum_cscv_pbo,
            self.maximum_cscv_loss_probability,
            self.maximum_profit_contribution,
        )
        if any(not _finite(value) or not 0 <= value <= 1 for value in fractions):
            raise ValueError("promotion fraction thresholds must be finite within [0, 1]")


OFFLINE_TO_SHADOW_POLICY = OfflineToShadowPolicy()


@dataclass(frozen=True, slots=True)
class NestedFoldProtocol:
    """Predeclared absolute expanding-window layout and CSCV design."""

    protocol_name: str
    train_origin: date
    first_test_start: date
    fold_count: int
    minimum_train_days: int
    test_days: int
    minimum_test_trades: int
    purge_days: int
    cscv_blocks: int
    cscv_performance_measure: PerformanceMeasure
    cscv_algorithm_sha256: str
    minimum_candidate_log_growth: float
    selection_rule_sha256: str
    fingerprint: str = field(init=False)

    def __post_init__(self) -> None:
        if (
            not isinstance(self.protocol_name, str)
            or not self.protocol_name
            or self.protocol_name != self.protocol_name.strip()
        ):
            raise ValueError("nested fold protocol name must be normalized")
        counts = (
            self.fold_count,
            self.minimum_train_days,
            self.test_days,
            self.minimum_test_trades,
            self.purge_days,
        )
        if any(isinstance(value, bool) or not isinstance(value, int) or value <= 0 for value in counts):
            raise ValueError("nested fold protocol counts must be positive integers")
        if type(self.train_origin) is not date or type(self.first_test_start) is not date:
            raise TypeError("nested fold absolute boundaries must be dates")
        if self.train_origin + timedelta(days=self.minimum_train_days + self.purge_days) > (
            self.first_test_start
        ):
            raise ValueError("first nested fold cannot satisfy its predeclared training and purge periods")
        if (
            isinstance(self.cscv_blocks, bool)
            or not isinstance(self.cscv_blocks, int)
            or self.cscv_blocks < 4
            or self.cscv_blocks > 20
            or self.cscv_blocks % 2
        ):
            raise ValueError("cscv_blocks must be an even integer within [4, 20]")
        if self.cscv_performance_measure not in {"mean", "sharpe"}:
            raise ValueError("CSCV performance measure must be 'mean' or 'sharpe'")
        if self.cscv_algorithm_sha256 != CSCV_ALGORITHM_SHA256:
            raise ValueError("unsupported CSCV algorithm fingerprint")
        if not _finite(self.minimum_candidate_log_growth):
            raise ValueError("candidate log-growth floor must be finite")
        if self.selection_rule_sha256 != SELECTION_RULE_SHA256:
            raise ValueError("unsupported candidate-selection rule fingerprint")
        object.__setattr__(
            self,
            "fingerprint",
            _canonical_sha256(
                {
                    "cscv_blocks": self.cscv_blocks,
                    "cscv_algorithm_sha256": self.cscv_algorithm_sha256,
                    "cscv_performance_measure": self.cscv_performance_measure,
                    "fold_count": self.fold_count,
                    "fold_boundaries": [
                        {
                            "test_end_exclusive": boundaries[3].isoformat(),
                            "test_start": boundaries[2].isoformat(),
                            "train_end_exclusive": boundaries[1].isoformat(),
                            "train_start": boundaries[0].isoformat(),
                        }
                        for boundaries in (
                            self.expected_boundaries(index) for index in range(self.fold_count)
                        )
                    ],
                    "first_test_start": self.first_test_start.isoformat(),
                    "minimum_test_trades": self.minimum_test_trades,
                    "minimum_train_days": self.minimum_train_days,
                    "minimum_candidate_log_growth": self.minimum_candidate_log_growth,
                    "protocol_name": self.protocol_name,
                    "purge_days": self.purge_days,
                    "selection_rule_sha256": self.selection_rule_sha256,
                    "test_days": self.test_days,
                    "train_origin": self.train_origin.isoformat(),
                }
            ),
        )

    def expected_boundaries(self, zero_based_index: int) -> tuple[date, date, date, date]:
        """Return the exact train/test boundaries committed for one fold."""

        if (
            isinstance(zero_based_index, bool)
            or not isinstance(zero_based_index, int)
            or not 0 <= zero_based_index < self.fold_count
        ):
            raise ValueError("nested fold index is outside the predeclared schedule")
        test_start = self.first_test_start + timedelta(days=zero_based_index * self.test_days)
        return (
            self.train_origin,
            test_start - timedelta(days=self.purge_days),
            test_start,
            test_start + timedelta(days=self.test_days),
        )


@dataclass(frozen=True, slots=True)
class ExperimentPlan:
    """Immutable pre-selection plan whose SHA is known to every trial."""

    research_protocol: ResearchProtocol
    nested_folds: NestedFoldProtocol
    nested_evaluation_window_name: str
    final_trial_window_name: str
    terminal_holdout_window_name: str
    final_trial_config_sha256s: tuple[str, ...]
    nested_trial_config_sha256s: tuple[str, ...]
    preregistration_sha256: str = field(init=False)

    def __post_init__(self) -> None:
        if not isinstance(self.research_protocol, ResearchProtocol):
            raise TypeError("research_protocol must be ResearchProtocol")
        if self.research_protocol.is_frozen:
            raise ValueError("experiment plan must be created before candidate selection")
        if not isinstance(self.nested_folds, NestedFoldProtocol):
            raise TypeError("nested_folds must be NestedFoldProtocol")
        names = (
            self.nested_evaluation_window_name,
            self.final_trial_window_name,
            self.terminal_holdout_window_name,
        )
        if any(not isinstance(name, str) or not name or name != name.strip() for name in names):
            raise ValueError("experiment data window names must be normalized")
        if len(set(names)) != len(names):
            raise ValueError("experiment data windows must be distinct")
        inventories = (self.final_trial_config_sha256s, self.nested_trial_config_sha256s)
        if any(
            not isinstance(inventory, tuple)
            or not inventory
            or len(set(inventory)) != len(inventory)
            or any(not _is_sha256(config_sha256) for config_sha256 in inventory)
            for inventory in inventories
        ):
            raise ValueError("trial config inventories must be non-empty ordered unique SHA-256 tuples")
        if len(self.final_trial_config_sha256s) != self.research_protocol.max_trials:
            raise ValueError("final trial inventory must exactly equal the preregistered trial budget")
        selection = self.research_protocol.window(self.final_trial_window_name)
        nested_window = self.research_protocol.window(self.nested_evaluation_window_name)
        holdout = self.research_protocol.window(self.terminal_holdout_window_name)
        if selection.role is not DataRole.SELECTION:
            raise ValueError("final trial inventory must use a selection data window")
        if holdout.role is not DataRole.BLIND:
            raise ValueError("terminal promotion evidence must use a blind data window")
        if nested_window.role is not DataRole.ROBUSTNESS:
            raise ValueError("nested evaluation must use a robustness data window")
        required_purge_days = math.ceil(self.research_protocol.purge_ms / _DAY_MS)
        if self.nested_folds.purge_days < required_purge_days:
            raise ValueError("nested fold purge is shorter than the registered outcome horizon")
        first = self.nested_folds.expected_boundaries(0)
        last = self.nested_folds.expected_boundaries(self.nested_folds.fold_count - 1)
        if selection.end + timedelta(days=self.nested_folds.purge_days) > nested_window.start:
            raise ValueError("final trial window must precede the registered nested evaluation window")
        if first[0] < nested_window.start or last[3] > nested_window.end:
            raise ValueError("absolute nested schedule must stay inside its registered robustness window")
        if last[3] + timedelta(days=self.nested_folds.purge_days) > holdout.start:
            raise ValueError("blind holdout must follow the complete predeclared nested schedule")
        object.__setattr__(
            self,
            "preregistration_sha256",
            _canonical_sha256(
                {
                    "final_trial_window": self.final_trial_window_name,
                    "final_trial_config_sha256s": self.final_trial_config_sha256s,
                    "nested_fold_protocol_sha256": self.nested_folds.fingerprint,
                    "nested_evaluation_window": self.nested_evaluation_window_name,
                    "nested_trial_config_sha256s": self.nested_trial_config_sha256s,
                    "research_preregistration_sha256": (self.research_protocol.preregistration_fingerprint()),
                    "schema": 1,
                    "terminal_holdout_window": self.terminal_holdout_window_name,
                }
            ),
        )

    @property
    def terminal_holdout_window(self) -> DataWindow:
        return self.research_protocol.window(self.terminal_holdout_window_name)

    @property
    def final_trial_window(self) -> DataWindow:
        return self.research_protocol.window(self.final_trial_window_name)


@dataclass(frozen=True, slots=True)
class CandidateFreeze:
    """Post-selection identity over the sealed registry and exact provenance."""

    experiment_plan_sha256: str
    registry_line_count: int
    registry_head_hash: str
    registry_terminal_sha256: str
    outcome: RegistryOutcome
    selected_trial_id: int | None
    selected_trial_record_hash: str | None
    selected_config_sha256: str | None
    candidate_commit: str
    code_sha256: str
    data_sha256: str
    dependency_sha256: str
    container_sha256: str
    frozen_at: datetime
    candidate_freeze_sha256: str = field(init=False)

    def __post_init__(self) -> None:
        hashes = (
            self.experiment_plan_sha256,
            self.registry_head_hash,
            self.registry_terminal_sha256,
            self.code_sha256,
            self.data_sha256,
            self.dependency_sha256,
            self.container_sha256,
        )
        if any(not _is_sha256(value) for value in hashes):
            raise ValueError("candidate freeze fingerprints must be lowercase SHA-256 values")
        if not _is_git_sha(self.candidate_commit):
            raise ValueError("candidate freeze commit must be a full lowercase Git SHA")
        if (
            isinstance(self.registry_line_count, bool)
            or not isinstance(self.registry_line_count, int)
            or self.registry_line_count < 1
        ):
            raise ValueError("candidate freeze registry line count must be positive")
        if not isinstance(self.outcome, RegistryOutcome):
            raise TypeError("candidate freeze outcome must be RegistryOutcome")
        if (
            not isinstance(self.frozen_at, datetime)
            or self.frozen_at.tzinfo is None
            or self.frozen_at.utcoffset() != timedelta(0)
        ):
            raise ValueError("candidate freeze timestamp must be expressed in UTC")
        selected_values = (
            self.selected_trial_id,
            self.selected_trial_record_hash,
            self.selected_config_sha256,
        )
        if self.outcome is RegistryOutcome.SELECTION:
            if any(value is None for value in selected_values):
                raise ValueError("selected candidate freeze requires complete selected-trial identity")
            if (
                isinstance(self.selected_trial_id, bool)
                or not isinstance(self.selected_trial_id, int)
                or self.selected_trial_id < 1
                or not _is_sha256(self.selected_trial_record_hash)
                or not _is_sha256(self.selected_config_sha256)
            ):
                raise ValueError("selected candidate freeze identity is invalid")
        elif any(value is not None for value in selected_values):
            raise ValueError("REJECT_ALL candidate freeze cannot identify a selected trial")
        object.__setattr__(
            self,
            "candidate_freeze_sha256",
            _canonical_sha256(
                {
                    "code_sha256": self.code_sha256,
                    "candidate_commit": self.candidate_commit,
                    "container_sha256": self.container_sha256,
                    "data_sha256": self.data_sha256,
                    "dependency_sha256": self.dependency_sha256,
                    "experiment_plan_sha256": self.experiment_plan_sha256,
                    "frozen_at": self.frozen_at.isoformat(),
                    "outcome": self.outcome.value,
                    "registry_head_hash": self.registry_head_hash,
                    "registry_line_count": self.registry_line_count,
                    "registry_terminal_sha256": self.registry_terminal_sha256,
                    "schema": 1,
                    "selected_config_sha256": self.selected_config_sha256,
                    "selected_trial_id": self.selected_trial_id,
                    "selected_trial_record_hash": self.selected_trial_record_hash,
                }
            ),
        )

    @classmethod
    def capture_selection(
        cls,
        experiment_plan: ExperimentPlan,
        registry: FrozenTrialRegistryEvidence,
        *,
        candidate_commit: str,
        frozen_at: datetime,
    ) -> CandidateFreeze:
        """Derive a selected-candidate freeze from verified registry evidence."""

        if not isinstance(experiment_plan, ExperimentPlan):
            raise TypeError("experiment_plan must be ExperimentPlan")
        if not isinstance(registry, FrozenTrialRegistryEvidence):
            raise TypeError("registry must be FrozenTrialRegistryEvidence")
        snapshot = registry.verify()
        selection = registry.final_selection
        if selection is None:
            raise ValueError("selected candidate freeze requires a selection registry")
        selected = registry.selected_trial
        if selected.fingerprints.protocol_sha256 != experiment_plan.preregistration_sha256:
            raise ValueError("selected trial does not bind the experiment plan")
        shared = ("code_sha256", "data_sha256", "dependency_sha256", "container_sha256")
        if any(
            trial.fingerprints.protocol_sha256 != experiment_plan.preregistration_sha256
            or any(
                getattr(trial.fingerprints, name) != getattr(selected.fingerprints, name) for name in shared
            )
            for trial in snapshot.trials
        ):
            raise ValueError("selection registry does not share one plan and provenance")
        if tuple(trial.fingerprints.config_sha256 for trial in snapshot.trials) != (
            experiment_plan.final_trial_config_sha256s
        ):
            raise ValueError("selection registry does not match the exact predeclared trial inventory")
        expected_selected = _rule_selected_trial(snapshot, experiment_plan.nested_folds)
        if expected_selected is None or expected_selected.trial_id != selected.trial_id:
            raise ValueError("final selection does not match the predeclared selection rule")
        return cls(
            experiment_plan_sha256=experiment_plan.preregistration_sha256,
            registry_line_count=registry.sealed_anchor.line_count,
            registry_head_hash=snapshot.head_hash,
            registry_terminal_sha256=selection.candidate_sha256,
            outcome=RegistryOutcome.SELECTION,
            selected_trial_id=selected.trial_id,
            selected_trial_record_hash=selected.record_hash,
            selected_config_sha256=selected.fingerprints.config_sha256,
            candidate_commit=candidate_commit,
            code_sha256=selected.fingerprints.code_sha256,
            data_sha256=selected.fingerprints.data_sha256,
            dependency_sha256=selected.fingerprints.dependency_sha256,
            container_sha256=selected.fingerprints.container_sha256,
            frozen_at=frozen_at,
        )

    @classmethod
    def capture_reject_all(
        cls,
        experiment_plan: ExperimentPlan,
        registry: FrozenTrialRegistryEvidence,
        *,
        candidate_commit: str,
        code_sha256: str,
        data_sha256: str,
        dependency_sha256: str,
        container_sha256: str,
        frozen_at: datetime,
    ) -> CandidateFreeze:
        """Derive a no-candidate freeze without manufacturing a winner."""

        if not isinstance(experiment_plan, ExperimentPlan):
            raise TypeError("experiment_plan must be ExperimentPlan")
        if not isinstance(registry, FrozenTrialRegistryEvidence):
            raise TypeError("registry must be FrozenTrialRegistryEvidence")
        snapshot = registry.verify()
        rejection = registry.final_rejection
        if rejection is None or registry.outcome is not RegistryOutcome.REJECT_ALL:
            raise ValueError("REJECT_ALL candidate freeze requires a rejection registry")
        provenance = {
            "code_sha256": code_sha256,
            "data_sha256": data_sha256,
            "dependency_sha256": dependency_sha256,
            "container_sha256": container_sha256,
        }
        for trial in snapshot.trials:
            if trial.fingerprints.protocol_sha256 != experiment_plan.preregistration_sha256:
                raise ValueError("rejected trial does not bind the experiment plan")
            if any(getattr(trial.fingerprints, name) != value for name, value in provenance.items()):
                raise ValueError("rejected trials do not share the frozen provenance")
        if tuple(trial.fingerprints.config_sha256 for trial in snapshot.trials) != (
            experiment_plan.final_trial_config_sha256s
        ):
            raise ValueError("rejection registry does not match the exact predeclared trial inventory")
        if _rule_selected_trial(snapshot, experiment_plan.nested_folds) is not None:
            raise ValueError("REJECT_ALL conflicts with the predeclared selection rule")
        return cls(
            experiment_plan_sha256=experiment_plan.preregistration_sha256,
            registry_line_count=registry.sealed_anchor.line_count,
            registry_head_hash=snapshot.head_hash,
            registry_terminal_sha256=rejection.rejection_sha256,
            outcome=RegistryOutcome.REJECT_ALL,
            selected_trial_id=None,
            selected_trial_record_hash=None,
            selected_config_sha256=None,
            candidate_commit=candidate_commit,
            code_sha256=code_sha256,
            data_sha256=data_sha256,
            dependency_sha256=dependency_sha256,
            container_sha256=container_sha256,
            frozen_at=frozen_at,
        )


@dataclass(frozen=True, slots=True)
class PromotionProtocolEvidence:
    """Post-selection protocol state linked to its causal experiment plan."""

    experiment_plan: ExperimentPlan
    research_protocol: ResearchProtocol
    candidate_freeze: CandidateFreeze
    blind_authorized_at: datetime
    evidence_sha256: str = field(init=False)

    def __post_init__(self) -> None:
        if not isinstance(self.experiment_plan, ExperimentPlan):
            raise TypeError("experiment_plan must be ExperimentPlan")
        if not isinstance(self.research_protocol, ResearchProtocol):
            raise TypeError("research_protocol must be ResearchProtocol")
        if not isinstance(self.candidate_freeze, CandidateFreeze):
            raise TypeError("candidate_freeze must be CandidateFreeze")
        if not self.research_protocol.is_frozen:
            raise ValueError("promotion requires a frozen research protocol")
        if (
            self.research_protocol.preregistration_fingerprint()
            != self.experiment_plan.research_protocol.preregistration_fingerprint()
        ):
            raise ValueError("frozen research protocol does not match the experiment plan")
        if self.candidate_freeze.outcome is not RegistryOutcome.SELECTION:
            raise ValueError("promotion protocol requires a selected candidate")
        if self.candidate_freeze.experiment_plan_sha256 != self.experiment_plan.preregistration_sha256:
            raise ValueError("candidate freeze does not bind the experiment plan")
        if self.research_protocol.parameter_set_sha256 != self.candidate_freeze.selected_config_sha256:
            raise ValueError("frozen parameter set does not match the selected candidate")
        if self.research_protocol.candidate_commit != self.candidate_freeze.candidate_commit:
            raise ValueError("frozen candidate commit does not match the candidate freeze")
        if self.research_protocol.frozen_at != self.candidate_freeze.frozen_at:
            raise ValueError("research protocol and candidate freeze timestamps differ")
        final_trial_window = self.research_protocol.assert_access(
            self.experiment_plan.final_trial_window_name,
            ResearchPurpose.SELECT,
        )
        if final_trial_window.role is not DataRole.SELECTION:
            raise ValueError("final trial inventory must use a selection data window")
        window = self.research_protocol.assert_access(
            self.experiment_plan.terminal_holdout_window_name,
            ResearchPurpose.PROMOTE,
            blind_authorized_at=self.blind_authorized_at,
        )
        if window.role is not DataRole.BLIND:
            raise ValueError("terminal promotion evidence must use a blind data window")
        object.__setattr__(
            self,
            "evidence_sha256",
            _canonical_sha256(
                {
                    "blind_authorized_at": self.blind_authorized_at.isoformat(),
                    "candidate_freeze_sha256": self.candidate_freeze.candidate_freeze_sha256,
                    "experiment_plan_sha256": self.experiment_plan.preregistration_sha256,
                    "research_protocol_sha256": self.research_protocol.fingerprint(),
                    "schema": 1,
                }
            ),
        )

    @property
    def protocol_sha256(self) -> str:
        """Backward-compatible name for the pre-selection plan identifier."""

        return self.experiment_plan.preregistration_sha256

    @property
    def preregistration_sha256(self) -> str:
        return self.experiment_plan.preregistration_sha256

    @property
    def nested_folds(self) -> NestedFoldProtocol:
        return self.experiment_plan.nested_folds

    @property
    def terminal_holdout_window(self) -> DataWindow:
        return self.research_protocol.window(self.experiment_plan.terminal_holdout_window_name)

    @property
    def final_trial_window(self) -> DataWindow:
        return self.research_protocol.window(self.experiment_plan.final_trial_window_name)


@dataclass(frozen=True, slots=True)
class NestedOOSFoldEvidence:
    """One claimed outer test fold selected by its own sealed inner registry.

    Local immutable evidence can validate layout and content, but cannot prove
    that selection was actually sealed before the test data became observable.
    Promotion therefore remains blocked until an external signed pre-test
    attestation verifier is integrated.
    """

    fold_id: str
    train_start: date
    train_end_exclusive: date
    test_start: date
    test_end_exclusive: date
    test_returns: DailyReturnSeries
    test_closed_trades: int
    selection_registry: FrozenTrialRegistryEvidence
    selection_frozen_at: datetime
    candidate_sha256: str
    fold_protocol_sha256: str

    def __post_init__(self) -> None:
        if not isinstance(self.fold_id, str) or not self.fold_id or self.fold_id != self.fold_id.strip():
            raise ValueError("nested fold identifier must be normalized")
        boundaries = (
            self.train_start,
            self.train_end_exclusive,
            self.test_start,
            self.test_end_exclusive,
        )
        if any(type(value) is not date for value in boundaries):
            raise TypeError("nested train/test boundaries must be dates")
        if not self.train_start < self.train_end_exclusive <= self.test_start < self.test_end_exclusive:
            raise ValueError("nested fold train/test boundaries are invalid")
        if not isinstance(self.test_returns, DailyReturnSeries):
            raise TypeError("nested test returns must be DailyReturnSeries")
        if self.test_returns.dates != _date_range(self.test_start, self.test_end_exclusive):
            raise ValueError("nested test returns must exactly cover their declared test window")
        if (
            isinstance(self.test_closed_trades, bool)
            or not isinstance(self.test_closed_trades, int)
            or self.test_closed_trades < 0
        ):
            raise ValueError("nested test trade count must be a non-negative integer")
        if not isinstance(self.selection_registry, FrozenTrialRegistryEvidence):
            raise TypeError("nested fold selection must be a frozen trial registry")
        if (
            not isinstance(self.selection_frozen_at, datetime)
            or self.selection_frozen_at.tzinfo is None
            or self.selection_frozen_at.utcoffset() != timedelta(0)
        ):
            raise ValueError("nested fold selection time must be expressed in UTC")
        if self.selection_frozen_at.date() < self.train_end_exclusive:
            raise ValueError("nested fold selection cannot predate the completed training window")
        if self.selection_frozen_at.date() >= self.test_start:
            raise ValueError("nested fold selection must be frozen before its test window")
        if not _is_sha256(self.candidate_sha256) or not _is_sha256(self.fold_protocol_sha256):
            raise ValueError("nested fold fingerprints must be lowercase SHA-256 values")

    @property
    def train_days(self) -> int:
        return (self.train_end_exclusive - self.train_start).days

    @property
    def test_days(self) -> int:
        return (self.test_end_exclusive - self.test_start).days

    @property
    def profitable(self) -> bool:
        return _log_growth(self.test_returns) > 0


@dataclass(frozen=True, slots=True)
class FrozenTrialRegistryEvidence:
    """A verified frozen registry snapshot that is rechecked at decision time."""

    registry_path: Path
    snapshot: RegistrySnapshot
    sealed_anchor: SealedRegistryAnchor
    final_selection: SelectionRecord | None
    final_rejection: RejectionRecord | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.registry_path, Path) or not self.registry_path.is_absolute():
            raise ValueError("registry_path must be an absolute pathlib.Path")
        if not isinstance(self.snapshot, RegistrySnapshot):
            raise TypeError("snapshot must be RegistrySnapshot")
        if not isinstance(self.sealed_anchor, SealedRegistryAnchor):
            raise TypeError("sealed_anchor must be SealedRegistryAnchor")
        if self.final_selection is not None and not isinstance(self.final_selection, SelectionRecord):
            raise TypeError("final_selection must be SelectionRecord or None")
        if self.final_rejection is not None and not isinstance(self.final_rejection, RejectionRecord):
            raise TypeError("final_rejection must be RejectionRecord or None")
        if (
            not self.snapshot.frozen
            or self.snapshot.selection != self.final_selection
            or self.snapshot.rejection != self.final_rejection
            or self.snapshot.sealed_anchor != self.sealed_anchor
            or self.sealed_anchor.head_hash != self.snapshot.head_hash
            or self.sealed_anchor.outcome != self.snapshot.outcome
            or self.sealed_anchor.terminal_sha256
            != (
                self.final_selection.candidate_sha256
                if self.final_selection is not None
                else self.final_rejection.rejection_sha256
                if self.final_rejection is not None
                else None
            )
        ):
            raise ValueError("registry snapshot, terminal outcome and sealed anchor are inconsistent")
        self.verify()

    @classmethod
    def capture(cls, registry: TrialRegistry) -> FrozenTrialRegistryEvidence:
        if not isinstance(registry, TrialRegistry):
            raise TypeError("registry must be TrialRegistry")
        snapshot = registry.read()
        if snapshot.terminal_record is None or snapshot.sealed_anchor is None:
            raise ValueError("registry must have a sealed terminal outcome")
        return cls(
            registry.path.resolve(),
            snapshot,
            snapshot.sealed_anchor,
            snapshot.selection,
            snapshot.rejection,
        )

    def verify(self) -> RegistrySnapshot:
        current = TrialRegistry(self.registry_path).read()
        if current != self.snapshot:
            raise ValueError("frozen registry no longer matches its captured complete snapshot")
        return current

    @property
    def candidate_sha256(self) -> str:
        if self.final_selection is None:
            raise ValueError("REJECT_ALL registry has no candidate")
        return self.final_selection.candidate_sha256

    @property
    def selected_trial(self) -> TrialRecord:
        if self.final_selection is None:
            raise ValueError("REJECT_ALL registry has no selected trial")
        return self.snapshot.trials[self.final_selection.selected_trial_id - 1]

    @property
    def outcome(self) -> RegistryOutcome:
        outcome = self.snapshot.outcome
        if outcome is None:  # pragma: no cover - guarded by __post_init__
            raise ValueError("frozen registry has no terminal outcome")
        return outcome


@dataclass(frozen=True, slots=True)
class InputIntentInventory:
    """Complete input candidates supplied to one scenario before admission/fills."""

    intents: tuple[SleeveIntent, ...]
    fingerprint: str = field(init=False)

    def __post_init__(self) -> None:
        if not isinstance(self.intents, tuple) or not self.intents:
            raise ValueError("input intent inventory must be a non-empty immutable tuple")
        if any(not isinstance(intent, SleeveIntent) for intent in self.intents):
            raise TypeError("input intent inventory must contain SleeveIntent values")
        identifiers = tuple(intent.intent_id for intent in self.intents)
        if len(set(identifiers)) != len(identifiers):
            raise ValueError("input intent inventory identifiers must be unique")
        if identifiers != tuple(sorted(identifiers)):
            raise ValueError("input intent inventory must be canonically ordered by intent_id")
        object.__setattr__(
            self,
            "fingerprint",
            _canonical_sha256({"intent_ids": identifiers, "schema": 1}),
        )


@dataclass(frozen=True, slots=True)
class ScenarioRunFingerprint:
    data_sha256: str
    code_sha256: str
    candidate_sha256: str
    input_intent_inventory_sha256: str
    execution_config_sha256: str
    cost_config_sha256: str
    output_artifact_sha256: str

    def __post_init__(self) -> None:
        if any(not _is_sha256(value) for value in asdict(self).values()):
            raise ValueError("scenario run fingerprints must be lowercase SHA-256 values")


@dataclass(frozen=True, slots=True)
class TerminalHoldoutEvidence:
    """Baseline and adverse rerun of one terminal, post-freeze blind window."""

    baseline_cells: tuple[CellEquityCurve, ...]
    stress_cells: tuple[CellEquityCurve, ...]
    baseline_input_intents: InputIntentInventory
    stress_input_intents: InputIntentInventory
    baseline_run: ScenarioRunFingerprint
    stress_run: ScenarioRunFingerprint

    def __post_init__(self) -> None:
        for name, cells in (("baseline", self.baseline_cells), ("stress", self.stress_cells)):
            if not isinstance(cells, tuple) or not cells:
                raise ValueError(f"{name} cells must be a non-empty immutable tuple")
            if any(not isinstance(cell, CellEquityCurve) for cell in cells):
                raise TypeError(f"{name} cells must contain CellEquityCurve values")
        if not isinstance(self.baseline_input_intents, InputIntentInventory) or not isinstance(
            self.stress_input_intents, InputIntentInventory
        ):
            raise TypeError("scenario input inventories must be InputIntentInventory")
        if not isinstance(self.baseline_run, ScenarioRunFingerprint) or not isinstance(
            self.stress_run, ScenarioRunFingerprint
        ):
            raise TypeError("scenario fingerprints must be ScenarioRunFingerprint")
        if self.baseline_input_intents != self.stress_input_intents:
            raise ValueError("baseline and stress must receive the exact same input intent inventory")
        if (
            self.baseline_run.input_intent_inventory_sha256 != self.baseline_input_intents.fingerprint
            or self.stress_run.input_intent_inventory_sha256 != self.stress_input_intents.fingerprint
        ):
            raise ValueError("scenario fingerprint does not match its complete input inventory")
        common = ("data_sha256", "code_sha256", "candidate_sha256", "input_intent_inventory_sha256")
        if any(getattr(self.baseline_run, name) != getattr(self.stress_run, name) for name in common):
            raise ValueError("baseline and stress common provenance fingerprints must match")
        if self.baseline_run.execution_config_sha256 == self.stress_run.execution_config_sha256:
            raise ValueError("stress must use a distinct execution configuration")
        if self.baseline_run.cost_config_sha256 == self.stress_run.cost_config_sha256:
            raise ValueError("stress must use a distinct cost configuration")
        if self.baseline_cells == self.stress_cells:
            raise ValueError("stress evidence must be a genuinely distinct adverse rerun")
        if self.baseline_run.output_artifact_sha256 != _cells_artifact_sha256(
            self.baseline_cells
        ) or self.stress_run.output_artifact_sha256 != _cells_artifact_sha256(self.stress_cells):
            raise ValueError("scenario output artifact fingerprint does not match its cells/trades")

        baseline = synchronize_cells(self.baseline_cells)
        stress = synchronize_cells(self.stress_cells)
        baseline_map = {cell.cell_id: cell for cell in self.baseline_cells}
        stress_map = {cell.cell_id: cell for cell in self.stress_cells}
        if set(baseline_map) != set(stress_map):
            raise ValueError("baseline and stress must contain the same enabled cell inventory")
        if baseline.dates != stress.dates:
            raise ValueError("baseline and stress must share one complete terminal horizon")
        for cell_id in sorted(baseline_map):
            baseline_cell, stress_cell = baseline_map[cell_id], stress_map[cell_id]
            if (
                baseline_cell.sleeve_id != stress_cell.sleeve_id
                or baseline_cell.symbol != stress_cell.symbol
                or baseline_cell.dates != stress_cell.dates
                or baseline_cell.initial_equity_usd != stress_cell.initial_equity_usd
            ):
                raise ValueError("baseline and stress cell identity/capital must match")

        inventory_pairs = {
            (intent.sleeve_id, intent.symbol) for intent in self.baseline_input_intents.intents
        }
        cell_pairs = {(cell.sleeve_id, cell.symbol) for cell in self.baseline_cells}
        if inventory_pairs != cell_pairs:
            raise ValueError("input intent inventory must cover exactly the enabled sleeve-symbol cells")
        allowed_ids = {intent.intent_id for intent in self.baseline_input_intents.intents}
        for name, cells in (("baseline", self.baseline_cells), ("stress", self.stress_cells)):
            executed_ids = tuple(trade.intent.intent_id for cell in cells for trade in cell.trades)
            if len(set(executed_ids)) != len(executed_ids):
                raise ValueError(f"{name} cannot execute one input intent more than once")
            if not set(executed_ids).issubset(allowed_ids):
                raise ValueError(f"{name} executed an intent absent from the input inventory")


@dataclass(frozen=True, slots=True)
class ParameterPlateauEvidence:
    """Local plateau diagnostics.

    Baseline growth is registry-derived below. Stress growth and profit factor
    remain non-authorizing until their per-trial artifacts are externally
    sealed and independently recomputed.
    """

    selected: ParameterOutcome
    neighbors: tuple[ParameterOutcome, ...]
    selected_on_boundary: bool = False
    policy: ParameterPlateauPolicy = field(default_factory=ParameterPlateauPolicy)
    report: ParameterPlateauReport = field(init=False)

    def __post_init__(self) -> None:
        if not isinstance(self.selected, ParameterOutcome):
            raise TypeError("plateau selected outcome must be ParameterOutcome")
        if not isinstance(self.neighbors, tuple) or any(
            not isinstance(item, ParameterOutcome) for item in self.neighbors
        ):
            raise TypeError("plateau neighbors must be ParameterOutcome values")
        if not isinstance(self.selected_on_boundary, bool):
            raise TypeError("plateau boundary flag must be boolean")
        if not isinstance(self.policy, ParameterPlateauPolicy):
            raise TypeError("plateau policy must be ParameterPlateauPolicy")
        object.__setattr__(
            self,
            "report",
            parameter_plateau_report(
                self.selected,
                self.neighbors,
                selected_on_boundary=self.selected_on_boundary,
                policy=self.policy,
            ),
        )


@dataclass(frozen=True, slots=True)
class PromotionEvidenceV2:
    registry: FrozenTrialRegistryEvidence
    protocol: PromotionProtocolEvidence
    trial_matrix: SynchronousTrialMatrix
    nested_oos_folds: tuple[NestedOOSFoldEvidence, ...]
    parameter_plateau: ParameterPlateauEvidence
    terminal_holdout: TerminalHoldoutEvidence
    evidence_sha256: str = field(init=False)

    def __post_init__(self) -> None:
        if not isinstance(self.registry, FrozenTrialRegistryEvidence):
            raise TypeError("registry must be FrozenTrialRegistryEvidence")
        if not isinstance(self.protocol, PromotionProtocolEvidence):
            raise TypeError("protocol must be PromotionProtocolEvidence")
        if not isinstance(self.trial_matrix, SynchronousTrialMatrix):
            raise TypeError("trial_matrix must be SynchronousTrialMatrix")
        if not isinstance(self.nested_oos_folds, tuple) or any(
            not isinstance(fold, NestedOOSFoldEvidence) for fold in self.nested_oos_folds
        ):
            raise TypeError("nested_oos_folds must be an immutable tuple of NestedOOSFoldEvidence")
        if not isinstance(self.parameter_plateau, ParameterPlateauEvidence):
            raise TypeError("parameter_plateau must be ParameterPlateauEvidence")
        if not isinstance(self.terminal_holdout, TerminalHoldoutEvidence):
            raise TypeError("terminal_holdout must be TerminalHoldoutEvidence")
        fold_protocol = self.protocol.nested_folds
        if self.trial_matrix.observations % fold_protocol.cscv_blocks:
            raise ValueError("cscv_blocks must evenly divide observations and be even within [4, 20]")

        snapshot = self.registry.verify()
        if self.registry.outcome is not RegistryOutcome.SELECTION:
            raise ValueError("promotion evidence requires a selected candidate, not REJECT_ALL")
        if not snapshot.trials:
            raise ValueError("promotion requires a non-empty frozen trial inventory")
        if any(trial.status is not TrialStatus.SUCCESS for trial in snapshot.trials):
            raise ValueError("promotion rejects registries containing failed trials")
        if any(trial.daily_returns is None for trial in snapshot.trials):
            raise ValueError("every registered trial must contain return evidence")
        expected_ids = tuple(_trial_key(trial.trial_id) for trial in snapshot.trials)
        expected_series = tuple(trial.daily_returns for trial in snapshot.trials)
        if self.trial_matrix.trial_ids != expected_ids or self.trial_matrix.series != expected_series:
            raise ValueError("trial matrix must equal the complete frozen registry inventory in order")
        selection_window = self.protocol.final_trial_window
        expected_selection_dates = _date_range(selection_window.start, selection_window.end)
        if any(series.dates != expected_selection_dates for series in self.trial_matrix.series):
            raise ValueError("trial matrix must exactly cover the registered final selection window")
        if len(snapshot.trials) > self.protocol.research_protocol.max_trials:
            raise ValueError("frozen registry exceeds the preregistered trial budget")
        if tuple(trial.fingerprints.config_sha256 for trial in snapshot.trials) != (
            self.protocol.experiment_plan.final_trial_config_sha256s
        ):
            raise ValueError("frozen registry differs from the exact predeclared trial inventory")
        if any(
            trial.fingerprints.protocol_sha256 != self.protocol.experiment_plan.preregistration_sha256
            for trial in snapshot.trials
        ):
            raise ValueError("every registered trial must bind to the pre-selection experiment plan")
        selected = self.registry.selected_trial
        expected_selected = _rule_selected_trial(snapshot, fold_protocol)
        if expected_selected is None or selected.trial_id != expected_selected.trial_id:
            raise ValueError("final selection does not match the predeclared selection rule")
        candidate_freeze = self.protocol.candidate_freeze
        if (
            candidate_freeze.registry_line_count != self.registry.sealed_anchor.line_count
            or candidate_freeze.registry_head_hash != snapshot.head_hash
            or candidate_freeze.registry_terminal_sha256 != self.registry.candidate_sha256
            or candidate_freeze.selected_trial_id != selected.trial_id
            or candidate_freeze.selected_trial_record_hash != selected.record_hash
        ):
            raise ValueError("candidate freeze does not match the exact sealed registry selection")
        common_fingerprints = ("code_sha256", "data_sha256", "dependency_sha256", "container_sha256")
        if any(
            getattr(trial.fingerprints, name) != getattr(selected.fingerprints, name)
            for trial in snapshot.trials
            for name in common_fingerprints
        ):
            raise ValueError("registered trials must share code, data, dependency and container provenance")
        if selected.fingerprints.config_sha256 != candidate_freeze.selected_config_sha256 or any(
            getattr(selected.fingerprints, name) != getattr(candidate_freeze, name)
            for name in common_fingerprints
        ):
            raise ValueError("candidate freeze does not match selected trial provenance")
        if selected.fingerprints.config_sha256 != self.protocol.research_protocol.parameter_set_sha256:
            raise ValueError("final selection does not match the frozen parameter set")

        if len(self.nested_oos_folds) != fold_protocol.fold_count:
            raise ValueError("nested fold evidence must match the predeclared fold count")
        expected_fold_ids = tuple(
            f"nested-oos-{index:03d}" for index in range(1, fold_protocol.fold_count + 1)
        )
        if tuple(fold.fold_id for fold in self.nested_oos_folds) != expected_fold_ids:
            raise ValueError("nested fold identifiers must use canonical chronological numbering")
        fold_registry_paths: set[Path] = set()
        fold_registry_heads: set[str] = set()
        fold_training_data_hashes: set[str] = set()
        for index, fold in enumerate(self.nested_oos_folds):
            if (
                fold.train_start,
                fold.train_end_exclusive,
                fold.test_start,
                fold.test_end_exclusive,
            ) != fold_protocol.expected_boundaries(index):
                raise ValueError("nested fold boundaries differ from the absolute predeclared schedule")
            fold_snapshot = fold.selection_registry.verify()
            if fold.candidate_sha256 != fold.selection_registry.candidate_sha256:
                raise ValueError("nested test candidate must match its fold-specific final selection")
            if fold.fold_protocol_sha256 != fold_protocol.fingerprint:
                raise ValueError("nested folds must bind to the predeclared fold protocol")
            if (
                fold.selection_registry.registry_path in fold_registry_paths
                or fold_snapshot.head_hash in fold_registry_heads
            ):
                raise ValueError("each nested fold requires a distinct sealed inner selection registry")
            fold_registry_paths.add(fold.selection_registry.registry_path)
            fold_registry_heads.add(fold_snapshot.head_hash)
            if not fold_snapshot.trials or any(
                trial.status is not TrialStatus.SUCCESS or trial.daily_returns is None
                for trial in fold_snapshot.trials
            ):
                raise ValueError("nested fold selection registry requires only complete successful trials")
            fold_selected = fold.selection_registry.selected_trial
            if tuple(trial.fingerprints.config_sha256 for trial in fold_snapshot.trials) != (
                self.protocol.experiment_plan.nested_trial_config_sha256s
            ):
                raise ValueError("nested registry differs from its exact predeclared trial inventory")
            expected_fold_selection = _rule_selected_trial(fold_snapshot, fold_protocol)
            if expected_fold_selection is None or fold_selected.trial_id != expected_fold_selection.trial_id:
                raise ValueError("nested selection does not match the predeclared selection rule")
            fold_common = ("code_sha256", "data_sha256", "dependency_sha256", "container_sha256")
            if any(
                getattr(trial.fingerprints, name) != getattr(fold_selected.fingerprints, name)
                for trial in fold_snapshot.trials
                for name in fold_common
            ):
                raise ValueError("nested fold trials must share one exact training provenance")
            fold_data_sha256 = fold_selected.fingerprints.data_sha256
            if fold_data_sha256 in fold_training_data_hashes:
                raise ValueError("each expanding nested training window requires a distinct data fingerprint")
            fold_training_data_hashes.add(fold_data_sha256)
            expected_train_dates = _date_range(fold.train_start, fold.train_end_exclusive)
            if any(
                trial.daily_returns is None
                or trial.daily_returns.dates != expected_train_dates
                or trial.fingerprints.protocol_sha256 != self.protocol.experiment_plan.preregistration_sha256
                for trial in fold_snapshot.trials
            ):
                raise ValueError("nested fold selection trials must use only its completed training window")
            if fold.test_days != fold_protocol.test_days:
                raise ValueError("nested test folds must have the predeclared equal duration")
            if fold.train_days < fold_protocol.minimum_train_days:
                raise ValueError("nested fold has less training history than predeclared")
            if fold.test_closed_trades < fold_protocol.minimum_test_trades:
                raise ValueError("nested fold has fewer trades than predeclared")
            if fold.train_end_exclusive + timedelta(days=fold_protocol.purge_days) != fold.test_start:
                raise ValueError("nested fold must use the exact predeclared purge gap")
            if index:
                previous = self.nested_oos_folds[index - 1]
                if fold.test_start != previous.test_end_exclusive:
                    raise ValueError("nested test folds must be contiguous chronological blocks")
                if fold.train_start != previous.train_start:
                    raise ValueError("nested folds must use one deterministic expanding train origin")
        registered_selection_dates = set(self.trial_matrix.series[0].dates)
        nested_test_dates = {day for fold in self.nested_oos_folds for day in fold.test_returns.dates}
        if registered_selection_dates & nested_test_dates:
            raise ValueError("nested OOS test dates cannot be reused in the final trial inventory")
        if (
            self.nested_oos_folds
            and selection_window.end + timedelta(days=fold_protocol.purge_days)
            > self.nested_oos_folds[0].test_start
        ):
            raise ValueError("final trial selection window must precede nested tests and purge gap")

        holdout_window = self.protocol.terminal_holdout_window
        holdout_dates = _date_range(holdout_window.start, holdout_window.end)
        baseline_portfolio = synchronize_cells(self.terminal_holdout.baseline_cells)
        stress_portfolio = synchronize_cells(self.terminal_holdout.stress_cells)
        if baseline_portfolio.dates != holdout_dates or stress_portfolio.dates != holdout_dates:
            raise ValueError("terminal portfolios must exactly cover the registered blind window")
        if self.nested_oos_folds:
            required_gap = timedelta(days=fold_protocol.purge_days)
            if self.nested_oos_folds[-1].test_end_exclusive + required_gap > holdout_window.start:
                raise ValueError("terminal holdout must start after nested evidence and its purge gap")
        if (
            self.terminal_holdout.baseline_run.candidate_sha256 != candidate_freeze.candidate_freeze_sha256
            or self.terminal_holdout.baseline_run.code_sha256 != candidate_freeze.code_sha256
        ):
            raise ValueError("terminal run provenance does not match the frozen selected candidate")

        outcomes = (self.parameter_plateau.selected, *self.parameter_plateau.neighbors)
        if self.parameter_plateau.selected.parameter_id != self.selected_trial_id:
            raise ValueError("plateau selected parameter must equal the frozen final trial")
        if {outcome.parameter_id for outcome in outcomes} != set(expected_ids):
            raise ValueError("plateau outcomes must account for every registered trial exactly once")
        series_by_id = dict(zip(self.trial_matrix.trial_ids, self.trial_matrix.series, strict=True))
        if any(
            outcome.baseline_log_growth != _log_growth(series_by_id[outcome.parameter_id])
            for outcome in outcomes
        ):
            raise ValueError("plateau baseline growth must be recomputed from registered returns")

        object.__setattr__(
            self,
            "evidence_sha256",
            _canonical_sha256(
                {
                    "baseline_run": asdict(self.terminal_holdout.baseline_run),
                    "candidate_freeze_sha256": candidate_freeze.candidate_freeze_sha256,
                    "input_inventory_sha256": self.terminal_holdout.baseline_input_intents.fingerprint,
                    "nested_fold_protocol_sha256": fold_protocol.fingerprint,
                    "nested_folds": [
                        {
                            "candidate_sha256": fold.candidate_sha256,
                            "fold_id": fold.fold_id,
                            "selection_registry_head": fold.selection_registry.snapshot.head_hash,
                            "selection_frozen_at": fold.selection_frozen_at.isoformat(),
                            "test_closed_trades": fold.test_closed_trades,
                            "test_dates": [day.isoformat() for day in fold.test_returns.dates],
                            "test_returns": fold.test_returns.returns,
                            "train_end_exclusive": fold.train_end_exclusive.isoformat(),
                            "train_start": fold.train_start.isoformat(),
                            "training_data_sha256": (
                                fold.selection_registry.selected_trial.fingerprints.data_sha256
                            ),
                        }
                        for fold in self.nested_oos_folds
                    ],
                    "parameter_plateau": {
                        "neighbors": [asdict(item) for item in self.parameter_plateau.neighbors],
                        "policy": asdict(self.parameter_plateau.policy),
                        "report": asdict(self.parameter_plateau.report),
                        "selected": asdict(self.parameter_plateau.selected),
                        "selected_on_boundary": self.parameter_plateau.selected_on_boundary,
                    },
                    "experiment_plan_sha256": self.protocol.experiment_plan.preregistration_sha256,
                    "promotion_protocol_evidence_sha256": self.protocol.evidence_sha256,
                    "registry_head_hash": snapshot.head_hash,
                    "stress_run": asdict(self.terminal_holdout.stress_run),
                    "trial_ids": expected_ids,
                }
            ),
        )

    @property
    def selected_trial_id(self) -> str:
        return _trial_key(self.registry.selected_trial.trial_id)

    @property
    def cscv_blocks(self) -> int:
        """CSCV layout fixed by the pre-selection experiment plan."""

        return self.protocol.nested_folds.cscv_blocks


@dataclass(frozen=True, slots=True)
class ScenarioPerformance:
    days: int
    trades: int
    net_growth: float
    log_growth: float
    profit_factor: float | None
    hac_sharpe: float | None
    sortino: float | None
    calmar: float | None
    maximum_drawdown: float

    def __post_init__(self) -> None:
        if (
            isinstance(self.days, bool)
            or not isinstance(self.days, int)
            or self.days <= 0
            or isinstance(self.trades, bool)
            or not isinstance(self.trades, int)
            or self.trades < 0
        ):
            raise ValueError("scenario counts must be valid integers")
        if not _finite(self.net_growth) or self.net_growth <= -1 or not _finite(self.log_growth):
            raise ValueError("scenario growth must be finite")
        if self.log_growth != math.log1p(self.net_growth):
            raise ValueError("scenario simple and log growth are inconsistent")
        if not _finite(self.maximum_drawdown) or not 0 <= self.maximum_drawdown <= 1:
            raise ValueError("scenario drawdown must be finite within [0, 1]")
        for name, value in (
            ("profit_factor", self.profit_factor),
            ("hac_sharpe", self.hac_sharpe),
            ("sortino", self.sortino),
            ("calmar", self.calmar),
        ):
            if value is not None and not _finite(value):
                raise ValueError(f"scenario {name} must be finite when present")
        if self.profit_factor is not None and self.profit_factor < 0:
            raise ValueError("scenario profit factor cannot be negative")


@dataclass(frozen=True, slots=True)
class CellPerformance:
    cell_id: str
    sleeve_id: str
    symbol: str
    trades: int
    profit_factor: float | None
    expectancy_usd: float | None

    def __post_init__(self) -> None:
        identifiers = (self.cell_id, self.sleeve_id, self.symbol)
        if any(not isinstance(value, str) or not value or value != value.strip() for value in identifiers):
            raise ValueError("cell performance identifiers must be normalized")
        if self.symbol != self.symbol.upper():
            raise ValueError("cell performance symbol must be uppercase")
        _validate_trade_performance(self.trades, self.profit_factor, self.expectancy_usd)


@dataclass(frozen=True, slots=True)
class SleevePerformance:
    sleeve_id: str
    trades: int
    profit_factor: float | None
    expectancy_usd: float | None

    def __post_init__(self) -> None:
        if (
            not isinstance(self.sleeve_id, str)
            or not self.sleeve_id
            or self.sleeve_id != self.sleeve_id.strip()
        ):
            raise ValueError("sleeve performance identifier must be normalized")
        _validate_trade_performance(self.trades, self.profit_factor, self.expectancy_usd)


class PromotionTarget(StrEnum):
    SHADOW = "SHADOW"


_REPORT_FACTORY_TOKEN = object()


@dataclass(frozen=True, slots=True)
class PromotionReportV2:
    target: PromotionTarget | None
    reasons: tuple[str, ...]
    baseline: ScenarioPerformance
    stress: ScenarioPerformance
    profitable_nested_fold_fraction: float
    dsr: DeflatedSharpeResult | None
    cscv: CSCVResult | None
    baseline_cells: tuple[CellPerformance, ...]
    stress_cells: tuple[CellPerformance, ...]
    baseline_sleeves: tuple[SleevePerformance, ...]
    stress_sleeves: tuple[SleevePerformance, ...]
    parameter_plateau: ParameterPlateauReport
    registry_head_hash: str
    candidate_sha256: str
    evidence_sha256: str
    attestation_sha256: str
    _factory_token: object = field(repr=False, compare=False)

    def __post_init__(self) -> None:
        if self._factory_token is not _REPORT_FACTORY_TOKEN:
            raise ValueError("promotion reports can only be created by the verified evaluator")
        if self.target is not None and not isinstance(self.target, PromotionTarget):
            raise TypeError("promotion target must be SHADOW or None")
        if not isinstance(self.reasons, tuple) or any(
            not isinstance(reason, str) or not reason or reason != reason.strip() for reason in self.reasons
        ):
            raise ValueError("promotion reasons must be normalized immutable strings")
        if len(set(self.reasons)) != len(self.reasons):
            raise ValueError("promotion reasons must be unique")
        if self.target is PromotionTarget.SHADOW:
            raise ValueError("external signed evidence verifier is unavailable")
        if not self.reasons:
            raise ValueError("fail-closed promotion reports require rejection reasons")
        if any(
            not _is_sha256(value)
            for value in (
                self.registry_head_hash,
                self.candidate_sha256,
                self.evidence_sha256,
                self.attestation_sha256,
            )
        ):
            raise ValueError("promotion report fingerprints must be lowercase SHA-256 values")
        if not hmac.compare_digest(self.attestation_sha256, _report_attestation(self)):
            raise ValueError("promotion report attestation does not match its contents")

    @property
    def shadow_allowed(self) -> bool:
        """Local evidence is diagnostic only until external proof is verified."""

        return False

    @property
    def live_allowed(self) -> bool:
        """Offline evidence can never authorize real orders directly."""

        return False


def _validate_trade_performance(
    trades: int,
    profit_factor: float | None,
    expectancy_usd: float | None,
) -> None:
    if isinstance(trades, bool) or not isinstance(trades, int) or trades < 0:
        raise ValueError("performance trade count must be a non-negative integer")
    if profit_factor is not None and (not _finite(profit_factor) or profit_factor < 0):
        raise ValueError("performance profit factor must be finite and non-negative when present")
    if expectancy_usd is not None and not _finite(expectancy_usd):
        raise ValueError("performance expectancy must be finite when present")
    if trades == 0 and (profit_factor is not None or expectancy_usd is not None):
        raise ValueError("an empty performance sample cannot contain trade statistics")
    if trades > 0 and expectancy_usd is None:
        raise ValueError("a non-empty performance sample requires expectancy")


def _trade_statistics(trades: tuple[TradeRecord, ...]) -> tuple[float | None, float | None]:
    if not trades:
        return None, None
    pnl = tuple(trade.net_pnl_usd for trade in trades)
    if any(not math.isfinite(value) for value in pnl):
        raise ValueError("trade PnL evidence must be finite")
    gross_profit = sum(max(0.0, value) for value in pnl)
    gross_loss = -sum(min(0.0, value) for value in pnl)
    profit_factor = gross_profit / gross_loss if gross_loss > 0 else None
    expectancy = sum(pnl) / len(pnl)
    if profit_factor is not None and not math.isfinite(profit_factor):
        raise ValueError("profit factor must be finite")
    if not math.isfinite(expectancy):
        raise ValueError("expectancy must be finite")
    return profit_factor, expectancy


def _risk_adjusted_metrics(
    portfolio: PortfolioEvidence,
) -> tuple[float | None, float | None, float | None]:
    try:
        sharpe: float | None = hac_sharpe(portfolio.daily_returns)
    except ValueError:
        sharpe = None
    values = portfolio.daily_returns.returns
    average = fmean(values)
    downside = math.sqrt(fmean(min(value, 0.0) ** 2 for value in values))
    sortino = average / downside * math.sqrt(365) if downside > 0 else None
    log_growth = _log_growth(portfolio.daily_returns)
    try:
        annualized_growth = math.expm1(log_growth * 365 / len(values))
    except OverflowError:
        annualized_growth = math.inf
    calmar = annualized_growth / portfolio.maximum_drawdown if portfolio.maximum_drawdown > 0 else None
    outputs = tuple(value for value in (sharpe, sortino, calmar) if value is not None)
    if any(not math.isfinite(value) for value in outputs):
        raise ValueError("risk-adjusted portfolio metrics must be finite")
    return sharpe, sortino, calmar


def _scenario(cells: tuple[CellEquityCurve, ...]) -> ScenarioPerformance:
    portfolio = synchronize_cells(cells)
    trades = tuple(trade for cell in cells for trade in cell.trades)
    profit_factor, _ = _trade_statistics(trades)
    sharpe, sortino, calmar = _risk_adjusted_metrics(portfolio)
    return ScenarioPerformance(
        days=len(portfolio.dates),
        trades=portfolio.trades,
        net_growth=portfolio.total_return,
        log_growth=math.log1p(portfolio.total_return),
        profit_factor=profit_factor,
        hac_sharpe=sharpe,
        sortino=sortino,
        calmar=calmar,
        maximum_drawdown=portfolio.maximum_drawdown,
    )


def _cell_performance(cells: tuple[CellEquityCurve, ...]) -> tuple[CellPerformance, ...]:
    output: list[CellPerformance] = []
    for cell in sorted(cells, key=lambda item: item.cell_id):
        profit_factor, expectancy = _trade_statistics(cell.trades)
        output.append(
            CellPerformance(
                cell.cell_id,
                cell.sleeve_id,
                cell.symbol,
                len(cell.trades),
                profit_factor,
                expectancy,
            )
        )
    return tuple(output)


def _sleeve_performance(cells: tuple[CellEquityCurve, ...]) -> tuple[SleevePerformance, ...]:
    grouped: dict[str, list[TradeRecord]] = {}
    for cell in cells:
        grouped.setdefault(cell.sleeve_id, []).extend(cell.trades)
    output: list[SleevePerformance] = []
    for sleeve_id in sorted(grouped):
        trades = tuple(grouped[sleeve_id])
        profit_factor, expectancy = _trade_statistics(trades)
        output.append(SleevePerformance(sleeve_id, len(trades), profit_factor, expectancy))
    return tuple(output)


def _valid_dsr(result: DeflatedSharpeResult, matrix: SynchronousTrialMatrix, selected_id: str) -> bool:
    trial_sharpes = tuple(non_annualized_sharpe(series) for series in matrix.series)
    selected = matrix.selected(selected_id)
    observed = non_annualized_sharpe(selected)
    average = fmean(selected.returns)
    centered = tuple(value - average for value in selected.returns)
    second = fmean(value**2 for value in centered)
    skewness = fmean(value**3 for value in centered) / second**1.5
    kurtosis = fmean(value**4 for value in centered) / second**2
    variance = pvariance(trial_sharpes)
    expected_maximum = 0.0
    if matrix.trials > 1:
        normal = NormalDist()
        euler_mascheroni = 0.5772156649015329
        expected_standard_maximum = (1 - euler_mascheroni) * normal.inv_cdf(
            1 - 1 / matrix.trials
        ) + euler_mascheroni * normal.inv_cdf(1 - 1 / (matrix.trials * math.e))
        expected_maximum = math.sqrt(variance) * expected_standard_maximum
    denominator_squared = 1 - skewness * observed + (kurtosis - 1) / 4 * observed**2
    probability = NormalDist().cdf(
        (observed - expected_maximum) * math.sqrt(matrix.observations - 1) / math.sqrt(denominator_squared)
    )
    return (
        result.probability == probability
        and result.observed_sharpe == observed
        and result.expected_maximum_sharpe == expected_maximum
        and result.observations == matrix.observations
        and result.trials == matrix.trials
        and result.skewness == skewness
        and result.kurtosis == kurtosis
        and result.trial_sharpe_variance == variance
    )


def _valid_cscv(
    result: CSCVResult,
    *,
    blocks: int,
    performance_measure: PerformanceMeasure,
    trial_ids: tuple[str, ...],
) -> bool:
    expected_combinations = math.comb(blocks, blocks // 2)
    return (
        _finite(result.pbo)
        and _finite(result.probability_of_loss)
        and 0 <= result.pbo <= 1
        and 0 <= result.probability_of_loss <= 1
        and result.blocks == blocks
        and result.combinations == expected_combinations
        and result.performance_measure == performance_measure
        and isinstance(result.logits, tuple)
        and isinstance(result.selected_trial_ids, tuple)
        and isinstance(result.oos_log_growth, tuple)
        and len(result.logits) == expected_combinations
        and len(result.selected_trial_ids) == expected_combinations
        and len(result.oos_log_growth) == expected_combinations
        and all(_finite(value) for value in result.logits)
        and all(_finite(value) for value in result.oos_log_growth)
        and all(trial_id in trial_ids for trial_id in result.selected_trial_ids)
        and result.pbo == sum(value <= 0 for value in result.logits) / expected_combinations
        and result.probability_of_loss
        == sum(value < 0 for value in result.oos_log_growth) / expected_combinations
    )


def _performance_payload(values: tuple[CellPerformance | SleevePerformance, ...]) -> list[dict[str, object]]:
    return [asdict(value) for value in values]


def _report_attestation(report: PromotionReportV2) -> str:
    return _canonical_sha256(
        {
            "baseline": asdict(report.baseline),
            "baseline_cells": _performance_payload(report.baseline_cells),
            "baseline_sleeves": _performance_payload(report.baseline_sleeves),
            "candidate_sha256": report.candidate_sha256,
            "cscv": asdict(report.cscv) if report.cscv is not None else None,
            "dsr": asdict(report.dsr) if report.dsr is not None else None,
            "evidence_sha256": report.evidence_sha256,
            "parameter_plateau": asdict(report.parameter_plateau),
            "profitable_nested_fold_fraction": report.profitable_nested_fold_fraction,
            "reasons": report.reasons,
            "registry_head_hash": report.registry_head_hash,
            "stress": asdict(report.stress),
            "stress_cells": _performance_payload(report.stress_cells),
            "stress_sleeves": _performance_payload(report.stress_sleeves),
            "target": report.target.value if report.target is not None else None,
        }
    )


def _build_report(
    *,
    target: PromotionTarget | None,
    reasons: tuple[str, ...],
    baseline: ScenarioPerformance,
    stress: ScenarioPerformance,
    profitable_nested_fold_fraction: float,
    dsr: DeflatedSharpeResult | None,
    cscv: CSCVResult | None,
    baseline_cells: tuple[CellPerformance, ...],
    stress_cells: tuple[CellPerformance, ...],
    baseline_sleeves: tuple[SleevePerformance, ...],
    stress_sleeves: tuple[SleevePerformance, ...],
    parameter_plateau: ParameterPlateauReport,
    registry_head_hash: str,
    candidate_sha256: str,
    evidence_sha256: str,
) -> PromotionReportV2:
    placeholder = "0" * 64
    report = PromotionReportV2.__new__(PromotionReportV2)
    values: dict[str, object] = {
        "target": target,
        "reasons": reasons,
        "baseline": baseline,
        "stress": stress,
        "profitable_nested_fold_fraction": profitable_nested_fold_fraction,
        "dsr": dsr,
        "cscv": cscv,
        "baseline_cells": baseline_cells,
        "stress_cells": stress_cells,
        "baseline_sleeves": baseline_sleeves,
        "stress_sleeves": stress_sleeves,
        "parameter_plateau": parameter_plateau,
        "registry_head_hash": registry_head_hash,
        "candidate_sha256": candidate_sha256,
        "evidence_sha256": evidence_sha256,
        "attestation_sha256": placeholder,
        "_factory_token": _REPORT_FACTORY_TOKEN,
    }
    for name, value in values.items():
        object.__setattr__(report, name, value)
    object.__setattr__(report, "attestation_sha256", _report_attestation(report))
    report.__post_init__()
    return report


def _append_scenario_reasons(
    reasons: list[str],
    *,
    name: str,
    scenario: ScenarioPerformance,
    policy: OfflineToShadowPolicy,
) -> None:
    if scenario.trades < policy.minimum_closed_trades:
        reasons.append(f"insufficient_{name}_closed_trades")
    if scenario.net_growth <= 0:
        reasons.append(f"non_positive_{name}_growth")
    minimum_profit_factor = (
        policy.minimum_baseline_profit_factor if name == "baseline" else policy.minimum_stress_profit_factor
    )
    if scenario.profit_factor is None:
        reasons.append(f"{name}_profit_factor_missing")
    elif scenario.profit_factor < minimum_profit_factor:
        reasons.append(f"weak_{name}_profit_factor")
    minimum_sharpe = (
        policy.minimum_baseline_hac_sharpe if name == "baseline" else policy.minimum_stress_hac_sharpe
    )
    if scenario.hac_sharpe is None:
        reasons.append(f"{name}_hac_sharpe_missing")
    elif scenario.hac_sharpe < minimum_sharpe:
        reasons.append(f"weak_{name}_hac_sharpe")
    maximum_drawdown = (
        policy.maximum_baseline_drawdown if name == "baseline" else policy.maximum_stress_drawdown
    )
    if scenario.maximum_drawdown > maximum_drawdown:
        reasons.append(f"excessive_{name}_drawdown")


def _append_cell_reasons(
    reasons: list[str],
    *,
    scenario_name: str,
    cells: tuple[CellPerformance, ...],
    policy: OfflineToShadowPolicy,
) -> None:
    for cell in cells:
        suffix = f":{cell.cell_id}"
        if cell.trades < policy.minimum_cell_trades:
            reasons.append(f"{scenario_name}_cell_insufficient_trades{suffix}")
        if cell.profit_factor is None:
            reasons.append(f"{scenario_name}_cell_profit_factor_missing{suffix}")
        elif cell.profit_factor < policy.minimum_cell_profit_factor:
            reasons.append(f"{scenario_name}_cell_weak_profit_factor{suffix}")
        if cell.expectancy_usd is None or cell.expectancy_usd <= 0:
            reasons.append(f"{scenario_name}_cell_non_positive_expectancy{suffix}")


def _append_sleeve_reasons(
    reasons: list[str],
    *,
    scenario_name: str,
    sleeves: tuple[SleevePerformance, ...],
    policy: OfflineToShadowPolicy,
) -> None:
    for sleeve in sleeves:
        suffix = f":{sleeve.sleeve_id}"
        if sleeve.trades < policy.minimum_sleeve_trades:
            reasons.append(f"{scenario_name}_sleeve_insufficient_trades{suffix}")
        if sleeve.profit_factor is None:
            reasons.append(f"{scenario_name}_sleeve_profit_factor_missing{suffix}")
        elif sleeve.profit_factor < policy.minimum_sleeve_profit_factor:
            reasons.append(f"{scenario_name}_sleeve_weak_profit_factor{suffix}")


def evaluate_offline_to_shadow(evidence: PromotionEvidenceV2) -> PromotionReportV2:
    """Reverify local evidence and fail closed pending external attestations."""

    if not isinstance(evidence, PromotionEvidenceV2):
        raise TypeError("evidence must be PromotionEvidenceV2")
    snapshot = evidence.registry.verify()
    for fold in evidence.nested_oos_folds:
        fold.selection_registry.verify()
    policy = OFFLINE_TO_SHADOW_POLICY
    holdout = evidence.terminal_holdout
    baseline_portfolio = synchronize_cells(holdout.baseline_cells)
    stress_portfolio = synchronize_cells(holdout.stress_cells)
    baseline = _scenario(holdout.baseline_cells)
    stress = _scenario(holdout.stress_cells)
    baseline_cells = _cell_performance(holdout.baseline_cells)
    stress_cells = _cell_performance(holdout.stress_cells)
    baseline_sleeves = _sleeve_performance(holdout.baseline_cells)
    stress_sleeves = _sleeve_performance(holdout.stress_cells)
    fold_fraction = (
        sum(fold.profitable for fold in evidence.nested_oos_folds) / len(evidence.nested_oos_folds)
        if evidence.nested_oos_folds
        else 0.0
    )
    reasons: list[str] = [
        "external_nested_oos_attestation_unavailable",
        "unsealed_parameter_plateau_stress_evidence",
    ]

    if baseline.days < policy.minimum_evaluation_days:
        reasons.append("insufficient_evaluation_days")
    if len(evidence.nested_oos_folds) < policy.minimum_nested_folds:
        reasons.append("insufficient_nested_oos_folds")
    if fold_fraction < policy.minimum_profitable_fold_fraction:
        reasons.append("insufficient_profitable_nested_oos_folds")
    for fold in evidence.nested_oos_folds:
        if fold.train_days < policy.minimum_nested_train_days:
            reasons.append(f"insufficient_nested_train_days:{fold.fold_id}")
        if fold.test_days < policy.minimum_nested_test_days:
            reasons.append(f"insufficient_nested_test_days:{fold.fold_id}")
        if fold.test_closed_trades < policy.minimum_nested_test_trades:
            reasons.append(f"insufficient_nested_test_trades:{fold.fold_id}")

    _append_scenario_reasons(reasons, name="baseline", scenario=baseline, policy=policy)
    _append_scenario_reasons(reasons, name="stress", scenario=stress, policy=policy)
    if baseline.sortino is None:
        reasons.append("baseline_sortino_missing")
    elif baseline.sortino < policy.minimum_baseline_sortino:
        reasons.append("weak_baseline_sortino")
    if baseline.calmar is None:
        reasons.append("baseline_calmar_missing")
    elif baseline.calmar < policy.minimum_baseline_calmar:
        reasons.append("weak_baseline_calmar")
    if baseline.log_growth > 0 and (
        stress.log_growth / baseline.log_growth < policy.minimum_stress_log_growth_retention
    ):
        reasons.append("insufficient_stress_log_growth_retention")

    dsr: DeflatedSharpeResult | None
    try:
        dsr = deflated_sharpe_ratio(evidence.trial_matrix, evidence.selected_trial_id)
    except (KeyError, ValueError):
        dsr = None
        reasons.append("dsr_evidence_unavailable")
    else:
        if not _valid_dsr(dsr, evidence.trial_matrix, evidence.selected_trial_id):
            dsr = None
            reasons.append("dsr_evidence_invalid")
        elif dsr.probability < policy.minimum_dsr_probability:
            reasons.append("insufficient_deflated_sharpe_probability")

    cscv: CSCVResult | None
    try:
        cscv = cscv_pbo(
            evidence.trial_matrix,
            blocks=evidence.cscv_blocks,
            performance_measure=evidence.protocol.nested_folds.cscv_performance_measure,
        )
    except ValueError:
        cscv = None
        reasons.append("cscv_evidence_unavailable")
    else:
        if not _valid_cscv(
            cscv,
            blocks=evidence.cscv_blocks,
            performance_measure=evidence.protocol.nested_folds.cscv_performance_measure,
            trial_ids=evidence.trial_matrix.trial_ids,
        ):
            cscv = None
            reasons.append("cscv_evidence_invalid")
        else:
            if cscv.pbo > policy.maximum_cscv_pbo:
                reasons.append("excessive_cscv_pbo")
            if cscv.probability_of_loss > policy.maximum_cscv_loss_probability:
                reasons.append("excessive_cscv_oos_loss_probability")

    plateau = parameter_plateau_report(
        evidence.parameter_plateau.selected,
        evidence.parameter_plateau.neighbors,
        selected_on_boundary=evidence.parameter_plateau.selected_on_boundary,
        policy=evidence.parameter_plateau.policy,
    )
    if plateau != evidence.parameter_plateau.report:
        reasons.append("parameter_plateau_evidence_changed")
    if not plateau.stable:
        reasons.append("unstable_parameter_plateau")
        reasons.extend(f"parameter_plateau:{reason}" for reason in plateau.reasons)

    _append_cell_reasons(
        reasons,
        scenario_name="baseline",
        cells=baseline_cells,
        policy=policy,
    )
    _append_cell_reasons(
        reasons,
        scenario_name="stress",
        cells=stress_cells,
        policy=policy,
    )
    _append_sleeve_reasons(
        reasons,
        scenario_name="baseline",
        sleeves=baseline_sleeves,
        policy=policy,
    )
    _append_sleeve_reasons(
        reasons,
        scenario_name="stress",
        sleeves=stress_sleeves,
        policy=policy,
    )
    for name, portfolio in (("baseline", baseline_portfolio), ("stress", stress_portfolio)):
        if portfolio.active_sleeves < policy.minimum_active_sleeves:
            reasons.append(f"insufficient_{name}_active_sleeves")
        if portfolio.active_symbols < policy.minimum_active_symbols:
            reasons.append(f"insufficient_{name}_active_symbols")
        if portfolio.maximum_sleeve_profit_contribution > policy.maximum_profit_contribution:
            reasons.append(f"excessive_{name}_sleeve_profit_concentration")
        if portfolio.maximum_symbol_profit_contribution > policy.maximum_profit_contribution:
            reasons.append(f"excessive_{name}_symbol_profit_concentration")

    return _build_report(
        target=None,
        reasons=tuple(reasons),
        baseline=baseline,
        stress=stress,
        profitable_nested_fold_fraction=fold_fraction,
        dsr=dsr,
        cscv=cscv,
        baseline_cells=baseline_cells,
        stress_cells=stress_cells,
        baseline_sleeves=baseline_sleeves,
        stress_sleeves=stress_sleeves,
        parameter_plateau=plateau,
        registry_head_hash=snapshot.head_hash,
        candidate_sha256=evidence.protocol.candidate_freeze.candidate_freeze_sha256,
        evidence_sha256=evidence.evidence_sha256,
    )
