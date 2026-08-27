"""One-shot reused-data screen for ``crowded_trend_continuation_v1``.

The candidate direction was discovered post-hoc in ``derivatives_state_v1``.
Consequently this screen can only reject it or freeze the exact definition for
future data. It cannot authorize alpha, PAPER or LIVE trading.
"""

from __future__ import annotations

import argparse
import hashlib
from collections.abc import Callable, Mapping
from dataclasses import asdict
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import cast

from kairos_strategy.factors import DerivativeStateObservation
from kairos_strategy.provenance import installed_source_tree_sha256
from kairos_strategy.registry import StrategyStatus, get_contextual_strategy
from kairos_strategy.sleeves import (
    CrowdedTrendContinuationConfig,
    generate_crowded_trend_continuation_intents,
)

from .cost_risk import AllInCostModel, RiskLimits
from .data import BinanceArchiveLoader, audit_cached_archives
from .execution import ExecutionConfig
from .factor_data import DATA_END, LEVERAGE_START, FactorDataset, LeverageObservation, load_factor_cache
from .managed_evaluation import ManagedCellResult, ManagedEvaluationPolicy, evaluate_sleeve_cell
from .provenance import runtime_provenance, source_fingerprint
from .quarter_hour_screen import (
    DATA_START,
    INITIAL_EQUITY_USD,
    SYMBOLS,
    WINDOWS,
    _atomic_write,
    _execution_scenarios,
    _json_bytes,
    _json_value,
    _sha256,
    _validate_archive_audit,
    _validate_window_manifest,
    _window_intents,
    _window_slice,
)
from .research_protocol import DataRole, ResearchProtocol
from .right_tail_screen import (
    FORWARD_MINIMUM_DAYS,
    FORWARD_MINIMUM_TRADES,
    MAXIMUM_DRAWDOWN,
    MAXIMUM_ONE_SYMBOL_TRADE_SHARE,
    MINIMUM_ACTIVE_SYMBOLS,
    MINIMUM_DIRECTION_TRADES,
    MINIMUM_POSITIVE_EXPECTANCY_SYMBOLS,
    MINIMUM_PROFIT_FACTOR,
    MINIMUM_STRESS_TRADE_RETENTION,
    MINIMUM_TRADES_PER_SYMBOL,
    MINIMUM_TRADES_PER_WINDOW_SCENARIO,
    _gate_failures,
    _summarize_cells,
)
from .seeding import derive_seed

SCHEMA_VERSION = "kairos.crowded-trend-reused-screen.v1"
PLAN_FILENAME = "reports/crowded-trend-screen/plan.json"
ATTEMPT_FILENAME = "reports/crowded-trend-screen/attempt.json"
RESULT_FILENAME = "reports/crowded-trend-screen/summary.json"
STRATEGY_ID = "crowded_trend_continuation_v1"
STRATEGY_COMMIT = "f92bd6dbc5414167557b6ee69eea1b768264f5ef"
DERIVATIVES_PLAN_SHA256 = "4072f504942ffeb993fccaea7e26fad2d5e2459b33611b36c7f22ac5d00bb309"
DERIVATIVES_RESULT_SHA256 = "c0e5769d26670cdfe1fd224bb39b39850c4a2ca3b68e7750156fe3c55205ed76"
RIGHT_TAIL_PLAN_SHA256 = "4b98938b7880c4a799a528a1f7f3e0a83fbd4bf2b4cb606ff51bf6daec1ecef4"
RIGHT_TAIL_RESULT_SHA256 = "b3b62e262d2a60be8fd9f1a101b4df3d1ee9d342907bb2e21598d8eb17642a0b"
FACTOR_INVENTORY_SHA256 = "b9e4c038adfd2ec70fadcdbce46e6ed040cb8e2e855b0335c7bb6552140ad2cc"
WARMUP_DAYS = 2
BASE_SEED = 53
LINEAGE_TRIAL_NUMBER = 12
_HOUR_MS = 60 * 60 * 1_000
_DAY_MS = 24 * _HOUR_MS
EVALUATION_WINDOWS = tuple(window for window in WINDOWS if window.role is not DataRole.RESEARCH)

PROTOCOL = ResearchProtocol(
    protocol_name="crowded-trend-continuation-reused-data-v1",
    universe=SYMBOLS,
    windows=EVALUATION_WINDOWS,
    max_trials=1,
    maximum_holding_ms=24 * _HOUR_MS,
    maximum_label_horizon_ms=25 * _HOUR_MS,
    maximum_execution_latency_ms=500,
    warmup_ms=WARMUP_DAYS * _DAY_MS,
)


