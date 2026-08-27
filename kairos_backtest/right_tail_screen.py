"""One-candidate reused-data screen for ``right_tail_trend_v1``.

The candidate is independently motivated by time-series trend literature.  Its
feature definition was already observed in ``market_anatomy_v1``, whose failed
descriptive gate is explicitly not reinterpreted as prototype authorization.
This screen can reject the candidate or freeze it before future data; it cannot
authorize PAPER, LIVE, or alpha promotion.
"""

from __future__ import annotations

import argparse
import hashlib
import math
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from pathlib import Path

from kairos_strategy.provenance import installed_source_tree_sha256
from kairos_strategy.registry import StrategyStatus, get_strategy
from kairos_strategy.sleeves import RightTailTrendConfig, generate_right_tail_trend_intents

from .cost_risk import AllInCostModel, RiskLimits
from .data import BinanceArchiveLoader, audit_cached_archives
from .execution import ExecutionConfig
from .managed_evaluation import ManagedCellResult, ManagedEvaluationPolicy, evaluate_sleeve_cell
from .portfolio import CellEquityCurve, synchronize_cells
from .provenance import runtime_provenance, source_fingerprint
from .quarter_hour_screen import (
    DATA_END,
    DATA_START,
    INITIAL_EQUITY_USD,
    SYMBOLS,
    WINDOWS,
    _atomic_write,
    _execution_scenarios,
    _git,
    _json_bytes,
    _json_value,
    _sha256,
    _validate_archive_audit,
    _validate_window_manifest,
    _window_intents,
    _window_slice,
)
from .research_protocol import DataRole, ResearchProtocol
from .robustness import hac_sharpe
from .seeding import derive_seed

SCHEMA_VERSION = "kairos.right-tail-reused-screen.v1"
PLAN_FILENAME = "reports/right-tail-screen/plan.json"
ATTEMPT_FILENAME = "reports/right-tail-screen/attempt.json"
RESULT_FILENAME = "reports/right-tail-screen/summary.json"
STRATEGY_ID = "right_tail_trend_v1"
STRATEGY_COMMIT = "331d8751901a8566fc4c99afd25d18cfd6db2f8f"
MARKET_ANATOMY_PLAN_SHA256 = "07b6d8f4a79eec0781719d5777db88b31a00310a095f58267e71fad014e9c149"
MARKET_ANATOMY_RESULT_SHA256 = "6f0a17e0edd702afff2a83d0b9bdb810f0f75d049993c39399751c5dfc227b5f"
WARMUP_DAYS = 2
BASE_SEED = 47
LINEAGE_TRIAL_NUMBER = 11
MINIMUM_TRADES_PER_WINDOW_SCENARIO = 100
MINIMUM_TRADES_PER_SYMBOL = 10
MINIMUM_ACTIVE_SYMBOLS = 3
MINIMUM_POSITIVE_EXPECTANCY_SYMBOLS = 3
MINIMUM_DIRECTION_TRADES = 25
MINIMUM_PROFIT_FACTOR = 1.05
MAXIMUM_ONE_SYMBOL_TRADE_SHARE = 0.50
MAXIMUM_DRAWDOWN = 0.10
MINIMUM_STRESS_TRADE_RETENTION = 0.65
FORWARD_MINIMUM_DAYS = 365
FORWARD_MINIMUM_TRADES = 500
_HOUR_MS = 60 * 60 * 1_000
_DAY_MS = 24 * _HOUR_MS

PROTOCOL = ResearchProtocol(
    protocol_name="right-tail-trend-reused-data-v1",
    universe=SYMBOLS,
    windows=WINDOWS,
    max_trials=1,
    maximum_holding_ms=72 * _HOUR_MS,
    maximum_label_horizon_ms=74 * _HOUR_MS,
    maximum_execution_latency_ms=500,
    warmup_ms=WARMUP_DAYS * _DAY_MS,
)


