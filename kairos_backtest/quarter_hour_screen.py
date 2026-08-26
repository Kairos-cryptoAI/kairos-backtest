"""Single-candidate reused-data screen for ``quarter_hour_flow_v1``.

The committed plan is verified before the Binance cache is opened.  Results
can reject the hypothesis or freeze it for future observation, but this module
contains no PAPER, LIVE, or alpha-promotion path.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import subprocess  # nosec B404
import tempfile
from bisect import bisect_left
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict
from datetime import UTC, date, datetime, timedelta
from enum import Enum
from pathlib import Path
from typing import Any

from kairos_strategy.candles import Candle
from kairos_strategy.provenance import installed_source_tree_sha256
from kairos_strategy.registry import StrategyStatus, get_strategy
from kairos_strategy.sleeves import QuarterHourFlowConfig, generate_quarter_hour_flow_intents

from .cost_risk import AllInCostModel, RiskLimits
from .data import (
    ArchiveInventoryAudit,
    BinanceArchiveLoader,
    DatasetManifest,
    audit_cached_archives,
    month_starts,
)
from .execution import ExecutionConfig, FundingConfig
from .managed_evaluation import ManagedCellResult, ManagedEvaluationPolicy, evaluate_sleeve_cell
from .portfolio import CellEquityCurve, synchronize_cells
from .provenance import runtime_provenance, source_fingerprint
from .research_protocol import DataRole, DataWindow, ResearchProtocol
from .robustness import hac_sharpe
from .scenarios import SYMBOLS
from .seeding import derive_seed

SCHEMA_VERSION = "kairos.quarter-hour-reused-screen.v1"
PLAN_FILENAME = "reports/quarter-hour-screen/plan.json"
RESULT_FILENAME = "reports/quarter-hour-screen/summary.json"
STRATEGY_ID = "quarter_hour_flow_v1"
STRATEGY_COMMIT = "505012c70aed28608ee9edf10cb8338c2c02279d"
DATA_START = date(2021, 7, 1)
DATA_END = date(2026, 8, 1)
WARMUP_DAYS = 2
INITIAL_EQUITY_USD = 100_000.0
BASE_SEED = 42
MINIMUM_TRADES_PER_WINDOW_SCENARIO = 100
MINIMUM_TRADES_PER_SYMBOL = 10
MINIMUM_ACTIVE_SYMBOLS = 3
MAXIMUM_DRAWDOWN = 0.10
FORWARD_MINIMUM_MONTHS = 8
FORWARD_MINIMUM_TRADES = 500
_ONE_MINUTE_MS = 60_000
_ONE_HOUR_MS = 60 * _ONE_MINUTE_MS
_DAY_MS = 24 * _ONE_HOUR_MS

WINDOWS = (
    DataWindow("research", date(2021, 8, 1), date(2024, 7, 1), DataRole.RESEARCH),
    DataWindow("selection", date(2024, 7, 1), date(2025, 7, 1), DataRole.SELECTION),
    DataWindow("robustness", date(2025, 7, 1), date(2026, 8, 1), DataRole.ROBUSTNESS),
)

PROTOCOL = ResearchProtocol(
    protocol_name="quarter-hour-flow-reused-data-v1",
    universe=SYMBOLS,
    windows=WINDOWS,
    max_trials=1,
    maximum_holding_ms=8 * _ONE_HOUR_MS,
    maximum_label_horizon_ms=9 * _ONE_HOUR_MS,
    maximum_execution_latency_ms=500,
    warmup_ms=WARMUP_DAYS * _DAY_MS,
)


def _execution_scenarios() -> dict[str, ExecutionConfig]:
    return {
        "baseline": ExecutionConfig(
            latency_ms=250,
            spread_bps=2,
            slippage_bps=2,
            fee_bps=4.5,
            funding=FundingConfig(),
        ),
        "stress": ExecutionConfig(
            latency_ms=500,
            spread_bps=4,
            slippage_bps=4,
            fee_bps=4.5,
            funding=FundingConfig(
                rate_8h_bps=5.0,
                source="assumed_adverse_stress",
                evidence="assumed",
            ),
        ),
    }


def _costs(execution: ExecutionConfig) -> AllInCostModel:
    holding_and_grace_hours = 9
    adverse_funding_bps = 0.0
    if execution.funding.evidence == "assumed":
        rate = execution.funding.rate_8h_bps
        if rate is None:
            raise RuntimeError("assumed funding lost its configured rate")
        adverse_funding_bps = holding_and_grace_hours * rate / 8
    return AllInCostModel(
        fee_bps_per_side=execution.fee_bps,
        spread_bps=execution.spread_bps,
        slippage_bps_per_side=execution.slippage_bps + execution.slippage_jitter_bps,
        adverse_funding_bps=adverse_funding_bps,
        uncertainty_buffer_bps=2.0,
    )


def expected_plan() -> dict[str, object]:
    config = QuarterHourFlowConfig()
    scenarios = _execution_scenarios()
    return {
        "classification": "reused_data_development_only",
        "data": {
            "archive_end_exclusive": DATA_END.isoformat(),
            "archive_start": DATA_START.isoformat(),
            "official_checksums_required": True,
            "proxy_limitation": "closed_first_1m_not_first_10s_trades",
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
            "minimum_active_symbols": MINIMUM_ACTIVE_SYMBOLS,
            "minimum_hac_sharpe": 0.0,
            "minimum_profit_factor": 1.0,
            "minimum_total_return": 0.0,
            "minimum_trades_per_symbol": MINIMUM_TRADES_PER_SYMBOL,
            "minimum_trades_per_window_scenario": MINIMUM_TRADES_PER_WINDOW_SCENARIO,
            "maximum_drawdown": MAXIMUM_DRAWDOWN,
        },
        "forward_gate": {
            "blind_start_not_before": "2026-09-01",
            "minimum_months": FORWARD_MINIMUM_MONTHS,
            "minimum_trades": FORWARD_MINIMUM_TRADES,
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
                        terminal_liquidation_grace_ms=_ONE_HOUR_MS,
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


def _json_value(value: object) -> object:
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("result datetimes must be timezone-aware")
        return value.isoformat()
    if type(value) is date:
        return value.isoformat()
    if isinstance(value, Mapping):
        if any(not isinstance(key, str) for key in value):
            raise TypeError("JSON mappings require string keys")
        return {key: _json_value(value[key]) for key in sorted(value)}
    if isinstance(value, (tuple, list)):
        return [_json_value(item) for item in value]
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("JSON results cannot contain non-finite numbers")
        return 0.0 if value == 0 else value
    raise TypeError(f"unsupported JSON value: {type(value).__name__}")


def _json_bytes(value: object) -> bytes:
    return (
        json.dumps(
            _json_value(value),
            allow_nan=False,
            ensure_ascii=True,
            indent=2,
            sort_keys=True,
        )
        + "\n"
    ).encode()


def _sha256(value: object) -> str:
    encoded = json.dumps(
        _json_value(value),
        allow_nan=False,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def load_preregistered_plan(path: Path) -> dict[str, object]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or _json_value(payload) != _json_value(expected_plan()):
        raise ValueError("committed quarter-hour plan differs from the executable plan")
    return payload


def _git(project_root: Path, *arguments: str) -> str:
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
        raise RuntimeError("quarter-hour screen refuses to open data from a dirty Git worktree")
    definition = get_strategy(STRATEGY_ID)
    if definition.status is not StrategyStatus.RESEARCH or definition.revision != "1":
        raise RuntimeError("quarter-hour strategy registration changed after preregistration")
    lock_path = project_root / "uv.lock"
    return {
        "git_head_sha": _git(project_root, "rev-parse", "HEAD"),
        "git_tree_sha": _git(project_root, "rev-parse", "HEAD^{tree}"),
        "pyproject_sha256": hashlib.sha256((project_root / "pyproject.toml").read_bytes()).hexdigest(),
        "runtime": runtime_provenance().as_dict(),
        "source_sha256": source_fingerprint(project_root / "kairos_backtest"),
        "strategy_source_sha256": installed_source_tree_sha256(definition.source_files),
        "uv_lock_sha256": hashlib.sha256(lock_path.read_bytes()).hexdigest(),
    }


def _utc_ms(day: date) -> int:
    return int(datetime.combine(day, datetime.min.time(), UTC).timestamp() * 1_000)


def _validate_archive_audit(audit: ArchiveInventoryAudit) -> None:
    expected_files = len(month_starts(DATA_START, DATA_END)) * len(SYMBOLS)
    if (
        audit.requested_start != DATA_START.isoformat()
        or audit.requested_end != DATA_END.isoformat()
        or audit.expected_files != expected_files
        or audit.present_files != expected_files
        or audit.checksum_files_verified != expected_files
        or audit.csv_schema != "binance_futures_kline_v1_12_columns"
        or tuple(item.symbol for item in audit.symbols) != SYMBOLS
    ):
        raise ValueError("archive inventory is incomplete or lacks official integrity evidence")


def _validate_window_manifest(
    symbol: str,
    candles: Sequence[Candle],
    manifest: DatasetManifest,
    *,
    start: date,
    end: date,
) -> None:
    expected_rows = (end - start).days * 24 * 60
    expected_files = len(month_starts(start, end))
    expected = (
        manifest.symbol == symbol,
        manifest.interval == "1m",
        manifest.requested_start == start.isoformat(),
        manifest.requested_end == end.isoformat(),
        manifest.actual_start_ms == _utc_ms(start),
        manifest.actual_end_ms == _utc_ms(end) - 1,
        manifest.rows == expected_rows == len(candles),
        manifest.gaps == 0,
        manifest.expected_files == expected_files,
        manifest.checksum_files_verified == expected_files,
        manifest.checksum_status == "official_sha256_verified",
        manifest.transport_verification == "zip_crc_and_parsed_rows_sha256",
        manifest.csv_schema == "binance_futures_kline_v1_12_columns",
    )
    if not all(expected):
        raise ValueError(f"{symbol} {start}..{end} is not a complete verified evaluation slice")


def _window_slice(candles: list[Candle], window: DataWindow) -> list[Candle]:
    opens = [candle.open_time_ms for candle in candles]
    start = _utc_ms(window.start - timedelta(days=WARMUP_DAYS))
    end = _utc_ms(window.end)
    left = bisect_left(opens, start)
    right = bisect_left(opens, end)
    rows = candles[left:right]
    expected = (window.end - (window.start - timedelta(days=WARMUP_DAYS))).days * 24 * 60
    if (
        len(rows) != expected
        or not rows
        or rows[0].open_time_ms != start
        or rows[-1].close_time_ms != end - 1
    ):
        raise ValueError(f"{window.name} window does not contain complete warmup and evaluation minutes")
    return rows


def _window_intents(intents: Sequence[Any], window: DataWindow) -> list[Any]:
    start = _utc_ms(window.start)
    end = _utc_ms(window.end)
    return [intent for intent in intents if start <= intent.decision_ts_ms < end]


def _profit_factor(trades: Sequence[Any]) -> float | None:
    profit = sum(max(0.0, trade.net_pnl_usd) for trade in trades)
    loss = -sum(min(0.0, trade.net_pnl_usd) for trade in trades)
    return profit / loss if loss > 0 else None


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
    profit_factor = _profit_factor(trades)
    dispositions = Counter(
        disposition.reason.value for result in results for disposition in result.dispositions
    )
    return {
        "active_symbols": portfolio.active_symbols,
        "carry_cost_usd": sum(result.carry_cost_usd for result in results),
        "dispositions": dict(sorted(dispositions.items())),
        "fees_usd": sum(result.fees_usd for result in results),
        "hac_sharpe": _safe_hac(cells),
        "implementation_shortfall_usd": sum(result.implementation_shortfall_usd for result in results),
        "maximum_drawdown": portfolio.maximum_drawdown,
        "per_symbol": [
            {
                "replay_evidence_sha256": result.replay_evidence_sha256,
                "symbol": result.cell.symbol,
                "total_return": result.cell.closing_equity_usd[-1] / result.cell.initial_equity_usd - 1,
                "trades": len(result.cell.trades),
            }
            for result in results
        ],
        "profit_factor": profit_factor,
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
        for scenario_name in ("baseline", "stress"):
            metrics = windows[window_name][scenario_name]
            prefix = f"{window_name}.{scenario_name}"
            if integer(metrics, "trades") < MINIMUM_TRADES_PER_WINDOW_SCENARIO:
                failures.append(f"{prefix}.trades_below_minimum")
            if integer(metrics, "active_symbols") < MINIMUM_ACTIVE_SYMBOLS:
                failures.append(f"{prefix}.active_symbols_below_minimum")
            if number(metrics, "total_return") <= 0:
                failures.append(f"{prefix}.total_return_not_positive")
            if number(metrics, "hac_sharpe") <= 0:
                failures.append(f"{prefix}.hac_sharpe_not_positive")
            factor = metrics["profit_factor"]
            if factor is not None and number(metrics, "profit_factor") <= 1:
                failures.append(f"{prefix}.profit_factor_not_above_one")
            if factor is None and integer(metrics, "trades") == 0:
                failures.append(f"{prefix}.profit_factor_unavailable")
            if number(metrics, "maximum_drawdown") > MAXIMUM_DRAWDOWN:
                failures.append(f"{prefix}.drawdown_above_maximum")
            per_symbol = metrics["per_symbol"]
            if not isinstance(per_symbol, list) or any(not isinstance(item, dict) for item in per_symbol):
                raise TypeError("per_symbol metric must be a list of mappings")
            for item in per_symbol:
                if integer(item, "trades") < MINIMUM_TRADES_PER_SYMBOL:
                    failures.append(f"{prefix}.{item['symbol']}.trades_below_minimum")
    return tuple(failures)


def run_quarter_hour_screen(
    *,
    project_root: Path,
    cache_dir: Path,
    plan_path: Path,
    result_path: Path,
    loader_factory: Callable[[Path], BinanceArchiveLoader] | None = None,
) -> dict[str, object]:
    if result_path.exists():
        raise FileExistsError(f"refusing to overwrite existing result: {result_path}")
    plan = load_preregistered_plan(plan_path)
    environment = _environment(project_root)
    loader = (
        BinanceArchiveLoader(cache_dir, allow_download=False)
        if loader_factory is None
        else loader_factory(cache_dir)
    )
    config = QuarterHourFlowConfig()
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
            # The old research role contains known checksum-valid venue gaps
            # for SOL/XRP plus one invalid XRP row.  No minute is invented or
            # deleted silently: those two research cells are unavailable.
            # Selection and robustness are loaded and checked independently.
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
            _validate_window_manifest(
                symbol,
                candles,
                manifest,
                start=load_start,
                end=window.end,
            )
            datasets.append({"window": window.name, **asdict(manifest)})
            rows = _window_slice(candles, window)
            generated = generate_quarter_hour_flow_intents(rows, config)
            intents = _window_intents(generated, window)
            for scenario_name, execution in scenarios.items():
                cell_result = evaluate_sleeve_cell(
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
                        terminal_liquidation_grace_ms=_ONE_HOUR_MS,
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
                results_by_window[window.name][scenario_name].append(cell_result)

    summaries = {
        window.name: {
            scenario_name: _summarize_cells(tuple(results_by_window[window.name][scenario_name]))
            for scenario_name in scenarios
        }
        for window in WINDOWS
    }
    failures = _gate_failures(summaries)
    summary: dict[str, object] = {
        "classification": ("FORWARD_FREEZE_CANDIDATE" if not failures else "REJECT_REUSED_DATA_SCREEN"),
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


def _atomic_write(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(_json_bytes(payload))
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError as exc:
            raise FileExistsError(f"refusing to overwrite existing result: {path}") from exc
        temporary.unlink()
    finally:
        if temporary.exists():
            temporary.unlink()


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the preregistered quarter-hour reused-data screen")
    parser.add_argument("--cache-dir", type=Path, default=Path("data/historical"))
    parser.add_argument("--plan", type=Path, default=Path(PLAN_FILENAME))
    parser.add_argument("--result", type=Path, default=Path(RESULT_FILENAME))
    parser.add_argument("--verify-plan", action="store_true")
    arguments = parser.parse_args()
    plan = load_preregistered_plan(arguments.plan)
    if arguments.verify_plan:
        print(f"plan_sha256={_sha256(plan)}")
        return 0
    project_root = Path(__file__).resolve().parents[1]
    result = run_quarter_hour_screen(
        project_root=project_root,
        cache_dir=arguments.cache_dir,
        plan_path=arguments.plan,
        result_path=arguments.result,
    )
    print(f"classification={result['classification']}")
    print("alpha_ready=false paper_allowed=false live_allowed=false")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
