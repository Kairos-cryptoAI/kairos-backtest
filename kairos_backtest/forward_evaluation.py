"""Sealed one-shot evaluation for the frozen regime-aligned forward campaign.

The public eligibility path discloses no return, PnL, risk or quality metric.
Only after the preregistered duration and closed-trade count gates pass does
the evaluator durably consume its single attempt and calculate the final
candidate-versus-base decision.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any

from kairos_core.contracts import StrategyIntentV1
from kairos_strategy.candles import Candle
from kairos_strategy.models import ExitPlan, SleeveIntent
from kairos_strategy.runtime import closed_bar_to_candle, generate_runtime_strategy_intents
from kairos_strategy.runtime_requirements import get_runtime_requirements
from kairos_strategy.sleeves import RightTailTrendConfig, generate_right_tail_trend_intents

from .cost_risk import RiskLimits
from .forward_observation import (
    BASE_STRATEGY_ID,
    BLIND_START_MS,
    MINIMUM_BASE_TRADE_RETENTION,
    MINIMUM_END_MS,
    MINIMUM_FORWARD_DAYS,
    MINIMUM_FORWARD_TRADES,
    OBSERVATION_WINDOW_BARS,
    PLAN_FILENAME,
    STRATEGY_CONFIG_SHA256,
    STRATEGY_ID,
    SYMBOLS,
    ForwardIntegrityError,
    ForwardLedger,
    _canonical_document,
    load_plan,
    plan_sha256,
)
from .managed_evaluation import ManagedCellResult, ManagedEvaluationPolicy, evaluate_sleeve_cell
from .quarter_hour_screen import INITIAL_EQUITY_USD, _atomic_write, _execution_scenarios
from .right_tail_screen import (
    MAXIMUM_DRAWDOWN,
    MAXIMUM_ONE_SYMBOL_TRADE_SHARE,
    MINIMUM_ACTIVE_SYMBOLS,
    MINIMUM_DIRECTION_TRADES,
    MINIMUM_POSITIVE_EXPECTANCY_SYMBOLS,
    MINIMUM_PROFIT_FACTOR,
    MINIMUM_STRESS_TRADE_RETENTION,
    MINIMUM_TRADES_PER_SYMBOL,
    _costs,
    _summarize_cells,
)
from .seeding import derive_seed

SCHEMA_VERSION = "kairos.regime-aligned-forward-evaluation.v1"
LOCK_SCHEMA_VERSION = "kairos.regime-aligned-forward-evaluator-lock.v1"
LOCK_FILENAME = "reports/regime-aligned-forward/evaluator-lock.json"
ATTEMPT_FILENAME = "reports/regime-aligned-forward/final-attempt.json"
RESULT_FILENAME = "reports/regime-aligned-forward/final-result.json"
BASE_SEED = 61
_MINUTE_MS = 60_000
_HOUR_MS = 60 * 60 * 1_000

EVALUATOR_FILES = (
    "kairos_backtest/barrier_engine.py",
    "kairos_backtest/cost_risk.py",
    "kairos_backtest/execution.py",
    "kairos_backtest/forward_evaluation.py",
    "kairos_backtest/forward_observation.py",
    "kairos_backtest/managed_evaluation.py",
    "kairos_backtest/portfolio.py",
    "kairos_backtest/quarter_hour_screen.py",
    "kairos_backtest/right_tail_screen.py",
    "kairos_backtest/robustness.py",
    "kairos_backtest/seeding.py",
    "kairos_backtest/strategy_models.py",
    "kairos_backtest/validation.py",
    "pyproject.toml",
    "uv.lock",
)


class ForwardNotEligibleError(RuntimeError):
    """The one-shot attempt remains unconsumed because a precondition failed."""


CampaignEvaluator = Callable[
    [ForwardLedger, int, str, bool],
    tuple[dict[str, dict[str, object]], dict[str, dict[str, object]] | None],
]


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _normalized_file_sha256(path: Path) -> str:
    return _sha256(path.read_bytes().replace(b"\r\n", b"\n"))


def evaluator_lock_sha256(lock: Mapping[str, object]) -> str:
    return _sha256(_canonical_document(lock))


def load_evaluator_lock(project_root: Path, path: Path) -> dict[str, object]:
    """Validate the pre-blind semantic-source lock without trusting Git state."""

    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("evaluator lock must be a JSON object")
    if payload.get("schema_version") != LOCK_SCHEMA_VERSION:
        raise ValueError("evaluator lock schema differs from the frozen evaluator")
    if payload.get("plan_sha256") != plan_sha256(load_plan(project_root / PLAN_FILENAME)):
        raise ValueError("evaluator lock belongs to a different forward plan")
    files = payload.get("files")
    if not isinstance(files, dict) or set(files) != set(EVALUATOR_FILES):
        raise ValueError("evaluator lock does not cover the exact semantic file set")
    for relative_name in EVALUATOR_FILES:
        pure = PurePosixPath(relative_name)
        if pure.is_absolute() or ".." in pure.parts:
            raise ValueError("evaluator lock contains an unsafe path")
        expected = files[relative_name]
        if not isinstance(expected, str) or len(expected) != 64:
            raise ValueError(f"invalid evaluator hash for {relative_name}")
        actual = _normalized_file_sha256(project_root.joinpath(*pure.parts))
        if actual != expected:
            raise ValueError(f"semantic evaluator source changed after freeze: {relative_name}")
    return payload


def _strict_to_raw(intent: StrategyIntentV1) -> SleeveIntent:
    """Recover the exact pure-sleeve identity embedded in a strict runtime intent."""

    return SleeveIntent(
        sleeve_id=intent.strategy_id,
        symbol=intent.symbol,
        side=intent.side,
        decision_ts_ms=intent.decision_ts_ms,
        entry_eligible_ts_ms=intent.entry_eligible_ts_ms,
        entry_expires_ts_ms=intent.entry_expires_ts_ms,
        reference_price=intent.reference_price,
        signal_strength=intent.signal_strength,
        gross_reward_bps=intent.gross_reward_bps,
        exit_plan=ExitPlan(
            stop_price=intent.exit_plan.stop_price,
            target_price=intent.exit_plan.target_price,
            max_holding_ms=intent.exit_plan.max_holding_ms,
        ),
        metadata=intent.metadata,
    )


def _is_candidate_decision(candle: Candle) -> bool:
    requirements = get_runtime_requirements(STRATEGY_ID)
    closed_minute = (candle.close_time_ms + 1) // _MINUTE_MS
    return closed_minute % requirements.decision_interval_bars == requirements.decision_phase_bars


def _rolling_intents(
    bars: Sequence[Any],
    *,
    watermark_ms: int,
    include_base: bool,
) -> tuple[list[SleeveIntent], list[SleeveIntent]]:
    """Regenerate the same bounded runtime window at every frozen decision."""

    candidate: list[SleeveIntent] = []
    base: list[SleeveIntent] = []
    base_config = RightTailTrendConfig()
    for index, bar in enumerate(bars):
        candle = closed_bar_to_candle(bar)
        if candle.close_time_ms >= watermark_ms:
            break
        if candle.close_time_ms < BLIND_START_MS or not _is_candidate_decision(candle):
            continue
        window_start = index + 1 - OBSERVATION_WINDOW_BARS
        if window_start < 0:
            continue
        window_bars = bars[window_start : index + 1]
        strict_current = [
            item
            for item in generate_runtime_strategy_intents(STRATEGY_ID, window_bars)
            if item.decision_ts_ms == candle.close_time_ms
        ]
        if len(strict_current) > 1:
            raise ForwardIntegrityError("candidate emitted multiple intents at one frozen decision")
        candidate.extend(_strict_to_raw(item) for item in strict_current)
        if include_base:
            window_candles = [closed_bar_to_candle(item) for item in window_bars]
            base_current = [
                item
                for item in generate_right_tail_trend_intents(window_candles, base_config)
                if item.decision_ts_ms == candle.close_time_ms
            ]
            if len(base_current) > 1:
                raise ForwardIntegrityError("base emitted multiple intents at one frozen decision")
            base.extend(base_current)
    return candidate, base


def _verify_candidate_inventory(
    generated: Sequence[SleeveIntent],
    stored: Sequence[StrategyIntentV1],
) -> None:
    reconstructed = tuple(_strict_to_raw(item) for item in stored)
    generated_identity = tuple((item.decision_ts_ms, item.intent_id) for item in generated)
    stored_identity = tuple((item.decision_ts_ms, item.intent_id) for item in reconstructed)
    if generated_identity != stored_identity:
        raise ForwardIntegrityError("stored candidate inventory differs from frozen runtime regeneration")


def _evaluate_symbol(
    rows: list[Candle],
    intents: list[SleeveIntent],
    *,
    sleeve_id: str,
    symbol: str,
    dataset_sha256: str,
    config_sha256: str,
) -> dict[str, ManagedCellResult]:
    cells: dict[str, ManagedCellResult] = {}
    for scenario_name, execution in _execution_scenarios().items():
        cells[scenario_name] = evaluate_sleeve_cell(
            rows,
            intents,
            cell_id=f"forward:{scenario_name}:{sleeve_id}:{symbol}",
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
                dataset_sha256,
                scenario_name,
                sleeve_id,
                symbol,
                config_sha256,
            ),
        )
    return cells


def _evaluate_campaign(
    ledger: ForwardLedger,
    watermark_ms: int,
    dataset_sha256: str,
    include_base: bool,
) -> tuple[dict[str, dict[str, object]], dict[str, dict[str, object]] | None]:
    candidate_cells: dict[str, list[ManagedCellResult]] = {name: [] for name in _execution_scenarios()}
    base_cells: dict[str, list[ManagedCellResult]] = {name: [] for name in _execution_scenarios()}
    base_config_sha256 = RightTailTrendConfig().fingerprint
    for symbol in SYMBOLS:
        bars = list(ledger.iter_bars(symbol, end_exclusive_ms=watermark_ms))
        blind_rows = [closed_bar_to_candle(bar) for bar in bars if bar.open_time_ms >= BLIND_START_MS]
        if not blind_rows or blind_rows[-1].close_time_ms != watermark_ms - 1:
            raise ForwardIntegrityError(f"{symbol} does not have a complete sealed blind slice")
        candidate_intents, base_intents = _rolling_intents(
            bars,
            watermark_ms=watermark_ms,
            include_base=include_base,
        )
        _verify_candidate_inventory(
            candidate_intents,
            ledger.intents_before(symbol, watermark_ms),
        )
        evaluated_candidate = _evaluate_symbol(
            blind_rows,
            candidate_intents,
            sleeve_id=STRATEGY_ID,
            symbol=symbol,
            dataset_sha256=dataset_sha256,
            config_sha256=STRATEGY_CONFIG_SHA256,
        )
        for scenario_name, cell in evaluated_candidate.items():
            candidate_cells[scenario_name].append(cell)
        if include_base:
            evaluated_base = _evaluate_symbol(
                blind_rows,
                base_intents,
                sleeve_id=BASE_STRATEGY_ID,
                symbol=symbol,
                dataset_sha256=dataset_sha256,
                config_sha256=base_config_sha256,
            )
            for scenario_name, cell in evaluated_base.items():
                base_cells[scenario_name].append(cell)
    candidate = {name: _summarize_cells(tuple(candidate_cells[name])) for name in _execution_scenarios()}
    base = (
        {name: _summarize_cells(tuple(base_cells[name])) for name in _execution_scenarios()}
        if include_base
        else None
    )
    return candidate, base


def _integer(metrics: Mapping[str, object], name: str) -> int:
    value = metrics[name]
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{name} metric must be an integer")
    return value


def _number(metrics: Mapping[str, object], name: str) -> float:
    value = metrics[name]
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"{name} metric must be numeric")
    return float(value)


def _trade_counts(candidate: Mapping[str, Mapping[str, object]]) -> dict[str, int]:
    if set(candidate) != set(_execution_scenarios()):
        raise TypeError("candidate evaluation does not cover the frozen scenarios")
    return {name: _integer(candidate[name], "trades") for name in sorted(candidate)}


def _absolute_gate_failures(candidate: Mapping[str, Mapping[str, object]]) -> list[str]:
    failures: list[str] = []
    baseline_trades = _integer(candidate["baseline"], "trades")
    stress_trades = _integer(candidate["stress"], "trades")
    if baseline_trades and stress_trades / baseline_trades < MINIMUM_STRESS_TRADE_RETENTION:
        failures.append("stress_trade_retention_below_minimum")
    for scenario_name in ("baseline", "stress"):
        metrics = candidate[scenario_name]
        prefix = f"candidate.{scenario_name}"
        if _integer(metrics, "trades") < MINIMUM_FORWARD_TRADES:
            failures.append(f"{prefix}.trades_below_minimum")
        if _integer(metrics, "active_symbols") < MINIMUM_ACTIVE_SYMBOLS:
            failures.append(f"{prefix}.active_symbols_below_minimum")
        if _number(metrics, "total_return") <= 0:
            failures.append(f"{prefix}.total_return_not_positive")
        if _number(metrics, "expectancy_usd_per_trade") <= 0:
            failures.append(f"{prefix}.expectancy_not_positive")
        if _number(metrics, "hac_sharpe") <= 0:
            failures.append(f"{prefix}.hac_sharpe_not_positive")
        factor = metrics["profit_factor"]
        if factor is None or _number(metrics, "profit_factor") <= MINIMUM_PROFIT_FACTOR:
            failures.append(f"{prefix}.profit_factor_below_minimum")
        if _number(metrics, "maximum_drawdown") > MAXIMUM_DRAWDOWN:
            failures.append(f"{prefix}.drawdown_above_maximum")
        if _integer(metrics, "positive_expectancy_symbols") < MINIMUM_POSITIVE_EXPECTANCY_SYMBOLS:
            failures.append(f"{prefix}.positive_expectancy_symbols_below_minimum")
        if _number(metrics, "maximum_one_symbol_trade_share") > MAXIMUM_ONE_SYMBOL_TRADE_SHARE:
            failures.append(f"{prefix}.one_symbol_trade_share_above_maximum")
        directions = metrics["direction_trades"]
        if not isinstance(directions, dict):
            raise TypeError("direction_trades must be a mapping")
        for side in ("LONG", "SHORT"):
            if _integer(directions, side) < MINIMUM_DIRECTION_TRADES:
                failures.append(f"{prefix}.{side.lower()}_trades_below_minimum")
        per_symbol = metrics["per_symbol"]
        if not isinstance(per_symbol, list) or any(not isinstance(item, dict) for item in per_symbol):
            raise TypeError("per_symbol must be a list of mappings")
        for item in per_symbol:
            if _integer(item, "trades") < MINIMUM_TRADES_PER_SYMBOL:
                failures.append(f"{prefix}.{item['symbol']}.trades_below_minimum")
    return failures


def gate_failures(
    candidate: Mapping[str, Mapping[str, object]],
    base: Mapping[str, Mapping[str, object]],
) -> tuple[str, ...]:
    """Apply only the preregistered absolute and base-relative forward gates."""

    failures = _absolute_gate_failures(candidate)
    candidate_stress = candidate["stress"]
    base_stress = base["stress"]
    candidate_trades = _integer(candidate_stress, "trades")
    base_trades = _integer(base_stress, "trades")
    if not base_trades or candidate_trades / base_trades < MINIMUM_BASE_TRADE_RETENTION:
        failures.append("candidate.stress.base_trade_retention_below_minimum")
    candidate_factor = candidate_stress["profit_factor"]
    base_factor = base_stress["profit_factor"]
    if (
        candidate_factor is None
        or base_factor is None
        or _number(candidate_stress, "profit_factor") <= _number(base_stress, "profit_factor")
    ):
        failures.append("candidate.stress.profit_factor_not_above_base")
    if _number(candidate_stress, "maximum_drawdown") > _number(base_stress, "maximum_drawdown"):
        failures.append("candidate.stress.drawdown_above_base")
    return tuple(failures)


def _eligibility_without_metrics(ledger: ForwardLedger) -> dict[str, object]:
    status = ledger.status()
    return {
        "blind_performance_disclosed": False,
        "campaign_id": ledger.plan_sha256,
        "complete_blind_days": status["complete_blind_days"],
        "duration_gate_satisfied": status["duration_gate_satisfied"],
        "minimum_forward_days": MINIMUM_FORWARD_DAYS,
        "minimum_forward_trades_per_scenario": MINIMUM_FORWARD_TRADES,
        "scenario_closed_trades": None,
        "trade_count_gate_satisfied": False,
        "trade_count_evaluated": False,
        "watermark_ms": status["watermark_ms"],
    }


def evaluate_eligibility(
    ledger: ForwardLedger,
    *,
    campaign_evaluator: CampaignEvaluator = _evaluate_campaign,
) -> dict[str, object]:
    """Return duration and, only after it passes, closed counts without performance."""

    ledger.verify_integrity()
    status = _eligibility_without_metrics(ledger)
    if not status["duration_gate_satisfied"]:
        return status
    watermark_ms = status["watermark_ms"]
    if not isinstance(watermark_ms, int) or watermark_ms < MINIMUM_END_MS:
        raise ForwardIntegrityError("duration gate has no valid common watermark")
    dataset_sha256 = ledger.sealed_dataset_sha256(watermark_ms)
    candidate, base = campaign_evaluator(ledger, watermark_ms, dataset_sha256, False)
    if base is not None:
        raise RuntimeError("performance-blind preflight unexpectedly evaluated the base benchmark")
    counts = _trade_counts(candidate)
    status.update(
        {
            "scenario_closed_trades": counts,
            "trade_count_evaluated": True,
            "trade_count_gate_satisfied": all(count >= MINIMUM_FORWARD_TRADES for count in counts.values()),
        }
    )
    return status


def _consume_attempt(
    *,
    ledger: ForwardLedger,
    watermark_ms: int,
    dataset_sha256: str,
    evaluator_lock: Mapping[str, object],
    attempt_path: Path,
    now: Callable[[], datetime],
) -> dict[str, object]:
    payload: dict[str, object] = {
        "campaign_id": ledger.plan_sha256,
        "consumed_at": now().astimezone(UTC).isoformat().replace("+00:00", "Z"),
        "consumption_point": "after_duration_and_blind_trade_counts_before_final_metrics",
        "crash_or_failure_releases_attempt": False,
        "dataset_sha256": dataset_sha256,
        "evaluator_lock_sha256": evaluator_lock_sha256(evaluator_lock),
        "rerun_allowed": False,
        "schema_version": SCHEMA_VERSION,
        "status": "consumed",
        "strategy_id": STRATEGY_ID,
        "watermark_ms": watermark_ms,
    }
    payload["attempt_sha256"] = _sha256(_canonical_document(payload))
    _atomic_write(attempt_path, payload)
    return payload


def evaluate_forward_ledger(
    ledger: ForwardLedger,
    *,
    evaluator_lock: Mapping[str, object],
    attempt_path: Path,
    result_path: Path,
    campaign_evaluator: CampaignEvaluator = _evaluate_campaign,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> dict[str, object]:
    """Consume and execute the final attempt, or leave it untouched if ineligible."""

    existing = tuple(path for path in (attempt_path, result_path) if path.exists())
    if existing:
        raise FileExistsError(
            "one-shot forward evaluation is unavailable; existing output: "
            + ", ".join(str(path) for path in existing)
        )
    if ledger.plan_sha256 != evaluator_lock.get("plan_sha256"):
        raise ValueError("evaluator lock and ledger campaign differ")
    eligibility = evaluate_eligibility(ledger, campaign_evaluator=campaign_evaluator)
    if not eligibility["duration_gate_satisfied"]:
        raise ForwardNotEligibleError("minimum blind duration has not elapsed")
    if not eligibility["trade_count_gate_satisfied"]:
        raise ForwardNotEligibleError("both execution scenarios require 500 closed trades")
    watermark_ms = eligibility["watermark_ms"]
    if not isinstance(watermark_ms, int):
        raise ForwardIntegrityError("eligible campaign has no common watermark")
    dataset_sha256 = ledger.sealed_dataset_sha256(watermark_ms)
    attempt = _consume_attempt(
        ledger=ledger,
        watermark_ms=watermark_ms,
        dataset_sha256=dataset_sha256,
        evaluator_lock=evaluator_lock,
        attempt_path=attempt_path,
        now=now,
    )
    candidate, base = campaign_evaluator(ledger, watermark_ms, dataset_sha256, True)
    if base is None:
        raise RuntimeError("final evaluation did not produce the frozen base benchmark")
    failures = gate_failures(candidate, base)
    result: dict[str, object] = {
        "attempt": attempt,
        "base_benchmark": {"strategy_id": BASE_STRATEGY_ID, "scenarios": base},
        "campaign_id": ledger.plan_sha256,
        "candidate": {"strategy_id": STRATEGY_ID, "scenarios": candidate},
        "classification": (
            "ALPHA_CANDIDATE_REQUIRES_SEPARATE_PAPER_APPROVAL" if not failures else "REJECT_FORWARD_EVIDENCE"
        ),
        "dataset_sha256": dataset_sha256,
        "evaluator_lock_sha256": evaluator_lock_sha256(evaluator_lock),
        "gate_failures": list(failures),
        "permissions": {
            "alpha_ready": False,
            "live_allowed": False,
            "paper_allowed": False,
            "promotion_eligible": False,
        },
        "result_schema_version": SCHEMA_VERSION,
        "watermark_ms": watermark_ms,
    }
    result["result_sha256"] = _sha256(_canonical_document(result))
    _atomic_write(result_path, result)
    return result


def _print_json(value: object) -> None:
    print(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True))


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-root", type=Path, default=Path("."))
    parser.add_argument("--plan", type=Path, default=Path(PLAN_FILENAME))
    parser.add_argument("--lock", type=Path, default=Path(LOCK_FILENAME))
    subparsers = parser.add_subparsers(dest="command", required=True)
    for command in ("eligibility", "evaluate"):
        child = subparsers.add_parser(command)
        child.add_argument("--ledger", type=Path, required=True)
        if command == "evaluate":
            child.add_argument("--attempt", type=Path, default=Path(ATTEMPT_FILENAME))
            child.add_argument("--result", type=Path, default=Path(RESULT_FILENAME))
    arguments = parser.parse_args(argv)
    project_root = arguments.project_root.resolve()
    plan = load_plan(arguments.plan)
    evaluator_lock = load_evaluator_lock(project_root, arguments.lock)
    with ForwardLedger(arguments.ledger, plan) as ledger:
        if arguments.command == "eligibility":
            _print_json(evaluate_eligibility(ledger))
        elif arguments.command == "evaluate":
            _print_json(
                evaluate_forward_ledger(
                    ledger,
                    evaluator_lock=evaluator_lock,
                    attempt_path=arguments.attempt,
                    result_path=arguments.result,
                )
            )
        else:  # pragma: no cover - argparse owns the command domain
            raise RuntimeError(f"unsupported command: {arguments.command}")


if __name__ == "__main__":  # pragma: no cover
    main()