def _costs(execution: ExecutionConfig) -> AllInCostModel:
    adverse_funding_bps = 0.0
    if execution.funding.evidence == "assumed":
        rate = execution.funding.rate_8h_bps
        if rate is None:
            raise RuntimeError("assumed funding lost its configured rate")
        adverse_funding_bps = 73 * rate / 8
    return AllInCostModel(
        fee_bps_per_side=execution.fee_bps,
        spread_bps=execution.spread_bps,
        slippage_bps_per_side=execution.slippage_bps + execution.slippage_jitter_bps,
        adverse_funding_bps=adverse_funding_bps,
        uncertainty_buffer_bps=2.0,
    )


def expected_plan() -> dict[str, object]:
    config = RightTailTrendConfig()
    scenarios = _execution_scenarios()
    return {
        "classification": "reused_data_development_only",
        "data": {
            "archive_end_exclusive": DATA_END.isoformat(),
            "archive_start": DATA_START.isoformat(),
            "official_checksums_required": True,
            "reused_data": True,
            "universe": list(SYMBOLS),
            "warmup_days": WARMUP_DAYS,
            "windows": [
                {
                    "end_exclusive": window.end.isoformat(),
                    "name": window.name,
                    "role": window.role.value,
                    "start": window.start.isoformat(),
                }
                for window in WINDOWS
            ],
        },
        "decision_rule": {
            "forward_candidate_only_if_all_gates_pass": True,  # nosec B105
            "gated_windows": ["selection", "robustness"],
            "maximum_drawdown": MAXIMUM_DRAWDOWN,
            "maximum_one_symbol_trade_share": MAXIMUM_ONE_SYMBOL_TRADE_SHARE,
            "minimum_active_symbols": MINIMUM_ACTIVE_SYMBOLS,
            "minimum_direction_trades": MINIMUM_DIRECTION_TRADES,
            "minimum_expectancy_usd_per_trade": 0.0,
            "minimum_hac_sharpe": 0.0,
            "minimum_positive_expectancy_symbols": MINIMUM_POSITIVE_EXPECTANCY_SYMBOLS,
            "minimum_profit_factor": MINIMUM_PROFIT_FACTOR,
            "minimum_stress_trade_retention": MINIMUM_STRESS_TRADE_RETENTION,
            "minimum_total_return": 0.0,
            "minimum_trades_per_symbol": MINIMUM_TRADES_PER_SYMBOL,
            "minimum_trades_per_window_scenario": MINIMUM_TRADES_PER_WINDOW_SCENARIO,
        },
        "forward_gate": {
            "blind_start_not_before": "2026-09-01",
            "minimum_days": FORWARD_MINIMUM_DAYS,
            "minimum_trades": FORWARD_MINIMUM_TRADES,
        },
        "hypothesis": {
            "economic_premise": "daily_sampled_time_series_trend_has_positive_skew_after_costs",
            "market_anatomy_decision": "NO_PROTOTYPE_PASSED_DESCRIPTIVE_GATES",
            "market_anatomy_plan_sha256": MARKET_ANATOMY_PLAN_SHA256,
            "market_anatomy_result_sha256": MARKET_ANATOMY_RESULT_SHA256,
            "market_anatomy_reinterpreted_as_authorization": False,
            "parameter_search_allowed": False,
            "research_lineage_trial_number": LINEAGE_TRIAL_NUMBER,
            "trial_count": 1,
        },
        "permissions": {
            "alpha_ready": False,
            "live_allowed": False,
            "paper_allowed": False,
            "promotion_eligible": False,
        },
        "protocol": {
            "fingerprint": PROTOCOL.preregistration_fingerprint(),
            "max_trials": 1,
            "seed": BASE_SEED,
        },
        "scenarios": {
            name: {
                "costs": asdict(_costs(execution)),
                "execution": asdict(execution),
                "policy": asdict(
                    ManagedEvaluationPolicy(
                        application_exit_latency_ms=execution.latency_ms,
                        terminal_liquidation_grace_ms=_HOUR_MS,
                    )
                ),
            }
            for name, execution in scenarios.items()
        },
        "schema_version": SCHEMA_VERSION,
        "strategy": {
            "config": asdict(config),
            "config_sha256": config.fingerprint,
            "id": STRATEGY_ID,
            "revision": "1",
            "source_commit": STRATEGY_COMMIT,
            "status": "research",
        },
    }


