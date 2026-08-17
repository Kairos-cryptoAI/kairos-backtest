"""Deterministic reused-data development evidence for managed strategy sleeves.

This module is deliberately not a promotion or report-writing entry point.  It
evaluates the fixed development roles only, keeps warm-up observations outside
the managed PnL window, and returns immutable evidence suitable for a later
trial-registry or report orchestration layer.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from datetime import UTC, date, datetime, timedelta
from enum import Enum
from typing import cast

from kairos_quant.candles import Candle

from .cost_risk import AllInCostModel, RiskLimits
from .execution import ExecutionConfig
from .managed_evaluation import (
    ManagedCellResult,
    ManagedEvaluationPolicy,
    evaluate_sleeve_cell,
)
from .portfolio import PortfolioEvidence, synchronize_cells
from .research_protocol import (
    DEVELOPMENT_WINDOWS,
    DataRole,
    ResearchProtocol,
    ResearchPurpose,
)
from .scenarios import BASELINE, STRESS, SYMBOLS
from .seeding import derive_seed
from .sleeves.range_mean_reversion import (
    RangeMeanReversionConfig,
    generate_range_mean_reversion_intents,
)
from .sleeves.trend_breakout import TrendBreakoutConfig, generate_trend_breakout_intents
from .sleeves.trend_pullback_reclaim import (
    TrendPullbackReclaimConfig,
    generate_trend_pullback_reclaim_intents,
)
from .strategy_models import SleeveIntent
from .validation import canonical_candles

_MINUTE_MS = 60_000
_DAY_MS = 24 * 60 * _MINUTE_MS
_MINUTES_PER_DAY = _DAY_MS // _MINUTE_MS
_TREND_SLEEVE_ID = "trend_breakout_v1"
_RANGE_SLEEVE_ID = "range_mean_reversion_v1"
_PULLBACK_SLEEVE_ID = "trend_pullback_reclaim_v1"
_SLEEVE_IDS = (_TREND_SLEEVE_ID, _RANGE_SLEEVE_ID, _PULLBACK_SLEEVE_ID)
_SCENARIO_NAMES = ("baseline", "stress")


def _json_ready(value: object) -> object:
    """Return a canonical JSON-compatible value and reject unsupported evidence."""

    if isinstance(value, Enum):
        return value.value
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("evidence datetimes must be timezone-aware")
        return value.isoformat()
    if type(value) is date:
        return value.isoformat()
    if isinstance(value, Mapping):
        if any(not isinstance(key, str) for key in value):
            raise TypeError("JSON evidence mappings require string keys")
        return {key: _json_ready(value[key]) for key in sorted(value)}
    if isinstance(value, (tuple, list)):
        return [_json_ready(item) for item in value]
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("JSON evidence cannot contain non-finite numbers")
        return 0.0 if value == 0 else value
    raise TypeError(f"unsupported JSON evidence value: {type(value).__name__}")


def _canonical_hash_value(value: object) -> object:
    """Canonicalize semantically equal numeric evidence to one hash payload."""

    if isinstance(value, Enum):
        return value.value
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("hash datetimes must be timezone-aware")
        return value.isoformat()
    if type(value) is date:
        return value.isoformat()
    if isinstance(value, Mapping):
        if any(not isinstance(key, str) for key in value):
            raise TypeError("canonical hash mappings require string keys")
        return {key: _canonical_hash_value(value[key]) for key in sorted(value)}
    if isinstance(value, (tuple, list)):
        return [_canonical_hash_value(item) for item in value]
    if value is None or isinstance(value, (str, bool)):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("canonical hash evidence cannot contain non-finite numbers")
        normalized = 0.0 if value == 0 else value
        if normalized.is_integer():
            return int(normalized)
        return {"__float_hex__": normalized.hex()}
    raise TypeError(f"unsupported canonical hash value: {type(value).__name__}")


def _canonical_json_bytes(value: object) -> bytes:
    return json.dumps(
        _canonical_hash_value(value),
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode()


def _utc_ms(day: date) -> int:
    return int(datetime.combine(day, datetime.min.time(), UTC).timestamp() * 1_000)


def _lowercase_sha256(name: str, value: str) -> None:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or value != value.lower()
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ValueError(f"{name} must be a lowercase SHA-256")


def _intent_inventory_sha256(intents: tuple[SleeveIntent, ...] | list[SleeveIntent]) -> str:
    return hashlib.sha256(_canonical_json_bytes([intent.intent_id for intent in intents])).hexdigest()


@dataclass(frozen=True, slots=True)
class DevelopmentCandidate:
    """One typed multi-sleeve parameter candidate with a canonical identity."""

    trend: TrendBreakoutConfig = field(default_factory=TrendBreakoutConfig)
    range_mean_reversion: RangeMeanReversionConfig = field(default_factory=RangeMeanReversionConfig)
    trend_pullback_reclaim: TrendPullbackReclaimConfig = field(default_factory=TrendPullbackReclaimConfig)
    risk: RiskLimits = field(default_factory=RiskLimits)
    terminal_liquidation_grace_ms: int = 60 * _MINUTE_MS
    candidate_sha256: str = field(init=False)

    def __post_init__(self) -> None:
        if not isinstance(self.trend, TrendBreakoutConfig):
            raise TypeError("trend must be a TrendBreakoutConfig")
        if not isinstance(self.range_mean_reversion, RangeMeanReversionConfig):
            raise TypeError("range_mean_reversion must be a RangeMeanReversionConfig")
        if not isinstance(self.trend_pullback_reclaim, TrendPullbackReclaimConfig):
            raise TypeError("trend_pullback_reclaim must be a TrendPullbackReclaimConfig")
        if not isinstance(self.risk, RiskLimits):
            raise TypeError("risk must be RiskLimits")
        ManagedEvaluationPolicy(
            application_exit_latency_ms=0,
            terminal_liquidation_grace_ms=self.terminal_liquidation_grace_ms,
        )
        object.__setattr__(
            self,
            "candidate_sha256",
            hashlib.sha256(_canonical_json_bytes(self.parameter_dict())).hexdigest(),
        )

    @property
    def maximum_holding_ms(self) -> int:
        return max(
            self.trend.max_hold_bars * 5 * _MINUTE_MS,
            self.range_mean_reversion.max_hold_bars * 5 * _MINUTE_MS,
            self.trend_pullback_reclaim.max_hold_bars * 5 * _MINUTE_MS,
        )

    @property
    def maximum_liquidation_horizon_ms(self) -> int:
        return self.maximum_holding_ms + self.terminal_liquidation_grace_ms

    def parameter_dict(self) -> dict[str, object]:
        return {
            "range_mean_reversion": asdict(self.range_mean_reversion),
            "risk": asdict(self.risk),
            "terminal_liquidation_grace_ms": self.terminal_liquidation_grace_ms,
            "schema_version": 2,
            "trend": asdict(self.trend),
            "trend_pullback_reclaim": asdict(self.trend_pullback_reclaim),
        }

    def to_dict(self) -> dict[str, object]:
        return cast(
            dict[str, object],
            _json_ready(
                {
                    "candidate_sha256": self.candidate_sha256,
                    "parameters": self.parameter_dict(),
                }
            ),
        )


def _maximum_adverse_funding_bps(
    execution: ExecutionConfig,
    maximum_liquidation_horizon_ms: int,
) -> float:
    funding = execution.funding
    if funding.evidence == "unavailable":
        return 0.0
    if funding.evidence != "assumed":
        raise ValueError("development scenarios support only unavailable or assumed funding")
    rate = funding.rate_8h_bps
    if rate is None:
        raise RuntimeError("validated assumed funding lost its configured rate")
    settlements = (maximum_liquidation_horizon_ms + funding.settlement_interval_ms - 1) // (
        funding.settlement_interval_ms
    )
    return settlements * rate / 8


@dataclass(frozen=True, slots=True)
class DevelopmentScenario:
    """One execution scenario and its exactly corresponding admission hurdle."""

    name: str
    execution: ExecutionConfig
    costs: AllInCostModel
    policy: ManagedEvaluationPolicy
    limits: RiskLimits
    maximum_liquidation_horizon_ms: int

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not self.name or self.name != self.name.strip():
            raise ValueError("scenario name must be a normalized non-empty string")
        if not isinstance(self.execution, ExecutionConfig):
            raise TypeError("scenario execution must be an ExecutionConfig")
        if not isinstance(self.costs, AllInCostModel):
            raise TypeError("scenario costs must be an AllInCostModel")
        if not isinstance(self.policy, ManagedEvaluationPolicy):
            raise TypeError("scenario policy must be a ManagedEvaluationPolicy")
        if not isinstance(self.limits, RiskLimits):
            raise TypeError("scenario limits must be RiskLimits")
        if (
            isinstance(self.maximum_liquidation_horizon_ms, bool)
            or not isinstance(self.maximum_liquidation_horizon_ms, int)
            or self.maximum_liquidation_horizon_ms <= 0
        ):
            raise ValueError("scenario liquidation horizon must be a positive integer")
        expected_funding = _maximum_adverse_funding_bps(
            self.execution,
            self.maximum_liquidation_horizon_ms,
        )
        expected = (
            (self.costs.fee_bps_per_side, self.execution.fee_bps),
            (self.costs.spread_bps, self.execution.spread_bps),
            (
                self.costs.slippage_bps_per_side,
                self.execution.slippage_bps + self.execution.slippage_jitter_bps,
            ),
            (self.costs.adverse_funding_bps, expected_funding),
        )
        if any(actual != required for actual, required in expected):
            raise ValueError("scenario cost model must exactly match execution and funding assumptions")

    def to_dict(self) -> dict[str, object]:
        return cast(
            dict[str, object],
            _json_ready(
                {
                    "costs": asdict(self.costs),
                    "execution": asdict(self.execution),
                    "limits": asdict(self.limits),
                    "policy": asdict(self.policy),
                    "maximum_liquidation_horizon_ms": self.maximum_liquidation_horizon_ms,
                    "name": self.name,
                }
            ),
        )


def development_scenarios(
    candidate: DevelopmentCandidate,
) -> tuple[DevelopmentScenario, DevelopmentScenario]:
    """Build the fixed baseline/stress pair from the candidate's longest hold."""

    if not isinstance(candidate, DevelopmentCandidate):
        raise TypeError("candidate must be a DevelopmentCandidate")

    def scenario(name: str, execution: ExecutionConfig) -> DevelopmentScenario:
        policy = ManagedEvaluationPolicy(
            application_exit_latency_ms=execution.latency_ms,
            terminal_liquidation_grace_ms=candidate.terminal_liquidation_grace_ms,
        )
        costs = AllInCostModel(
            fee_bps_per_side=execution.fee_bps,
            spread_bps=execution.spread_bps,
            slippage_bps_per_side=execution.slippage_bps + execution.slippage_jitter_bps,
            adverse_funding_bps=_maximum_adverse_funding_bps(
                execution,
                candidate.maximum_liquidation_horizon_ms,
            ),
            latency_bps=0.0,
            uncertainty_buffer_bps=2.0,
        )
        return DevelopmentScenario(
            name,
            execution,
            costs,
            policy,
            candidate.risk,
            candidate.maximum_liquidation_horizon_ms,
        )

    return scenario("baseline", BASELINE), scenario("stress", STRESS)


