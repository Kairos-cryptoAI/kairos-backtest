"""Offline, preregistered screen of the three pullback-depth variants.

The screen deliberately reuses one fixed RESEARCH slice.  It is a development
diagnostic only: it cannot promote a candidate, authorize shadow execution, or
authorize live trading.  The immutable plan is persisted before cache access
or evaluation so the trial set, eligibility gates, and ranking rule cannot be
chosen after observing results.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import tempfile
from collections import Counter
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from datetime import UTC, date, datetime, timedelta
from enum import Enum
from pathlib import Path
from typing import cast

from kairos_quant.candles import Candle

from .data import BinanceArchiveLoader, DatasetManifest, month_starts
from .development_campaign import (
    DEFAULT_DEVELOPMENT_PROTOCOL,
    DevelopmentCampaignEvidence,
    DevelopmentCandidate,
    DevelopmentCellEvidence,
    development_scenarios,
    run_development_campaign,
)
from .managed_evaluation import IntentDispositionReason
from .portfolio import PortfolioEvidence, synchronize_cells
from .provenance import RuntimeProvenance, runtime_provenance, source_fingerprint
from .research_protocol import DataRole, ResearchProtocol, ResearchPurpose
from .robustness import hac_sharpe
from .scenarios import SYMBOLS
from .sleeves.trend_pullback_reclaim import (
    PullbackDepthVariant,
    TrendPullbackReclaimConfig,
)

PLAN_VERSION = "kairos.development-screen.v1"
CLASSIFICATION = "development_diagnostics_only"
REJECT_ALL = "REJECT_ALL"
EVALUATION_START = date(2023, 1, 1)
EVALUATION_END = date(2023, 7, 1)
WARMUP_DAYS = 35
GENERATION_START = EVALUATION_START - timedelta(days=WARMUP_DAYS)
WINDOW_NAME = "research"
BASE_SEED = 42
INITIAL_EQUITY_USD = 100_000.0
MINIMUM_PULLBACK_CLOSED_TRADES = 100
SLEEVE_IDS = (
    "trend_breakout_v1",
    "range_mean_reversion_v1",
    "trend_pullback_reclaim_v1",
)
PULLBACK_SLEEVE_ID = "trend_pullback_reclaim_v1"
SCENARIO_NAMES = ("baseline", "stress")
TRIAL_VARIANTS = (
    PullbackDepthVariant.SHALLOW,
    PullbackDepthVariant.MEDIUM,
    PullbackDepthVariant.DEEP,
)


def _json_ready(value: object) -> object:
    """Convert immutable evidence into finite, deterministic JSON values."""

    if isinstance(value, Enum):
        return value.value
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("screen datetimes must be timezone-aware")
        return value.isoformat()
    if type(value) is date:
        return value.isoformat()
    if isinstance(value, Mapping):
        if any(not isinstance(key, str) for key in value):
            raise TypeError("screen JSON mappings require string keys")
        return {key: _json_ready(value[key]) for key in sorted(value)}
    if isinstance(value, (tuple, list)):
        return [_json_ready(item) for item in value]
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("screen JSON cannot contain non-finite numbers")
        return 0.0 if value == 0 else value
    raise TypeError(f"unsupported screen JSON value: {type(value).__name__}")


def _canonical_json_bytes(value: object) -> bytes:
    return json.dumps(
        _json_ready(value),
        allow_nan=False,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _sha256(value: object) -> str:
    return hashlib.sha256(_canonical_json_bytes(value)).hexdigest()


def _file_sha256(path: Path) -> str:
    if not path.is_file():
        raise FileNotFoundError(f"screen provenance file is unavailable: {path}")
    return hashlib.sha256(path.read_bytes()).hexdigest()


@dataclass(frozen=True, slots=True)
class ScreenEnvironment:
    """Exact source, dependency lock and runtime bound before evaluation."""

    source_sha256: str
    pyproject_sha256: str
    uv_lock_sha256: str
    runtime: RuntimeProvenance
    environment_sha256: str = field(init=False)

    def __post_init__(self) -> None:
        for name, value in (
            ("source_sha256", self.source_sha256),
            ("pyproject_sha256", self.pyproject_sha256),
            ("uv_lock_sha256", self.uv_lock_sha256),
        ):
            if (
                type(value) is not str
                or len(value) != 64
                or value != value.lower()
                or any(character not in "0123456789abcdef" for character in value)
            ):
                raise ValueError(f"screen {name} must be a lowercase SHA-256")
        if not isinstance(self.runtime, RuntimeProvenance):
            raise TypeError("screen runtime must be RuntimeProvenance")
        object.__setattr__(self, "environment_sha256", _sha256(self._payload()))

    @classmethod
    def capture(cls) -> ScreenEnvironment:
        project_root = Path(__file__).resolve().parents[1]
        return cls(
            source_sha256=source_fingerprint(),
            pyproject_sha256=_file_sha256(project_root / "pyproject.toml"),
            uv_lock_sha256=_file_sha256(project_root / "uv.lock"),
            runtime=runtime_provenance(),
        )

    def _payload(self) -> dict[str, object]:
        return {
            "pyproject_sha256": self.pyproject_sha256,
            "runtime": self.runtime.as_dict(),
            "source_sha256": self.source_sha256,
            "uv_lock_sha256": self.uv_lock_sha256,
        }

    def to_dict(self) -> dict[str, object]:
        return {**self._payload(), "environment_sha256": self.environment_sha256}


def _candidate_for_variant(variant: PullbackDepthVariant) -> DevelopmentCandidate:
    if not isinstance(variant, PullbackDepthVariant):
        raise TypeError("screen variant must be a PullbackDepthVariant")
    return DevelopmentCandidate(trend_pullback_reclaim=TrendPullbackReclaimConfig(depth_variant=variant))


@dataclass(frozen=True, slots=True)
class ScreenTrialPlan:
    """One and only one predeclared pullback-depth trial."""

    trial_id: str
    fixed_order: int
    variant: PullbackDepthVariant
    candidate: DevelopmentCandidate
    candidate_sha256: str

    def __post_init__(self) -> None:
        if type(self.trial_id) is not str or type(self.fixed_order) is not int:
            raise TypeError("trial identity and fixed order must use their canonical types")
        if not isinstance(self.variant, PullbackDepthVariant):
            raise TypeError("trial variant must be a PullbackDepthVariant")
        if not isinstance(self.candidate, DevelopmentCandidate):
            raise TypeError("trial candidate must be a DevelopmentCandidate")
        if type(self.candidate_sha256) is not str:
            raise TypeError("trial candidate SHA-256 must be text")
        expected_order = TRIAL_VARIANTS.index(self.variant)
        if self.trial_id != self.variant.name or self.fixed_order != expected_order:
            raise ValueError("trial identity must match the fixed SHALLOW/MEDIUM/DEEP order")
        expected_candidate = _candidate_for_variant(self.variant)
        if self.candidate != expected_candidate:
            raise ValueError("trial candidate must differ only by its registered depth variant")
        if self.candidate_sha256 != self.candidate.candidate_sha256:
            raise ValueError("trial candidate SHA-256 does not bind its complete configuration")

    def to_dict(self) -> dict[str, object]:
        return {
            "candidate": self.candidate.to_dict(),
            "candidate_sha256": self.candidate_sha256,
            "fixed_order": self.fixed_order,
            "trial_id": self.trial_id,
            "variant": self.variant.value,
        }


def _fixed_trials() -> tuple[ScreenTrialPlan, ...]:
    return tuple(
        ScreenTrialPlan(
            trial_id=variant.name,
            fixed_order=index,
            variant=variant,
            candidate=(candidate := _candidate_for_variant(variant)),
            candidate_sha256=candidate.candidate_sha256,
        )
        for index, variant in enumerate(TRIAL_VARIANTS)
    )


@dataclass(frozen=True, slots=True)
class ExperimentScreenPlan:
    """Canonical preregistration for the complete three-trial screen."""

    version: str = PLAN_VERSION
    classification: str = CLASSIFICATION
    reused_data: bool = True
    out_of_sample: bool = False
    promotion_eligible: bool = False
    shadow_allowed: bool = False
    live_allowed: bool = False
    window_name: str = WINDOW_NAME
    role: DataRole = DataRole.RESEARCH
    purpose: ResearchPurpose = ResearchPurpose.FIT
    generation_start: date = GENERATION_START
    evaluation_start: date = EVALUATION_START
    evaluation_end: date = EVALUATION_END
    warmup_days: int = WARMUP_DAYS
    universe: tuple[str, ...] = SYMBOLS
    scenario_names: tuple[str, ...] = SCENARIO_NAMES
    seed: int = BASE_SEED
    initial_equity_usd: float = INITIAL_EQUITY_USD
    minimum_pullback_closed_trades_per_scenario: int = MINIMUM_PULLBACK_CLOSED_TRADES
    protocol: ResearchProtocol = DEFAULT_DEVELOPMENT_PROTOCOL
    trials: tuple[ScreenTrialPlan, ...] = field(default_factory=_fixed_trials)
    environment: ScreenEnvironment = field(default_factory=ScreenEnvironment.capture)
    plan_sha256: str = field(init=False)

    def __post_init__(self) -> None:
        if any(type(value) is not str for value in (self.version, self.classification, self.window_name)):
            raise TypeError("screen plan text fields must use canonical string types")
        if not isinstance(self.role, DataRole) or not isinstance(self.purpose, ResearchPurpose):
            raise TypeError("screen plan role and purpose must use their registered enums")
        if any(
            type(value) is not date
            for value in (self.generation_start, self.evaluation_start, self.evaluation_end)
        ):
            raise TypeError("screen plan boundaries must be date values")
        if type(self.universe) is not tuple or type(self.scenario_names) is not tuple:
            raise TypeError("screen universe and scenario names must be immutable tuples")
        if not isinstance(self.protocol, ResearchProtocol):
            raise TypeError("screen protocol must be a ResearchProtocol")
        if not isinstance(self.environment, ScreenEnvironment):
            raise TypeError("screen environment must be ScreenEnvironment")
        if type(self.trials) is not tuple or any(
            not isinstance(trial, ScreenTrialPlan) for trial in self.trials
        ):
            raise TypeError("screen trials must be an immutable ScreenTrialPlan tuple")
        exact_values = (
            (self.version, PLAN_VERSION),
            (self.classification, CLASSIFICATION),
            (self.window_name, WINDOW_NAME),
            (self.role, DataRole.RESEARCH),
            (self.purpose, ResearchPurpose.FIT),
            (self.generation_start, GENERATION_START),
            (self.evaluation_start, EVALUATION_START),
            (self.evaluation_end, EVALUATION_END),
            (self.warmup_days, WARMUP_DAYS),
            (self.universe, SYMBOLS),
            (self.scenario_names, SCENARIO_NAMES),
            (self.seed, BASE_SEED),
            (self.initial_equity_usd, INITIAL_EQUITY_USD),
            (
                self.minimum_pullback_closed_trades_per_scenario,
                MINIMUM_PULLBACK_CLOSED_TRADES,
            ),
            (self.protocol, DEFAULT_DEVELOPMENT_PROTOCOL),
            (self.trials, _fixed_trials()),
        )
        if any(actual != expected for actual, expected in exact_values):
            raise ValueError("screen plan must preserve every fixed preregistered value")
        exact_flags = (
            (self.reused_data, True),
            (self.out_of_sample, False),
            (self.promotion_eligible, False),
            (self.shadow_allowed, False),
            (self.live_allowed, False),
        )
        if any(type(actual) is not bool or actual is not expected for actual, expected in exact_flags):
            raise ValueError("screen plan classification flags are immutable")
        if type(self.warmup_days) is not int or type(self.seed) is not int:
            raise TypeError("screen warmup and seed must be integers")
        if type(self.minimum_pullback_closed_trades_per_scenario) is not int:
            raise TypeError("screen trade-count gate must be an integer")
        if isinstance(self.initial_equity_usd, bool) or not isinstance(self.initial_equity_usd, (int, float)):
            raise TypeError("screen initial capital must be numeric")
        object.__setattr__(self, "plan_sha256", _sha256(self._payload()))

    def _payload(self) -> dict[str, object]:
        return {
            "classification": self.classification,
            "data": {
                "evaluation_end_exclusive": self.evaluation_end,
                "evaluation_start": self.evaluation_start,
                "generation_start": self.generation_start,
                "no_downloads": True,
                "no_imputation": True,
                "out_of_sample": self.out_of_sample,
                "purpose": self.purpose,
                "reused_data": self.reused_data,
                "role": self.role,
                "universe": self.universe,
                "warmup_days": self.warmup_days,
                "window_name": self.window_name,
            },
            "eligibility": {
                "minimum_pullback_closed_trades_per_scenario": (
                    self.minimum_pullback_closed_trades_per_scenario
                ),
                "requires_pullback_sleeve_each_scenario": {
                    "expectancy_usd_per_trade_above": 0.0,
                    "log_growth_above": 0.0,
                    "profit_factor_above": 1.0,
                },
                "trade_count_is_ranking_objective": False,
            },
            "execution": {
                "initial_equity_usd": self.initial_equity_usd,
                "scenario_factory": "development_scenarios",
                "scenario_names": self.scenario_names,
                "seed": self.seed,
            },
            "environment": self.environment.to_dict(),
            "live_allowed": self.live_allowed,
            "out_of_sample": self.out_of_sample,
            "permissions": {
                "live_allowed": self.live_allowed,
                "promotion_eligible": self.promotion_eligible,
                "shadow_allowed": self.shadow_allowed,
            },
            "plan_version": self.version,
            "promotion_eligible": self.promotion_eligible,
            "protocol": {
                "definition": asdict(self.protocol),
                "name": self.protocol.protocol_name,
                "sha256": self.protocol.fingerprint(),
            },
            "ranking": {
                "primary": "maximize_minimum_baseline_stress_log_growth",
                "secondary": "minimize_stress_maximum_drawdown",
                "tertiary": "fixed_SHALLOW_MEDIUM_DEEP_order",
            },
            "reused_data": self.reused_data,
            "shadow_allowed": self.shadow_allowed,
            "trials": [trial.to_dict() for trial in self.trials],
        }

    def to_dict(self) -> dict[str, object]:
        payload = self._payload()
        payload["plan_sha256"] = self.plan_sha256
        return cast(dict[str, object], _json_ready(payload))


def _rejection_inventory() -> tuple[str, ...]:
    return tuple(
        reason.value for reason in IntentDispositionReason if reason is not IntentDispositionReason.ENTERED
    )


@dataclass(frozen=True, slots=True)
class ScreenMetrics:
    """Compact cost-aware metrics for a synchronized group of managed cells."""

    trades: int
    net_return: float
    log_growth: float
    profit_factor: float | None
    expectancy_usd_per_trade: float
    maximum_drawdown: float
    hac_sharpe: float | None
    fees_usd: float
    shortfall_usd: float
    funding_usd: float
    rejection_dispositions: tuple[tuple[str, int], ...]

    def __post_init__(self) -> None:
        if isinstance(self.trades, bool) or not isinstance(self.trades, int) or self.trades < 0:
            raise ValueError("screen trade count must be a non-negative integer")
        finite_values = (
            self.net_return,
            self.log_growth,
            self.expectancy_usd_per_trade,
            self.maximum_drawdown,
            self.fees_usd,
            self.shortfall_usd,
            self.funding_usd,
        )
        if any(isinstance(value, bool) or not math.isfinite(value) for value in finite_values):
            raise ValueError("screen metrics must be finite numeric values")
        if self.net_return <= -1 or self.log_growth != math.log1p(self.net_return):
            raise ValueError("screen log growth must exactly match its net return")
        if not 0 <= self.maximum_drawdown < 1:
            raise ValueError("screen drawdown must lie in [0, 1)")
        if any(value < 0 for value in (self.fees_usd, self.shortfall_usd, self.funding_usd)):
            raise ValueError("screen costs must be non-negative")
        if self.profit_factor is not None and (
            isinstance(self.profit_factor, bool)
            or not math.isfinite(self.profit_factor)
            or self.profit_factor < 0
        ):
            raise ValueError("screen profit factor must be finite and non-negative when available")
        if self.hac_sharpe is not None and (
            isinstance(self.hac_sharpe, bool) or not math.isfinite(self.hac_sharpe)
        ):
            raise ValueError("screen HAC Sharpe must be finite when available")
        if not isinstance(self.rejection_dispositions, tuple):
            raise TypeError("screen rejection dispositions must be an immutable tuple")
        if tuple(name for name, _ in self.rejection_dispositions) != _rejection_inventory():
            raise ValueError("screen rejection dispositions must use the complete fixed inventory")
        if any(
            isinstance(count, bool) or not isinstance(count, int) or count < 0
            for _, count in self.rejection_dispositions
        ):
            raise ValueError("screen rejection counts must be non-negative integers")

    def to_dict(self) -> dict[str, object]:
        return {
            "expectancy_usd_per_trade": self.expectancy_usd_per_trade,
            "fees_usd": self.fees_usd,
            "funding_usd": self.funding_usd,
            "hac_sharpe": self.hac_sharpe,
            "log_growth": self.log_growth,
            "maximum_drawdown": self.maximum_drawdown,
            "net_return": self.net_return,
            "profit_factor": self.profit_factor,
            "rejection_dispositions": dict(self.rejection_dispositions),
            "shortfall_usd": self.shortfall_usd,
            "trades": self.trades,
        }


@dataclass(frozen=True, slots=True)
class SleeveScreenMetrics:
    sleeve_id: str
    metrics: ScreenMetrics

    def __post_init__(self) -> None:
        if self.sleeve_id not in SLEEVE_IDS:
            raise ValueError("screen sleeve metric has an unknown sleeve identity")
        if not isinstance(self.metrics, ScreenMetrics):
            raise TypeError("screen sleeve metric requires ScreenMetrics")

    def to_dict(self) -> dict[str, object]:
        return {"sleeve_id": self.sleeve_id, **self.metrics.to_dict()}


@dataclass(frozen=True, slots=True)
class ScenarioScreenMetrics:
    scenario_name: str
    per_sleeve: tuple[SleeveScreenMetrics, ...]
    combined: ScreenMetrics

    def __post_init__(self) -> None:
        if self.scenario_name not in SCENARIO_NAMES:
            raise ValueError("screen scenario metric has an unknown scenario")
        if not isinstance(self.per_sleeve, tuple) or any(
            not isinstance(item, SleeveScreenMetrics) for item in self.per_sleeve
        ):
            raise TypeError("per-sleeve screen metrics must be an immutable tuple")
        if tuple(item.sleeve_id for item in self.per_sleeve) != SLEEVE_IDS:
            raise ValueError("per-sleeve screen metrics must preserve the fixed sleeve order")
        if not isinstance(self.combined, ScreenMetrics):
            raise TypeError("combined scenario metric requires ScreenMetrics")

    def sleeve(self, sleeve_id: str) -> ScreenMetrics:
        try:
            return next(item.metrics for item in self.per_sleeve if item.sleeve_id == sleeve_id)
        except StopIteration as exc:
            raise KeyError(f"missing sleeve screen metrics: {sleeve_id}") from exc

    def to_dict(self) -> dict[str, object]:
        return {
            "combined": self.combined.to_dict(),
            "per_sleeve": [item.to_dict() for item in self.per_sleeve],
            "scenario_name": self.scenario_name,
        }


def _metrics_for_cells(
    portfolio: PortfolioEvidence,
    cells: tuple[DevelopmentCellEvidence, ...],
) -> ScreenMetrics:
    trades = tuple(trade for cell in cells for trade in cell.result.cell.trades)
    if portfolio.trades != len(trades):
        raise ValueError("screen portfolio trade count does not match its managed cells")
    gross_profit = math.fsum(max(0.0, trade.net_pnl_usd) for trade in trades)
    gross_loss = math.fsum(max(0.0, -trade.net_pnl_usd) for trade in trades)
    profit_factor = gross_profit / gross_loss if gross_loss > 0 else None
    expectancy = math.fsum(trade.net_pnl_usd for trade in trades) / len(trades) if trades else 0.0
    try:
        robust_sharpe: float | None = hac_sharpe(portfolio.daily_returns)
    except ValueError:
        robust_sharpe = None
    rejection_counts = Counter(
        disposition.reason.value
        for cell in cells
        for disposition in cell.result.dispositions
        if disposition.reason is not IntentDispositionReason.ENTERED
    )
    rejections = tuple((reason, rejection_counts[reason]) for reason in _rejection_inventory())
    return ScreenMetrics(
        trades=len(trades),
        net_return=portfolio.total_return,
        log_growth=math.log1p(portfolio.total_return),
        profit_factor=profit_factor,
        expectancy_usd_per_trade=expectancy,
        maximum_drawdown=portfolio.maximum_drawdown,
        hac_sharpe=robust_sharpe,
        fees_usd=math.fsum(cell.result.fees_usd for cell in cells),
        shortfall_usd=math.fsum(cell.result.implementation_shortfall_usd for cell in cells),
        funding_usd=math.fsum(cell.result.carry_cost_usd for cell in cells),
        rejection_dispositions=rejections,
    )


def _summarize_campaign(
    evidence: DevelopmentCampaignEvidence,
) -> tuple[ScenarioScreenMetrics, ...]:
    rows: list[ScenarioScreenMetrics] = []
    for scenario in evidence.scenarios:
        sleeve_rows: list[SleeveScreenMetrics] = []
        for sleeve_id in SLEEVE_IDS:
            sleeve_cells = tuple(cell for cell in scenario.cells if cell.sleeve_id == sleeve_id)
            if len(sleeve_cells) != len(SYMBOLS):
                raise ValueError("screen requires every symbol for every sleeve")
            sleeve_portfolio = synchronize_cells(tuple(cell.result.cell for cell in sleeve_cells))
            sleeve_rows.append(
                SleeveScreenMetrics(
                    sleeve_id=sleeve_id,
                    metrics=_metrics_for_cells(sleeve_portfolio, sleeve_cells),
                )
            )
        rows.append(
            ScenarioScreenMetrics(
                scenario_name=scenario.scenario.name,
                per_sleeve=tuple(sleeve_rows),
                combined=_metrics_for_cells(scenario.portfolio, scenario.cells),
            )
        )
    result = tuple(rows)
    if tuple(row.scenario_name for row in result) != SCENARIO_NAMES:
        raise ValueError("screen campaign must contain ordered baseline and stress evidence")
    return result


def _eligibility_failures(
    scenarios: tuple[ScenarioScreenMetrics, ...],
    *,
    minimum_pullback_trades: int,
) -> tuple[str, ...]:
    failures: list[str] = []
    for scenario in scenarios:
        pullback = scenario.sleeve(PULLBACK_SLEEVE_ID)
        if pullback.log_growth <= 0:
            failures.append(f"{scenario.scenario_name}_pullback_log_growth_not_positive")
        if pullback.profit_factor is None or pullback.profit_factor <= 1:
            failures.append(f"{scenario.scenario_name}_pullback_profit_factor_not_above_one")
        if pullback.expectancy_usd_per_trade <= 0:
            failures.append(f"{scenario.scenario_name}_pullback_expectancy_not_positive")
        if pullback.trades < minimum_pullback_trades:
            failures.append(f"{scenario.scenario_name}_pullback_closed_trades_below_minimum")
    return tuple(failures)


@dataclass(frozen=True, slots=True)
class TrialScreenResult:
    trial: ScreenTrialPlan
    campaign_evidence: DevelopmentCampaignEvidence
    scenarios: tuple[ScenarioScreenMetrics, ...]
    eligibility_failures: tuple[str, ...] = field(init=False)
    eligible: bool = field(init=False)
    worst_scenario_log_growth: float = field(init=False)

    def __post_init__(self) -> None:
        if not isinstance(self.trial, ScreenTrialPlan):
            raise TypeError("screen result trial must be a ScreenTrialPlan")
        evidence = self.campaign_evidence
        if not isinstance(evidence, DevelopmentCampaignEvidence):
            raise TypeError("screen trial must retain full DevelopmentCampaignEvidence")
        if evidence.candidate != self.trial.candidate:
            raise ValueError("campaign candidate does not match its preregistered trial")
        exact_campaign_values = (
            (evidence.protocol, DEFAULT_DEVELOPMENT_PROTOCOL),
            (evidence.protocol_name, DEFAULT_DEVELOPMENT_PROTOCOL.protocol_name),
            (evidence.protocol_sha256, DEFAULT_DEVELOPMENT_PROTOCOL.fingerprint()),
            (evidence.window_name, WINDOW_NAME),
            (evidence.role, DataRole.RESEARCH),
            (evidence.purpose, ResearchPurpose.FIT),
            (evidence.generation_start, GENERATION_START),
            (evidence.evaluation_start, EVALUATION_START),
            (evidence.evaluation_end, EVALUATION_END),
            (evidence.requested_initial_equity_usd, INITIAL_EQUITY_USD),
            (evidence.seed, BASE_SEED),
            (
                tuple(item.scenario for item in evidence.scenarios),
                development_scenarios(self.trial.candidate),
            ),
        )
        if any(actual != expected for actual, expected in exact_campaign_values):
            raise ValueError("campaign evidence violates the fixed screen plan")
        expected_scenarios = _summarize_campaign(evidence)
        if self.scenarios != expected_scenarios:
            raise ValueError("screen metrics must exactly match the full campaign evidence")
        failures = _eligibility_failures(
            self.scenarios,
            minimum_pullback_trades=MINIMUM_PULLBACK_CLOSED_TRADES,
        )
        object.__setattr__(self, "eligibility_failures", failures)
        object.__setattr__(self, "eligible", not failures)
        object.__setattr__(
            self,
            "worst_scenario_log_growth",
            min(scenario.sleeve(PULLBACK_SLEEVE_ID).log_growth for scenario in self.scenarios),
        )

    @property
    def stress_maximum_drawdown(self) -> float:
        return next(
            row.sleeve(PULLBACK_SLEEVE_ID).maximum_drawdown
            for row in self.scenarios
            if row.scenario_name == "stress"
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "campaign_evidence": self.campaign_evidence.to_dict(),
            "candidate_sha256": self.trial.candidate_sha256,
            "classification": CLASSIFICATION,
            "eligibility_failures": self.eligibility_failures,
            "eligible": self.eligible,
            "live_allowed": False,
            "metrics": [scenario.to_dict() for scenario in self.scenarios],
            "out_of_sample": False,
            "promotion_eligible": False,
            "reused_data": True,
            "shadow_allowed": False,
            "trial_id": self.trial.trial_id,
            "variant": self.trial.variant.value,
            "worst_scenario_log_growth": self.worst_scenario_log_growth,
        }


@dataclass(frozen=True, slots=True)
class ExperimentScreenResult:
    plan: ExperimentScreenPlan
    trials: tuple[TrialScreenResult, ...]
    classification: str = field(init=False, default=CLASSIFICATION)
    reused_data: bool = field(init=False, default=True)
    out_of_sample: bool = field(init=False, default=False)
    promotion_eligible: bool = field(init=False, default=False)
    shadow_allowed: bool = field(init=False, default=False)
    live_allowed: bool = field(init=False, default=False)
    ranked_eligible_trial_ids: tuple[str, ...] = field(init=False)
    selected_trial_id: str = field(init=False)

    def __post_init__(self) -> None:
        if not isinstance(self.plan, ExperimentScreenPlan):
            raise TypeError("screen result requires its canonical ExperimentScreenPlan")
        if not isinstance(self.trials, tuple) or any(
            not isinstance(trial, TrialScreenResult) for trial in self.trials
        ):
            raise TypeError("screen trials must be an immutable TrialScreenResult tuple")
        if tuple(trial.trial for trial in self.trials) != self.plan.trials:
            raise ValueError("screen results must contain exactly the three planned trials")
        eligible = [trial for trial in self.trials if trial.eligible]
        eligible.sort(
            key=lambda trial: (
                -trial.worst_scenario_log_growth,
                trial.stress_maximum_drawdown,
                trial.trial.fixed_order,
            )
        )
        ranking = tuple(trial.trial.trial_id for trial in eligible)
        object.__setattr__(self, "ranked_eligible_trial_ids", ranking)
        object.__setattr__(self, "selected_trial_id", ranking[0] if ranking else REJECT_ALL)

    def to_dict(self) -> dict[str, object]:
        return cast(
            dict[str, object],
            _json_ready(
                {
                    "classification": self.classification,
                    "live_allowed": self.live_allowed,
                    "out_of_sample": self.out_of_sample,
                    "permissions": {
                        "live_allowed": self.live_allowed,
                        "promotion_eligible": self.promotion_eligible,
                        "shadow_allowed": self.shadow_allowed,
                    },
                    "plan_sha256": self.plan.plan_sha256,
                    "promotion_eligible": self.promotion_eligible,
                    "ranking": {
                        "ranked_eligible_trial_ids": self.ranked_eligible_trial_ids,
                        "selected_trial_id": self.selected_trial_id,
                    },
                    "reused_data": self.reused_data,
                    "schema_version": 1,
                    "shadow_allowed": self.shadow_allowed,
                    "trials": [trial.to_dict() for trial in self.trials],
                }
            ),
        )


def _validate_complete_cached_slice(
    symbol: str,
    candles: list[Candle],
    manifest: DatasetManifest,
) -> None:
    """Fail closed on any missing, duplicated, or imputed required minute."""

    expected_rows = (EVALUATION_END - GENERATION_START).days * 24 * 60
    expected_start_ms = int(datetime.combine(GENERATION_START, datetime.min.time(), UTC).timestamp() * 1_000)
    expected_end_ms = int(datetime.combine(EVALUATION_END, datetime.min.time(), UTC).timestamp() * 1_000)
    if not isinstance(manifest, DatasetManifest):
        raise TypeError("offline loader must return a DatasetManifest")
    manifest_values = (
        (manifest.symbol, symbol),
        (manifest.interval, "1m"),
        (manifest.requested_start, GENERATION_START.isoformat()),
        (manifest.requested_end, EVALUATION_END.isoformat()),
        (manifest.actual_start_ms, expected_start_ms),
        (manifest.actual_end_ms, expected_end_ms - 1),
        (manifest.rows, expected_rows),
        (manifest.gaps, 0),
        (manifest.expected_files, len(month_starts(GENERATION_START, EVALUATION_END))),
        (len(manifest.files), manifest.expected_files),
    )
    if any(actual != expected for actual, expected in manifest_values):
        raise ValueError(f"incomplete offline cache evidence for {symbol}")
    if (
        manifest.transport_verification != "zip_crc_and_parsed_rows_sha256"
        or manifest.checksum_status != "official_sha256_verified"
        or manifest.checksum_files_verified != manifest.expected_files
    ):
        raise ValueError(f"offline cache lacks verified official checksums for {symbol}")
    if (
        manifest.csv_schema != "binance_futures_kline_v1_12_columns"
        or len(manifest.sha256) != 64
        or manifest.sha256 != manifest.sha256.lower()
        or any(character not in "0123456789abcdef" for character in manifest.sha256)
    ):
        raise ValueError(f"offline cache provenance is malformed for {symbol}")
    if len(candles) != expected_rows:
        raise ValueError(f"incomplete offline candle slice for {symbol}")
    for index, candle in enumerate(candles):
        expected_open = expected_start_ms + index * 60_000
        if not isinstance(candle, Candle):
            raise TypeError("offline cache must contain Candle values")
        if (
            candle.symbol != symbol
            or candle.timeframe != "1m"
            or candle.open_time_ms != expected_open
            or candle.close_time_ms != expected_open + 59_999
        ):
            raise ValueError(f"offline cache has a missing, duplicate, or malformed minute for {symbol}")


def _load_fixed_cache(cache_dir: Path) -> dict[str, list[Candle]]:
    loader = BinanceArchiveLoader(cache_dir, allow_download=False)
    candles_by_symbol: dict[str, list[Candle]] = {}
    for symbol in SYMBOLS:
        candles, manifest = loader.load(symbol, GENERATION_START, EVALUATION_END, "1m")
        _validate_complete_cached_slice(symbol, candles, manifest)
        candles_by_symbol[symbol] = candles
    return candles_by_symbol


def _fsync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    descriptor: int | None = None
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        os.fsync(descriptor)
    except OSError:
        return
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _serialized_json(payload: object) -> bytes:
    return _canonical_json_bytes(payload) + b"\n"


def _atomic_write_json(path: Path, payload: object, *, overwrite: bool) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as output:
            output.write(_serialized_json(payload))
            output.flush()
            os.fsync(output.fileno())
        if overwrite:
            os.replace(temporary_path, path)
        else:
            try:
                os.link(temporary_path, path)
            except FileExistsError as exc:
                raise FileExistsError(f"refusing to overwrite existing output: {path}") from exc
            temporary_path.unlink()
        _fsync_directory(path.parent)
    finally:
        if temporary_path.exists():
            temporary_path.unlink()


def _validate_output_paths(plan_output: Path, result_output: Path, *, overwrite: bool) -> None:
    if not isinstance(plan_output, Path) or not isinstance(result_output, Path):
        raise TypeError("screen output paths must be pathlib.Path values")
    if plan_output.resolve() == result_output.resolve():
        raise ValueError("plan and result outputs must be different files")
    if not overwrite:
        existing = tuple(path for path in (plan_output, result_output) if path.exists())
        if existing:
            names = ", ".join(str(path) for path in existing)
            raise FileExistsError(f"refusing to overwrite existing output: {names}")


def run_development_screen(
    cache_dir: Path,
    plan_output: Path,
    result_output: Path,
    *,
    overwrite: bool = False,
) -> ExperimentScreenResult:
    """Run the exact offline screen and persist preregistration plus evidence."""

    if not isinstance(cache_dir, Path):
        raise TypeError("cache_dir must be a pathlib.Path")
    if type(overwrite) is not bool:
        raise TypeError("overwrite must be boolean")
    _validate_output_paths(plan_output, result_output, overwrite=overwrite)

    plan = ExperimentScreenPlan()
    _atomic_write_json(plan_output, plan.to_dict(), overwrite=overwrite)
    if overwrite and result_output.exists():
        result_output.unlink()
        _fsync_directory(result_output.parent)

    candles_by_symbol = _load_fixed_cache(cache_dir)
    trial_results: list[TrialScreenResult] = []
    for trial in plan.trials:
        evidence = run_development_campaign(
            candles_by_symbol,
            window_name=plan.window_name,
            purpose=plan.purpose,
            candidate=trial.candidate,
            protocol=plan.protocol,
            evaluation_start=plan.evaluation_start,
            evaluation_end=plan.evaluation_end,
            initial_equity_usd=plan.initial_equity_usd,
            scenarios=development_scenarios(trial.candidate),
            seed=plan.seed,
        )
        trial_results.append(
            TrialScreenResult(
                trial=trial,
                campaign_evidence=evidence,
                scenarios=_summarize_campaign(evidence),
            )
        )
    result = ExperimentScreenResult(plan=plan, trials=tuple(trial_results))

    expected_plan_bytes = _serialized_json(plan.to_dict())
    if plan_output.read_bytes() != expected_plan_bytes:
        raise RuntimeError("screen plan changed after preregistration; result will not be written")
    if ScreenEnvironment.capture() != plan.environment:
        raise RuntimeError("screen source, dependency lock, or runtime changed during evaluation")
    _atomic_write_json(result_output, result.to_dict(), overwrite=False)
    return result


def _print_summary(result: ExperimentScreenResult) -> None:
    print("Development diagnostics only — reused RESEARCH data, never promotion evidence.")
    for trial in result.trials:
        baseline, stress = trial.scenarios
        status = "ELIGIBLE" if trial.eligible else "REJECT"
        print(
            f"{trial.trial.trial_id}: {status}; "
            f"baseline log={baseline.combined.log_growth:.6f}, "
            f"stress log={stress.combined.log_growth:.6f}, "
            f"stress DD={stress.combined.maximum_drawdown:.4%}, "
            f"pullback trades={baseline.sleeve(PULLBACK_SLEEVE_ID).trades}/"
            f"{stress.sleeve(PULLBACK_SLEEVE_ID).trades}"
        )
    print(f"Screen decision: {result.selected_trial_id}")
    print("promotion_eligible=false shadow_allowed=false live_allowed=false")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run the fixed three-variant offline development diagnostic screen"
    )
    parser.add_argument("--cache-dir", type=Path, default=Path("data/historical"))
    parser.add_argument(
        "--plan-output",
        type=Path,
        default=Path("reports/development-screen/plan.json"),
    )
    parser.add_argument(
        "--result-output",
        type=Path,
        default=Path("reports/development-screen/result.json"),
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="replace prior outputs explicitly; default is fail-closed",
    )
    args = parser.parse_args()
    result = run_development_screen(
        args.cache_dir,
        args.plan_output,
        args.result_output,
        overwrite=args.overwrite,
    )
    _print_summary(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