def load_preregistered_plan(path: Path) -> dict[str, object]:
    import json

    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or _json_value(payload) != _json_value(expected_plan()):
        raise ValueError("committed right-tail plan differs from the executable plan")
    return payload


def _environment(project_root: Path) -> dict[str, object]:
    if _git(project_root, "status", "--porcelain=v1", "--untracked-files=all"):
        raise RuntimeError("right-tail screen refuses to open data from a dirty Git worktree")
    definition = get_strategy(STRATEGY_ID)
    if definition.status is not StrategyStatus.RESEARCH or definition.revision != "1":
        raise RuntimeError("right-tail strategy registration changed after preregistration")
    return {
        "git_head_sha": _git(project_root, "rev-parse", "HEAD"),
        "git_tree_sha": _git(project_root, "rev-parse", "HEAD^{tree}"),
        "pyproject_sha256": hashlib.sha256((project_root / "pyproject.toml").read_bytes()).hexdigest(),
        "runtime": runtime_provenance().as_dict(),
        "source_sha256": source_fingerprint(project_root / "kairos_backtest"),
        "strategy_source_sha256": installed_source_tree_sha256(definition.source_files),
        "uv_lock_sha256": hashlib.sha256((project_root / "uv.lock").read_bytes()).hexdigest(),
    }


def _now_utc() -> datetime:
    return datetime.now(UTC)


def _consume_attempt(
    plan: Mapping[str, object],
    *,
    plan_path: Path,
    attempt_path: Path,
) -> dict[str, object]:
    """Irreversibly consume the one permitted trial before market data access."""

    if attempt_path.exists():
        raise FileExistsError(f"one-shot attempt is already consumed: {attempt_path}")
    plan_bytes = plan_path.read_bytes()
    payload: dict[str, object] = {
        "classification": "reused_data_development_only",
        "consumed_at": _now_utc(),
        "consumption_point": "immediately_before_first_market_archive_access",
        "crash_or_failure_releases_attempt": False,
        "lineage_trial_number": LINEAGE_TRIAL_NUMBER,
        "plan_file": {
            "bytes": len(plan_bytes),
            "sha256": hashlib.sha256(plan_bytes).hexdigest(),
        },
        "plan_sha256": _sha256(plan),
        "rerun_allowed": False,
        "schema_version": SCHEMA_VERSION,
        "status": "consumed",
        "strategy_id": STRATEGY_ID,
    }
    payload["attempt_sha256"] = _sha256(payload)
    expected = _json_bytes(payload)
    _atomic_write(attempt_path, payload)
    if attempt_path.read_bytes() != expected:
        raise RuntimeError("right-tail attempt ledger changed after publication")
    return payload