DEFAULT_DEVELOPMENT_PROTOCOL = ResearchProtocol(
    protocol_name="multi-strategy-v2-development",
    universe=SYMBOLS,
    windows=DEVELOPMENT_WINDOWS,
    max_trials=3,
    maximum_holding_ms=12 * 60 * 60 * 1_000,
    maximum_label_horizon_ms=12 * 60 * 60 * 1_000,
    maximum_execution_latency_ms=max(BASELINE.latency_ms, STRESS.latency_ms),
    warmup_ms=35 * _DAY_MS,
)


@dataclass(frozen=True, slots=True)
class DatasetSliceEvidence:
    symbol: str
    generation_start: date
    evaluation_start: date
    evaluation_end: date
    warmup_candles: int
    evaluation_candles: int
    candles_sha256: str

    def __post_init__(self) -> None:
        if not isinstance(self.symbol, str) or not self.symbol or self.symbol != self.symbol.strip().upper():
            raise ValueError("dataset symbol must be normalized uppercase text")
        for name, value in (
            ("generation_start", self.generation_start),
            ("evaluation_start", self.evaluation_start),
            ("evaluation_end", self.evaluation_end),
        ):
            if type(value) is not date:
                raise TypeError(f"dataset {name} must be a date")
        if not self.generation_start <= self.evaluation_start < self.evaluation_end:
            raise ValueError("dataset evidence dates must be ordered")
        expected_warmup = (self.evaluation_start - self.generation_start).days * _MINUTES_PER_DAY
        expected_evaluation = (self.evaluation_end - self.evaluation_start).days * _MINUTES_PER_DAY
        if self.warmup_candles != expected_warmup or self.evaluation_candles != expected_evaluation:
            raise ValueError("dataset candle counts must exactly match complete UTC-day bounds")
        _lowercase_sha256("dataset candles_sha256", self.candles_sha256)


