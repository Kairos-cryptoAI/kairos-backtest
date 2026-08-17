"""Immutable research campaign for the regime-veto retest/reclaim family.

The campaign is intentionally isolated from every historical candidate and
artifact schema.  It evaluates one fully bound candidate on one frozen
RESEARCH/FIT slice, replays an identical intent inventory under baseline and
stress execution, and cannot authorize promotion, shadow, or live trading.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from datetime import UTC, date, datetime
from enum import Enum
from typing import ClassVar, cast

from kairos_core.enums import Side
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
from .sleeves.regime_retest_reclaim import (
    RegimeRetestGenerationEvidence,
    RegimeRetestReclaimVariant,
    RegimeVetoRetestReclaimConfig,
    generate_regime_veto_retest_reclaim_evidence,
)
from .strategy_models import SleeveIntent
from .validation import canonical_candles

_MINUTE_MS = 60_000
_FIVE_MINUTES_MS = 5 * _MINUTE_MS
_HOUR_MS = 60 * _MINUTE_MS
_DAY_MS = 24 * _HOUR_MS
_MINUTES_PER_DAY = _DAY_MS // _MINUTE_MS
_SCENARIO_NAMES = ("baseline", "stress")
_SEED_NAMESPACE = "regime-veto-retest-campaign-v1"
_CANDIDATE_SCHEMA = "kairos.regime-veto-retest-candidate.v1"
_CANDIDATE_FAMILY = "regime_veto_retest_reclaim"
_INTENT_PARTITION_SCHEMA = "kairos.regime-veto-retest-intent-partition.v1"
_DATASET_SCHEMA = b"kairos.regime-veto-retest-dataset.v1\0"
_DATASET_EVIDENCE_SCHEMA = "kairos.regime-veto-retest-dataset-evidence.v1"
_GENERATION_EVIDENCE_BINDING_SCHEMA = "kairos.regime-veto-retest-generation-binding.v1"
_CANONICAL_RESERVED_MAPPING_KEYS = frozenset({"__float_hex__"})
_LONG_POSITION_HORIZON_MS = 90 * _MINUTE_MS
_SHORT_POSITION_HORIZON_MS = 60 * _MINUTE_MS
_TERMINAL_GRACE_MS = 60 * _MINUTE_MS
_MAXIMUM_LIQUIDATION_HORIZON_MS = 9_000_000
_MAXIMUM_LABEL_HORIZON_MS = 9_060_000
_MAXIMUM_EXECUTION_LATENCY_MS = 500

REGIME_RETEST_SLEEVE_ID = "regime_veto_retest_reclaim_v1"
REGIME_RETEST_GENERATION_START = date(2023, 12, 1)
REGIME_RETEST_EVALUATION_START = date(2024, 2, 1)
REGIME_RETEST_EVALUATION_END = date(2024, 7, 1)
DEFAULT_REGIME_RETEST_SEED = 44
REGIME_RETEST_INITIAL_EQUITY_USD = 100_000.0
REGIME_RETEST_OPERATIONAL_HORIZON_MS = 152 * _MINUTE_MS
REGIME_RETEST_WINDOW_RATIONALE = (
    "start after the known pre-existing invalid XRP minute in November 2023; no imputation"
)
REGIME_RETEST_VARIANTS = (
    RegimeRetestReclaimVariant.STRUCTURAL_RECLAIM,
    RegimeRetestReclaimVariant.FLOW_REACCELERATION,
    RegimeRetestReclaimVariant.ABSORPTION_RECLAIM,
)


def _json_ready(value: object) -> object:
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
        reserved_keys = _CANONICAL_RESERVED_MAPPING_KEYS.intersection(value)
        if reserved_keys:
            raise ValueError(
                "canonical hash mappings cannot use reserved marker keys: " + ", ".join(sorted(reserved_keys))
            )
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


def _validate_ordered_intent_ids(name: str, intent_ids: tuple[str, ...]) -> None:
    if not isinstance(intent_ids, tuple):
        raise TypeError(f"{name} must be an immutable tuple")
    for intent_id in intent_ids:
        _lowercase_sha256(f"{name} item", intent_id)
    if len(intent_ids) != len(set(intent_ids)):
        raise ValueError(f"{name} must contain unique intent IDs")


def _intent_ids_sha256(intent_ids: tuple[str, ...]) -> str:
    _validate_ordered_intent_ids("intent_ids", intent_ids)
    return hashlib.sha256(_canonical_json_bytes(intent_ids)).hexdigest()


def _intent_inventory_sha256(intents: tuple[SleeveIntent, ...] | list[SleeveIntent]) -> str:
    return _intent_ids_sha256(tuple(intent.intent_id for intent in intents))


def _generated_intents_sha256(
    *,
    warmup_intents: int,
    warmup_intents_sha256: str,
    evaluated_intents: int,
    evaluated_intents_sha256: str,
    terminal_embargo_intents: int,
    terminal_embargo_intents_sha256: str,
) -> str:
    for name, value in (
        ("warmup_intents_sha256", warmup_intents_sha256),
        ("evaluated_intents_sha256", evaluated_intents_sha256),
        ("terminal_embargo_intents_sha256", terminal_embargo_intents_sha256),
    ):
        _lowercase_sha256(name, value)
    for name, count in (
        ("warmup_intents", warmup_intents),
        ("evaluated_intents", evaluated_intents),
        ("terminal_embargo_intents", terminal_embargo_intents),
    ):
        if isinstance(count, bool) or not isinstance(count, int) or count < 0:
            raise ValueError(f"{name} must be a non-negative integer")
    return hashlib.sha256(
        _canonical_json_bytes(
            {
                "evaluation": {
                    "count": evaluated_intents,
                    "sha256": evaluated_intents_sha256,
                },
                "schema": _INTENT_PARTITION_SCHEMA,
                "terminal_embargo": {
                    "count": terminal_embargo_intents,
                    "sha256": terminal_embargo_intents_sha256,
                },
                "warmup": {
                    "count": warmup_intents,
                    "sha256": warmup_intents_sha256,
                },
            }
        )
    ).hexdigest()


def _generation_evidence_sha256(
    *,
    generation_evidence: RegimeRetestGenerationEvidence,
    candidate_sha256: str,
    dataset_sha256: str,
    dataset_evidence_sha256: str,
    symbol: str,
    warmup_intent_ids: tuple[str, ...],
    evaluated_intent_ids: tuple[str, ...],
    terminal_embargo_intent_ids: tuple[str, ...],
) -> str:
    if not isinstance(generation_evidence, RegimeRetestGenerationEvidence):
        raise TypeError("generation_evidence must be RegimeRetestGenerationEvidence")
    _lowercase_sha256("generation candidate_sha256", candidate_sha256)
    _lowercase_sha256("generation dataset_sha256", dataset_sha256)
    _lowercase_sha256(
        "generation dataset_evidence_sha256",
        dataset_evidence_sha256,
    )
    for name, intent_ids in (
        ("warmup_intent_ids", warmup_intent_ids),
        ("evaluated_intent_ids", evaluated_intent_ids),
        ("terminal_embargo_intent_ids", terminal_embargo_intent_ids),
    ):
        _validate_ordered_intent_ids(name, intent_ids)
    return hashlib.sha256(
        _canonical_json_bytes(
            {
                "candidate_sha256": candidate_sha256,
                "dataset_sha256": dataset_sha256,
                "dataset_evidence_sha256": dataset_evidence_sha256,
                "generation_evidence": generation_evidence.to_dict(),
                "intent_partitions": {
                    "evaluated": evaluated_intent_ids,
                    "terminal_embargo": terminal_embargo_intent_ids,
                    "warmup": warmup_intent_ids,
                },
                "schema": _GENERATION_EVIDENCE_BINDING_SCHEMA,
                "sleeve_id": REGIME_RETEST_SLEEVE_ID,
                "symbol": symbol,
            }
        )
    ).hexdigest()


@dataclass(frozen=True, slots=True)
class RegimeRetestCandidate:
    """One fully bound, domain-separated regime/retest candidate."""

    config: RegimeVetoRetestReclaimConfig = field(default_factory=RegimeVetoRetestReclaimConfig)
    risk: RiskLimits = field(default_factory=RiskLimits)
    terminal_liquidation_grace_ms: int = _TERMINAL_GRACE_MS
    candidate_sha256: str = field(init=False)

    def __post_init__(self) -> None:
        if not isinstance(self.config, RegimeVetoRetestReclaimConfig):
            raise TypeError("config must be a RegimeVetoRetestReclaimConfig")
        if self.config.variant not in REGIME_RETEST_VARIANTS:
            raise ValueError("candidate variant must be one of the three frozen variants")
        if self.config != RegimeVetoRetestReclaimConfig(variant=self.config.variant):
            raise ValueError("candidate config must match its exact frozen variant")
        if not isinstance(self.risk, RiskLimits):
            raise TypeError("risk must be RiskLimits")
        if self.risk != RiskLimits():
            raise ValueError("regime/retest risk limits are frozen at the v1 defaults")
        if self.config.maximum_holding_ms != _LONG_POSITION_HORIZON_MS:
            raise ValueError("candidate maximum position horizon must be exactly 90 minutes")
        if self.config.long_max_hold_bars * _FIVE_MINUTES_MS != _LONG_POSITION_HORIZON_MS:
            raise ValueError("candidate long holding horizon must be exactly 90 minutes")
        if self.config.short_max_hold_bars * _FIVE_MINUTES_MS != _SHORT_POSITION_HORIZON_MS:
            raise ValueError("candidate short holding horizon must be exactly 60 minutes")
        if self.terminal_liquidation_grace_ms != _TERMINAL_GRACE_MS:
            raise ValueError("candidate liquidation grace must be exactly 60 minutes")
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
        return self.config.maximum_holding_ms

    @property
    def maximum_liquidation_horizon_ms(self) -> int:
        return self.maximum_holding_ms + self.terminal_liquidation_grace_ms

    @property
    def maximum_label_horizon_ms(self) -> int:
        return self.maximum_liquidation_horizon_ms + _MINUTE_MS

    @property
    def operational_horizon_ms(self) -> int:
        return REGIME_RETEST_OPERATIONAL_HORIZON_MS

    def parameter_dict(self) -> dict[str, object]:
        return {
            "config": asdict(self.config),
            "family": _CANDIDATE_FAMILY,
            "risk": asdict(self.risk),
            "schema": _CANDIDATE_SCHEMA,
            "sleeve_id": REGIME_RETEST_SLEEVE_ID,
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
        raise ValueError("regime/retest scenarios support unavailable or assumed funding only")
    if funding.rate_8h_bps is None:
        raise RuntimeError("validated assumed funding lost its configured rate")
    settlements = (maximum_liquidation_horizon_ms + funding.settlement_interval_ms - 1) // (
        funding.settlement_interval_ms
    )
    return settlements * funding.rate_8h_bps / 8


@dataclass(frozen=True, slots=True)
class RegimeRetestScenario:
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
        if self.maximum_liquidation_horizon_ms != _MAXIMUM_LIQUIDATION_HORIZON_MS:
            raise ValueError("scenario liquidation horizon must be exactly 150 minutes")
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


def regime_retest_scenarios(
    candidate: RegimeRetestCandidate,
) -> tuple[RegimeRetestScenario, RegimeRetestScenario]:
    if not isinstance(candidate, RegimeRetestCandidate):
        raise TypeError("candidate must be a RegimeRetestCandidate")

    def build(name: str, execution: ExecutionConfig) -> RegimeRetestScenario:
        return RegimeRetestScenario(
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


DEFAULT_REGIME_RETEST_PROTOCOL = ResearchProtocol(
    protocol_name="regime-veto-retest-development-v1",
    universe=SYMBOLS,
    windows=DEVELOPMENT_WINDOWS,
    max_trials=3,
    maximum_holding_ms=_MAXIMUM_LIQUIDATION_HORIZON_MS,
    maximum_label_horizon_ms=_MAXIMUM_LABEL_HORIZON_MS,
    maximum_execution_latency_ms=_MAXIMUM_EXECUTION_LATENCY_MS,
    warmup_ms=(REGIME_RETEST_EVALUATION_START - REGIME_RETEST_GENERATION_START).days * _DAY_MS,
)


def _dataset_evidence_sha256(
    *,
    symbol: str,
    generation_start: date,
    evaluation_start: date,
    evaluation_end: date,
    warmup_candles: int,
    evaluation_candles: int,
    warmup_zero_volume_candles: int,
    evaluation_zero_volume_candles: int,
    candles_sha256: str,
) -> str:
    return hashlib.sha256(
        _canonical_json_bytes(
            {
                "candles_sha256": candles_sha256,
                "evaluation_candles": evaluation_candles,
                "evaluation_end": evaluation_end,
                "evaluation_start": evaluation_start,
                "evaluation_zero_volume_candles": evaluation_zero_volume_candles,
                "generation_start": generation_start,
                "schema": _DATASET_EVIDENCE_SCHEMA,
                "symbol": symbol,
                "warmup_candles": warmup_candles,
                "warmup_zero_volume_candles": warmup_zero_volume_candles,
            }
        )
    ).hexdigest()


@dataclass(frozen=True, slots=True)
class RegimeRetestDatasetEvidence:
    symbol: str
    generation_start: date
    evaluation_start: date
    evaluation_end: date
    warmup_candles: int
    evaluation_candles: int
    warmup_zero_volume_candles: int
    evaluation_zero_volume_candles: int
    candles_sha256: str
    dataset_evidence_sha256: str

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
        for name, count in (
            ("warmup_candles", self.warmup_candles),
            ("evaluation_candles", self.evaluation_candles),
        ):
            if type(count) is not int:
                raise TypeError(f"dataset {name} must be an integer")
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
            if type(count) is not int or not 0 <= count <= upper_bound:
                raise ValueError(f"dataset {name} must be an in-range integer count")
        _lowercase_sha256("dataset candles_sha256", self.candles_sha256)
        _lowercase_sha256("dataset dataset_evidence_sha256", self.dataset_evidence_sha256)
        expected_evidence_sha256 = _dataset_evidence_sha256(
            symbol=self.symbol,
            generation_start=self.generation_start,
            evaluation_start=self.evaluation_start,
            evaluation_end=self.evaluation_end,
            warmup_candles=self.warmup_candles,
            evaluation_candles=self.evaluation_candles,
            warmup_zero_volume_candles=self.warmup_zero_volume_candles,
            evaluation_zero_volume_candles=self.evaluation_zero_volume_candles,
            candles_sha256=self.candles_sha256,
        )
        if self.dataset_evidence_sha256 != expected_evidence_sha256:
            raise ValueError("dataset evidence commitment does not match its exact payload")


@dataclass(frozen=True, slots=True)
class RegimeRetestCellEvidence:
    scenario_name: str
    cell_id: str
    sleeve_id: str
    symbol: str
    initial_equity_usd: float
    generated_intents: int
    evaluated_intents: int
    warmup_intents_filtered: int
    terminal_embargo_intents_filtered: int
    evaluation_seed: int
    candidate_sha256: str
    dataset_sha256: str
    dataset_evidence_sha256: str
    generated_intents_sha256: str
    warmup_intents_sha256: str
    evaluated_intents_sha256: str
    terminal_embargo_intents_sha256: str
    warmup_intent_ids: tuple[str, ...]
    evaluated_intent_ids: tuple[str, ...]
    terminal_embargo_intent_ids: tuple[str, ...]
    generation_evidence: RegimeRetestGenerationEvidence
    generation_evidence_sha256: str
    result: ManagedCellResult

    def __post_init__(self) -> None:
        if not isinstance(self.result, ManagedCellResult):
            raise TypeError("cell result must be a ManagedCellResult")
        if not isinstance(self.generation_evidence, RegimeRetestGenerationEvidence):
            raise TypeError("cell generation evidence must be RegimeRetestGenerationEvidence")
        if self.sleeve_id != REGIME_RETEST_SLEEVE_ID:
            raise ValueError("cell sleeve must be the frozen regime/retest sleeve")
        if self.symbol not in SYMBOLS:
            raise ValueError("cell symbol must belong to the fixed universe")
        if isinstance(self.evaluation_seed, bool) or not isinstance(self.evaluation_seed, int):
            raise TypeError("cell evaluation seed must be an integer")
        if self.result.assumptions.seed != self.evaluation_seed:
            raise ValueError("cell result seed does not match its declared evaluation seed")
        for name, value in (
            ("cell candidate_sha256", self.candidate_sha256),
            ("cell dataset_sha256", self.dataset_sha256),
            ("cell dataset_evidence_sha256", self.dataset_evidence_sha256),
            ("cell generated_intents_sha256", self.generated_intents_sha256),
            ("cell warmup_intents_sha256", self.warmup_intents_sha256),
            ("cell evaluated_intents_sha256", self.evaluated_intents_sha256),
            (
                "cell terminal_embargo_intents_sha256",
                self.terminal_embargo_intents_sha256,
            ),
            ("cell generation_evidence_sha256", self.generation_evidence_sha256),
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
            self.terminal_embargo_intents_filtered,
        )
        if any(type(value) is not int or value < 0 for value in counts):
            raise ValueError("cell intent counts must be non-negative integers")
        if self.generated_intents != (
            self.evaluated_intents + self.warmup_intents_filtered + self.terminal_embargo_intents_filtered
        ):
            raise ValueError(
                "warmup, evaluated, and terminal-embargo intents must partition generated intents"
            )
        if self.result.counters.intents != self.evaluated_intents:
            raise ValueError("managed result must disposition every evaluated intent")
        partitions = (
            ("warmup_intent_ids", self.warmup_intent_ids, self.warmup_intents_filtered),
            ("evaluated_intent_ids", self.evaluated_intent_ids, self.evaluated_intents),
            (
                "terminal_embargo_intent_ids",
                self.terminal_embargo_intent_ids,
                self.terminal_embargo_intents_filtered,
            ),
        )
        for name, intent_ids, expected_count in partitions:
            _validate_ordered_intent_ids(name, intent_ids)
            if len(intent_ids) != expected_count:
                raise ValueError(f"{name} count does not match its declared partition")
        all_intent_ids = (
            *self.warmup_intent_ids,
            *self.evaluated_intent_ids,
            *self.terminal_embargo_intent_ids,
        )
        if len(all_intent_ids) != len(set(all_intent_ids)):
            raise ValueError("intent IDs must be unique across every role partition")
        evidence_intent_ids = tuple(intent.intent_id for intent in self.generation_evidence.intents)
        if evidence_intent_ids != all_intent_ids:
            raise ValueError("generation evidence intents do not match the exact role partitions")
        if any(event.symbol != self.symbol for event in self.generation_evidence.events):
            raise ValueError("generation evidence events do not match the cell symbol")
        expected_partition_hashes = (
            (self.warmup_intents_sha256, _intent_ids_sha256(self.warmup_intent_ids)),
            (self.evaluated_intents_sha256, _intent_ids_sha256(self.evaluated_intent_ids)),
            (
                self.terminal_embargo_intents_sha256,
                _intent_ids_sha256(self.terminal_embargo_intent_ids),
            ),
        )
        if any(declared != expected for declared, expected in expected_partition_hashes):
            raise ValueError("cell intent partition hash does not match its ordered intent IDs")
        disposition_ids = tuple(disposition.intent.intent_id for disposition in self.result.dispositions)
        if self.evaluated_intent_ids != disposition_ids:
            raise ValueError("evaluated intent IDs do not exactly match managed dispositions")
        expected_generated_sha256 = _generated_intents_sha256(
            warmup_intents=self.warmup_intents_filtered,
            warmup_intents_sha256=self.warmup_intents_sha256,
            evaluated_intents=self.evaluated_intents,
            evaluated_intents_sha256=self.evaluated_intents_sha256,
            terminal_embargo_intents=self.terminal_embargo_intents_filtered,
            terminal_embargo_intents_sha256=self.terminal_embargo_intents_sha256,
        )
        if self.generated_intents_sha256 != expected_generated_sha256:
            raise ValueError("generated intent inventory does not match its exact role partition")
        expected_generation_evidence_sha256 = _generation_evidence_sha256(
            generation_evidence=self.generation_evidence,
            candidate_sha256=self.candidate_sha256,
            dataset_sha256=self.dataset_sha256,
            dataset_evidence_sha256=self.dataset_evidence_sha256,
            symbol=self.symbol,
            warmup_intent_ids=self.warmup_intent_ids,
            evaluated_intent_ids=self.evaluated_intent_ids,
            terminal_embargo_intent_ids=self.terminal_embargo_intent_ids,
        )
        if self.generation_evidence_sha256 != expected_generation_evidence_sha256:
            raise ValueError("generation evidence SHA-256 does not match its exact campaign binding")


@dataclass(frozen=True, slots=True)
class RegimeRetestScenarioEvidence:
    scenario: RegimeRetestScenario
    cells: tuple[RegimeRetestCellEvidence, ...]
    portfolio: PortfolioEvidence

    def __post_init__(self) -> None:
        if not isinstance(self.scenario, RegimeRetestScenario):
            raise TypeError("scenario evidence requires a RegimeRetestScenario")
        if not isinstance(self.cells, tuple) or any(
            not isinstance(cell, RegimeRetestCellEvidence) for cell in self.cells
        ):
            raise TypeError("scenario cells must be immutable RegimeRetestCellEvidence values")
        if len(self.cells) != len(SYMBOLS):
            raise ValueError("scenario evidence requires exactly five regime/retest cells")
        if tuple((cell.sleeve_id, cell.symbol) for cell in self.cells) != tuple(
            (REGIME_RETEST_SLEEVE_ID, symbol) for symbol in SYMBOLS
        ):
            raise ValueError("scenario evidence has an incomplete or unordered five-symbol grid")
        if any(cell.scenario_name != self.scenario.name for cell in self.cells):
            raise ValueError("every cell must belong to its declared scenario")
        if len({cell.cell_id for cell in self.cells}) != len(self.cells):
            raise ValueError("scenario cell identities must be unique")
        for cell in self.cells:
            expected_cell_id = f"{self.scenario.name}:{REGIME_RETEST_SLEEVE_ID}:{cell.symbol}"
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
class RegimeRetestCampaignEvidence:
    external_dataset_attestation_verified: ClassVar[bool] = False

    candidate: RegimeRetestCandidate
    protocol: ResearchProtocol
    protocol_name: str
    protocol_sha256: str
    window_name: str
    role: DataRole
    purpose: ResearchPurpose
    generation_start: date
    evaluation_start: date
    evaluation_end: date
    window_rationale: str
    requested_initial_equity_usd: float
    cell_initial_equity_usd: float
    datasets: tuple[RegimeRetestDatasetEvidence, ...]
    scenarios: tuple[RegimeRetestScenarioEvidence, ...]
    seed: int
    development_only: bool = field(init=False, default=True)
    reused_data: bool = field(init=False, default=True)
    out_of_sample: bool = field(init=False, default=False)
    promotion_eligible: bool = field(init=False, default=False)
    shadow_allowed: bool = field(init=False, default=False)
    live_allowed: bool = field(init=False, default=False)

    def __post_init__(self) -> None:
        if not isinstance(self.candidate, RegimeRetestCandidate):
            raise TypeError("campaign candidate must be a RegimeRetestCandidate")
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
            raise ValueError("regime/retest campaign is fixed to the research/FIT role")
        window = self.protocol.assert_access(self.window_name, self.purpose)
        if window.role is not self.role:
            raise ValueError("campaign role does not match its registered window")
        if (
            self.generation_start,
            self.evaluation_start,
            self.evaluation_end,
        ) != (
            REGIME_RETEST_GENERATION_START,
            REGIME_RETEST_EVALUATION_START,
            REGIME_RETEST_EVALUATION_END,
        ):
            raise ValueError("campaign bounds do not match the frozen regime/retest screen")
        if self.window_rationale != REGIME_RETEST_WINDOW_RATIONALE:
            raise ValueError("campaign window rationale must remain explicit and frozen")
        if self.seed != DEFAULT_REGIME_RETEST_SEED:
            raise ValueError("campaign seed must remain fixed at 44")
        if self.requested_initial_equity_usd != REGIME_RETEST_INITIAL_EQUITY_USD:
            raise ValueError("campaign capital must remain fixed at 100,000 USD")
        expected_cell_equity = REGIME_RETEST_INITIAL_EQUITY_USD / len(SYMBOLS)
        if self.cell_initial_equity_usd != expected_cell_equity:
            raise ValueError("campaign cell capital must be the exact five-way allocation")
        if not isinstance(self.datasets, tuple) or any(
            not isinstance(dataset, RegimeRetestDatasetEvidence) for dataset in self.datasets
        ):
            raise TypeError("campaign datasets must be immutable RegimeRetestDatasetEvidence values")
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
            raise ValueError("all dataset evidence must match the campaign bounds")
        if not isinstance(self.scenarios, tuple) or any(
            not isinstance(item, RegimeRetestScenarioEvidence) for item in self.scenarios
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
                cell.terminal_embargo_intents_filtered,
                cell.generated_intents_sha256,
                cell.warmup_intents_sha256,
                cell.evaluated_intents_sha256,
                cell.terminal_embargo_intents_sha256,
                cell.warmup_intent_ids,
                cell.evaluated_intent_ids,
                cell.terminal_embargo_intent_ids,
                cell.generation_evidence,
                cell.generation_evidence_sha256,
            )
            for cell in self.scenarios[0].cells
        }
        for scenario in self.scenarios:
            if not math.isclose(
                scenario.portfolio.initial_equity_usd,
                REGIME_RETEST_INITIAL_EQUITY_USD,
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
                if cell.dataset_evidence_sha256 != dataset_by_symbol[cell.symbol].dataset_evidence_sha256:
                    raise ValueError("cell dataset evidence fingerprint does not match the campaign")
                if (
                    cell.generation_evidence.config_sha256 != self.candidate.config.fingerprint
                    or cell.generation_evidence.variant is not self.candidate.config.variant
                ):
                    raise ValueError("cell generation evidence does not match the campaign candidate")
                expected_seed = derive_seed(
                    self.seed,
                    _SEED_NAMESPACE,
                    self.protocol_sha256,
                    self.candidate.candidate_sha256,
                    dataset_by_symbol[cell.symbol].candles_sha256,
                    dataset_by_symbol[cell.symbol].dataset_evidence_sha256,
                    cell.generated_intents_sha256,
                    cell.generation_evidence_sha256,
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
                    cell.terminal_embargo_intents_filtered,
                    cell.generated_intents_sha256,
                    cell.warmup_intents_sha256,
                    cell.evaluated_intents_sha256,
                    cell.terminal_embargo_intents_sha256,
                    cell.warmup_intent_ids,
                    cell.evaluated_intent_ids,
                    cell.terminal_embargo_intent_ids,
                    cell.generation_evidence,
                    cell.generation_evidence_sha256,
                )
                if inventory != baseline_inventory[(cell.sleeve_id, cell.symbol)]:
                    raise ValueError("baseline and stress must evaluate the same intent inventory")

    def to_dict(self) -> dict[str, object]:
        scenario_rows: list[dict[str, object]] = []
        for scenario in self.scenarios:
            cell_rows: list[dict[str, object]] = []
            for cell in scenario.cells:
                cell_rows.append(
                    {
                        "candidate_sha256": cell.candidate_sha256,
                        "cell_id": cell.cell_id,
                        "dataset_sha256": cell.dataset_sha256,
                        "dataset_evidence_sha256": cell.dataset_evidence_sha256,
                        "evaluated_intents": cell.evaluated_intents,
                        "evaluated_intent_ids": cell.evaluated_intent_ids,
                        "evaluated_intents_sha256": cell.evaluated_intents_sha256,
                        "evaluation_seed": cell.evaluation_seed,
                        "generation_evidence": cell.generation_evidence.to_dict(),
                        "generation_evidence_sha256": cell.generation_evidence_sha256,
                        "generated_intents": cell.generated_intents,
                        "generated_intents_sha256": cell.generated_intents_sha256,
                        "initial_equity_usd": cell.initial_equity_usd,
                        "managed_result": asdict(cell.result),
                        "scenario_name": cell.scenario_name,
                        "sleeve_id": cell.sleeve_id,
                        "symbol": cell.symbol,
                        "warmup_intents_filtered": cell.warmup_intents_filtered,
                        "warmup_intent_ids": cell.warmup_intent_ids,
                        "warmup_intents_sha256": cell.warmup_intents_sha256,
                        "terminal_embargo_intents_filtered": (cell.terminal_embargo_intents_filtered),
                        "terminal_embargo_intent_ids": cell.terminal_embargo_intent_ids,
                        "terminal_embargo_intents_sha256": (cell.terminal_embargo_intents_sha256),
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
                "cells_per_scenario": len(SYMBOLS),
                "equal_fixed_capital": True,
                "requested_initial_equity_usd": self.requested_initial_equity_usd,
            },
            "candidate": self.candidate.to_dict(),
            "data": {
                "datasets": [asdict(dataset) for dataset in self.datasets],
                "evaluation_end_exclusive": self.evaluation_end,
                "evaluation_start": self.evaluation_start,
                "external_dataset_attestation_verified": (self.external_dataset_attestation_verified),
                "generation_start": self.generation_start,
                "no_imputation": True,
                "purpose": self.purpose,
                "role": self.role,
                "window_rationale": self.window_rationale,
                "window_name": self.window_name,
            },
            "development_only": self.development_only,
            "horizons": {
                "candidate_maximum_holding_ms": self.candidate.maximum_holding_ms,
                "candidate_maximum_liquidation_horizon_ms": (self.candidate.maximum_liquidation_horizon_ms),
                "maximum_label_horizon_ms": self.protocol.maximum_label_horizon_ms,
                "operational_horizon_ms": self.candidate.operational_horizon_ms,
                "purge_ms": self.protocol.purge_ms,
            },
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
            "scenarios": scenario_rows,
            "schema_version": 1,
            "seed": self.seed,
        }
        return cast(dict[str, object], _json_ready(payload))


def _validate_protocol(protocol: ResearchProtocol, candidate: RegimeRetestCandidate) -> None:
    if not isinstance(protocol, ResearchProtocol):
        raise TypeError("protocol must be a ResearchProtocol")
    if protocol != DEFAULT_REGIME_RETEST_PROTOCOL:
        raise ValueError("regime/retest campaign requires the frozen v1 research protocol")
    if protocol.max_trials != len(REGIME_RETEST_VARIANTS) or len(REGIME_RETEST_VARIANTS) != 3:
        raise ValueError("regime/retest protocol must keep exactly three trials")
    if protocol.warmup_ms != 62 * _DAY_MS:
        raise ValueError("regime/retest protocol must keep the December-January warmup")
    if protocol.maximum_holding_ms != _MAXIMUM_LIQUIDATION_HORIZON_MS:
        raise ValueError("protocol holding bound must include 90-minute hold and 60-minute grace")
    if protocol.maximum_label_horizon_ms != _MAXIMUM_LABEL_HORIZON_MS:
        raise ValueError("protocol label horizon must include minute-open quantization")
    if protocol.maximum_execution_latency_ms != _MAXIMUM_EXECUTION_LATENCY_MS:
        raise ValueError("protocol execution latency bound must remain 500ms")
    if protocol.purge_ms != 9_060_500:
        raise ValueError("protocol purge must remain exactly 9,060,500ms")
    if candidate.maximum_liquidation_horizon_ms != protocol.maximum_holding_ms:
        raise ValueError("candidate liquidation horizon must equal the protocol holding bound")
    if candidate.maximum_label_horizon_ms != protocol.maximum_label_horizon_ms:
        raise ValueError("candidate label horizon must equal the protocol label bound")
    expected_operational = (protocol.purge_ms + _MINUTE_MS - 1) // _MINUTE_MS * _MINUTE_MS
    if candidate.operational_horizon_ms != expected_operational:
        raise ValueError("candidate operational horizon must round purge to 152 minutes")


def _validate_scenarios(
    scenarios: tuple[RegimeRetestScenario, ...],
    *,
    candidate: RegimeRetestCandidate,
    protocol: ResearchProtocol,
) -> None:
    if not isinstance(scenarios, tuple) or any(
        not isinstance(scenario, RegimeRetestScenario) for scenario in scenarios
    ):
        raise TypeError("scenarios must be an immutable tuple of RegimeRetestScenario values")
    if tuple(scenario.name for scenario in scenarios) != _SCENARIO_NAMES:
        raise ValueError("regime/retest scenarios must be ordered baseline and stress")
    if scenarios != regime_retest_scenarios(candidate):
        raise ValueError("regime/retest scenarios must exactly match the frozen candidate factory")
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
            raise ValueError("scenario latency exceeds the frozen research protocol")


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
    digest = hashlib.sha256(_DATASET_SCHEMA)
    for candle in candles:
        digest.update(_canonical_json_bytes(_candle_payload(candle)))
        digest.update(b"\n")
    return digest.hexdigest()


def _validate_generated_intent(
    intent: SleeveIntent,
    *,
    candidate: RegimeRetestCandidate,
    symbol: str,
) -> None:
    if intent.sleeve_id != REGIME_RETEST_SLEEVE_ID or intent.symbol != symbol:
        raise ValueError("regime/retest generator emitted an intent for the wrong cell")
    expected_holding_ms = (
        candidate.config.long_max_hold_bars * _FIVE_MINUTES_MS
        if intent.side is Side.LONG
        else candidate.config.short_max_hold_bars * _FIVE_MINUTES_MS
    )
    if intent.exit_plan.max_holding_ms != expected_holding_ms:
        raise ValueError("regime/retest intent holding bound does not match its side")
    if intent.entry_eligible_ts_ms != intent.decision_ts_ms + 1:
        raise ValueError("regime/retest intent must become eligible at the next five-minute open")
    if intent.entry_eligible_ts_ms % _FIVE_MINUTES_MS:
        raise ValueError("regime/retest intent entry must align with a five-minute open")
    expected_expiry = intent.decision_ts_ms + candidate.config.intent_valid_bars * _FIVE_MINUTES_MS
    if intent.entry_expires_ts_ms != expected_expiry:
        raise ValueError("regime/retest intent expiry does not match its candidate")
    metadata = dict(intent.metadata)
    expected_metadata = {
        "config_sha256": candidate.config.fingerprint,
        "strategy_version": REGIME_RETEST_SLEEVE_ID,
        "variant": candidate.config.variant.value,
    }
    if any(metadata.get(key) != value for key, value in expected_metadata.items()):
        raise ValueError("regime/retest intent metadata does not match its candidate")


def _slice_symbol(
    candles: list[Candle],
    *,
    symbol: str,
    generation_start: date,
    evaluation_start: date,
    evaluation_end: date,
) -> tuple[list[Candle], list[Candle], RegimeRetestDatasetEvidence]:
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
        raise ValueError(f"{symbol} selected regime/retest data are incomplete; gaps are not imputed")
    for index, candle in enumerate(selected):
        expected_open = generation_start_ms + index * _MINUTE_MS
        if candle.open_time_ms != expected_open or candle.close_time_ms != expected_open + _MINUTE_MS - 1:
            raise ValueError(f"{symbol} selected regime/retest data must be contiguous aligned minutes")
    evaluation_index = (evaluation_start_ms - generation_start_ms) // _MINUTE_MS
    evaluation = selected[evaluation_index:]
    expected_evaluation_count = (evaluation_end_ms - evaluation_start_ms) // _MINUTE_MS
    if len(evaluation) != expected_evaluation_count or len(evaluation) % _MINUTES_PER_DAY:
        raise RuntimeError("validated regime/retest slice lost complete UTC evaluation days")
    warmup = selected[:evaluation_index]
    warmup_zero_volume_candles = sum(candle.volume == 0 for candle in warmup)
    evaluation_zero_volume_candles = sum(candle.volume == 0 for candle in evaluation)
    candles_sha256 = _dataset_sha256(selected)
    dataset_evidence_sha256 = _dataset_evidence_sha256(
        symbol=symbol,
        generation_start=generation_start,
        evaluation_start=evaluation_start,
        evaluation_end=evaluation_end,
        warmup_candles=len(warmup),
        evaluation_candles=len(evaluation),
        warmup_zero_volume_candles=warmup_zero_volume_candles,
        evaluation_zero_volume_candles=evaluation_zero_volume_candles,
        candles_sha256=candles_sha256,
    )
    evidence = RegimeRetestDatasetEvidence(
        symbol=symbol,
        generation_start=generation_start,
        evaluation_start=evaluation_start,
        evaluation_end=evaluation_end,
        warmup_candles=len(warmup),
        evaluation_candles=len(evaluation),
        warmup_zero_volume_candles=warmup_zero_volume_candles,
        evaluation_zero_volume_candles=evaluation_zero_volume_candles,
        candles_sha256=candles_sha256,
        dataset_evidence_sha256=dataset_evidence_sha256,
    )
    return selected, evaluation, evidence


def run_regime_retest_campaign(
    candles_by_symbol: Mapping[str, list[Candle]],
    *,
    candidate: RegimeRetestCandidate | None = None,
    protocol: ResearchProtocol = DEFAULT_REGIME_RETEST_PROTOCOL,
    initial_equity_usd: float = REGIME_RETEST_INITIAL_EQUITY_USD,
    scenarios: tuple[RegimeRetestScenario, ...] | None = None,
    seed: int = DEFAULT_REGIME_RETEST_SEED,
) -> RegimeRetestCampaignEvidence:
    """Evaluate one frozen regime/retest candidate on reused RESEARCH/FIT data."""

    selected_candidate = RegimeRetestCandidate() if candidate is None else candidate
    if not isinstance(selected_candidate, RegimeRetestCandidate):
        raise TypeError("candidate must be a RegimeRetestCandidate")
    _validate_protocol(protocol, selected_candidate)
    if isinstance(seed, bool) or not isinstance(seed, int):
        raise TypeError("seed must be an integer")
    if seed != DEFAULT_REGIME_RETEST_SEED:
        raise ValueError("regime/retest campaign seed is fixed at 44")
    if (
        isinstance(initial_equity_usd, bool)
        or not isinstance(initial_equity_usd, (int, float))
        or not math.isfinite(initial_equity_usd)
        or float(initial_equity_usd) != REGIME_RETEST_INITIAL_EQUITY_USD
    ):
        raise ValueError("regime/retest campaign equity is fixed at 100,000 USD")
    if not isinstance(candles_by_symbol, Mapping):
        raise TypeError("candles_by_symbol must be a mapping")
    if set(candles_by_symbol) != set(SYMBOLS):
        raise ValueError("candles_by_symbol must contain exactly the fixed five-symbol universe")

    window = protocol.assert_access("research", ResearchPurpose.FIT)
    if not (
        window.start
        <= REGIME_RETEST_GENERATION_START
        < REGIME_RETEST_EVALUATION_START
        < REGIME_RETEST_EVALUATION_END
        <= window.end
    ):
        raise ValueError("frozen regime/retest bounds must remain inside the research role")
    selected_scenarios = regime_retest_scenarios(selected_candidate) if scenarios is None else scenarios
    _validate_scenarios(
        selected_scenarios,
        candidate=selected_candidate,
        protocol=protocol,
    )

    generation_rows: dict[str, list[Candle]] = {}
    evaluation_rows: dict[str, list[Candle]] = {}
    datasets: list[RegimeRetestDatasetEvidence] = []
    for symbol in SYMBOLS:
        generated, evaluated, evidence = _slice_symbol(
            candles_by_symbol[symbol],
            symbol=symbol,
            generation_start=REGIME_RETEST_GENERATION_START,
            evaluation_start=REGIME_RETEST_EVALUATION_START,
            evaluation_end=REGIME_RETEST_EVALUATION_END,
        )
        generation_rows[symbol] = generated
        evaluation_rows[symbol] = evaluated
        datasets.append(evidence)
    dataset_by_symbol = {dataset.symbol: dataset for dataset in datasets}

    generation_start_ms = _utc_ms(REGIME_RETEST_GENERATION_START)
    evaluation_start_ms = _utc_ms(REGIME_RETEST_EVALUATION_START)
    evaluation_end_ms = _utc_ms(REGIME_RETEST_EVALUATION_END)
    evaluation_cutoff_ms = evaluation_end_ms - REGIME_RETEST_OPERATIONAL_HORIZON_MS
    intent_sets: dict[
        str,
        tuple[
            list[SleeveIntent],
            list[SleeveIntent],
            list[SleeveIntent],
            list[SleeveIntent],
            RegimeRetestGenerationEvidence,
        ],
    ] = {}
    for symbol in SYMBOLS:
        generation_evidence = generate_regime_veto_retest_reclaim_evidence(
            generation_rows[symbol],
            selected_candidate.config,
        )
        if not isinstance(generation_evidence, RegimeRetestGenerationEvidence):
            raise TypeError("regime/retest generator must return RegimeRetestGenerationEvidence")
        if (
            generation_evidence.config_sha256 != selected_candidate.config.fingerprint
            or generation_evidence.variant is not selected_candidate.config.variant
        ):
            raise ValueError("regime/retest generation evidence does not match the candidate")
        if any(event.symbol != symbol for event in generation_evidence.events):
            raise ValueError("regime/retest generation evidence emitted an event for the wrong cell")
        if any(
            not generation_start_ms <= event.decision_ts_ms < evaluation_end_ms
            for event in generation_evidence.events
        ):
            raise ValueError("regime/retest generation evidence contains an out-of-bounds event")
        all_intents = list(generation_evidence.intents)
        for intent in all_intents:
            _validate_generated_intent(
                intent,
                candidate=selected_candidate,
                symbol=symbol,
            )
        if any(
            not generation_start_ms <= intent.decision_ts_ms < evaluation_end_ms for intent in all_intents
        ):
            raise ValueError("regime/retest generator emitted an intent outside supplied data")
        ordered_intents = sorted(
            all_intents,
            key=lambda intent: (
                intent.decision_ts_ms,
                intent.entry_eligible_ts_ms,
                intent.intent_id,
            ),
        )
        if all_intents != ordered_intents:
            raise ValueError("generation evidence intents must already be canonically ordered")
        in_window = [
            intent
            for intent in all_intents
            if evaluation_start_ms <= intent.decision_ts_ms < evaluation_cutoff_ms
        ]
        warmup = [intent for intent in all_intents if intent.decision_ts_ms < evaluation_start_ms]
        terminal_embargo = [
            intent
            for intent in all_intents
            if evaluation_cutoff_ms <= intent.decision_ts_ms < evaluation_end_ms
        ]
        if len(all_intents) != len(warmup) + len(in_window) + len(terminal_embargo):
            raise RuntimeError(
                "warmup, evaluation, and terminal-embargo bounds did not partition regime/retest intents"
            )
        if tuple(intent.intent_id for intent in all_intents) != tuple(
            intent.intent_id for intent in (*warmup, *in_window, *terminal_embargo)
        ):
            raise RuntimeError("generated regime/retest inventory lost its exact role partition")
        _validate_ordered_intent_ids(
            "generated intent IDs",
            tuple(intent.intent_id for intent in all_intents),
        )
        intent_sets[symbol] = (
            all_intents,
            warmup,
            in_window,
            terminal_embargo,
            generation_evidence,
        )

    cell_equity = REGIME_RETEST_INITIAL_EQUITY_USD / len(SYMBOLS)
    protocol_sha256 = protocol.fingerprint()
    scenario_evidence: list[RegimeRetestScenarioEvidence] = []
    for scenario in selected_scenarios:
        cells: list[RegimeRetestCellEvidence] = []
        for symbol in SYMBOLS:
            (
                all_intents,
                warmup,
                in_window,
                terminal_embargo,
                generation_evidence,
            ) = intent_sets[symbol]
            warmup_intent_ids = tuple(intent.intent_id for intent in warmup)
            evaluated_intent_ids = tuple(intent.intent_id for intent in in_window)
            terminal_embargo_intent_ids = tuple(intent.intent_id for intent in terminal_embargo)
            warmup_intents_sha256 = _intent_ids_sha256(warmup_intent_ids)
            evaluated_intents_sha256 = _intent_ids_sha256(evaluated_intent_ids)
            terminal_embargo_intents_sha256 = _intent_ids_sha256(terminal_embargo_intent_ids)
            generated_intents_sha256 = _generated_intents_sha256(
                warmup_intents=len(warmup),
                warmup_intents_sha256=warmup_intents_sha256,
                evaluated_intents=len(in_window),
                evaluated_intents_sha256=evaluated_intents_sha256,
                terminal_embargo_intents=len(terminal_embargo),
                terminal_embargo_intents_sha256=terminal_embargo_intents_sha256,
            )
            generation_evidence_sha256 = _generation_evidence_sha256(
                generation_evidence=generation_evidence,
                candidate_sha256=selected_candidate.candidate_sha256,
                dataset_sha256=dataset_by_symbol[symbol].candles_sha256,
                dataset_evidence_sha256=(dataset_by_symbol[symbol].dataset_evidence_sha256),
                symbol=symbol,
                warmup_intent_ids=warmup_intent_ids,
                evaluated_intent_ids=evaluated_intent_ids,
                terminal_embargo_intent_ids=terminal_embargo_intent_ids,
            )
            cell_id = f"{scenario.name}:{REGIME_RETEST_SLEEVE_ID}:{symbol}"
            evaluation_seed = derive_seed(
                seed,
                _SEED_NAMESPACE,
                protocol_sha256,
                selected_candidate.candidate_sha256,
                dataset_by_symbol[symbol].candles_sha256,
                dataset_by_symbol[symbol].dataset_evidence_sha256,
                generated_intents_sha256,
                generation_evidence_sha256,
                "research",
                REGIME_RETEST_EVALUATION_START.isoformat(),
                REGIME_RETEST_EVALUATION_END.isoformat(),
                scenario.name,
                REGIME_RETEST_SLEEVE_ID,
                symbol,
            )
            result = evaluate_sleeve_cell(
                evaluation_rows[symbol],
                in_window,
                cell_id=cell_id,
                sleeve_id=REGIME_RETEST_SLEEVE_ID,
                symbol=symbol,
                initial_equity_usd=cell_equity,
                execution=scenario.execution,
                costs=scenario.costs,
                limits=selected_candidate.risk,
                policy=scenario.policy,
                seed=evaluation_seed,
            )
            cells.append(
                RegimeRetestCellEvidence(
                    scenario_name=scenario.name,
                    cell_id=cell_id,
                    sleeve_id=REGIME_RETEST_SLEEVE_ID,
                    symbol=symbol,
                    initial_equity_usd=cell_equity,
                    generated_intents=len(all_intents),
                    evaluated_intents=len(in_window),
                    warmup_intents_filtered=len(warmup),
                    terminal_embargo_intents_filtered=len(terminal_embargo),
                    evaluation_seed=evaluation_seed,
                    candidate_sha256=selected_candidate.candidate_sha256,
                    dataset_sha256=dataset_by_symbol[symbol].candles_sha256,
                    dataset_evidence_sha256=(dataset_by_symbol[symbol].dataset_evidence_sha256),
                    generated_intents_sha256=generated_intents_sha256,
                    warmup_intents_sha256=warmup_intents_sha256,
                    evaluated_intents_sha256=evaluated_intents_sha256,
                    terminal_embargo_intents_sha256=terminal_embargo_intents_sha256,
                    warmup_intent_ids=warmup_intent_ids,
                    evaluated_intent_ids=evaluated_intent_ids,
                    terminal_embargo_intent_ids=terminal_embargo_intent_ids,
                    generation_evidence=generation_evidence,
                    generation_evidence_sha256=generation_evidence_sha256,
                    result=result,
                )
            )
        immutable_cells = tuple(cells)
        scenario_evidence.append(
            RegimeRetestScenarioEvidence(
                scenario=scenario,
                cells=immutable_cells,
                portfolio=synchronize_cells(tuple(cell.result.cell for cell in immutable_cells)),
            )
        )

    return RegimeRetestCampaignEvidence(
        candidate=selected_candidate,
        protocol=protocol,
        protocol_name=protocol.protocol_name,
        protocol_sha256=protocol_sha256,
        window_name="research",
        role=DataRole.RESEARCH,
        purpose=ResearchPurpose.FIT,
        generation_start=REGIME_RETEST_GENERATION_START,
        evaluation_start=REGIME_RETEST_EVALUATION_START,
        evaluation_end=REGIME_RETEST_EVALUATION_END,
        window_rationale=REGIME_RETEST_WINDOW_RATIONALE,
        requested_initial_equity_usd=REGIME_RETEST_INITIAL_EQUITY_USD,
        cell_initial_equity_usd=cell_equity,
        datasets=tuple(datasets),
        scenarios=tuple(scenario_evidence),
        seed=seed,
    )