def _quantile(values: Sequence[float], probability: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * probability
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    weight = position - lower
    return ordered[lower] * (1 - weight) + ordered[upper] * weight


def _safe_hac(cell_curves: tuple[CellEquityCurve, ...]) -> float:
    portfolio = synchronize_cells(cell_curves)
    try:
        return hac_sharpe(portfolio.daily_returns)
    except ValueError:
        return 0.0


def _summarize_cells(results: tuple[ManagedCellResult, ...]) -> dict[str, object]:
    cells = tuple(result.cell for result in results)
    portfolio = synchronize_cells(cells)
    trades = tuple(trade for cell in cells for trade in cell.trades)
    profits = math.fsum(max(0.0, trade.net_pnl_usd) for trade in trades)
    losses = -math.fsum(min(0.0, trade.net_pnl_usd) for trade in trades)
    per_symbol: list[dict[str, object]] = []
    counts: list[int] = []
    positive_expectancy_symbols = 0
    for result in results:
        symbol_trades = result.cell.trades
        expectancy = (
            math.fsum(trade.net_pnl_usd for trade in symbol_trades) / len(symbol_trades)
            if symbol_trades
            else 0.0
        )
        counts.append(len(symbol_trades))
        positive_expectancy_symbols += expectancy > 0
        per_symbol.append(
            {
                "expectancy_usd_per_trade": expectancy,
                "replay_evidence_sha256": result.replay_evidence_sha256,
                "symbol": result.cell.symbol,
                "total_return": result.cell.closing_equity_usd[-1] / result.cell.initial_equity_usd - 1,
                "trades": len(symbol_trades),
            }
        )
    r_multiples = [trade.r_multiple for trade in trades]
    direction_counts = Counter(trade.intent.side.value for trade in trades)
    dispositions = Counter(
        disposition.reason.value for result in results for disposition in result.dispositions
    )
    return {
        "active_symbols": portfolio.active_symbols,
        "carry_cost_usd": math.fsum(result.carry_cost_usd for result in results),
        "direction_trades": {
            "LONG": direction_counts.get("LONG", 0),
            "SHORT": direction_counts.get("SHORT", 0),
        },
        "distinct_utc_exit_days": len({trade.exit_timestamp_ms // _DAY_MS for trade in trades}),
        "dispositions": dict(sorted(dispositions.items())),
        "expectancy_usd_per_trade": (
            math.fsum(trade.net_pnl_usd for trade in trades) / len(trades) if trades else 0.0
        ),
        "fees_usd": math.fsum(result.fees_usd for result in results),
        "hac_sharpe": _safe_hac(cells),
        "implementation_shortfall_usd": math.fsum(result.implementation_shortfall_usd for result in results),
        "maximum_drawdown": portfolio.maximum_drawdown,
        "maximum_one_symbol_trade_share": max(counts, default=0) / len(trades) if trades else 0.0,
        "per_symbol": per_symbol,
        "positive_expectancy_symbols": positive_expectancy_symbols,
        "profit_factor": profits / losses if losses > 0 else None,
        "r_multiple_distribution": {
            "maximum": max(r_multiples) if r_multiples else None,
            "p10": _quantile(r_multiples, 0.10),
            "p50": _quantile(r_multiples, 0.50),
            "p90": _quantile(r_multiples, 0.90),
        },
        "total_return": portfolio.total_return,
        "trades": len(trades),
        "win_rate": sum(trade.net_pnl_usd > 0 for trade in trades) / len(trades) if trades else 0.0,
    }


def _gate_failures(windows: Mapping[str, Mapping[str, Mapping[str, object]]]) -> tuple[str, ...]:
    def integer(metrics: Mapping[str, object], name: str) -> int:
        value = metrics[name]
        if isinstance(value, bool) or not isinstance(value, int):
            raise TypeError(f"{name} metric must be an integer")
        return value

    def number(metrics: Mapping[str, object], name: str) -> float:
        value = metrics[name]
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise TypeError(f"{name} metric must be numeric")
        return float(value)

    failures: list[str] = []
    for window_name in ("selection", "robustness"):
        scenarios = windows[window_name]
        baseline_trades = integer(scenarios["baseline"], "trades")
        stress_trades = integer(scenarios["stress"], "trades")
        if baseline_trades and stress_trades / baseline_trades < MINIMUM_STRESS_TRADE_RETENTION:
            failures.append(f"{window_name}.stress_trade_retention_below_minimum")
        for scenario_name in ("baseline", "stress"):
            metrics = scenarios[scenario_name]
            prefix = f"{window_name}.{scenario_name}"
            trades = integer(metrics, "trades")
            if trades < MINIMUM_TRADES_PER_WINDOW_SCENARIO:
                failures.append(f"{prefix}.trades_below_minimum")
            if integer(metrics, "active_symbols") < MINIMUM_ACTIVE_SYMBOLS:
                failures.append(f"{prefix}.active_symbols_below_minimum")
            if number(metrics, "total_return") <= 0:
                failures.append(f"{prefix}.total_return_not_positive")
            if number(metrics, "expectancy_usd_per_trade") <= 0:
                failures.append(f"{prefix}.expectancy_not_positive")
            if number(metrics, "hac_sharpe") <= 0:
                failures.append(f"{prefix}.hac_sharpe_not_positive")
            profit_factor = metrics["profit_factor"]
            if profit_factor is None or number(metrics, "profit_factor") <= MINIMUM_PROFIT_FACTOR:
                failures.append(f"{prefix}.profit_factor_below_minimum")
            if number(metrics, "maximum_drawdown") > MAXIMUM_DRAWDOWN:
                failures.append(f"{prefix}.drawdown_above_maximum")
            if integer(metrics, "positive_expectancy_symbols") < MINIMUM_POSITIVE_EXPECTANCY_SYMBOLS:
                failures.append(f"{prefix}.positive_expectancy_symbols_below_minimum")
            if number(metrics, "maximum_one_symbol_trade_share") > MAXIMUM_ONE_SYMBOL_TRADE_SHARE:
                failures.append(f"{prefix}.one_symbol_trade_share_above_maximum")
            directions = metrics["direction_trades"]
            if not isinstance(directions, dict):
                raise TypeError("direction_trades must be a mapping")
            for side in ("LONG", "SHORT"):
                direction_count = directions[side]
                if isinstance(direction_count, bool) or not isinstance(direction_count, int):
                    raise TypeError("direction trade counts must be integers")
                if direction_count < MINIMUM_DIRECTION_TRADES:
                    failures.append(f"{prefix}.{side.lower()}_trades_below_minimum")
            per_symbol = metrics["per_symbol"]
            if not isinstance(per_symbol, list) or any(not isinstance(item, dict) for item in per_symbol):
                raise TypeError("per_symbol metric must be a list of mappings")
            for item in per_symbol:
                if integer(item, "trades") < MINIMUM_TRADES_PER_SYMBOL:
                    failures.append(f"{prefix}.{item['symbol']}.trades_below_minimum")
    return tuple(failures)


def run_right_tail_screen(
    *,
    project_root: Path,
    cache_dir: Path,
    plan_path: Path,
    attempt_path: Path,
    result_path: Path,
    loader_factory: Callable[[Path], BinanceArchiveLoader] | None = None,
) -> dict[str, object]:
    existing = tuple(path for path in (attempt_path, result_path) if path.exists())
    if existing:
        names = ", ".join(str(path) for path in existing)
        raise FileExistsError(f"one-shot attempt is unavailable; existing output: {names}")
    plan = load_preregistered_plan(plan_path)
    environment = _environment(project_root)
    attempt = _consume_attempt(plan, plan_path=plan_path, attempt_path=attempt_path)
    loader = (
        BinanceArchiveLoader(cache_dir, allow_download=False)
        if loader_factory is None
        else loader_factory(cache_dir)
    )
    config = RightTailTrendConfig()
    scenarios = _execution_scenarios()
    limits = RiskLimits()
    results_by_window: dict[str, dict[str, list[ManagedCellResult]]] = {
        window.name: {name: [] for name in scenarios} for window in WINDOWS
    }
    audit = audit_cached_archives(cache_dir, SYMBOLS, DATA_START, DATA_END)
    _validate_archive_audit(audit)
    issue_by_symbol = {
        item.symbol: item
        for item in audit.symbols
        if item.gaps > 0 or item.invalid_rows > 0 or item.missing_minutes > 0
    }
    datasets: list[dict[str, object]] = []
    skipped_cells: list[dict[str, object]] = []
    for symbol in SYMBOLS:
        for window in WINDOWS:
            if window.role is DataRole.RESEARCH and symbol in issue_by_symbol:
                issue = issue_by_symbol[symbol]
                skipped_cells.append(
                    {
                        "gaps": issue.gaps,
                        "invalid_rows": issue.invalid_rows,
                        "missing_minutes": issue.missing_minutes,
                        "reason": "historical_archive_not_contiguous",
                        "symbol": symbol,
                        "window": window.name,
                    }
                )
                continue
            load_start = window.start - timedelta(days=WARMUP_DAYS)
            candles, manifest = loader.load(symbol, load_start, window.end, "1m")
            _validate_window_manifest(symbol, candles, manifest, start=load_start, end=window.end)
            datasets.append({"window": window.name, **asdict(manifest)})
            rows = _window_slice(candles, window)
            intents = _window_intents(generate_right_tail_trend_intents(rows, config), window)
            for scenario_name, execution in scenarios.items():
                results_by_window[window.name][scenario_name].append(
                    evaluate_sleeve_cell(
                        rows,
                        intents,
                        cell_id=f"{window.name}:{scenario_name}:{STRATEGY_ID}:{symbol}",
                        sleeve_id=STRATEGY_ID,
                        symbol=symbol,
                        initial_equity_usd=INITIAL_EQUITY_USD / len(SYMBOLS),
                        execution=execution,
                        costs=_costs(execution),
                        limits=limits,
                        policy=ManagedEvaluationPolicy(
                            application_exit_latency_ms=execution.latency_ms,
                            terminal_liquidation_grace_ms=_HOUR_MS,
                        ),
                        seed=derive_seed(
                            BASE_SEED,
                            SCHEMA_VERSION,
                            window.name,
                            scenario_name,
                            symbol,
                            manifest.sha256,
                            config.fingerprint,
                        ),
                    )
                )
    summaries = {
        window.name: {
            scenario_name: _summarize_cells(tuple(results_by_window[window.name][scenario_name]))
            for scenario_name in scenarios
        }
        for window in WINDOWS
    }
    failures = _gate_failures(summaries)
    summary: dict[str, object] = {
        "attempt": attempt,
        "classification": "FORWARD_FREEZE_CANDIDATE" if not failures else "REJECT_REUSED_DATA_SCREEN",
        "data_quality": {
            "archive_audit": asdict(audit),
            "evaluated_slices": datasets,
            "skipped_cells": skipped_cells,
        },
        "environment": environment,
        "gate_failures": failures,
        "permissions": {
            "alpha_ready": False,
            "live_allowed": False,
            "paper_allowed": False,
            "promotion_eligible": False,
        },
        "plan_sha256": _sha256(plan),
        "result_schema_version": SCHEMA_VERSION,
        "strategy": plan["strategy"],
        "windows": summaries,
    }
    _atomic_write(result_path, summary)
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the preregistered right-tail reused-data screen")
    parser.add_argument("--cache-dir", type=Path, default=Path("data/historical"))
    parser.add_argument("--plan", type=Path, default=Path(PLAN_FILENAME))
    parser.add_argument("--attempt", type=Path, default=Path(ATTEMPT_FILENAME))
    parser.add_argument("--result", type=Path, default=Path(RESULT_FILENAME))
    parser.add_argument("--verify-plan", action="store_true")
    arguments = parser.parse_args()
    plan = load_preregistered_plan(arguments.plan)
    if arguments.verify_plan:
        print(f"plan_sha256={_sha256(plan)}")
        return 0
    project_root = Path(__file__).resolve().parents[1]
    result = run_right_tail_screen(
        project_root=project_root,
        cache_dir=arguments.cache_dir,
        plan_path=arguments.plan,
        attempt_path=arguments.attempt,
        result_path=arguments.result,
    )
    print(f"classification={result['classification']}")
    print("alpha_ready=false paper_allowed=false live_allowed=false")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