@dataclass(frozen=True, slots=True)
class DevelopmentCellEvidence:
    scenario_name: str
    cell_id: str
    sleeve_id: str
    symbol: str
    initial_equity_usd: float
    generated_intents: int
    evaluated_intents: int
    warmup_intents_filtered: int
    evaluation_seed: int
    candidate_sha256: str
    dataset_sha256: str
    evaluated_intents_sha256: str
    result: ManagedCellResult

    def __post_init__(self) -> None:
        if not isinstance(self.result, ManagedCellResult):
            raise TypeError("cell result must be a ManagedCellResult")
        if isinstance(self.evaluation_seed, bool) or not isinstance(self.evaluation_seed, int):
            raise TypeError("cell evaluation seed must be an integer")
        if self.result.assumptions.seed != self.evaluation_seed:
            raise ValueError("cell result seed does not match its declared evaluation seed")
        _lowercase_sha256("cell candidate_sha256", self.candidate_sha256)
        _lowercase_sha256("cell dataset_sha256", self.dataset_sha256)
        _lowercase_sha256("cell evaluated_intents_sha256", self.evaluated_intents_sha256)
        if self.result.cell.cell_id != self.cell_id:
            raise ValueError("cell evidence and managed result identities differ")
        if self.result.cell.sleeve_id != self.sleeve_id or self.result.cell.symbol != self.symbol:
            raise ValueError("cell evidence does not match its sleeve-symbol result")
        if not math.isclose(
            self.result.cell.initial_equity_usd,
            self.initial_equity_usd,
            rel_tol=1e-12,
            abs_tol=1e-9,
        ):
            raise ValueError("cell evidence initial capital is inconsistent")
        counts = (
            self.generated_intents,
            self.evaluated_intents,
            self.warmup_intents_filtered,
        )
        if any(isinstance(value, bool) or not isinstance(value, int) or value < 0 for value in counts):
            raise ValueError("cell intent counts must be non-negative integers")
        if self.generated_intents != self.evaluated_intents + self.warmup_intents_filtered:
            raise ValueError("warmup and evaluated intents must partition generated intents")
        if self.result.counters.intents != self.evaluated_intents:
            raise ValueError("managed result must disposition every evaluated intent")
        expected_inventory_sha256 = _intent_inventory_sha256(
            tuple(disposition.intent for disposition in self.result.dispositions)
        )
        if self.evaluated_intents_sha256 != expected_inventory_sha256:
            raise ValueError("cell intent inventory does not match its managed dispositions")


