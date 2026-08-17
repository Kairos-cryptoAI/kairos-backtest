"""Immutable offline development screen for the order-flow expansion sleeve.

The screen deliberately reuses one fixed RESEARCH/FIT slice.  Its output is
development evidence only: it cannot authorize promotion, shadow execution,
or live trading.  The complete plan is written before the historical cache is
opened, and the screen runs only from a clean, exactly identified Git tree.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import subprocess  # nosec B404
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
from .managed_evaluation import IntentDispositionReason
from .orderflow_campaign import (
    DEFAULT_ORDERFLOW_PROTOCOL,
    DEFAULT_ORDERFLOW_SEED,
    ORDERFLOW_EVALUATION_END,
    ORDERFLOW_EVALUATION_START,
    ORDERFLOW_GENERATION_START,
    ORDERFLOW_SLEEVE_ID,
    OrderFlowCampaignEvidence,
    OrderFlowCandidate,
    OrderFlowCellEvidence,
    orderflow_scenarios,
    run_orderflow_campaign,
)
from .portfolio import PortfolioEvidence, synchronize_cells
from .provenance import RuntimeProvenance, runtime_provenance, source_fingerprint
from .research_protocol import DataRole, ResearchProtocol, ResearchPurpose
from .robustness import hac_sharpe
from .scenarios import SYMBOLS
from .sleeves.orderflow_volatility_expansion import (
    OrderFlowExpansionVariant,
    OrderFlowVolatilityExpansionConfig,
)

PLAN_VERSION = "kairos.orderflow-development-screen.v1"
RESULT_SCHEMA_VERSION = 1
CLASSIFICATION = "development_diagnostics_only"
REJECT_ALL = "REJECT_ALL"
GENERATION_START = ORDERFLOW_GENERATION_START
EVALUATION_START = ORDERFLOW_EVALUATION_START
EVALUATION_END = ORDERFLOW_EVALUATION_END
WARMUP_DAYS = 35
WINDOW_NAME = "research"
BASE_SEED = DEFAULT_ORDERFLOW_SEED
INITIAL_EQUITY_USD = 100_000.0
SCENARIO_NAMES = ("baseline", "stress")
TRIAL_VARIANTS = (
    OrderFlowExpansionVariant.IMPULSE,
    OrderFlowExpansionVariant.PERSISTENCE,
    OrderFlowExpansionVariant.FLIP_RELEASE,
)

MINIMUM_CLOSED_TRADES_PER_SCENARIO = 200
MINIMUM_CLOSED_TRADES_PER_SYMBOL = 20
MINIMUM_DISTINCT_UTC_EXIT_DAYS = 60
MINIMUM_POSITIVE_EXPECTANCY_SYMBOLS = 3
MAXIMUM_ONE_SYMBOL_TRADE_SHARE = 0.50
MAXIMUM_STRESS_DRAWDOWN = 0.05

_MINUTE_MS = 60_000
_HEX = frozenset("0123456789abcdef")


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


def _file_sha256(path: Path, *, normalize_newlines: bool = False) -> str:
    if not path.is_file():
        raise FileNotFoundError(f"screen provenance file is unavailable: {path}")
    payload = path.read_bytes()
    if normalize_newlines:
        payload = payload.replace(b"\r\n", b"\n").replace(b"\r", b"\n")
    return hashlib.sha256(payload).hexdigest()


def _validate_hex_digest(name: str, value: str, *, lengths: tuple[int, ...]) -> None:
    if (
        type(value) is not str
        or len(value) not in lengths
        or value != value.lower()
        or any(character not in _HEX for character in value)
    ):
        choices = "/".join(str(length) for length in lengths)
        raise ValueError(f"screen {name} must be a lowercase {choices}-character hex digest")


def _git_text(project_root: Path, *arguments: str) -> str:
    """Invoke Git without a shell using fixed arguments from provenance calls."""

    try:
        # Arguments come only from the fixed calls in the provenance routines.
        completed = subprocess.run(  # nosec B603
            ("git", *arguments),
            cwd=project_root,
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        raise RuntimeError("order-flow screen requires an accessible Git worktree") from exc
    return completed.stdout.strip()


def _package_source_files(project_root: Path) -> tuple[tuple[str, str], ...]:
    package_root = project_root / "kairos_backtest"
    rows = tuple(
        (
            path.relative_to(project_root).as_posix(),
            _file_sha256(path, normalize_newlines=True),
        )
        for path in sorted(package_root.rglob("*.py"))
    )
    if not rows:
        raise FileNotFoundError("kairos_backtest package sources are unavailable")
    return rows


@dataclass(frozen=True, slots=True)
class _EnvironmentSnapshot:
    """One internally consistent read of every execution-relevant input."""

    git_head_sha: str
    git_tree_sha: str
    source_sha256: str
    source_files: tuple[tuple[str, str], ...]
    pyproject_sha256: str
    uv_lock_sha256: str
    runtime: RuntimeProvenance


def _read_environment_snapshot(project_root: Path) -> _EnvironmentSnapshot:
    return _EnvironmentSnapshot(
        git_head_sha=_git_text(project_root, "rev-parse", "HEAD"),
        git_tree_sha=_git_text(project_root, "rev-parse", "HEAD^{tree}"),
        source_sha256=source_fingerprint(project_root / "kairos_backtest"),
        source_files=_package_source_files(project_root),
        pyproject_sha256=_file_sha256(project_root / "pyproject.toml"),
        uv_lock_sha256=_file_sha256(project_root / "uv.lock"),
        runtime=runtime_provenance(),
    )


def _git_status(project_root: Path, *, relevant_source_only: bool) -> str:
    arguments = ["status", "--porcelain=v1", "--untracked-files=all"]
    if relevant_source_only:
        arguments.extend(
            (
                "--",
                ":(glob)kairos_backtest/**/*.py",
                "pyproject.toml",
                "uv.lock",
            )
        )
    return _git_text(project_root, *arguments)


def _double_checked_snapshot(
    project_root: Path,
    *,
    relevant_source_only: bool,
) -> tuple[str, _EnvironmentSnapshot, _EnvironmentSnapshot, str]:
    """Bracket two complete snapshots with Git status observations."""

    status_before = _git_status(project_root, relevant_source_only=relevant_source_only)
    first = _read_environment_snapshot(project_root)
    second = _read_environment_snapshot(project_root)
    status_after = _git_status(project_root, relevant_source_only=relevant_source_only)
    return status_before, first, second, status_after


@dataclass(frozen=True, slots=True)
class OrderFlowScreenEnvironment:
    """Committed source, dependency lock, runtime, and per-file provenance."""

    git_head_sha: str
    git_tree_sha: str
    git_dirty: bool
    source_sha256: str
    source_files: tuple[tuple[str, str], ...]
    pyproject_sha256: str
    uv_lock_sha256: str
    runtime: RuntimeProvenance
    environment_sha256: str = field(init=False)

    def __post_init__(self) -> None:
        _validate_hex_digest("git_head_sha", self.git_head_sha, lengths=(40, 64))
        _validate_hex_digest("git_tree_sha", self.git_tree_sha, lengths=(40, 64))
        for name, value in (
            ("source_sha256", self.source_sha256),
            ("pyproject_sha256", self.pyproject_sha256),
            ("uv_lock_sha256", self.uv_lock_sha256),
        ):
            _validate_hex_digest(name, value, lengths=(64,))
        if type(self.git_dirty) is not bool or self.git_dirty:
            raise ValueError("order-flow screen provenance requires git_dirty=false")
        if not isinstance(self.runtime, RuntimeProvenance):
            raise TypeError("screen runtime must be RuntimeProvenance")
        if type(self.source_files) is not tuple or not self.source_files:
            raise TypeError("screen source_files must be a non-empty immutable tuple")
        paths = tuple(path for path, _ in self.source_files)
        if paths != tuple(sorted(paths)) or len(paths) != len(set(paths)):
            raise ValueError("screen source-file paths must be unique and sorted")
        for path, digest in self.source_files:
            if (
                type(path) is not str
                or not path.startswith("kairos_backtest/")
                or not path.endswith(".py")
                or "\\" in path
                or path.startswith("/")
                or "/../" in f"/{path}/"
            ):
                raise ValueError("screen source-file paths must be normalized package-relative paths")
            _validate_hex_digest(f"source file {path}", digest, lengths=(64,))
        object.__setattr__(self, "environment_sha256", _sha256(self._payload()))

    @classmethod
    def capture(cls) -> OrderFlowScreenEnvironment:
        project_root = Path(__file__).resolve().parents[1]
        status_before, first, second, status_after = _double_checked_snapshot(
            project_root,
            relevant_source_only=False,
        )
        if status_before or status_after:
            raise RuntimeError("order-flow screen refuses to run from a dirty Git worktree")
        if first != second:
            raise RuntimeError("order-flow screen provenance changed while it was being captured")
        return cls(
            git_head_sha=second.git_head_sha,
            git_tree_sha=second.git_tree_sha,
            git_dirty=False,
            source_sha256=second.source_sha256,
            source_files=second.source_files,
            pyproject_sha256=second.pyproject_sha256,
            uv_lock_sha256=second.uv_lock_sha256,
            runtime=second.runtime,
        )

    def _payload(self) -> dict[str, object]:
        return {
            "git": {
                "dirty": self.git_dirty,
                "head_sha": self.git_head_sha,
                "tree_sha": self.git_tree_sha,
            },
            "pyproject_sha256": self.pyproject_sha256,
            "runtime": self.runtime.as_dict(),
            "source_files": [{"path": path, "sha256": digest} for path, digest in self.source_files],
            "source_sha256": self.source_sha256,
            "uv_lock_sha256": self.uv_lock_sha256,
        }

    def to_dict(self) -> dict[str, object]:
        return {**self._payload(), "environment_sha256": self.environment_sha256}


def _capture_clean_environment() -> OrderFlowScreenEnvironment:
    return OrderFlowScreenEnvironment.capture()


def _assert_environment_stable(expected: OrderFlowScreenEnvironment) -> None:
    """Detect source/dependency/runtime drift after expected output files appear."""

    if not isinstance(expected, OrderFlowScreenEnvironment):
        raise TypeError("expected environment must be OrderFlowScreenEnvironment")
    project_root = Path(__file__).resolve().parents[1]
    status_before, first, second, status_after = _double_checked_snapshot(
        project_root,
        relevant_source_only=True,
    )
    registered = _EnvironmentSnapshot(
        git_head_sha=expected.git_head_sha,
        git_tree_sha=expected.git_tree_sha,
        source_sha256=expected.source_sha256,
        source_files=expected.source_files,
        pyproject_sha256=expected.pyproject_sha256,
        uv_lock_sha256=expected.uv_lock_sha256,
        runtime=expected.runtime,
    )
    if status_before or status_after or first != second or second != registered:
        raise RuntimeError("screen source, Git tree, dependency lock, or runtime changed during evaluation")


def _candidate_for_variant(variant: OrderFlowExpansionVariant) -> OrderFlowCandidate:
    if not isinstance(variant, OrderFlowExpansionVariant):
        raise TypeError("screen variant must be an OrderFlowExpansionVariant")
    return OrderFlowCandidate(config=OrderFlowVolatilityExpansionConfig(variant=variant))


@dataclass(frozen=True, slots=True)
class OrderFlowScreenTrial:
    """One and only one preregistered order-flow explanation."""

    trial_id: str
    fixed_order: int
    variant: OrderFlowExpansionVariant
    candidate: OrderFlowCandidate
    candidate_sha256: str

    def __post_init__(self) -> None:
        if type(self.trial_id) is not str or type(self.fixed_order) is not int:
            raise TypeError("trial identity and fixed order must use canonical types")
        if not isinstance(self.variant, OrderFlowExpansionVariant):
            raise TypeError("trial variant must be an OrderFlowExpansionVariant")
        if not isinstance(self.candidate, OrderFlowCandidate):
            raise TypeError("trial candidate must be an OrderFlowCandidate")
        expected_order = TRIAL_VARIANTS.index(self.variant)
        if self.trial_id != self.variant.name or self.fixed_order != expected_order:
            raise ValueError("trial identity must match the fixed IMPULSE/PERSISTENCE/FLIP_RELEASE order")
        expected_candidate = _candidate_for_variant(self.variant)
        if self.candidate != expected_candidate:
            raise ValueError("trial candidate must match the exact registered variant configuration")
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


def _fixed_trials() -> tuple[OrderFlowScreenTrial, ...]:
    return tuple(
        OrderFlowScreenTrial(
            trial_id=variant.name,
            fixed_order=index,
            variant=variant,
            candidate=(candidate := _candidate_for_variant(variant)),
            candidate_sha256=candidate.candidate_sha256,
        )
        for index, variant in enumerate(TRIAL_VARIANTS)
    )


@dataclass(frozen=True, slots=True)
class OrderFlowScreenPlan:
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
    protocol: ResearchProtocol = DEFAULT_ORDERFLOW_PROTOCOL
    trials: tuple[OrderFlowScreenTrial, ...] = field(default_factory=_fixed_trials)
    environment: OrderFlowScreenEnvironment = field(default_factory=lambda: _capture_clean_environment())
    plan_sha256: str = field(init=False)

    def __post_init__(self) -> None:
        if not isinstance(self.environment, OrderFlowScreenEnvironment):
            raise TypeError("screen plan environment must be OrderFlowScreenEnvironment")
        if not isinstance(self.protocol, ResearchProtocol):
            raise TypeError("screen plan protocol must be a ResearchProtocol")
        if type(self.trials) is not tuple or any(
            not isinstance(trial, OrderFlowScreenTrial) for trial in self.trials
        ):
            raise TypeError("screen trials must be an immutable OrderFlowScreenTrial tuple")
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
            (self.protocol, DEFAULT_ORDERFLOW_PROTOCOL),
            (self.trials, _fixed_trials()),
        )
        if any(actual != expected for actual, expected in exact_values):
            raise ValueError("order-flow screen plan must preserve every fixed preregistered value")
        exact_flags = (
            (self.reused_data, True),
            (self.out_of_sample, False),
            (self.promotion_eligible, False),
            (self.shadow_allowed, False),
            (self.live_allowed, False),
        )
        if any(type(actual) is not bool or actual is not expected for actual, expected in exact_flags):
            raise ValueError("order-flow screen classification flags are immutable")
        if self.generation_start != self.evaluation_start - timedelta(days=self.warmup_days):
            raise ValueError("order-flow screen must preserve the fixed 35-day warm-up")
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
                "each_baseline_and_stress": {
                    "distinct_utc_exit_days_at_least": MINIMUM_DISTINCT_UTC_EXIT_DAYS,
                    "expectancy_usd_per_trade_above": 0.0,
                    "log_growth_above": 0.0,
                    "maximum_one_symbol_trade_share_at_most": MAXIMUM_ONE_SYMBOL_TRADE_SHARE,
                    "minimum_closed_trades_per_symbol": MINIMUM_CLOSED_TRADES_PER_SYMBOL,
                    "positive_expectancy_symbols_at_least": MINIMUM_POSITIVE_EXPECTANCY_SYMBOLS,
                    "profit_factor_above": 1.0,
                    "total_closed_trades_at_least": MINIMUM_CLOSED_TRADES_PER_SCENARIO,
                },
                "stress_maximum_drawdown_at_most": MAXIMUM_STRESS_DRAWDOWN,
                "trade_count_is_ranking_objective": False,
            },
            "environment": self.environment.to_dict(),
            "execution": {
                "initial_equity_usd": self.initial_equity_usd,
                "scenario_factory": "orderflow_scenarios",
                "scenario_names": self.scenario_names,
                "seed": self.seed,
            },
            "permissions": {
                "live_allowed": self.live_allowed,
                "promotion_eligible": self.promotion_eligible,
                "shadow_allowed": self.shadow_allowed,
            },
            "plan_version": self.version,
            "protocol": {
                "definition": asdict(self.protocol),
                "name": self.protocol.protocol_name,
                "sha256": self.protocol.fingerprint(),
            },
            "ranking": {
                "primary": "maximize_minimum_baseline_stress_log_growth",
                "secondary": "minimize_stress_maximum_drawdown",
                "tertiary": "fixed_IMPULSE_PERSISTENCE_FLIP_RELEASE_order",
            },
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
class OrderFlowMetrics:
    """Cost-aware metrics for one symbol or synchronized scenario."""

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
class OrderFlowSymbolMetrics:
    symbol: str
    metrics: OrderFlowMetrics

    def __post_init__(self) -> None:
        if self.symbol not in SYMBOLS:
            raise ValueError("order-flow screen symbol metric has an unknown symbol")
        if not isinstance(self.metrics, OrderFlowMetrics):
            raise TypeError("order-flow symbol metric requires OrderFlowMetrics")

    def to_dict(self) -> dict[str, object]:
        return {"symbol": self.symbol, **self.metrics.to_dict()}


@dataclass(frozen=True, slots=True)
class OrderFlowScenarioMetrics:
    scenario_name: str
    per_symbol: tuple[OrderFlowSymbolMetrics, ...]
    combined: OrderFlowMetrics
    distinct_utc_exit_days: int
    positive_expectancy_symbols: int
    maximum_one_symbol_trade_share: float

    def __post_init__(self) -> None:
        if self.scenario_name not in SCENARIO_NAMES:
            raise ValueError("order-flow screen metric has an unknown scenario")
        if not isinstance(self.per_symbol, tuple) or any(
            not isinstance(item, OrderFlowSymbolMetrics) for item in self.per_symbol
        ):
            raise TypeError("per-symbol screen metrics must be an immutable tuple")
        if tuple(item.symbol for item in self.per_symbol) != SYMBOLS:
            raise ValueError("per-symbol screen metrics must preserve the fixed universe order")
        if not isinstance(self.combined, OrderFlowMetrics):
            raise TypeError("combined scenario metric requires OrderFlowMetrics")
        for name, value in (
            ("distinct_utc_exit_days", self.distinct_utc_exit_days),
            ("positive_expectancy_symbols", self.positive_expectancy_symbols),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{name} must be a non-negative integer")
        if self.positive_expectancy_symbols > len(SYMBOLS):
            raise ValueError("positive expectancy symbol count exceeds the fixed universe")
        if (
            isinstance(self.maximum_one_symbol_trade_share, bool)
            or not math.isfinite(self.maximum_one_symbol_trade_share)
            or not 0 <= self.maximum_one_symbol_trade_share <= 1
        ):
            raise ValueError("maximum one-symbol trade share must lie in [0, 1]")
        symbol_trades = tuple(item.metrics.trades for item in self.per_symbol)
        if sum(symbol_trades) != self.combined.trades:
            raise ValueError("per-symbol trade counts must sum to the combined count")
        expected_share = max(symbol_trades, default=0) / self.combined.trades if self.combined.trades else 0.0
        if self.maximum_one_symbol_trade_share != expected_share:
            raise ValueError("maximum one-symbol trade share must match per-symbol counts")
        expected_positive = sum(item.metrics.expectancy_usd_per_trade > 0 for item in self.per_symbol)
        if self.positive_expectancy_symbols != expected_positive:
            raise ValueError("positive expectancy symbol count must match per-symbol metrics")

    def symbol(self, symbol: str) -> OrderFlowMetrics:
        try:
            return next(item.metrics for item in self.per_symbol if item.symbol == symbol)
        except StopIteration as exc:
            raise KeyError(f"missing order-flow symbol screen metrics: {symbol}") from exc

    def to_dict(self) -> dict[str, object]:
        return {
            "combined": self.combined.to_dict(),
            "distinct_utc_exit_days": self.distinct_utc_exit_days,
            "maximum_one_symbol_trade_share": self.maximum_one_symbol_trade_share,
            "per_symbol": [item.to_dict() for item in self.per_symbol],
            "positive_expectancy_symbols": self.positive_expectancy_symbols,
            "scenario_name": self.scenario_name,
        }


def _metrics_for_cells(
    portfolio: PortfolioEvidence,
    cells: tuple[OrderFlowCellEvidence, ...],
) -> OrderFlowMetrics:
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
    return OrderFlowMetrics(
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
        rejection_dispositions=tuple((reason, rejection_counts[reason]) for reason in _rejection_inventory()),
    )


def _summarize_campaign(
    evidence: OrderFlowCampaignEvidence,
) -> tuple[OrderFlowScenarioMetrics, ...]:
    rows: list[OrderFlowScenarioMetrics] = []
    for scenario in evidence.scenarios:
        symbol_rows: list[OrderFlowSymbolMetrics] = []
        exit_days: set[date] = set()
        for symbol in SYMBOLS:
            symbol_cells = tuple(cell for cell in scenario.cells if cell.symbol == symbol)
            if len(symbol_cells) != 1 or symbol_cells[0].sleeve_id != ORDERFLOW_SLEEVE_ID:
                raise ValueError("screen requires exactly one order-flow cell per symbol")
            cell = symbol_cells[0]
            portfolio = synchronize_cells((cell.result.cell,))
            symbol_rows.append(
                OrderFlowSymbolMetrics(
                    symbol=symbol,
                    metrics=_metrics_for_cells(portfolio, symbol_cells),
                )
            )
            exit_days.update(
                datetime.fromtimestamp(trade.exit_timestamp_ms / 1_000, UTC).date()
                for trade in cell.result.cell.trades
            )
        combined = _metrics_for_cells(scenario.portfolio, scenario.cells)
        per_symbol = tuple(symbol_rows)
        rows.append(
            OrderFlowScenarioMetrics(
                scenario_name=scenario.scenario.name,
                per_symbol=per_symbol,
                combined=combined,
                distinct_utc_exit_days=len(exit_days),
                positive_expectancy_symbols=sum(
                    item.metrics.expectancy_usd_per_trade > 0 for item in per_symbol
                ),
                maximum_one_symbol_trade_share=(
                    max(item.metrics.trades for item in per_symbol) / combined.trades
                    if combined.trades
                    else 0.0
                ),
            )
        )
    result = tuple(rows)
    if tuple(row.scenario_name for row in result) != SCENARIO_NAMES:
        raise ValueError("screen campaign must contain ordered baseline and stress evidence")
    return result


def _eligibility_failures(
    scenarios: tuple[OrderFlowScenarioMetrics, ...],
) -> tuple[str, ...]:
    if tuple(item.scenario_name for item in scenarios) != SCENARIO_NAMES:
        raise ValueError("eligibility requires ordered baseline and stress scenarios")
    failures: list[str] = []
    for scenario in scenarios:
        prefix = scenario.scenario_name
        metrics = scenario.combined
        if metrics.log_growth <= 0:
            failures.append(f"{prefix}_log_growth_not_positive")
        if metrics.profit_factor is None or metrics.profit_factor <= 1:
            failures.append(f"{prefix}_profit_factor_not_above_one")
        if metrics.expectancy_usd_per_trade <= 0:
            failures.append(f"{prefix}_expectancy_not_positive")
        if metrics.trades < MINIMUM_CLOSED_TRADES_PER_SCENARIO:
            failures.append(f"{prefix}_closed_trades_below_minimum")
        for item in scenario.per_symbol:
            if item.metrics.trades < MINIMUM_CLOSED_TRADES_PER_SYMBOL:
                failures.append(f"{prefix}_{item.symbol}_closed_trades_below_minimum")
        if scenario.distinct_utc_exit_days < MINIMUM_DISTINCT_UTC_EXIT_DAYS:
            failures.append(f"{prefix}_distinct_utc_exit_days_below_minimum")
        if scenario.positive_expectancy_symbols < MINIMUM_POSITIVE_EXPECTANCY_SYMBOLS:
            failures.append(f"{prefix}_positive_expectancy_symbols_below_minimum")
        if scenario.maximum_one_symbol_trade_share > MAXIMUM_ONE_SYMBOL_TRADE_SHARE:
            failures.append(f"{prefix}_one_symbol_trade_share_above_maximum")
    stress = scenarios[1]
    if stress.combined.maximum_drawdown > MAXIMUM_STRESS_DRAWDOWN:
        failures.append("stress_maximum_drawdown_above_maximum")
    return tuple(failures)


@dataclass(frozen=True, slots=True)
class OrderFlowTrialResult:
    trial: OrderFlowScreenTrial
    campaign_evidence: OrderFlowCampaignEvidence
    scenarios: tuple[OrderFlowScenarioMetrics, ...]
    eligibility_failures: tuple[str, ...] = field(init=False)
    eligible: bool = field(init=False)
    worst_scenario_log_growth: float = field(init=False)

    def __post_init__(self) -> None:
        if not isinstance(self.trial, OrderFlowScreenTrial):
            raise TypeError("screen result trial must be an OrderFlowScreenTrial")
        evidence = self.campaign_evidence
        if not isinstance(evidence, OrderFlowCampaignEvidence):
            raise TypeError("screen trial must retain full OrderFlowCampaignEvidence")
        if evidence.candidate != self.trial.candidate:
            raise ValueError("campaign candidate does not match its preregistered trial")
        exact_campaign_values = (
            (evidence.protocol, DEFAULT_ORDERFLOW_PROTOCOL),
            (evidence.protocol_name, DEFAULT_ORDERFLOW_PROTOCOL.protocol_name),
            (evidence.protocol_sha256, DEFAULT_ORDERFLOW_PROTOCOL.fingerprint()),
            (evidence.window_name, WINDOW_NAME),
            (evidence.role, DataRole.RESEARCH),
            (evidence.purpose, ResearchPurpose.FIT),
            (evidence.generation_start, GENERATION_START),
            (evidence.evaluation_start, EVALUATION_START),
            (evidence.evaluation_end, EVALUATION_END),
            (evidence.requested_initial_equity_usd, INITIAL_EQUITY_USD),
            (evidence.seed, BASE_SEED),
            (tuple(item.scenario for item in evidence.scenarios), orderflow_scenarios(self.trial.candidate)),
        )
        if any(actual != expected for actual, expected in exact_campaign_values):
            raise ValueError("campaign evidence violates the fixed order-flow screen plan")
        expected_scenarios = _summarize_campaign(evidence)
        if self.scenarios != expected_scenarios:
            raise ValueError("screen metrics must exactly match the full campaign evidence")
        failures = _eligibility_failures(self.scenarios)
        object.__setattr__(self, "eligibility_failures", failures)
        object.__setattr__(self, "eligible", not failures)
        object.__setattr__(
            self,
            "worst_scenario_log_growth",
            min(scenario.combined.log_growth for scenario in self.scenarios),
        )

    @property
    def stress_maximum_drawdown(self) -> float:
        return self.scenarios[1].combined.maximum_drawdown

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
class OrderFlowScreenResult:
    plan: OrderFlowScreenPlan
    trials: tuple[OrderFlowTrialResult, ...]
    classification: str = field(init=False, default=CLASSIFICATION)
    reused_data: bool = field(init=False, default=True)
    out_of_sample: bool = field(init=False, default=False)
    promotion_eligible: bool = field(init=False, default=False)
    shadow_allowed: bool = field(init=False, default=False)
    live_allowed: bool = field(init=False, default=False)
    ranked_eligible_trial_ids: tuple[str, ...] = field(init=False)
    selected_trial_id: str = field(init=False)

    def __post_init__(self) -> None:
        if not isinstance(self.plan, OrderFlowScreenPlan):
            raise TypeError("screen result requires its canonical OrderFlowScreenPlan")
        if not isinstance(self.trials, tuple) or any(
            not isinstance(trial, OrderFlowTrialResult) for trial in self.trials
        ):
            raise TypeError("screen trials must be an immutable OrderFlowTrialResult tuple")
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
                    "schema_version": RESULT_SCHEMA_VERSION,
                    "shadow_allowed": self.shadow_allowed,
                    "trials": [trial.to_dict() for trial in self.trials],
                }
            ),
        )


def compact_orderflow_summary(result: OrderFlowScreenResult) -> dict[str, object]:
    """Return deterministic, review-sized evidence without the full trade ledger."""

    if not isinstance(result, OrderFlowScreenResult):
        raise TypeError("compact summary requires OrderFlowScreenResult")
    full_result_bytes = _serialized_json(result.to_dict())
    payload = {
        "classification": result.classification,
        "data": {
            "evaluation_end_exclusive": result.plan.evaluation_end,
            "evaluation_start": result.plan.evaluation_start,
            "generation_start": result.plan.generation_start,
            "out_of_sample": result.out_of_sample,
            "reused_data": result.reused_data,
            "universe": result.plan.universe,
        },
        "environment": result.plan.environment.to_dict(),
        "full_result": {
            "bytes": len(full_result_bytes),
            "schema_version": RESULT_SCHEMA_VERSION,
            "sha256": hashlib.sha256(full_result_bytes).hexdigest(),
        },
        "permissions": {
            "live_allowed": result.live_allowed,
            "promotion_eligible": result.promotion_eligible,
            "shadow_allowed": result.shadow_allowed,
        },
        "plan_sha256": result.plan.plan_sha256,
        "ranking": {
            "ranked_eligible_trial_ids": result.ranked_eligible_trial_ids,
            "selected_trial_id": result.selected_trial_id,
        },
        "schema_version": RESULT_SCHEMA_VERSION,
        "trials": [
            {
                "candidate_sha256": trial.trial.candidate_sha256,
                "eligibility_failures": trial.eligibility_failures,
                "eligible": trial.eligible,
                "metrics": [scenario.to_dict() for scenario in trial.scenarios],
                "trial_id": trial.trial.trial_id,
                "variant": trial.trial.variant,
                "worst_scenario_log_growth": trial.worst_scenario_log_growth,
            }
            for trial in result.trials
        ],
    }
    return cast(dict[str, object], _json_ready(payload))


def _validate_complete_cached_slice(
    symbol: str,
    candles: list[Candle],
    manifest: DatasetManifest,
) -> None:
    """Fail closed on checksum, schema, gaps, or malformed quote/taker data."""

    expected_rows = (EVALUATION_END - GENERATION_START).days * 24 * 60
    expected_start_ms = int(datetime.combine(GENERATION_START, datetime.min.time(), UTC).timestamp() * 1_000)
    expected_end_ms = int(datetime.combine(EVALUATION_END, datetime.min.time(), UTC).timestamp() * 1_000)
    expected_files = tuple(
        f"{symbol}-1m-{month:%Y-%m}.zip" for month in month_starts(GENERATION_START, EVALUATION_END)
    )
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
        (manifest.expected_files, len(expected_files)),
        (manifest.files, expected_files),
    )
    if any(actual != expected for actual, expected in manifest_values):
        raise ValueError(f"incomplete offline cache evidence for {symbol}")
    if (
        manifest.transport_verification != "zip_crc_and_parsed_rows_sha256"
        or manifest.checksum_status != "official_sha256_verified"
        or manifest.checksum_files_verified != manifest.expected_files
    ):
        raise ValueError(f"offline cache lacks verified official checksums and ZIP CRC evidence for {symbol}")
    if manifest.csv_schema != "binance_futures_kline_v1_12_columns":
        raise ValueError(
            f"offline cache CSV schema is not the required twelve-column kline schema for {symbol}"
        )
    _validate_hex_digest("dataset sha256", manifest.sha256, lengths=(64,))
    if len(candles) != expected_rows:
        raise ValueError(f"incomplete offline candle slice for {symbol}")
    for index, candle in enumerate(candles):
        expected_open = expected_start_ms + index * _MINUTE_MS
        if not isinstance(candle, Candle):
            raise TypeError("offline cache must contain Candle values")
        if (
            candle.symbol != symbol
            or candle.timeframe != "1m"
            or candle.open_time_ms != expected_open
            or candle.close_time_ms != expected_open + _MINUTE_MS - 1
        ):
            raise ValueError(f"offline cache has a missing, duplicate, or malformed minute for {symbol}")
        numeric = (
            candle.open,
            candle.high,
            candle.low,
            candle.close,
            candle.volume,
            candle.quote_volume,
            candle.taker_buy_volume,
        )
        if any(isinstance(value, bool) or not math.isfinite(value) for value in numeric):
            raise ValueError(f"offline cache has non-finite OHLC/volume/quote/taker data for {symbol}")
        if (
            min(candle.open, candle.high, candle.low, candle.close) <= 0
            or min(candle.volume, candle.quote_volume, candle.taker_buy_volume) < 0
            or candle.high < max(candle.open, candle.close)
            or candle.low > min(candle.open, candle.close)
            or candle.taker_buy_volume > candle.volume
        ):
            raise ValueError(f"offline cache has invalid OHLC/volume/quote/taker domains for {symbol}")
        quote_tolerance = max(1e-8, abs(candle.quote_volume) * 1e-10)
        if (
            candle.quote_volume < candle.volume * candle.low - quote_tolerance
            or candle.quote_volume > candle.volume * candle.high + quote_tolerance
        ):
            raise ValueError(f"offline cache quote volume is inconsistent with price and volume for {symbol}")
    digest = hashlib.sha256(
        json.dumps([asdict(candle) for candle in candles], separators=(",", ":")).encode()
    ).hexdigest()
    if manifest.sha256 != digest:
        raise ValueError(f"offline cache parsed-row SHA-256 mismatch for {symbol}")


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


def _validate_output_paths(
    plan_output: Path,
    result_output: Path,
    summary_output: Path | None,
    *,
    overwrite: bool,
) -> None:
    if not isinstance(plan_output, Path) or not isinstance(result_output, Path):
        raise TypeError("screen output paths must be pathlib.Path values")
    if summary_output is not None and not isinstance(summary_output, Path):
        raise TypeError("summary_output must be a pathlib.Path or None")
    paths = (plan_output, result_output) + (() if summary_output is None else (summary_output,))
    if len({path.resolve() for path in paths}) != len(paths):
        raise ValueError("plan, result, and summary outputs must be different files")
    if not overwrite:
        existing = tuple(path for path in paths if path.exists())
        if existing:
            names = ", ".join(str(path) for path in existing)
            raise FileExistsError(f"refusing to overwrite existing output: {names}")


def run_orderflow_screen(
    cache_dir: Path,
    plan_output: Path,
    result_output: Path,
    *,
    summary_output: Path | None = None,
    overwrite: bool = False,
) -> OrderFlowScreenResult:
    """Run the exact offline screen and persist preregistration plus evidence."""

    if not isinstance(cache_dir, Path):
        raise TypeError("cache_dir must be a pathlib.Path")
    if type(overwrite) is not bool:
        raise TypeError("overwrite must be boolean")
    _validate_output_paths(
        plan_output,
        result_output,
        summary_output,
        overwrite=overwrite,
    )

    plan = OrderFlowScreenPlan()
    if overwrite:
        for stale_output in (result_output, summary_output):
            if stale_output is not None and stale_output.exists():
                stale_output.unlink()
                _fsync_directory(stale_output.parent)
    _atomic_write_json(plan_output, plan.to_dict(), overwrite=overwrite)

    candles_by_symbol = _load_fixed_cache(cache_dir)
    trial_results: list[OrderFlowTrialResult] = []
    for trial in plan.trials:
        evidence = run_orderflow_campaign(
            candles_by_symbol,
            candidate=trial.candidate,
            protocol=plan.protocol,
            initial_equity_usd=plan.initial_equity_usd,
            scenarios=orderflow_scenarios(trial.candidate),
            seed=plan.seed,
        )
        trial_results.append(
            OrderFlowTrialResult(
                trial=trial,
                campaign_evidence=evidence,
                scenarios=_summarize_campaign(evidence),
            )
        )
    result = OrderFlowScreenResult(plan=plan, trials=tuple(trial_results))

    if plan_output.read_bytes() != _serialized_json(plan.to_dict()):
        raise RuntimeError("screen plan changed after preregistration; result will not be written")
    _assert_environment_stable(plan.environment)
    _atomic_write_json(result_output, result.to_dict(), overwrite=False)
    if summary_output is not None:
        _atomic_write_json(
            summary_output,
            compact_orderflow_summary(result),
            overwrite=False,
        )
    return result


def _print_summary(result: OrderFlowScreenResult) -> None:
    print("Development diagnostics only - reused RESEARCH/FIT data, never promotion evidence.")
    for trial in result.trials:
        baseline, stress = trial.scenarios
        status = "ELIGIBLE" if trial.eligible else "REJECT"
        print(
            f"{trial.trial.trial_id}: {status}; "
            f"baseline log={baseline.combined.log_growth:.6f}, "
            f"stress log={stress.combined.log_growth:.6f}, "
            f"stress DD={stress.combined.maximum_drawdown:.4%}, "
            f"trades={baseline.combined.trades}/{stress.combined.trades}"
        )
    print(f"Screen decision: {result.selected_trial_id}")
    print("promotion_eligible=false shadow_allowed=false live_allowed=false")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run the fixed three-variant offline order-flow development screen"
    )
    parser.add_argument("--cache-dir", type=Path, default=Path("data/historical"))
    parser.add_argument(
        "--plan-output",
        type=Path,
        default=Path("reports/orderflow-screen/plan.json"),
    )
    parser.add_argument(
        "--result-output",
        type=Path,
        default=Path("reports/orderflow-screen/result.json"),
    )
    parser.add_argument(
        "--summary-output",
        type=Path,
        default=Path("reports/orderflow-screen/summary.json"),
    )
    parser.add_argument("--overwrite", action="store_true")
    args = parser.parse_args()

    result = run_orderflow_screen(
        args.cache_dir,
        args.plan_output,
        args.result_output,
        summary_output=args.summary_output,
        overwrite=args.overwrite,
    )
    _print_summary(result)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
