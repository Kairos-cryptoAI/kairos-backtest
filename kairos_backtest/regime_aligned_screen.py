"""One-shot reused-data screen for ``regime_aligned_right_tail_v1``.

Both source mechanisms and all available market windows have already been
observed.  The screen can therefore reject the synthesis or freeze it for
future evidence; it cannot establish alpha or authorize PAPER.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from collections.abc import Mapping
from dataclasses import asdict
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from kairos_strategy.candles import Candle
from kairos_strategy.models import SleeveIntent
from kairos_strategy.provenance import installed_source_tree_sha256
from kairos_strategy.registry import StrategyStatus, get_strategy
from kairos_strategy.sleeves import (
    RegimeAlignedRightTailConfig,
    RightTailTrendConfig,
    generate_regime_aligned_right_tail_intents,
    generate_right_tail_trend_intents,
)

from .cost_risk import RiskLimits
from .data import ArchiveFieldProfile, BinanceArchiveLoader, DatasetManifest
from .managed_evaluation import ManagedCellResult, ManagedEvaluationPolicy, evaluate_sleeve_cell
from .provenance import runtime_provenance, source_fingerprint
from .quarter_hour_screen import (
    INITIAL_EQUITY_USD,
    SYMBOLS,
    _atomic_write,
    _execution_scenarios,
    _git,
    _json_bytes,
    _json_value,
    _sha256,
    _utc_ms,
    _window_intents,
)
from .quarter_hour_screen import (
    WINDOWS as ALL_WINDOWS,
)
from .research_protocol import DataWindow, ResearchProtocol
from .right_tail_screen import (
    MAXIMUM_DRAWDOWN,
    MAXIMUM_ONE_SYMBOL_TRADE_SHARE,
    MINIMUM_ACTIVE_SYMBOLS,
    MINIMUM_DIRECTION_TRADES,
    MINIMUM_POSITIVE_EXPECTANCY_SYMBOLS,
    MINIMUM_PROFIT_FACTOR,
    MINIMUM_STRESS_TRADE_RETENTION,
    MINIMUM_TRADES_PER_SYMBOL,
    MINIMUM_TRADES_PER_WINDOW_SCENARIO,
    _costs,
    _summarize_cells,
)
from .right_tail_screen import (
    _gate_failures as _absolute_gate_failures,
)
from .seeding import derive_seed

SCHEMA_VERSION = "kairos.regime-aligned-right-tail-reused-screen.v1"
PLAN_FILENAME = "reports/regime-aligned-screen/plan.json"
ATTEMPT_FILENAME = "reports/regime-aligned-screen/attempt.json"
RESULT_FILENAME = "reports/regime-aligned-screen/summary.json"
PREFLIGHT_FILENAME = "reports/data-field-preflight/result-v3.json"
PREFLIGHT_FILE_SHA256 = "db99d1cdf42b5e7f98ba3e4b758138e849a6a281ae9e32afd409132fdb857386"
PREFLIGHT_RESULT_SHA256 = "91b1331fead7a7392b7a21f406f67e95e57c3ad1fd370f2c0a472c71d276a4dd"
PREFLIGHT_PLAN_SHA256 = "cdec435d635495d897e3e0b78a9e17b4cec38ff58bd2a62ed097bcacdbee3ae5"
STRATEGY_ID = "regime_aligned_right_tail_v1"
BASE_STRATEGY_ID = "right_tail_trend_v1"
STRATEGY_COMMIT = "8b00b82ed5d5dd5149532c596bed5ec8a825aadd"
RIGHT_TAIL_RESULT_SHA256 = "b3b62e262d2a60be8fd9f1a101b4df3d1ee9d342907bb2e21598d8eb17642a0b"
SMA200_RESULT_SHA256 = "1c2ecaeb2a961c9c858583878f5169dc000222b81342d80e79ab62a39841d83d"
SYNTHESIS_COMMIT = "390d2e81c5ade15f722f800c32fee733a2247184"
WARMUP_DAYS = 40
PREFLIGHT_WARMUP_DAYS = 40
BASE_SEED = 61
LINEAGE_TRIAL_NUMBER = 15
MINIMUM_BASE_TRADE_RETENTION = 0.50
FORWARD_MINIMUM_DAYS = 365
FORWARD_MINIMUM_TRADES = 500
_HOUR_MS = 60 * 60 * 1_000
_DAY_MS = 24 * _HOUR_MS
WINDOWS = tuple(window for window in ALL_WINDOWS if window.name in {"selection", "robustness"})

PROTOCOL = ResearchProtocol(
    protocol_name="regime-aligned-right-tail-reused-data-v1",
    universe=SYMBOLS,
    windows=WINDOWS,
    max_trials=1,
    maximum_holding_ms=72 * _HOUR_MS,
    maximum_label_horizon_ms=74 * _HOUR_MS,
    maximum_execution_latency_ms=500,
    warmup_ms=WARMUP_DAYS * _DAY_MS,
)


def expected_plan() -> dict[str, object]:
    config = RegimeAlignedRightTailConfig()
    base_config = RightTailTrendConfig()
    scenarios = _execution_scenarios()
    return {
        "classification": "post_hoc_synthesis_reused_data_only",
        "data": {
            "archive_end_exclusive": "2026-08-01",
            "archive_start": "2024-05-22",
            "field_profile": ArchiveFieldProfile.PRICE_VOLUME.value,
            "official_checksums_required": True,
            "preflight_file_sha256": PREFLIGHT_FILE_SHA256,
            "preflight_plan_sha256": PREFLIGHT_PLAN_SHA256,
            "preflight_result_sha256": PREFLIGHT_RESULT_SHA256,
            "preflight_warmup_days": PREFLIGHT_WARMUP_DAYS,
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
            "minimum_base_trade_retention": MINIMUM_BASE_TRADE_RETENTION,
            "minimum_direction_trades": MINIMUM_DIRECTION_TRADES,
            "minimum_expectancy_usd_per_trade": 0.0,
            "minimum_hac_sharpe": 0.0,
            "minimum_positive_expectancy_symbols": MINIMUM_POSITIVE_EXPECTANCY_SYMBOLS,
            "minimum_profit_factor_strictly_greater_than": MINIMUM_PROFIT_FACTOR,
            "minimum_stress_trade_retention": MINIMUM_STRESS_TRADE_RETENTION,
            "minimum_total_return": 0.0,
            "minimum_trades_per_symbol": MINIMUM_TRADES_PER_SYMBOL,
            "minimum_trades_per_window_scenario": MINIMUM_TRADES_PER_WINDOW_SCENARIO,
            "stress_drawdown_must_not_exceed_base": True,
            "stress_profit_factor_must_strictly_exceed_base": True,
        },
        "forward_gate": {
            "blind_start_not_before": "2026-09-01",
            "minimum_days": FORWARD_MINIMUM_DAYS,
            "minimum_trades": FORWARD_MINIMUM_TRADES,
        },
        "hypothesis": {
            "base_lifecycle_changed": False,
            "economic_premise": "slow_directional_state_filters_counter_regime_right_tail_entries",
            "global_symmetric_rule": True,
            "parameter_search_allowed": False,
            "post_hoc_synthesis": True,
            "research_lineage_trial_number": LINEAGE_TRIAL_NUMBER,
            "right_tail_result_sha256": RIGHT_TAIL_RESULT_SHA256,
            "sma200_result_sha256": SMA200_RESULT_SHA256,
            "synthesis_commit": SYNTHESIS_COMMIT,
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
            "base_config": asdict(base_config),
            "base_config_sha256": base_config.fingerprint,
            "base_id": BASE_STRATEGY_ID,
            "config": asdict(config),
            "config_sha256": config.fingerprint,
            "id": STRATEGY_ID,
            "revision": "1",
            "source_commit": STRATEGY_COMMIT,
            "status": "research",
        },
    }


def load_preregistered_plan(path: Path) -> dict[str, object]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or _json_value(payload) != _json_value(expected_plan()):
        raise ValueError("committed regime-aligned plan differs from the executable plan")
    return payload


def _validate_preflight_receipt(path: Path) -> dict[str, object]:
    raw = path.read_bytes()
    normalized = raw.replace(b"\r\n", b"\n")
    if hashlib.sha256(normalized).hexdigest() != PREFLIGHT_FILE_SHA256:
        raise ValueError("data preflight receipt file differs from preregistration")
    payload = json.loads(raw)
    if not isinstance(payload, dict) or payload.get("result_sha256") != PREFLIGHT_RESULT_SHA256:
        raise ValueError("data preflight receipt identity differs from preregistration")
    unsigned = dict(payload)
    unsigned.pop("result_sha256", None)
    if (
        _sha256(unsigned) != PREFLIGHT_RESULT_SHA256
        or payload.get("plan_sha256") != PREFLIGHT_PLAN_SHA256
        or payload.get("classification") != "DATA_PREFLIGHT_PASSED"
    ):
        raise ValueError("data preflight receipt is invalid")
    evidence = payload.get("evidence")
    if not isinstance(evidence, list):
        raise ValueError("data preflight receipt has no evidence")
    expected = {(window.name, symbol) for window in WINDOWS for symbol in SYMBOLS}
    purpose_windows = {
        "selection_with_40d_warmup": "selection",
        "robustness_with_40d_warmup": "robustness",
    }
    observed = {
        (purpose_windows[item["purpose"]], item.get("symbol"))
        for item in evidence
        if isinstance(item, dict)
        and item.get("purpose") in purpose_windows
        and item.get("field_profile") == ArchiveFieldProfile.PRICE_VOLUME.value
    }
    if observed != expected:
        raise ValueError("data preflight receipt does not qualify every evaluation slice")
    return payload


def _preflight_evidence(receipt: Mapping[str, object], symbol: str, purpose: str) -> Mapping[str, object]:
    evidence = receipt.get("evidence")
    if not isinstance(evidence, list):
        raise ValueError("preflight evidence is missing")
    matches = [
        item
        for item in evidence
        if isinstance(item, dict)
        and item.get("symbol") == symbol
        and item.get("purpose") == purpose
        and item.get("field_profile") == ArchiveFieldProfile.PRICE_VOLUME.value
    ]
    if len(matches) != 1:
        raise ValueError(f"preflight evidence is ambiguous for {symbol} {purpose}")
    return matches[0]


def _validate_manifest(
    manifest: DatasetManifest,
    candles: list[Candle],
    *,
    start: date,
    end: date,
    evidence: Mapping[str, object],
) -> None:
    expected_rows = (end - start).days * 24 * 60
    checks = (
        manifest.symbol == evidence.get("symbol"),
        manifest.requested_start == start.isoformat(),
        manifest.requested_end == end.isoformat(),
        manifest.rows == expected_rows == len(candles) == evidence.get("rows"),
        manifest.sha256 == evidence.get("normalized_rows_sha256"),
        manifest.gaps == 0,
        manifest.field_profile == ArchiveFieldProfile.PRICE_VOLUME.value,
        manifest.checksum_files_verified == evidence.get("checksum_files_verified"),
        manifest.quarantined_optional_rows == evidence.get("quarantined_optional_rows"),
    )
    if not all(checks):
        raise ValueError("loaded price slice differs from performance-blind preflight")


def _evaluation_slice(candles: list[Candle], window: DataWindow) -> list[Candle]:
    start_day = window.start - timedelta(days=WARMUP_DAYS)
    start_ms = _utc_ms(start_day)
    end_ms = _utc_ms(window.end)
    rows = [row for row in candles if start_ms <= row.open_time_ms < end_ms]
    expected = (window.end - start_day).days * 24 * 60
    if (
        len(rows) != expected
        or not rows
        or rows[0].open_time_ms != start_ms
        or rows[-1].close_time_ms != end_ms - 1
    ):
        raise ValueError("evaluation slice lacks complete warmup or evaluation minutes")
    return rows


def _number(metrics: Mapping[str, object], name: str) -> float:
    value = metrics[name]
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"{name} metric must be numeric")
    return float(value)


def _integer(metrics: Mapping[str, object], name: str) -> int:
    value = metrics[name]
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{name} metric must be an integer")
    return value


def _gate_failures(
    windows: Mapping[str, Mapping[str, Mapping[str, object]]],
    benchmarks: Mapping[str, Mapping[str, Mapping[str, object]]],
) -> tuple[str, ...]:
    failures = list(_absolute_gate_failures(windows))
    for window_name in ("selection", "robustness"):
        candidate = windows[window_name]["stress"]
        base = benchmarks[window_name]["stress"]
        candidate_trades = _integer(candidate, "trades")
        base_trades = _integer(base, "trades")
        if not base_trades or candidate_trades / base_trades < MINIMUM_BASE_TRADE_RETENTION:
            failures.append(f"{window_name}.stress.base_trade_retention_below_minimum")
        candidate_pf = candidate["profit_factor"]
        base_pf = base["profit_factor"]
        if (
            candidate_pf is None
            or base_pf is None
            or _number(candidate, "profit_factor") <= _number(base, "profit_factor")
        ):
            failures.append(f"{window_name}.stress.profit_factor_not_above_base")
        if _number(candidate, "maximum_drawdown") > _number(base, "maximum_drawdown"):
            failures.append(f"{window_name}.stress.drawdown_above_base")
    return tuple(failures)


def _environment(project_root: Path) -> dict[str, object]:
    if _git(project_root, "status", "--porcelain=v1", "--untracked-files=all"):
        raise RuntimeError("regime-aligned screen refuses to open data from a dirty Git worktree")
    definition = get_strategy(STRATEGY_ID)
    if definition.status is not StrategyStatus.RESEARCH or definition.revision != "1":
        raise RuntimeError("regime-aligned strategy registration changed after preregistration")
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
        "classification": "post_hoc_synthesis_reused_data_only",
        "consumed_at": _now_utc(),
        "consumption_point": "after_preflight_receipt_before_first_market_archive_access",
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
        raise RuntimeError("regime-aligned attempt ledger changed after publication")
    return payload


def _evaluate(
    *,
    rows: list[Candle],
    intents: list[SleeveIntent],
    sleeve_id: str,
    window_name: str,
    symbol: str,
    manifest: DatasetManifest,
    config_fingerprint: str,
) -> dict[str, ManagedCellResult]:
    scenarios = _execution_scenarios()
    result: dict[str, ManagedCellResult] = {}
    for scenario_name, execution in scenarios.items():
        result[scenario_name] = evaluate_sleeve_cell(
            rows,
            intents,
            cell_id=f"{window_name}:{scenario_name}:{sleeve_id}:{symbol}",
            sleeve_id=sleeve_id,
            symbol=symbol,
            initial_equity_usd=INITIAL_EQUITY_USD / len(SYMBOLS),
            execution=execution,
            costs=_costs(execution),
            limits=RiskLimits(),
            policy=ManagedEvaluationPolicy(
                application_exit_latency_ms=execution.latency_ms,
                terminal_liquidation_grace_ms=_HOUR_MS,
            ),
            seed=derive_seed(
                BASE_SEED,
                SCHEMA_VERSION,
                window_name,
                scenario_name,
                sleeve_id,
                symbol,
                manifest.sha256,
                config_fingerprint,
            ),
        )
    return result


def run_regime_aligned_screen(
    *,
    project_root: Path,
    cache_dir: Path,
    plan_path: Path,
    preflight_path: Path,
    attempt_path: Path,
    result_path: Path,
) -> dict[str, object]:
    if attempt_path.exists() or result_path.exists():
        raise FileExistsError("one-shot regime-aligned attempt is unavailable")
    plan = load_preregistered_plan(plan_path)
    receipt = _validate_preflight_receipt(preflight_path)
    environment = _environment(project_root)
    attempt = _consume_attempt(plan, plan_path=plan_path, attempt_path=attempt_path)
    loader = BinanceArchiveLoader(
        cache_dir,
        allow_download=False,
        field_profile=ArchiveFieldProfile.PRICE_VOLUME,
    )
    config = RegimeAlignedRightTailConfig()
    base_config = RightTailTrendConfig()
    candidate_cells: dict[str, dict[str, list[ManagedCellResult]]] = {
        window.name: {name: [] for name in _execution_scenarios()} for window in WINDOWS
    }
    base_cells: dict[str, dict[str, list[ManagedCellResult]]] = {
        window.name: {name: [] for name in _execution_scenarios()} for window in WINDOWS
    }
    datasets: list[dict[str, object]] = []
    for window in WINDOWS:
        purpose = f"{window.name}_with_40d_warmup"
        load_start = window.start - timedelta(days=PREFLIGHT_WARMUP_DAYS)
        for symbol in SYMBOLS:
            candles, manifest = loader.load(symbol, load_start, window.end, "1m")
            evidence = _preflight_evidence(receipt, symbol, purpose)
            _validate_manifest(
                manifest,
                candles,
                start=load_start,
                end=window.end,
                evidence=evidence,
            )
            datasets.append({"window": window.name, **asdict(manifest)})
            rows = _evaluation_slice(candles, window)
            candidate_intents = _window_intents(
                generate_regime_aligned_right_tail_intents(rows, config), window
            )
            base_intents = _window_intents(generate_right_tail_trend_intents(rows, base_config), window)
            evaluated_candidate = _evaluate(
                rows=rows,
                intents=candidate_intents,
                sleeve_id=STRATEGY_ID,
                window_name=window.name,
                symbol=symbol,
                manifest=manifest,
                config_fingerprint=config.fingerprint,
            )
            evaluated_base = _evaluate(
                rows=rows,
                intents=base_intents,
                sleeve_id=BASE_STRATEGY_ID,
                window_name=window.name,
                symbol=symbol,
                manifest=manifest,
                config_fingerprint=base_config.fingerprint,
            )
            for scenario_name in _execution_scenarios():
                candidate_cells[window.name][scenario_name].append(evaluated_candidate[scenario_name])
                base_cells[window.name][scenario_name].append(evaluated_base[scenario_name])
    windows = {
        window.name: {
            scenario_name: _summarize_cells(tuple(candidate_cells[window.name][scenario_name]))
            for scenario_name in _execution_scenarios()
        }
        for window in WINDOWS
    }
    benchmarks = {
        window.name: {
            scenario_name: _summarize_cells(tuple(base_cells[window.name][scenario_name]))
            for scenario_name in _execution_scenarios()
        }
        for window in WINDOWS
    }
    failures = _gate_failures(windows, benchmarks)
    summary: dict[str, object] = {
        "attempt": attempt,
        "base_benchmark": {"strategy_id": BASE_STRATEGY_ID, "windows": benchmarks},
        "classification": "FORWARD_FREEZE_CANDIDATE" if not failures else "REJECT_REUSED_DATA_SCREEN",
        "data_quality": {
            "evaluated_slices": datasets,
            "preflight_file_sha256": PREFLIGHT_FILE_SHA256,
            "preflight_result_sha256": PREFLIGHT_RESULT_SHA256,
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
    parser = argparse.ArgumentParser(description="Run the preregistered regime-aligned reused-data screen")
    parser.add_argument("--cache-dir", type=Path, default=Path("data/historical"))
    parser.add_argument("--plan", type=Path, default=Path(PLAN_FILENAME))
    parser.add_argument("--preflight", type=Path, default=Path(PREFLIGHT_FILENAME))
    parser.add_argument("--attempt", type=Path, default=Path(ATTEMPT_FILENAME))
    parser.add_argument("--result", type=Path, default=Path(RESULT_FILENAME))
    parser.add_argument("--verify-plan", action="store_true")
    parser.add_argument("--write-plan", action="store_true")
    arguments = parser.parse_args()
    if arguments.write_plan:
        if arguments.plan.exists():
            raise FileExistsError(f"refusing to overwrite plan: {arguments.plan}")
        _atomic_write(arguments.plan, expected_plan())
        print(f"plan_sha256={_sha256(expected_plan())}")
        return 0
    plan = load_preregistered_plan(arguments.plan)
    if arguments.verify_plan:
        _validate_preflight_receipt(arguments.preflight)
        print(f"plan_sha256={_sha256(plan)}")
        print(f"preflight_result_sha256={PREFLIGHT_RESULT_SHA256}")
        return 0
    result = run_regime_aligned_screen(
        project_root=Path(__file__).resolve().parents[1],
        cache_dir=arguments.cache_dir,
        plan_path=arguments.plan,
        preflight_path=arguments.preflight,
        attempt_path=arguments.attempt,
        result_path=arguments.result,
    )
    print(f"classification={result['classification']}")
    print("alpha_ready=false paper_allowed=false live_allowed=false")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