@dataclass(frozen=True, slots=True)
class DevelopmentScenarioEvidence:
    scenario: DevelopmentScenario
    cells: tuple[DevelopmentCellEvidence, ...]
    portfolio: PortfolioEvidence

    def __post_init__(self) -> None:
        if not isinstance(self.scenario, DevelopmentScenario):
            raise TypeError("scenario evidence requires a DevelopmentScenario")
        if not isinstance(self.cells, tuple) or any(
            not isinstance(cell, DevelopmentCellEvidence) for cell in self.cells
        ):
            raise TypeError("scenario cells must be immutable DevelopmentCellEvidence values")
        if len(self.cells) != len(_SLEEVE_IDS) * len(SYMBOLS):
            raise ValueError("scenario evidence requires every sleeve-symbol cell")
        if any(cell.scenario_name != self.scenario.name for cell in self.cells):
            raise ValueError("every cell must belong to its declared scenario")
        if len({cell.cell_id for cell in self.cells}) != len(self.cells):
            raise ValueError("scenario cell identities must be unique")
        if {(cell.sleeve_id, cell.symbol) for cell in self.cells} != {
            (sleeve_id, symbol) for sleeve_id in _SLEEVE_IDS for symbol in SYMBOLS
        }:
            raise ValueError("scenario evidence has an incomplete sleeve-symbol grid")
        for cell in self.cells:
            assumptions = cell.result.assumptions
            if not (
                assumptions.execution == self.scenario.execution
                and assumptions.costs == self.scenario.costs
                and assumptions.policy == self.scenario.policy
                and assumptions.limits == self.scenario.limits
                and assumptions.seed == cell.evaluation_seed
            ):
                raise ValueError("cell assumptions do not exactly match their scenario evidence")
        expected_portfolio = synchronize_cells(tuple(cell.result.cell for cell in self.cells))
        if self.portfolio != expected_portfolio:
            raise ValueError("portfolio evidence must exactly equal synchronized managed cells")


