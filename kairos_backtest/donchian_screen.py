"""One-shot reused-data screen for the published long-only Donchian ensemble."""

from __future__ import annotations

import argparse
import hashlib
import math
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from statistics import mean, stdev

from kairos_strategy.allocation import TargetAllocation
from kairos_strategy.candles import Candle
from kairos_strategy.provenance import installed_source_tree_sha256
from kairos_strategy.registry import StrategyStatus, get_allocation_strategy
from kairos_strategy.sleeves import DonchianEnsembleConfig, generate_donchian_ensemble_allocations
from kairos_strategy.timeframes import aggregate

from .data import BinanceArchiveLoader, audit_cached_archives
from .factor_data import DATA_END, FactorDataset, load_factor_cache
from .provenance import runtime_provenance, source_fingerprint
from .quarter_hour_screen import (
    DATA_START,
    INITIAL_EQUITY_USD,
    SYMBOLS,
    WINDOWS,
    _atomic_write,
    _json_bytes,
    _json_value,
    _sha256,
    _validate_archive_audit,
    _validate_window_manifest,
)
from .research_protocol import DataRole, ResearchProtocol

SCHEMA_VERSION = "kairos.donchian-ensemble-reused-screen.v1"
PLAN_FILENAME = "reports/donchian-screen/plan.json"
ATTEMPT_FILENAME = "reports/donchian-screen/attempt.json"
RESULT_FILENAME = "reports/donchian-screen/summary.json"
STRATEGY_ID = "donchian_ensemble_long_v1"
STRATEGY_COMMIT = "0ca9a69170e71fefcf9401481d1bb163985b9dee"
PAPER_URL = "https://papers.ssrn.com/sol3/papers.cfm?abstract_id=5209907"
FACTOR_INVENTORY_SHA256 = "b9e4c038adfd2ec70fadcdbce46e6ed040cb8e2e855b0335c7bb6552140ad2cc"
WARMUP_DAYS = 365
BASE_SEED = 59
LINEAGE_TRIAL_NUMBER = 13
MINIMUM_DAILY_PROFIT_FACTOR = 1.05
MINIMUM_ANNUALIZED_SHARPE = 0.50
MINIMUM_POSITIVE_SYMBOLS = 3
MINIMUM_ACTIVE_DAYS = 100
MINIMUM_ALLOCATION_CHANGES = 20
MAXIMUM_DRAWDOWN = 0.20
_DAY_MS = 86_400_000
EVALUATION_WINDOWS = tuple(window for window in WINDOWS if window.role is not DataRole.RESEARCH)

PROTOCOL = ResearchProtocol(
    protocol_name="published-donchian-ensemble-reused-data-v1",
    universe=SYMBOLS,
    windows=EVALUATION_WINDOWS,
    max_trials=1,
    maximum_holding_ms=(DATA_END - DATA_START).days * _DAY_MS,
    maximum_label_horizon_ms=((DATA_END - DATA_START).days + 1) * _DAY_MS,
    maximum_execution_latency_ms=0,
    warmup_ms=WARMUP_DAYS * _DAY_MS,
)


@dataclass(frozen=True, slots=True)
class AllocationCostScenario:
    transaction_cost_bps: float
    additional_funding_per_settlement: float


SCENARIOS = {
    "baseline": AllocationCostScenario(10.0, 0.0),
    "stress": AllocationCostScenario(25.0, 0.0005),
}


@dataclass(frozen=True, slots=True)
class SymbolDay:
    day_ms: int
    net_return: float
    gross_return: float
    funding_return: float
    transaction_cost_return: float
    target_weight: float
    turnover: float