def _costs(execution: ExecutionConfig) -> AllInCostModel:
    adverse_funding_bps = 0.0
    if execution.funding.evidence == "assumed":
        rate = execution.funding.rate_8h_bps
        if rate is None:
            raise RuntimeError("assumed funding lost its configured rate")
        adverse_funding_bps = 25 * rate / 8
    return AllInCostModel(
        fee_bps_per_side=execution.fee_bps,
        spread_bps=execution.spread_bps,
        slippage_bps_per_side=execution.slippage_bps + execution.slippage_jitter_bps,
        adverse_funding_bps=adverse_funding_bps,
        uncertainty_buffer_bps=2.0,
    )


def expected_plan() -> dict[str, object]:
    config = CrowdedTrendContinuationConfig()
    scenarios = _execution_scenarios()
    return {
        "classification": "post_hoc_reused_data_development_only",
        "data": {
            "archive_end_exclusive": DATA_END.isoformat(),
            "archive_start": LEVERAGE_START.isoformat(),
            "factor_inventory_sha256": FACTOR_INVENTORY_SHA256,
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
                for window in EVALUATION_WINDOWS
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
            "minimum_profit_factor_strictly_greater_than": MINIMUM_PROFIT_FACTOR,
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
            "contamination": "direction_selected_after_derivatives_state_v1_observation",
            "derivatives_plan_sha256": DERIVATIVES_PLAN_SHA256,
            "derivatives_result_sha256": DERIVATIVES_RESULT_SHA256,
            "economic_premise": "leveraged_aligned_crowding_can_continue_before_unwinding",
            "global_thresholds_only": True,
            "parameter_search_allowed": False,
            "post_hoc_direction_disclosed": True,
            "research_lineage_trial_number": LINEAGE_TRIAL_NUMBER,
            "right_tail_lifecycle_reused_without_search": True,
            "right_tail_plan_sha256": RIGHT_TAIL_PLAN_SHA256,
            "right_tail_result_sha256": RIGHT_TAIL_RESULT_SHA256,
            "symbol_exclusions_allowed": False,
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
            "decision_clock": "every_complete_utc_hour",
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
        raise ValueError("committed crowded-trend plan differs from the executable plan")
    return payload


def _git(project_root: Path, *arguments: str) -> str:
    import subprocess  # nosec B404

    completed = subprocess.run(  # nosec B603
        ("git", *arguments),
        cwd=project_root,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    return completed.stdout.strip()


def _environment(project_root: Path) -> dict[str, object]:
    if _git(project_root, "status", "--porcelain=v1", "--untracked-files=all"):
        raise RuntimeError("crowded-trend screen refuses to open data from a dirty Git worktree")
    definition = get_contextual_strategy(STRATEGY_ID)
    if definition.status is not StrategyStatus.RESEARCH or definition.revision != "1":
        raise RuntimeError("contextual strategy registration changed after preregistration")
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


def _consume_attempt(plan: Mapping[str, object], *, plan_path: Path, attempt_path: Path) -> dict[str, object]:
    if attempt_path.exists():
        raise FileExistsError(f"one-shot attempt is already consumed: {attempt_path}")
    plan_bytes = plan_path.read_bytes()
    payload: dict[str, object] = {
        "classification": "post_hoc_reused_data_development_only",
        "consumed_at": _now_utc(),
        "consumption_point": "before_first_price_or_factor_archive_access",
        "crash_or_failure_releases_attempt": False,
        "lineage_trial_number": LINEAGE_TRIAL_NUMBER,
        "plan_file": {"bytes": len(plan_bytes), "sha256": hashlib.sha256(plan_bytes).hexdigest()},
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
        raise RuntimeError("crowded-trend attempt ledger changed after publication")
    return payload


def _factor_observations(
    factors: FactorDataset,
    symbol: str,
    start: date,
    end: date,
) -> list[DerivativeStateObservation]:
    start_ms = int(datetime.combine(start, datetime.min.time(), UTC).timestamp() * 1_000)
    end_ms = int(datetime.combine(end, datetime.min.time(), UTC).timestamp() * 1_000)
    premiums = {
        item.open_time_ms: item for item in factors.premium[symbol] if start_ms <= item.open_time_ms < end_ms
    }
    leverage: dict[int, LeverageObservation] = {}
    for item in factors.leverage[symbol]:
        hour = item.timestamp_ms // _HOUR_MS * _HOUR_MS
        if not start_ms <= hour < end_ms or item.open_interest <= 0 or item.open_interest_value <= 0:
            continue
        current = leverage.get(hour)
        if current is None or item.timestamp_ms > current.timestamp_ms:
            leverage[hour] = item
    funding = factors.funding[symbol]
    funding_index = 0
    latest_funding = None
    observations: list[DerivativeStateObservation] = []
    for hour in range(start_ms, end_ms, _HOUR_MS):
        close_ms = hour + _HOUR_MS - 1
        while funding_index < len(funding) and funding[funding_index].timestamp_ms <= close_ms:
            latest_funding = funding[funding_index]
            funding_index += 1
        premium = premiums.get(hour)
        leverage_point = leverage.get(hour)
        if latest_funding is None or premium is None or leverage_point is None:
            continue
        if close_ms - latest_funding.timestamp_ms > 8 * _HOUR_MS:
            continue
        observations.append(
            DerivativeStateObservation(
                symbol=symbol,
                open_time_ms=hour,
                close_time_ms=close_ms,
                premium_close=premium.close,
                funding_rate=latest_funding.rate,
                funding_timestamp_ms=latest_funding.timestamp_ms,
                open_interest_value=leverage_point.open_interest_value,
                open_interest_timestamp_ms=leverage_point.timestamp_ms,
            )
        )
    return observations


def run_crowded_trend_screen(
    *,
    project_root: Path,
    price_cache: Path,
    factor_cache: Path,
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
        BinanceArchiveLoader(price_cache, allow_download=False)
        if loader_factory is None
        else loader_factory(price_cache)
    )
    price_audit = audit_cached_archives(price_cache, SYMBOLS, DATA_START, DATA_END)
    _validate_archive_audit(price_audit)
    factors = load_factor_cache(factor_cache)
    if factors.inventory_sha256 != FACTOR_INVENTORY_SHA256:
        raise ValueError("factor inventory differs from the preregistered official archive set")

    config = CrowdedTrendContinuationConfig()
    scenarios = _execution_scenarios()
    limits = RiskLimits()
    results_by_window: dict[str, dict[str, list[ManagedCellResult]]] = {
        window.name: {name: [] for name in scenarios} for window in EVALUATION_WINDOWS
    }
    datasets: list[dict[str, object]] = []
    factor_counts: list[dict[str, object]] = []
    for symbol in SYMBOLS:
        for window in EVALUATION_WINDOWS:
            load_start = window.start - timedelta(days=WARMUP_DAYS)
            candles, manifest = loader.load(symbol, load_start, window.end, "1m")
            _validate_window_manifest(symbol, candles, manifest, start=load_start, end=window.end)
            datasets.append({"window": window.name, **asdict(manifest)})
            rows = _window_slice(candles, window)
            factor_rows = _factor_observations(factors, symbol, load_start, window.end)
            factor_counts.append({"observations": len(factor_rows), "symbol": symbol, "window": window.name})
            intents = _window_intents(
                generate_crowded_trend_continuation_intents(rows, factor_rows, config), window
            )
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
                            factors.inventory_sha256,
                            config.fingerprint,
                        ),
                    )
                )
    summaries = {
        window.name: {
            scenario_name: _summarize_cells(tuple(results_by_window[window.name][scenario_name]))
            for scenario_name in scenarios
        }
        for window in EVALUATION_WINDOWS
    }
    failures = _gate_failures(cast("Mapping[str, Mapping[str, Mapping[str, object]]]", summaries))
    summary: dict[str, object] = {
        "attempt": attempt,
        "classification": "FORWARD_FREEZE_CANDIDATE" if not failures else "REJECT_REUSED_DATA_SCREEN",
        "data_quality": {
            "factor_audits": [asdict(audit) for audit in factors.audits],
            "factor_inventory_sha256": factors.inventory_sha256,
            "factor_observations": factor_counts,
            "price_archive_audit": asdict(price_audit),
            "price_slices": datasets,
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
    parser = argparse.ArgumentParser(description="Run the preregistered crowded-trend reused-data screen")
    parser.add_argument("--price-cache", type=Path, default=Path("data/historical"))
    parser.add_argument("--factor-cache", type=Path, default=Path("data/historical-factors"))
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
    result = run_crowded_trend_screen(
        project_root=project_root,
        price_cache=arguments.price_cache,
        factor_cache=arguments.factor_cache,
        plan_path=arguments.plan,
        attempt_path=arguments.attempt,
        result_path=arguments.result,
    )
    print(f"classification={result['classification']}")
    print("alpha_ready=false paper_allowed=false live_allowed=false")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