@dataclass(frozen=True, slots=True)
class DevelopmentCampaignEvidence:
    candidate: DevelopmentCandidate
    protocol: ResearchProtocol
    protocol_name: str
    protocol_sha256: str
    window_name: str
    role: DataRole
    purpose: ResearchPurpose
    generation_start: date
    evaluation_start: date
    evaluation_end: date
    requested_initial_equity_usd: float
    cell_initial_equity_usd: float
    datasets: tuple[DatasetSliceEvidence, ...]
    scenarios: tuple[DevelopmentScenarioEvidence, ...]
    seed: int

    def __post_init__(self) -> None:
        if not isinstance(self.candidate, DevelopmentCandidate):
            raise TypeError("campaign candidate must be a DevelopmentCandidate")
        if not isinstance(self.protocol, ResearchProtocol):
            raise TypeError("campaign protocol must be a ResearchProtocol")
        _validate_protocol(self.protocol, self.candidate)
        if self.protocol_name != self.protocol.protocol_name:
            raise ValueError("campaign protocol name does not match its canonical payload")
        _lowercase_sha256("campaign protocol_sha256", self.protocol_sha256)
        if self.protocol_sha256 != self.protocol.fingerprint():
            raise ValueError("campaign protocol fingerprint does not match its canonical payload")
        if not isinstance(self.purpose, ResearchPurpose) or self.purpose is ResearchPurpose.PROMOTE:
            raise ValueError("development campaign purpose must be a non-promotion ResearchPurpose")
        window = self.protocol.assert_access(self.window_name, self.purpose)
        if window.role is DataRole.BLIND or self.role is not window.role:
            raise ValueError("campaign data role does not match its registered development window")
        if type(self.evaluation_start) is not date or type(self.evaluation_end) is not date:
            raise TypeError("campaign evaluation bounds must be date values")
        if not window.start <= self.evaluation_start < self.evaluation_end <= window.end:
            raise ValueError("campaign evaluation bounds must remain inside their registered role")
        if (self.evaluation_end - self.evaluation_start).days < 2:
            raise ValueError("campaign evaluation requires at least two complete UTC days")
        earliest_development_day = min(item.start for item in DEVELOPMENT_WINDOWS)
        expected_generation_start = max(
            earliest_development_day,
            self.evaluation_start - timedelta(milliseconds=self.protocol.warmup_ms),
        )
        if self.generation_start != expected_generation_start:
            raise ValueError("campaign generation start does not match its registered warmup")
        if isinstance(self.seed, bool) or not isinstance(self.seed, int):
            raise TypeError("campaign seed must be an integer")
        capital_values = (self.requested_initial_equity_usd, self.cell_initial_equity_usd)
        if any(
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
            or value <= 0
            for value in capital_values
        ):
            raise ValueError("campaign capital values must be finite and positive")
        expected_cell_equity = self.requested_initial_equity_usd / (len(_SLEEVE_IDS) * len(SYMBOLS))
        if not math.isclose(
            self.cell_initial_equity_usd,
            expected_cell_equity,
            rel_tol=1e-12,
            abs_tol=0.0,
        ):
            raise ValueError("campaign cell capital must be the exact equal-weight allocation")
        if not isinstance(self.datasets, tuple) or any(
            not isinstance(dataset, DatasetSliceEvidence) for dataset in self.datasets
        ):
            raise TypeError("campaign datasets must be immutable DatasetSliceEvidence values")
        if tuple(dataset.symbol for dataset in self.datasets) != SYMBOLS:
            raise ValueError("dataset evidence must follow the fixed development universe")
        if any(
            dataset.generation_start != self.generation_start
            or dataset.evaluation_start != self.evaluation_start
            or dataset.evaluation_end != self.evaluation_end
            for dataset in self.datasets
        ):
            raise ValueError("all dataset evidence must match the campaign data bounds")
        dataset_by_symbol = {dataset.symbol: dataset for dataset in self.datasets}
        if not isinstance(self.scenarios, tuple) or any(
            not isinstance(item, DevelopmentScenarioEvidence) for item in self.scenarios
        ):
            raise TypeError("campaign scenarios must be immutable scenario evidence")
        if tuple(item.scenario.name for item in self.scenarios) != _SCENARIO_NAMES:
            raise ValueError("campaign evidence requires ordered baseline and stress scenarios")
        _validate_scenarios(
            tuple(item.scenario for item in self.scenarios),
            candidate=self.candidate,
            protocol=self.protocol,
        )
        all_cell_ids = [cell.cell_id for scenario in self.scenarios for cell in scenario.cells]
        if len(all_cell_ids) != len(set(all_cell_ids)):
            raise ValueError("scenario-qualified cell identities must be globally unique")
        for scenario in self.scenarios:
            if not math.isclose(
                scenario.portfolio.initial_equity_usd,
                self.requested_initial_equity_usd,
                rel_tol=1e-12,
                abs_tol=0.0,
            ):
                raise ValueError("each scenario must receive the same fixed portfolio capital")
            for cell in scenario.cells:
                if not math.isclose(
                    cell.initial_equity_usd,
                    self.cell_initial_equity_usd,
                    rel_tol=1e-12,
                    abs_tol=0.0,
                ):
                    raise ValueError("campaign cell capital is not equal-weight")
                if cell.candidate_sha256 != self.candidate.candidate_sha256:
                    raise ValueError("cell candidate fingerprint does not match the campaign")
                if cell.dataset_sha256 != dataset_by_symbol[cell.symbol].candles_sha256:
                    raise ValueError("cell dataset fingerprint does not match the campaign")
                expected_seed = derive_seed(
                    self.seed,
                    "development-campaign-v2",
                    self.candidate.candidate_sha256,
                    self.window_name,
                    self.evaluation_start.isoformat(),
                    self.evaluation_end.isoformat(),
                    scenario.scenario.name,
                    cell.sleeve_id,
                    cell.symbol,
                )
                if cell.evaluation_seed != expected_seed:
                    raise ValueError("cell evaluation seed does not match its derived campaign seed")
                if cell.result.assumptions.limits != self.candidate.risk:
                    raise ValueError("cell risk assumptions do not match the campaign candidate")

    def to_dict(self) -> dict[str, object]:
        scenario_rows: list[dict[str, object]] = []
        for scenario in self.scenarios:
            cell_rows = []
            for cell in scenario.cells:
                cell_rows.append(
                    {
                        "cell_id": cell.cell_id,
                        "evaluated_intents": cell.evaluated_intents,
                        "evaluated_intents_sha256": cell.evaluated_intents_sha256,
                        "evaluation_seed": cell.evaluation_seed,
                        "generated_intents": cell.generated_intents,
                        "initial_equity_usd": cell.initial_equity_usd,
                        "candidate_sha256": cell.candidate_sha256,
                        "dataset_sha256": cell.dataset_sha256,
                        "managed_result": asdict(cell.result),
                        "scenario_name": cell.scenario_name,
                        "sleeve_id": cell.sleeve_id,
                        "symbol": cell.symbol,
                        "warmup_intents_filtered": cell.warmup_intents_filtered,
                    }
                )
            scenario_rows.append(
                {
                    "cells": cell_rows,
                    "definition": scenario.scenario.to_dict(),
                    "portfolio": asdict(scenario.portfolio),
                }
            )
        payload = {
            "allocation": {
                "cell_initial_equity_usd": self.cell_initial_equity_usd,
                "cells_per_scenario": len(_SLEEVE_IDS) * len(SYMBOLS),
                "equal_fixed_capital": True,
                "requested_initial_equity_usd": self.requested_initial_equity_usd,
            },
            "candidate": self.candidate.to_dict(),
            "data": {
                "datasets": [asdict(dataset) for dataset in self.datasets],
                "evaluation_end_exclusive": self.evaluation_end,
                "evaluation_start": self.evaluation_start,
                "generation_start": self.generation_start,
                "no_imputation": True,
                "purpose": self.purpose,
                "role": self.role,
                "window_name": self.window_name,
            },
            "development_only": True,
            "out_of_sample": False,
            "promotion_eligible": False,
            "protocol": {
                "definition": asdict(self.protocol),
                "name": self.protocol_name,
                "sha256": self.protocol_sha256,
            },
            "reused_data": True,
            "scenarios": scenario_rows,
            "schema_version": 2,
            "seed": self.seed,
        }
        return cast(dict[str, object], _json_ready(payload))