def expected_plan() -> dict[str, object]:
    config = DonchianEnsembleConfig()
    return {
        "classification": "independent_published_model_reused_data_only",
        "data": {
            "end_exclusive": DATA_END.isoformat(),
            "factor_inventory_sha256": FACTOR_INVENTORY_SHA256,
            "funding_source": "official_binance_usdm_fundingRate",
            "official_checksums_required": True,
            "price_source": "official_binance_usdm_1m_klines_aggregated_to_complete_utc_days",
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
            "maximum_drawdown": MAXIMUM_DRAWDOWN,
            "minimum_active_days": MINIMUM_ACTIVE_DAYS,
            "minimum_allocation_changes": MINIMUM_ALLOCATION_CHANGES,
            "minimum_annualized_sharpe": MINIMUM_ANNUALIZED_SHARPE,
            "minimum_daily_profit_factor_strictly_greater_than": MINIMUM_DAILY_PROFIT_FACTOR,
            "minimum_positive_symbols": MINIMUM_POSITIVE_SYMBOLS,
            "minimum_total_return": 0.0,
            "required_scenarios": list(SCENARIOS),
            "required_windows": [window.name for window in EVALUATION_WINDOWS],
        },
        "forward_gate": {
            "blind_start_not_before": "2026-09-01",
            "minimum_days": 365,
            "minimum_allocation_changes": 100,
        },
        "hypothesis": {
            "deadband_interpretation": "relative_difference_from_last_executed_weight",
            "economic_premise": "diversified_long_only_breakouts_capture_crypto_positive_skew",
            "exit_rule": "no_timeout_first_daily_close_at_or_below_monotonic_mid_channel_stop",
            "paper_source": PAPER_URL,
            "parameter_search_allowed": False,
            "published_model_transcribed_without_horizon_search": True,
            "research_lineage_trial_number": LINEAGE_TRIAL_NUMBER,
            "trial_count": 1,
            "volatility_definition": "sample_stdev_simple_daily_returns_sqrt_365",
        },
        "permissions": {
            "alpha_ready": False,
            "live_allowed": False,
            "paper_allowed": False,
            "promotion_eligible": False,
        },
        "portfolio": {
            "asset_sleeves": len(SYMBOLS),
            "cross_asset_rebalance": "equal_capital_at_each_utc_month_start",
            "cross_asset_rebalance_costed": True,
            "fixed_universe_limitation": True,
            "initial_equity_usd": INITIAL_EQUITY_USD,
        },
        "protocol": {
            "fingerprint": PROTOCOL.preregistration_fingerprint(),
            "max_trials": 1,
            "seed": BASE_SEED,
        },
        "scenarios": {name: asdict(scenario) for name, scenario in SCENARIOS.items()},
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
        raise ValueError("committed Donchian plan differs from the executable plan")
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
        raise RuntimeError("Donchian screen refuses to open data from a dirty Git worktree")
    definition = get_allocation_strategy(STRATEGY_ID)
    if definition.status is not StrategyStatus.RESEARCH or definition.revision != "1":
        raise RuntimeError("allocation strategy registration changed after preregistration")
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
    raw = plan_path.read_bytes()
    payload: dict[str, object] = {
        "classification": "independent_published_model_reused_data_only",
        "consumed_at": _now_utc(),
        "consumption_point": "before_first_price_or_funding_archive_access",
        "crash_or_failure_releases_attempt": False,
        "lineage_trial_number": LINEAGE_TRIAL_NUMBER,
        "plan_file": {"bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest()},
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
        raise RuntimeError("Donchian attempt ledger changed after publication")
    return payload


def _funding_by_day(factors: FactorDataset, symbol: str) -> dict[int, tuple[float, int]]:
    result: dict[int, tuple[float, int]] = {}
    for item in factors.funding[symbol]:
        day_ms = item.timestamp_ms // _DAY_MS * _DAY_MS
        total, count = result.get(day_ms, (0.0, 0))
        result[day_ms] = (total + item.rate, count + 1)
    return result


def _symbol_days(
    daily: Sequence[Candle],
    allocations: Sequence[TargetAllocation],
    funding: Mapping[int, tuple[float, int]],
    scenario: AllocationCostScenario,
    start_ms: int,
    end_ms: int,
) -> tuple[SymbolDay, ...]:
    targets = {item.effective_ts_ms: item.target_weight for item in allocations}
    current_weight = 0.0
    result: list[SymbolDay] = []
    for previous, current in zip(daily, daily[1:], strict=False):
        new_weight = targets.get(current.open_time_ms, current_weight)
        turnover = abs(new_weight - current_weight)
        current_weight = new_weight
        if not start_ms <= current.open_time_ms < end_ms:
            continue
        asset_return = current.close / previous.close - 1
        gross = current_weight * asset_return
        actual_funding, settlements = funding.get(current.open_time_ms, (0.0, 0))
        funding_return = current_weight * (
            actual_funding + scenario.additional_funding_per_settlement * settlements
        )
        transaction_cost = turnover * scenario.transaction_cost_bps / 10_000
        net = gross - funding_return - transaction_cost
        if not all(math.isfinite(value) for value in (gross, funding_return, transaction_cost, net)):
            raise ValueError("non-finite Donchian replay economics")
        result.append(
            SymbolDay(
                current.open_time_ms,
                net,
                gross,
                funding_return,
                transaction_cost,
                current_weight,
                turnover,
            )
        )
    return tuple(result)


def _maximum_drawdown(equity: Sequence[float]) -> float:
    peak = equity[0]
    drawdown = 0.0
    for value in equity:
        peak = max(peak, value)
        drawdown = max(drawdown, 1 - value / peak)
    return drawdown


def _profit_factor(returns: Sequence[float]) -> float | None:
    profits = math.fsum(max(0.0, value) for value in returns)
    losses = -math.fsum(min(0.0, value) for value in returns)
    return profits / losses if losses > 0 else None


def _metric_number(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"{name} must be numeric")
    result = float(value)
    if not math.isfinite(result):
        raise ValueError(f"{name} must be finite")
    return result


def _metric_integer(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{name} must be an integer")
    return value


def _sharpe(returns: Sequence[float]) -> float:
    if len(returns) < 2 or stdev(returns) == 0:
        return 0.0
    return mean(returns) / stdev(returns) * math.sqrt(365)


def _summarize_symbol(days: Sequence[SymbolDay]) -> dict[str, object]:
    equity = [1.0]
    for item in days:
        equity.append(equity[-1] * (1 + item.net_return))
    returns = [item.net_return for item in days]
    return {
        "active_days": sum(item.target_weight > 0 for item in days),
        "allocation_changes": sum(item.turnover > 0 for item in days),
        "annualized_sharpe": _sharpe(returns),
        "average_target_weight": mean(item.target_weight for item in days),
        "funding_return": math.fsum(item.funding_return for item in days),
        "maximum_drawdown": _maximum_drawdown(equity),
        "profit_factor": _profit_factor(returns),
        "total_return": equity[-1] - 1,
        "transaction_cost_return": math.fsum(item.transaction_cost_return for item in days),
        "turnover": math.fsum(item.turnover for item in days),
    }


def _summarize_portfolio(
    by_symbol: Mapping[str, Sequence[SymbolDay]], scenario: AllocationCostScenario
) -> dict[str, object]:
    dates = tuple(item.day_ms for item in next(iter(by_symbol.values())))
    if any(tuple(item.day_ms for item in rows) != dates for rows in by_symbol.values()):
        raise ValueError("symbol daily return calendars differ")
    sleeve_values = {symbol: INITIAL_EQUITY_USD / len(SYMBOLS) for symbol in SYMBOLS}
    equity = [INITIAL_EQUITY_USD]
    returns: list[float] = []
    monthly_rebalance_cost_usd = 0.0
    previous_month: tuple[int, int] | None = None
    for index, day_ms in enumerate(dates):
        prior_equity = equity[-1]
        day = datetime.fromtimestamp(day_ms / 1_000, UTC)
        month = (day.year, day.month)
        if previous_month is not None and month != previous_month:
            total = math.fsum(sleeve_values.values())
            target = total / len(SYMBOLS)
            cost = (
                math.fsum(
                    abs(target - sleeve_values[symbol]) * by_symbol[symbol][index].target_weight
                    for symbol in SYMBOLS
                )
                * scenario.transaction_cost_bps
                / 10_000
            )
            monthly_rebalance_cost_usd += cost
            total -= cost
            sleeve_values = {symbol: total / len(SYMBOLS) for symbol in SYMBOLS}
        previous_month = month
        for symbol in SYMBOLS:
            sleeve_values[symbol] *= 1 + by_symbol[symbol][index].net_return
        after = math.fsum(sleeve_values.values())
        returns.append(after / prior_equity - 1)
        equity.append(after)
    per_symbol = {symbol: _summarize_symbol(by_symbol[symbol]) for symbol in SYMBOLS}
    return {
        "active_days": sum(
            any(by_symbol[symbol][i].target_weight > 0 for symbol in SYMBOLS) for i in range(len(dates))
        ),
        "allocation_changes": sum(sum(item.turnover > 0 for item in by_symbol[symbol]) for symbol in SYMBOLS),
        "annualized_sharpe": _sharpe(returns),
        "average_gross_exposure": mean(
            math.fsum(by_symbol[symbol][i].target_weight for symbol in SYMBOLS) / len(SYMBOLS)
            for i in range(len(dates))
        ),
        "days": len(dates),
        "funding_return": math.fsum(item.funding_return for rows in by_symbol.values() for item in rows)
        / len(SYMBOLS),
        "maximum_drawdown": _maximum_drawdown(equity),
        "monthly_rebalance_cost_usd": monthly_rebalance_cost_usd,
        "per_symbol": per_symbol,
        "positive_symbols": sum(
            _metric_number(item["total_return"], "symbol total return") > 0 for item in per_symbol.values()
        ),
        "profit_factor": _profit_factor(returns),
        "total_return": equity[-1] / INITIAL_EQUITY_USD - 1,
        "transaction_cost_return": math.fsum(
            item.transaction_cost_return for rows in by_symbol.values() for item in rows
        )
        / len(SYMBOLS),
        "turnover": math.fsum(item.turnover for rows in by_symbol.values() for item in rows),
    }


def _gate_failures(windows: Mapping[str, Mapping[str, Mapping[str, object]]]) -> tuple[str, ...]:
    failures: list[str] = []
    for window_name in ("selection", "robustness"):
        for scenario_name in SCENARIOS:
            metrics = windows[window_name][scenario_name]
            prefix = f"{window_name}.{scenario_name}"
            profit_factor = metrics["profit_factor"]
            checks = (
                (
                    _metric_number(metrics["total_return"], "total return") > 0,
                    "total_return_not_positive",
                ),
                (
                    profit_factor is not None
                    and _metric_number(profit_factor, "profit factor") > MINIMUM_DAILY_PROFIT_FACTOR,
                    "profit_factor_below_minimum",
                ),
                (
                    _metric_number(metrics["annualized_sharpe"], "annualized sharpe")
                    >= MINIMUM_ANNUALIZED_SHARPE,
                    "sharpe_below_minimum",
                ),
                (
                    _metric_integer(metrics["positive_symbols"], "positive symbols")
                    >= MINIMUM_POSITIVE_SYMBOLS,
                    "positive_symbols_below_minimum",
                ),
                (
                    _metric_integer(metrics["active_days"], "active days") >= MINIMUM_ACTIVE_DAYS,
                    "active_days_below_minimum",
                ),
                (
                    _metric_integer(metrics["allocation_changes"], "allocation changes")
                    >= MINIMUM_ALLOCATION_CHANGES,
                    "allocation_changes_below_minimum",
                ),
                (
                    _metric_number(metrics["maximum_drawdown"], "maximum drawdown") <= MAXIMUM_DRAWDOWN,
                    "drawdown_above_maximum",
                ),
            )
            failures.extend(f"{prefix}.{name}" for passed, name in checks if not passed)
    return tuple(failures)


def run_donchian_screen(
    *,
    project_root: Path,
    price_cache: Path,
    factor_cache: Path,
    plan_path: Path,
    attempt_path: Path,
    result_path: Path,
) -> dict[str, object]:
    if attempt_path.exists() or result_path.exists():
        raise FileExistsError("one-shot Donchian attempt is unavailable")
    plan = load_preregistered_plan(plan_path)
    environment = _environment(project_root)
    attempt = _consume_attempt(plan, plan_path=plan_path, attempt_path=attempt_path)
    price_audit = audit_cached_archives(price_cache, SYMBOLS, DATA_START, DATA_END)
    _validate_archive_audit(price_audit)
    factors = load_factor_cache(factor_cache)
    if factors.inventory_sha256 != FACTOR_INVENTORY_SHA256:
        raise ValueError("factor inventory differs from preregistration")
    loader = BinanceArchiveLoader(price_cache, allow_download=False)
    config = DonchianEnsembleConfig()
    windows: dict[str, dict[str, Mapping[str, object]]] = {}
    datasets: list[dict[str, object]] = []
    for window in EVALUATION_WINDOWS:
        start_ms = int(datetime.combine(window.start, datetime.min.time(), UTC).timestamp() * 1_000)
        end_ms = int(datetime.combine(window.end, datetime.min.time(), UTC).timestamp() * 1_000)
        by_scenario: dict[str, dict[str, tuple[SymbolDay, ...]]] = {name: {} for name in SCENARIOS}
        for symbol in SYMBOLS:
            load_start = window.start - timedelta(days=WARMUP_DAYS)
            candles, manifest = loader.load(symbol, load_start, window.end, "1m")
            _validate_window_manifest(symbol, candles, manifest, start=load_start, end=window.end)
            datasets.append({"window": window.name, **asdict(manifest)})
            allocations = generate_donchian_ensemble_allocations(candles, config)
            daily = aggregate(candles, "1d")
            funding = _funding_by_day(factors, symbol)
            for scenario_name, scenario in SCENARIOS.items():
                by_scenario[scenario_name][symbol] = _symbol_days(
                    daily, allocations, funding, scenario, start_ms, end_ms
                )
        windows[window.name] = {
            scenario_name: _summarize_portfolio(by_scenario[scenario_name], scenario)
            for scenario_name, scenario in SCENARIOS.items()
        }
    failures = _gate_failures(windows)
    summary: dict[str, object] = {
        "attempt": attempt,
        "classification": "FORWARD_FREEZE_CANDIDATE" if not failures else "REJECT_REUSED_DATA_SCREEN",
        "data_quality": {
            "factor_audits": [asdict(audit) for audit in factors.audits],
            "factor_inventory_sha256": factors.inventory_sha256,
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
        "windows": windows,
    }
    _atomic_write(result_path, summary)
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the preregistered Donchian ensemble screen")
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
    result = run_donchian_screen(
        project_root=Path(__file__).resolve().parents[1],
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
