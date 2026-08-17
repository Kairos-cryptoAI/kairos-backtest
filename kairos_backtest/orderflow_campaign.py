"""Immutable reused-data campaign for the order-flow expansion experiment.

This module deliberately has no promotion path.  It binds one complete
order-flow candidate to a fixed research-only dataset, evaluates the same
intent inventory under baseline and stress execution, and returns replay
evidence whose capital, data, scenario and seed relationships are checked on
construction.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from datetime import UTC, date, datetime
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
from .sleeves.orderflow_volatility_expansion import (
    OrderFlowVolatilityExpansionConfig,
    generate_orderflow_volatility_expansion_intents,
)
from .strategy_models import SleeveIntent
from .validation import canonical_candles

_MINUTE_MS = 60_000
_FIVE_MINUTES_MS = 5 * _MINUTE_MS
_HOUR_MS = 60 * _MINUTE_MS
_DAY_MS = 24 * _HOUR_MS
_MINUTES_PER_DAY = _DAY_MS // _MINUTE_MS
_SCENARIO_NAMES = ("baseline", "stress")
_SEED_NAMESPACE = "orderflow-campaign-v1"
_CANDIDATE_SCHEMA = "kairos.orderflow-candidate.v1"
_CANDIDATE_FAMILY = "orderflow_volatility_expansion"
_INTENT_PARTITION_SCHEMA = "kairos.orderflow-intent-partition.v1"

ORDERFLOW_SLEEVE_ID = "orderflow_volatility_expansion_v1"
ORDERFLOW_GENERATION_START = date(2022, 5, 27)
ORDERFLOW_EVALUATION_START = date(2022, 7, 1)
ORDERFLOW_EVALUATION_END = date(2023, 1, 1)
DEFAULT_ORDERFLOW_SEED = 42
ORDERFLOW_INITIAL_EQUITY_USD = 100_000.0


def _json_ready(value: object) -> object:
    """Return canonical JSON-compatible evidence and reject unsafe values."""

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
    """Normalize numerically equal values to one candidate/dataset hash."""

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
        allow_nan=False,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
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


def _generated_intents_sha256(
    *,
    warmup_intents: int,
    warmup_intents_sha256: str,
    evaluated_intents: int,
    evaluated_intents_sha256: str,
) -> str:
    """Commit to the complete generated inventory and its exact role partition."""

    for name, value in (
        ("warmup_intents_sha256", warmup_intents_sha256),
        ("evaluated_intents_sha256", evaluated_intents_sha256),
    ):
        _lowercase_sha256(name, value)
    for count_name, count in (
        ("warmup_intents", warmup_intents),
        ("evaluated_intents", evaluated_intents),
    ):
        if isinstance(count, bool) or not isinstance(count, int) or count < 0:
            raise ValueError(f"{count_name} must be a non-negative integer")
    return hashlib.sha256(
        _canonical_json_bytes(
            {
                "evaluation": {
                    "count": evaluated_intents,
                    "sha256": evaluated_intents_sha256,
                },
                "schema": _INTENT_PARTITION_SCHEMA,
                "warmup": {
                    "count": warmup_intents,
                    "sha256": warmup_intents_sha256,
                },
            }
        )
    ).hexdigest()


@dataclass(frozen=True, slots=True)
class OrderFlowCandidate:
    """One fully bound order-flow candidate using hash schema version one."""

    config: OrderFlowVolatilityExpansionConfig = field(default_factory=OrderFlowVolatilityExpansionConfig)
    risk: RiskLimits = field(default_factory=RiskLimits)
    terminal_liquidation_grace_ms: int = _HOUR_MS
    candidate_sha256: str = field(init=False)

    def __post_init__(self) -> None:
        if not isinstance(self.config, OrderFlowVolatilityExpansionConfig):
            raise TypeError("config must be an OrderFlowVolatilityExpansionConfig")
        if not isinstance(self.risk, RiskLimits):
            raise TypeError("risk must be RiskLimits")
        if self.maximum_holding_ms != _HOUR_MS:
            raise ValueError("order-flow candidate maximum hold must be exactly 60 minutes")
        if self.terminal_liquidation_grace_ms != _HOUR_MS:
            raise ValueError("order-flow candidate liquidation grace must be exactly 60 minutes")
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
        return self.config.max_hold_bars * _FIVE_MINUTES_MS

    @property
    def maximum_liquidation_horizon_ms(self) -> int:
        return self.maximum_holding_ms + self.terminal_liquidation_grace_ms

    def parameter_dict(self) -> dict[str, object]:
        return {
            "config": asdict(self.config),
            "family": _CANDIDATE_FAMILY,
            "risk": asdict(self.risk),
            "schema": _CANDIDATE_SCHEMA,
            "sleeve_id": ORDERFLOW_SLEEVE_ID,
            "terminal_liquidation_grace_ms": self.terminal_liquidation_grace_ms,
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
        raise ValueError("order-flow scenarios support only unavailable or assumed funding")
    if funding.rate_8h_bps is None:
        raise RuntimeError("validated assumed funding lost its configured rate")
    settlements = (maximum_liquidation_horizon_ms + funding.settlement_interval_ms - 1) // (
        funding.settlement_interval_ms
    )
    return settlements * funding.rate_8h_bps / 8


@dataclass(frozen=True, slots=True)
class OrderFlowScenario:
    """One fixed execution scenario and its matching cost hurdle."""

    name: str
    execution: ExecutionConfig
    costs: AllInCostModel
    policy: ManagedEvaluationPolicy
    limits: RiskLimits
    maximum_liquidation_horizon_ms: int

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not self.name or self.name != self.name.strip():
            raise ValueError("scenario name must be normalized non-empty text")
        if not isinstance(self.execution, ExecutionConfig):
            raise TypeError("scenario execution must be an ExecutionConfig")
        if not isinstance(self.costs, AllInCostModel):
            raise TypeError("scenario costs must be an AllInCostModel")
        if not isinstance(self.policy, ManagedEvaluationPolicy):
            raise TypeError("scenario policy must be a ManagedEvaluationPolicy")
        if not isinstance(self.limits, RiskLimits):
            raise TypeError("scenario limits must be RiskLimits")
        if self.maximum_liquidation_horizon_ms != 2 * _HOUR_MS:
            raise ValueError("scenario liquidation horizon must be exactly 120 minutes")
        expected = (
            (self.costs.fee_bps_per_side, self.execution.fee_bps),
            (self.costs.spread_bps, self.execution.spread_bps),
            (
                self.costs.slippage_bps_per_side,
                self.execution.slippage_bps + self.execution.slippage_jitter_bps,
            ),
            (
                self.costs.adverse_funding_bps,
                _maximum_adverse_funding_bps(
                    self.execution,
                    self.maximum_liquidation_horizon_ms,
                ),
            ),
        )
        if any(actual != required for actual, required in expected):
            raise ValueError("scenario costs must exactly match execution and funding assumptions")

    def to_dict(self) -> dict[str, object]:
        return cast(
            dict[str, object],
            _json_ready(
                {
                    "costs": asdict(self.costs),
                    "execution": asdict(self.execution),
                    "limits": asdict(self.limits),
                    "maximum_liquidation_horizon_ms": self.maximum_liquidation_horizon_ms,
                    "name": self.name,
                    "policy": asdict(self.policy),
                }
            ),
        )


def orderflow_scenarios(
    candidate: OrderFlowCandidate,
) -> tuple[OrderFlowScenario, OrderFlowScenario]:
    """Build the only admissible baseline/stress pair for this campaign."""

    if not isinstance(candidate, OrderFlowCandidate):
        raise TypeError("candidate must be an OrderFlowCandidate")

    def build(name: str, execution: ExecutionConfig) -> OrderFlowScenario:
        return OrderFlowScenario(
            name=name,
            execution=execution,
            costs=AllInCostModel(
                fee_bps_per_side=execution.fee_bps,
                spread_bps=execution.spread_bps,
                slippage_bps_per_side=(execution.slippage_bps + execution.slippage_jitter_bps),
                adverse_funding_bps=_maximum_adverse_funding_bps(
                    execution,
                    candidate.maximum_liquidation_horizon_ms,
                ),
                latency_bps=0.0,
                uncertainty_buffer_bps=2.0,
            ),
            policy=ManagedEvaluationPolicy(
                application_exit_latency_ms=execution.latency_ms,
                terminal_liquidation_grace_ms=candidate.terminal_liquidation_grace_ms,
            ),
            limits=candidate.risk,
            maximum_liquidation_horizon_ms=candidate.maximum_liquidation_horizon_ms,
        )

    return build("baseline", BASELINE), build("stress", STRESS)


DEFAULT_ORDERFLOW_PROTOCOL = ResearchProtocol(
    protocol_name="orderflow-volatility-expansion-development-v1",
    universe=SYMBOLS,
    windows=DEVELOPMENT_WINDOWS,
    max_trials=3,
    maximum_holding_ms=2 * _HOUR_MS,
    maximum_label_horizon_ms=2 * _HOUR_MS,
    maximum_execution_latency_ms=max(BASELINE.latency_ms, STRESS.latency_ms),
    warmup_ms=35 * _DAY_MS,
)


@dataclass(frozen=True, slots=True)
class OrderFlowDatasetEvidence:
    """Exact complete-minute dataset slice bound to one symbol."""

    symbol: str
    generation_start: date
    evaluation_start: date
    evaluation_end: date
    warmup_candles: int
    evaluation_candles: int
    warmup_zero_volume_candles: int
    evaluation_zero_volume_candles: int
    candles_sha256: str

    def __post_init__(self) -> None:
        if not isinstance(self.symbol, str) or self.symbol != self.symbol.strip().upper():
            raise ValueError("dataset symbol must be normalized uppercase text")
        if not self.symbol:
            raise ValueError("dataset symbol is required")
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
        for name, count, upper_bound in (
            ("warmup_zero_volume_candles", self.warmup_zero_volume_candles, self.warmup_candles),
            (
                "evaluation_zero_volume_candles",
                self.evaluation_zero_volume_candles,
                self.evaluation_candles,
            ),
        ):
            if isinstance(count, bool) or not isinstance(count, int) or not 0 <= count <= upper_bound:
                raise ValueError(f"dataset {name} must be an in-range integer count")
        _lowercase_sha256("dataset candles_sha256", self.candles_sha256)


@dataclass(frozen=True, slots=True)
class OrderFlowCellEvidence:
    """One scenario-qualified replay cell with all input bindings."""

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
    generated_intents_sha256: str
    warmup_intents_sha256: str
    evaluated_intents_sha256: str
    result: ManagedCellResult

    def __post_init__(self) -> None:
        if not isinstance(self.result, ManagedCellResult):
            raise TypeError("cell result must be a ManagedCellResult")
        if self.sleeve_id != ORDERFLOW_SLEEVE_ID:
            raise ValueError("cell sleeve must be the fixed order-flow sleeve")
        if self.symbol not in SYMBOLS:
            raise ValueError("cell symbol must belong to the fixed universe")
        if isinstance(self.evaluation_seed, bool) or not isinstance(self.evaluation_seed, int):
            raise TypeError("cell evaluation seed must be an integer")
        if self.result.assumptions.seed != self.evaluation_seed:
            raise ValueError("cell result seed does not match its declared evaluation seed")
        for name, value in (
            ("cell candidate_sha256", self.candidate_sha256),
            ("cell dataset_sha256", self.dataset_sha256),
            ("cell generated_intents_sha256", self.generated_intents_sha256),
            ("cell warmup_intents_sha256", self.warmup_intents_sha256),
            ("cell evaluated_intents_sha256", self.evaluated_intents_sha256),
        ):
            _lowercase_sha256(name, value)
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
        inventory = tuple(disposition.intent for disposition in self.result.dispositions)
        if self.evaluated_intents_sha256 != _intent_inventory_sha256(inventory):
            raise ValueError("cell intent inventory does not match its managed dispositions")
        expected_generated_sha256 = _generated_intents_sha256(
            warmup_intents=self.warmup_intents_filtered,
            warmup_intents_sha256=self.warmup_intents_sha256,
            evaluated_intents=self.evaluated_intents,
            evaluated_intents_sha256=self.evaluated_intents_sha256,
        )
        if self.generated_intents_sha256 != expected_generated_sha256:
            raise ValueError("generated intent inventory does not match its exact role partition")


@dataclass(frozen=True, slots=True)
class OrderFlowScenarioEvidence:
    """The exact five-symbol evidence grid for one execution scenario."""

    scenario: OrderFlowScenario
    cells: tuple[OrderFlowCellEvidence, ...]
    portfolio: PortfolioEvidence

    def __post_init__(self) -> None:
        if not isinstance(self.scenario, OrderFlowScenario):
            raise TypeError("scenario evidence requires an OrderFlowScenario")
        if not isinstance(self.cells, tuple) or any(
            not isinstance(cell, OrderFlowCellEvidence) for cell in self.cells
        ):
            raise TypeError("scenario cells must be immutable OrderFlowCellEvidence values")
        if len(self.cells) != len(SYMBOLS):
            raise ValueError("scenario evidence requires exactly five order-flow cells")
        if tuple((cell.sleeve_id, cell.symbol) for cell in self.cells) != tuple(
            (ORDERFLOW_SLEEVE_ID, symbol) for symbol in SYMBOLS
        ):
            raise ValueError("scenario evidence has an incomplete or unordered five-symbol grid")
        if any(cell.scenario_name != self.scenario.name for cell in self.cells):
            raise ValueError("every cell must belong to its declared scenario")
        if len({cell.cell_id for cell in self.cells}) != len(self.cells):
            raise ValueError("scenario cell identities must be unique")
        for cell in self.cells:
            expected_cell_id = f"{self.scenario.name}:{ORDERFLOW_SLEEVE_ID}:{cell.symbol}"
            if cell.cell_id != expected_cell_id:
                raise ValueError("scenario cell identity must match its fixed grid coordinates")
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
class OrderFlowCampaignEvidence:
    """Research-only campaign evidence with permissions fixed to false."""

    candidate: OrderFlowCandidate
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
    datasets: tuple[OrderFlowDatasetEvidence, ...]
    scenarios: tuple[OrderFlowScenarioEvidence, ...]
    seed: int
    development_only: bool = field(init=False, default=True)
    reused_data: bool = field(init=False, default=True)
    out_of_sample: bool = field(init=False, default=False)
    promotion_eligible: bool = field(init=False, default=False)
    shadow_allowed: bool = field(init=False, default=False)
    live_allowed: bool = field(init=False, default=False)

    def __post_init__(self) -> None:
        if not isinstance(self.candidate, OrderFlowCandidate):
            raise TypeError("campaign candidate must be an OrderFlowCandidate")
        _validate_protocol(self.protocol, self.candidate)
        if self.protocol_name != self.protocol.protocol_name:
            raise ValueError("campaign protocol name does not match its canonical payload")
        _lowercase_sha256("campaign protocol_sha256", self.protocol_sha256)
        if self.protocol_sha256 != self.protocol.fingerprint():
            raise ValueError("campaign protocol fingerprint does not match its canonical payload")
        if (self.window_name, self.role, self.purpose) != (
            "research",
            DataRole.RESEARCH,
            ResearchPurpose.FIT,
        ):
            raise ValueError("order-flow campaign is fixed to the research/FIT role")
        window = self.protocol.assert_access(self.window_name, self.purpose)
        if window.role is not self.role:
            raise ValueError("campaign role does not match its registered window")
        if (
            self.generation_start,
            self.evaluation_start,
            self.evaluation_end,
        ) != (
            ORDERFLOW_GENERATION_START,
            ORDERFLOW_EVALUATION_START,
            ORDERFLOW_EVALUATION_END,
        ):
            raise ValueError("campaign data bounds do not match the fixed research screen")
        if self.seed != DEFAULT_ORDERFLOW_SEED:
            raise ValueError("campaign seed must remain fixed at 42")
        if not math.isclose(
            self.requested_initial_equity_usd,
            ORDERFLOW_INITIAL_EQUITY_USD,
            rel_tol=0.0,
            abs_tol=0.0,
        ):
            raise ValueError("campaign capital must remain fixed at 100,000 USD")
        expected_cell_equity = ORDERFLOW_INITIAL_EQUITY_USD / len(SYMBOLS)
        if not math.isclose(
            self.cell_initial_equity_usd,
            expected_cell_equity,
            rel_tol=0.0,
            abs_tol=0.0,
        ):
            raise ValueError("campaign cell capital must be the exact five-way allocation")
        if not isinstance(self.datasets, tuple) or any(
            not isinstance(dataset, OrderFlowDatasetEvidence) for dataset in self.datasets
        ):
            raise TypeError("campaign datasets must be immutable OrderFlowDatasetEvidence values")
        if tuple(dataset.symbol for dataset in self.datasets) != SYMBOLS:
            raise ValueError("dataset evidence must follow the fixed five-symbol universe")
        if any(
            (
                dataset.generation_start,
                dataset.evaluation_start,
                dataset.evaluation_end,
            )
            != (
                self.generation_start,
                self.evaluation_start,
                self.evaluation_end,
            )
            for dataset in self.datasets
        ):
            raise ValueError("all dataset evidence must match the campaign data bounds")
        if not isinstance(self.scenarios, tuple) or any(
            not isinstance(item, OrderFlowScenarioEvidence) for item in self.scenarios
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

        dataset_by_symbol = {dataset.symbol: dataset for dataset in self.datasets}
        baseline_inventory = {
            (cell.sleeve_id, cell.symbol): (
                cell.generated_intents,
                cell.evaluated_intents,
                cell.warmup_intents_filtered,
                cell.generated_intents_sha256,
                cell.warmup_intents_sha256,
                cell.evaluated_intents_sha256,
            )
            for cell in self.scenarios[0].cells
        }
        for scenario in self.scenarios:
            if not math.isclose(
                scenario.portfolio.initial_equity_usd,
                ORDERFLOW_INITIAL_EQUITY_USD,
                rel_tol=1e-12,
                abs_tol=0.0,
            ):
                raise ValueError("each scenario must receive the same fixed portfolio capital")
            for cell in scenario.cells:
                if cell.initial_equity_usd != expected_cell_equity:
                    raise ValueError("campaign cells must use equal fixed capital")
                if cell.candidate_sha256 != self.candidate.candidate_sha256:
                    raise ValueError("cell candidate fingerprint does not match the campaign")
                if cell.dataset_sha256 != dataset_by_symbol[cell.symbol].candles_sha256:
                    raise ValueError("cell dataset fingerprint does not match the campaign")
                expected_seed = derive_seed(
                    self.seed,
                    _SEED_NAMESPACE,
                    self.protocol_sha256,
                    self.candidate.candidate_sha256,
                    dataset_by_symbol[cell.symbol].candles_sha256,
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
                inventory = (
                    cell.generated_intents,
                    cell.evaluated_intents,
                    cell.warmup_intents_filtered,
                    cell.generated_intents_sha256,
                    cell.warmup_intents_sha256,
                    cell.evaluated_intents_sha256,
                )
                if inventory != baseline_inventory[(cell.sleeve_id, cell.symbol)]:
                    raise ValueError("baseline and stress must evaluate the same intent inventory")

    def to_dict(self) -> dict[str, object]:
        scenarios = []
        for scenario in self.scenarios:
            cells = []
            for cell in scenario.cells:
                cells.append(
                    {
                        "candidate_sha256": cell.candidate_sha256,
                        "cell_id": cell.cell_id,
                        "dataset_sha256": cell.dataset_sha256,
                        "evaluated_intents": cell.evaluated_intents,
                        "evaluated_intents_sha256": cell.evaluated_intents_sha256,
                        "evaluation_seed": cell.evaluation_seed,
                        "generated_intents": cell.generated_intents,
                        "generated_intents_sha256": cell.generated_intents_sha256,
                        "initial_equity_usd": cell.initial_equity_usd,
                        "managed_result": asdict(cell.result),
                        "scenario_name": cell.scenario_name,
                        "sleeve_id": cell.sleeve_id,
                        "symbol": cell.symbol,
                        "warmup_intents_filtered": cell.warmup_intents_filtered,
                        "warmup_intents_sha256": cell.warmup_intents_sha256,
                    }
                )
            scenarios.append(
                {
                    "cells": cells,
                    "definition": scenario.scenario.to_dict(),
                    "portfolio": asdict(scenario.portfolio),
                }
            )
        payload = {
            "allocation": {
                "cell_initial_equity_usd": self.cell_initial_equity_usd,
                "cells_per_scenario": len(SYMBOLS),
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
            "development_only": self.development_only,
            "out_of_sample": self.out_of_sample,
            "permissions": {
                "live_allowed": self.live_allowed,
                "promotion_eligible": self.promotion_eligible,
                "shadow_allowed": self.shadow_allowed,
            },
            "promotion_eligible": self.promotion_eligible,
            "protocol": {
                "definition": asdict(self.protocol),
                "name": self.protocol_name,
                "sha256": self.protocol_sha256,
            },
            "reused_data": self.reused_data,
            "scenarios": scenarios,
            "schema_version": 1,
            "seed": self.seed,
        }
        return cast(dict[str, object], _json_ready(payload))


def _validate_protocol(protocol: ResearchProtocol, candidate: OrderFlowCandidate) -> None:
    if not isinstance(protocol, ResearchProtocol):
        raise TypeError("protocol must be a ResearchProtocol")
    if protocol != DEFAULT_ORDERFLOW_PROTOCOL:
        raise ValueError("order-flow campaign requires the fixed v1 research protocol")
    if protocol.max_trials != 3:
        raise ValueError("order-flow protocol must keep exactly three trials")
    if protocol.warmup_ms != 35 * _DAY_MS:
        raise ValueError("order-flow protocol must keep the 35-day warmup")
    if protocol.maximum_holding_ms != 2 * _HOUR_MS:
        raise ValueError("order-flow protocol maximum holding bound must be two hours")
    if protocol.maximum_label_horizon_ms != 2 * _HOUR_MS:
        raise ValueError("order-flow protocol maximum label bound must be two hours")
    if candidate.maximum_liquidation_horizon_ms != 2 * _HOUR_MS:
        raise ValueError("candidate liquidation horizon must be exactly two hours")


def _validate_scenarios(
    scenarios: tuple[OrderFlowScenario, ...],
    *,
    candidate: OrderFlowCandidate,
    protocol: ResearchProtocol,
) -> None:
    if not isinstance(scenarios, tuple) or any(
        not isinstance(scenario, OrderFlowScenario) for scenario in scenarios
    ):
        raise TypeError("scenarios must be an immutable tuple of OrderFlowScenario values")
    if tuple(scenario.name for scenario in scenarios) != _SCENARIO_NAMES:
        raise ValueError("order-flow scenarios must be ordered baseline and stress")
    if scenarios != orderflow_scenarios(candidate):
        raise ValueError("order-flow scenarios must exactly match the fixed candidate factory")
    baseline, stress = scenarios
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
    if stress.execution.funding.evidence != "assumed":
        raise ValueError("stress funding must be an explicit adverse assumption")
    for scenario in scenarios:
        if scenario.maximum_liquidation_horizon_ms != candidate.maximum_liquidation_horizon_ms:
            raise ValueError("scenario horizon must match the candidate liquidation bound")
        if scenario.policy.terminal_liquidation_grace_ms != candidate.terminal_liquidation_grace_ms:
            raise ValueError("scenario grace must match the candidate")
        if scenario.policy.application_exit_latency_ms != scenario.execution.latency_ms:
            raise ValueError("scenario entry and application-exit latency must match")
        if scenario.limits != candidate.risk:
            raise ValueError("scenario limits must match the candidate")
        if scenario.execution.latency_ms > protocol.maximum_execution_latency_ms:
            raise ValueError("scenario latency exceeds the fixed research protocol")


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


def _dataset_sha256(candles: list[Candle]) -> str:
    digest = hashlib.sha256(b"orderflow-dataset-v1\0")
    for candle in candles:
        digest.update(_canonical_json_bytes(_candle_payload(candle)))
        digest.update(b"\n")
    return digest.hexdigest()


def _validate_generated_intent(
    intent: SleeveIntent,
    *,
    candidate: OrderFlowCandidate,
    symbol: str,
) -> None:
    """Bind generator output to the complete candidate and causal 5m contract."""

    if intent.sleeve_id != ORDERFLOW_SLEEVE_ID or intent.symbol != symbol:
        raise ValueError("order-flow generator emitted an intent for the wrong cell")
    if intent.exit_plan.max_holding_ms != candidate.maximum_holding_ms:
        raise ValueError("order-flow intent holding bound does not match its candidate")
    if intent.entry_eligible_ts_ms != intent.decision_ts_ms + 1:
        raise ValueError("order-flow intent must enter no earlier than the next five-minute open")
    if intent.entry_eligible_ts_ms % _FIVE_MINUTES_MS:
        raise ValueError("order-flow intent entry must align with a five-minute open")
    expected_expiry = intent.decision_ts_ms + candidate.config.intent_valid_bars * _FIVE_MINUTES_MS
    if intent.entry_expires_ts_ms != expected_expiry:
        raise ValueError("order-flow intent expiry does not match its candidate")
    metadata = dict(intent.metadata)
    expected_metadata = {
        "config_sha256": candidate.config.fingerprint,
        "strategy_version": ORDERFLOW_SLEEVE_ID,
        "variant": candidate.config.variant.value,
    }
    if any(metadata.get(key) != value for key, value in expected_metadata.items()):
        raise ValueError("order-flow intent metadata does not match its candidate")


def _slice_symbol(
    candles: list[Candle],
    *,
    symbol: str,
    generation_start: date,
    evaluation_start: date,
    evaluation_end: date,
) -> tuple[list[Candle], list[Candle], OrderFlowDatasetEvidence]:
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
        raise ValueError(f"{symbol} selected order-flow data are incomplete; gaps are not imputed")
    for index, candle in enumerate(selected):
        if (
            candle.volume < 0
            or candle.quote_volume < 0
            or candle.taker_buy_volume < 0
            or candle.taker_buy_volume > candle.volume
        ):
            raise ValueError(f"{symbol} selected order-flow volume evidence must be non-negative and bounded")
        expected_open = generation_start_ms + index * _MINUTE_MS
        if candle.open_time_ms != expected_open or candle.close_time_ms != expected_open + _MINUTE_MS - 1:
            raise ValueError(f"{symbol} selected order-flow data must be contiguous aligned minutes")
    evaluation_index = (evaluation_start_ms - generation_start_ms) // _MINUTE_MS
    evaluation = selected[evaluation_index:]
    expected_evaluation_count = (evaluation_end_ms - evaluation_start_ms) // _MINUTE_MS
    if len(evaluation) != expected_evaluation_count or len(evaluation) % _MINUTES_PER_DAY:
        raise RuntimeError("validated order-flow slice lost complete UTC evaluation days")
    warmup = selected[:evaluation_index]
    evidence = OrderFlowDatasetEvidence(
        symbol=symbol,
        generation_start=generation_start,
        evaluation_start=evaluation_start,
        evaluation_end=evaluation_end,
        warmup_candles=len(warmup),
        evaluation_candles=len(evaluation),
        warmup_zero_volume_candles=sum(candle.volume == 0 for candle in warmup),
        evaluation_zero_volume_candles=sum(candle.volume == 0 for candle in evaluation),
        candles_sha256=_dataset_sha256(selected),
    )
    return selected, evaluation, evidence


def run_orderflow_campaign(
    candles_by_symbol: Mapping[str, list[Candle]],
    *,
    candidate: OrderFlowCandidate | None = None,
    protocol: ResearchProtocol = DEFAULT_ORDERFLOW_PROTOCOL,
    initial_equity_usd: float = ORDERFLOW_INITIAL_EQUITY_USD,
    scenarios: tuple[OrderFlowScenario, ...] | None = None,
    seed: int = DEFAULT_ORDERFLOW_SEED,
) -> OrderFlowCampaignEvidence:
    """Evaluate the fixed five-symbol order-flow grid on reused research data."""

    selected_candidate = OrderFlowCandidate() if candidate is None else candidate
    if not isinstance(selected_candidate, OrderFlowCandidate):
        raise TypeError("candidate must be an OrderFlowCandidate")
    _validate_protocol(protocol, selected_candidate)
    if isinstance(seed, bool) or not isinstance(seed, int):
        raise TypeError("seed must be an integer")
    if seed != DEFAULT_ORDERFLOW_SEED:
        raise ValueError("order-flow campaign seed is fixed at 42")
    if (
        isinstance(initial_equity_usd, bool)
        or not isinstance(initial_equity_usd, (int, float))
        or not math.isfinite(initial_equity_usd)
        or float(initial_equity_usd) != ORDERFLOW_INITIAL_EQUITY_USD
    ):
        raise ValueError("order-flow campaign equity is fixed at 100,000 USD")
    if not isinstance(candles_by_symbol, Mapping):
        raise TypeError("candles_by_symbol must be a mapping")
    if set(candles_by_symbol) != set(SYMBOLS):
        raise ValueError("candles_by_symbol must contain exactly the fixed five-symbol universe")

    window = protocol.assert_access("research", ResearchPurpose.FIT)
    if not (window.start <= ORDERFLOW_EVALUATION_START < ORDERFLOW_EVALUATION_END <= window.end):
        raise ValueError("fixed order-flow bounds must remain inside the research role")
    selected_scenarios = orderflow_scenarios(selected_candidate) if scenarios is None else scenarios
    _validate_scenarios(
        selected_scenarios,
        candidate=selected_candidate,
        protocol=protocol,
    )

    generation_rows: dict[str, list[Candle]] = {}
    evaluation_rows: dict[str, list[Candle]] = {}
    datasets: list[OrderFlowDatasetEvidence] = []
    for symbol in SYMBOLS:
        generated, evaluated, evidence = _slice_symbol(
            candles_by_symbol[symbol],
            symbol=symbol,
            generation_start=ORDERFLOW_GENERATION_START,
            evaluation_start=ORDERFLOW_EVALUATION_START,
            evaluation_end=ORDERFLOW_EVALUATION_END,
        )
        generation_rows[symbol] = generated
        evaluation_rows[symbol] = evaluated
        datasets.append(evidence)
    dataset_by_symbol = {dataset.symbol: dataset for dataset in datasets}

    generation_start_ms = _utc_ms(ORDERFLOW_GENERATION_START)
    evaluation_start_ms = _utc_ms(ORDERFLOW_EVALUATION_START)
    evaluation_end_ms = _utc_ms(ORDERFLOW_EVALUATION_END)
    intent_sets: dict[str, tuple[list[SleeveIntent], list[SleeveIntent], list[SleeveIntent]]] = {}
    for symbol in SYMBOLS:
        all_intents = generate_orderflow_volatility_expansion_intents(
            generation_rows[symbol],
            selected_candidate.config,
        )
        if not isinstance(all_intents, list) or any(
            not isinstance(intent, SleeveIntent) for intent in all_intents
        ):
            raise TypeError("order-flow generator must return a list of SleeveIntent values")
        for intent in all_intents:
            _validate_generated_intent(
                intent,
                candidate=selected_candidate,
                symbol=symbol,
            )
        if any(
            not generation_start_ms <= intent.decision_ts_ms < evaluation_end_ms for intent in all_intents
        ):
            raise ValueError("order-flow generator emitted an intent outside its supplied data")
        all_intents.sort(
            key=lambda intent: (
                intent.decision_ts_ms,
                intent.entry_eligible_ts_ms,
                intent.intent_id,
            )
        )
        in_window = [
            intent
            for intent in all_intents
            if evaluation_start_ms <= intent.decision_ts_ms < evaluation_end_ms
        ]
        warmup = [intent for intent in all_intents if intent.decision_ts_ms < evaluation_start_ms]
        if len(all_intents) != len(warmup) + len(in_window):
            raise RuntimeError("warmup and evaluation bounds did not partition order-flow intents")
        if tuple(intent.intent_id for intent in all_intents) != tuple(
            intent.intent_id for intent in (*warmup, *in_window)
        ):
            raise RuntimeError("generated order-flow inventory lost its exact role partition")
        intent_sets[symbol] = (all_intents, warmup, in_window)

    cell_equity = ORDERFLOW_INITIAL_EQUITY_USD / len(SYMBOLS)
    scenario_evidence: list[OrderFlowScenarioEvidence] = []
    for scenario in selected_scenarios:
        cells: list[OrderFlowCellEvidence] = []
        for symbol in SYMBOLS:
            all_intents, warmup, in_window = intent_sets[symbol]
            warmup_intents_sha256 = _intent_inventory_sha256(warmup)
            evaluated_intents_sha256 = _intent_inventory_sha256(in_window)
            generated_intents_sha256 = _generated_intents_sha256(
                warmup_intents=len(warmup),
                warmup_intents_sha256=warmup_intents_sha256,
                evaluated_intents=len(in_window),
                evaluated_intents_sha256=evaluated_intents_sha256,
            )
            cell_id = f"{scenario.name}:{ORDERFLOW_SLEEVE_ID}:{symbol}"
            evaluation_seed = derive_seed(
                seed,
                _SEED_NAMESPACE,
                protocol.fingerprint(),
                selected_candidate.candidate_sha256,
                dataset_by_symbol[symbol].candles_sha256,
                "research",
                ORDERFLOW_EVALUATION_START.isoformat(),
                ORDERFLOW_EVALUATION_END.isoformat(),
                scenario.name,
                ORDERFLOW_SLEEVE_ID,
                symbol,
            )
            result = evaluate_sleeve_cell(
                evaluation_rows[symbol],
                in_window,
                cell_id=cell_id,
                sleeve_id=ORDERFLOW_SLEEVE_ID,
                symbol=symbol,
                initial_equity_usd=cell_equity,
                execution=scenario.execution,
                costs=scenario.costs,
                limits=selected_candidate.risk,
                policy=scenario.policy,
                seed=evaluation_seed,
            )
            cells.append(
                OrderFlowCellEvidence(
                    scenario_name=scenario.name,
                    cell_id=cell_id,
                    sleeve_id=ORDERFLOW_SLEEVE_ID,
                    symbol=symbol,
                    initial_equity_usd=cell_equity,
                    generated_intents=len(all_intents),
                    evaluated_intents=len(in_window),
                    warmup_intents_filtered=len(warmup),
                    evaluation_seed=evaluation_seed,
                    candidate_sha256=selected_candidate.candidate_sha256,
                    dataset_sha256=dataset_by_symbol[symbol].candles_sha256,
                    generated_intents_sha256=generated_intents_sha256,
                    warmup_intents_sha256=warmup_intents_sha256,
                    evaluated_intents_sha256=evaluated_intents_sha256,
                    result=result,
                )
            )
        immutable_cells = tuple(cells)
        scenario_evidence.append(
            OrderFlowScenarioEvidence(
                scenario=scenario,
                cells=immutable_cells,
                portfolio=synchronize_cells(tuple(cell.result.cell for cell in immutable_cells)),
            )
        )

    return OrderFlowCampaignEvidence(
        candidate=selected_candidate,
        protocol=protocol,
        protocol_name=protocol.protocol_name,
        protocol_sha256=protocol.fingerprint(),
        window_name="research",
        role=DataRole.RESEARCH,
        purpose=ResearchPurpose.FIT,
        generation_start=ORDERFLOW_GENERATION_START,
        evaluation_start=ORDERFLOW_EVALUATION_START,
        evaluation_end=ORDERFLOW_EVALUATION_END,
        requested_initial_equity_usd=ORDERFLOW_INITIAL_EQUITY_USD,
        cell_initial_equity_usd=cell_equity,
        datasets=tuple(datasets),
        scenarios=tuple(scenario_evidence),
        seed=seed,
    )