def _validate_protocol(protocol: ResearchProtocol, candidate: DevelopmentCandidate) -> None:
    if not isinstance(protocol, ResearchProtocol):
        raise TypeError("protocol must be a ResearchProtocol")
    if protocol.universe != SYMBOLS:
        raise ValueError("development protocol must use the fixed five-symbol universe")
    if protocol.windows != DEVELOPMENT_WINDOWS:
        raise ValueError("development protocol must preserve the fixed reused-data roles")
    if protocol.warmup_ms % _DAY_MS:
        raise ValueError("development protocol warmup must align with complete UTC days")
    if candidate.maximum_liquidation_horizon_ms > protocol.maximum_holding_ms:
        raise ValueError("candidate holding horizon exceeds its research protocol")
    if candidate.maximum_liquidation_horizon_ms > protocol.maximum_label_horizon_ms:
        raise ValueError("candidate holding horizon exceeds the registered label horizon")


def _validate_scenarios(
    scenarios: tuple[DevelopmentScenario, ...],
    *,
    candidate: DevelopmentCandidate,
    protocol: ResearchProtocol,
) -> None:
    if not isinstance(scenarios, tuple) or any(
        not isinstance(scenario, DevelopmentScenario) for scenario in scenarios
    ):
        raise TypeError("scenarios must be an immutable tuple of DevelopmentScenario values")
    names = tuple(scenario.name for scenario in scenarios)
    if names != _SCENARIO_NAMES:
        raise ValueError("development scenarios must be unique ordered baseline and stress")
    baseline, stress = scenarios
    expected_scenarios = development_scenarios(candidate)
    if scenarios != expected_scenarios:
        raise ValueError("development scenarios must exactly match the fixed candidate factory")
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
    if any(stressed < base for base, stressed in zip(baseline_cost, stress_cost, strict=True)):
        raise ValueError("stress scenario must weakly dominate every baseline execution cost")
    if baseline.execution.funding.evidence != "unavailable":
        raise ValueError("baseline funding must be explicitly unavailable")
    if (
        baseline.execution.funding.source != "unavailable"
        or baseline.execution.funding.rate_8h_bps is not None
    ):
        raise ValueError("baseline cannot encode unavailable funding as an observed zero")
    if stress.execution.funding.evidence != "assumed":
        raise ValueError("stress funding must be an explicit adverse assumption")
    if (
        not stress.execution.funding.source.strip()
        or stress.execution.funding.source == "unavailable"
        or stress.execution.funding.rate_8h_bps is None
        or stress.execution.funding.rate_8h_bps <= 0
    ):
        raise ValueError("stress funding requires an explicit source and adverse rate")
    for scenario in scenarios:
        if scenario.maximum_liquidation_horizon_ms != candidate.maximum_liquidation_horizon_ms:
            raise ValueError("scenario funding horizon must match the candidate liquidation bound")
        if scenario.policy.terminal_liquidation_grace_ms != candidate.terminal_liquidation_grace_ms:
            raise ValueError("scenario liquidation grace must match the candidate")
        if scenario.policy.application_exit_latency_ms != scenario.execution.latency_ms:
            raise ValueError("scenario entry and application-exit latency must match")
        if scenario.limits != candidate.risk:
            raise ValueError("scenario risk limits must match the candidate")
        if scenario.execution.latency_ms > protocol.maximum_execution_latency_ms:
            raise ValueError("scenario latency exceeds its research protocol")


def _validated_bounds(
    window_start: date,
    window_end: date,
    evaluation_start: date | None,
    evaluation_end: date | None,
) -> tuple[date, date]:
    start = window_start if evaluation_start is None else evaluation_start
    end = window_end if evaluation_end is None else evaluation_end
    if type(start) is not date or type(end) is not date:
        raise TypeError("evaluation boundaries must be date values")
    if not window_start <= start < end <= window_end:
        raise ValueError("evaluation subwindow must remain inside its registered data role")
    if (end - start).days < 2:
        raise ValueError("development evaluation requires at least two complete UTC days")
    return start, end


def _candle_payload(candle: Candle) -> tuple[object, ...]:
    return (
        candle.symbol,
        candle.timeframe,
        candle.open_time_ms,
        candle.close_time_ms,
        candle.open,
        candle.high,
        candle.low,
        candle.close,
        candle.volume,
        candle.quote_volume,
        candle.taker_buy_volume,
    )


def _slice_symbol(
    candles: list[Candle],
    *,
    symbol: str,
    generation_start: date,
    evaluation_start: date,
    evaluation_end: date,
) -> tuple[list[Candle], list[Candle], DatasetSliceEvidence]:
    if not isinstance(candles, list) or not candles:
        raise ValueError(f"{symbol} candles must be a non-empty list")
    if any(not isinstance(candle, Candle) for candle in candles):
        raise TypeError(f"{symbol} candles must contain Candle values")
    ordered = canonical_candles(candles, expected_timeframe="1m")
    if ordered != candles:
        raise ValueError(f"{symbol} candles must already be chronological")
    if ordered[0].symbol != symbol:
        raise ValueError(f"{symbol} mapping key does not match its candles")

    generation_start_ms = _utc_ms(generation_start)
    evaluation_start_ms = _utc_ms(evaluation_start)
    evaluation_end_ms = _utc_ms(evaluation_end)
    selected = [
        candle for candle in ordered if generation_start_ms <= candle.open_time_ms < evaluation_end_ms
    ]
    expected_count = (evaluation_end_ms - generation_start_ms) // _MINUTE_MS
    if len(selected) != expected_count:
        raise ValueError(f"{symbol} selected development data are incomplete; gaps are not imputed")
    for index, candle in enumerate(selected):
        expected_open = generation_start_ms + index * _MINUTE_MS
        if candle.open_time_ms != expected_open or candle.close_time_ms != expected_open + _MINUTE_MS - 1:
            raise ValueError(f"{symbol} selected development data must be contiguous aligned minutes")
    evaluation_index = (evaluation_start_ms - generation_start_ms) // _MINUTE_MS
    evaluation = selected[evaluation_index:]
    expected_evaluation_count = (evaluation_end_ms - evaluation_start_ms) // _MINUTE_MS
    if len(evaluation) != expected_evaluation_count:
        raise RuntimeError("validated development slice lost its evaluation boundary")
    if len(evaluation) % _MINUTES_PER_DAY:
        raise RuntimeError("validated development evaluation lost complete UTC days")

    digest = hashlib.sha256(
        _canonical_json_bytes([_candle_payload(candle) for candle in selected])
    ).hexdigest()
    evidence = DatasetSliceEvidence(
        symbol=symbol,
        generation_start=generation_start,
        evaluation_start=evaluation_start,
        evaluation_end=evaluation_end,
        warmup_candles=evaluation_index,
        evaluation_candles=len(evaluation),
        candles_sha256=digest,
    )
    return selected, evaluation, evidence


def _generate_intents(
    sleeve_id: str,
    candles: list[Candle],
    candidate: DevelopmentCandidate,
) -> list[SleeveIntent]:
    if sleeve_id == _TREND_SLEEVE_ID:
        return generate_trend_breakout_intents(candles, candidate.trend)
    if sleeve_id == _RANGE_SLEEVE_ID:
        return generate_range_mean_reversion_intents(candles, candidate.range_mean_reversion)
    if sleeve_id == _PULLBACK_SLEEVE_ID:
        return generate_trend_pullback_reclaim_intents(candles, candidate.trend_pullback_reclaim)
    raise ValueError(f"unsupported development sleeve: {sleeve_id}")


def run_development_campaign(
    candles_by_symbol: Mapping[str, list[Candle]],
    *,
    window_name: str,
    purpose: ResearchPurpose,
    candidate: DevelopmentCandidate | None = None,
    protocol: ResearchProtocol = DEFAULT_DEVELOPMENT_PROTOCOL,
    evaluation_start: date | None = None,
    evaluation_end: date | None = None,
    initial_equity_usd: float = 100_000.0,
    scenarios: tuple[DevelopmentScenario, ...] | None = None,
    seed: int = 42,
) -> DevelopmentCampaignEvidence:
    """Evaluate the fixed managed-sleeve grid on explicitly reused development data."""

    selected_candidate = DevelopmentCandidate() if candidate is None else candidate
    if not isinstance(selected_candidate, DevelopmentCandidate):
        raise TypeError("candidate must be a DevelopmentCandidate")
    _validate_protocol(protocol, selected_candidate)
    if not isinstance(purpose, ResearchPurpose):
        raise TypeError("purpose must be a ResearchPurpose")
    if purpose is ResearchPurpose.PROMOTE:
        raise PermissionError("reused development data cannot be used to promote")
    window = protocol.assert_access(window_name, purpose)
    if window.role is DataRole.BLIND:
        raise PermissionError("development campaign cannot consume blind data")
    start, end = _validated_bounds(window.start, window.end, evaluation_start, evaluation_end)

    if (
        isinstance(initial_equity_usd, bool)
        or not isinstance(initial_equity_usd, (int, float))
        or not math.isfinite(initial_equity_usd)
        or initial_equity_usd <= 0
    ):
        raise ValueError("initial_equity_usd must be finite and positive")
    if isinstance(seed, bool) or not isinstance(seed, int):
        raise TypeError("seed must be an integer")
    if protocol.warmup_ms % _DAY_MS:
        raise ValueError("development warmup must align with complete UTC days")
    earliest_development_day = min(item.start for item in DEVELOPMENT_WINDOWS)
    requested_warmup_start = start - timedelta(milliseconds=protocol.warmup_ms)
    generation_start = max(earliest_development_day, requested_warmup_start)

    if not isinstance(candles_by_symbol, Mapping):
        raise TypeError("candles_by_symbol must be a mapping")
    if set(candles_by_symbol) != set(SYMBOLS):
        raise ValueError("candles_by_symbol must contain exactly the fixed five-symbol universe")

    selected_scenarios = development_scenarios(selected_candidate) if scenarios is None else scenarios
    _validate_scenarios(
        selected_scenarios,
        candidate=selected_candidate,
        protocol=protocol,
    )

    generation_rows: dict[str, list[Candle]] = {}
    evaluation_rows: dict[str, list[Candle]] = {}
    dataset_evidence: list[DatasetSliceEvidence] = []
    for symbol in SYMBOLS:
        generated, evaluated, evidence = _slice_symbol(
            candles_by_symbol[symbol],
            symbol=symbol,
            generation_start=generation_start,
            evaluation_start=start,
            evaluation_end=end,
        )
        generation_rows[symbol] = generated
        evaluation_rows[symbol] = evaluated
        dataset_evidence.append(evidence)
    dataset_by_symbol = {item.symbol: item for item in dataset_evidence}

    evaluation_start_ms = _utc_ms(start)
    evaluation_end_ms = _utc_ms(end)
    generation_start_ms = _utc_ms(generation_start)
    generated_intents: dict[
        tuple[str, str],
        tuple[list[SleeveIntent], list[SleeveIntent], list[SleeveIntent]],
    ] = {}
    for sleeve_id in _SLEEVE_IDS:
        for symbol in SYMBOLS:
            all_intents = _generate_intents(
                sleeve_id,
                generation_rows[symbol],
                selected_candidate,
            )
            if not isinstance(all_intents, list) or any(
                not isinstance(intent, SleeveIntent) for intent in all_intents
            ):
                raise TypeError("sleeve generators must return lists of SleeveIntent values")
            if any(intent.sleeve_id != sleeve_id or intent.symbol != symbol for intent in all_intents):
                raise ValueError("sleeve generator emitted an intent for the wrong cell")
            if any(
                not generation_start_ms <= intent.decision_ts_ms < evaluation_end_ms for intent in all_intents
            ):
                raise ValueError("sleeve generator emitted an intent outside its supplied data")
            in_window = [
                intent
                for intent in all_intents
                if evaluation_start_ms <= intent.decision_ts_ms < evaluation_end_ms
            ]
            warmup = [intent for intent in all_intents if intent.decision_ts_ms < evaluation_start_ms]
            if len(all_intents) != len(warmup) + len(in_window):
                raise RuntimeError("warmup and evaluation boundaries did not partition intents")
            in_window.sort(key=lambda intent: (intent.entry_eligible_ts_ms, intent.intent_id))
            generated_intents[(sleeve_id, symbol)] = (all_intents, warmup, in_window)

    cell_count = len(_SLEEVE_IDS) * len(SYMBOLS)
    cell_equity = float(initial_equity_usd) / cell_count
    scenario_evidence: list[DevelopmentScenarioEvidence] = []
    for scenario in selected_scenarios:
        cells: list[DevelopmentCellEvidence] = []
        for sleeve_id in _SLEEVE_IDS:
            for symbol in SYMBOLS:
                all_intents, warmup, in_window = generated_intents[(sleeve_id, symbol)]
                cell_id = f"{scenario.name}:{sleeve_id}:{symbol}"
                evaluation_seed = derive_seed(
                    seed,
                    "development-campaign-v2",
                    selected_candidate.candidate_sha256,
                    window_name,
                    start.isoformat(),
                    end.isoformat(),
                    scenario.name,
                    sleeve_id,
                    symbol,
                )
                result = evaluate_sleeve_cell(
                    evaluation_rows[symbol],
                    in_window,
                    cell_id=cell_id,
                    sleeve_id=sleeve_id,
                    symbol=symbol,
                    initial_equity_usd=cell_equity,
                    execution=scenario.execution,
                    costs=scenario.costs,
                    limits=selected_candidate.risk,
                    policy=scenario.policy,
                    seed=evaluation_seed,
                )
                cells.append(
                    DevelopmentCellEvidence(
                        scenario_name=scenario.name,
                        cell_id=cell_id,
                        sleeve_id=sleeve_id,
                        symbol=symbol,
                        initial_equity_usd=cell_equity,
                        generated_intents=len(all_intents),
                        evaluated_intents=len(in_window),
                        warmup_intents_filtered=len(warmup),
                        evaluation_seed=evaluation_seed,
                        candidate_sha256=selected_candidate.candidate_sha256,
                        dataset_sha256=dataset_by_symbol[symbol].candles_sha256,
                        evaluated_intents_sha256=_intent_inventory_sha256(in_window),
                        result=result,
                    )
                )
        immutable_cells = tuple(cells)
        scenario_evidence.append(
            DevelopmentScenarioEvidence(
                scenario=scenario,
                cells=immutable_cells,
                portfolio=synchronize_cells(tuple(cell.result.cell for cell in immutable_cells)),
            )
        )

    return DevelopmentCampaignEvidence(
        candidate=selected_candidate,
        protocol=protocol,
        protocol_name=protocol.protocol_name,
        protocol_sha256=protocol.fingerprint(),
        window_name=window_name,
        role=window.role,
        purpose=purpose,
        generation_start=generation_start,
        evaluation_start=start,
        evaluation_end=end,
        requested_initial_equity_usd=float(initial_equity_usd),
        cell_initial_equity_usd=cell_equity,
        datasets=tuple(dataset_evidence),
        scenarios=tuple(scenario_evidence),
        seed=seed,
    )
