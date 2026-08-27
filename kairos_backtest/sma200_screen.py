"""One-shot external reproduction of the fixed four-hour SMA200 long/flat rule."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import subprocess  # nosec B404
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from statistics import mean, stdev

from kairos_strategy.allocation import TargetAllocation
from kairos_strategy.candles import Candle
from kairos_strategy.provenance import installed_source_tree_sha256
from kairos_strategy.registry import StrategyStatus, get_allocation_strategy
from kairos_strategy.sleeves import FourHourSma200Config, generate_four_hour_sma200_allocations
from kairos_strategy.timeframes import aggregate

from .data import ArchiveFieldProfile, BinanceArchiveLoader, DatasetManifest, month_starts
from .factor_data import FactorDataset, FundingObservation, load_factor_cache
from .provenance import runtime_provenance, source_fingerprint
from .quarter_hour_screen import _atomic_write, _json_bytes, _json_value, _sha256
from .research_protocol import DataRole, DataWindow, ResearchProtocol

SCHEMA_VERSION = "kairos.four-hour-sma200-external-reproduction.v1"
PLAN_FILENAME = "reports/sma200-screen/plan.json"
ATTEMPT_FILENAME = "reports/sma200-screen/attempt.json"
RESULT_FILENAME = "reports/sma200-screen/summary.json"
PREFLIGHT_FILENAME = "reports/data-field-preflight/result-v2.json"
STRATEGY_ID = "four_hour_sma200_long_v1"
STRATEGY_COMMIT = "c7a9c7e296e3e6ad530706320d503b7e989d2da6"
SOURCE_COMMIT = "5acae6b7a4ff53bacb47a348233060f6a7090b24"
SOURCE_URL = f"https://github.com/iolufemi/crypto-trend-research/tree/{SOURCE_COMMIT}"
SOURCE_DATA_END_EXCLUSIVE = date(2026, 4, 1)
PREFLIGHT_RESULT_SHA256 = "908ba2b469bb5c2811e4763d07c34bde9e97fda4b64d5e277496af637400ea62"
PREFLIGHT_FILE_SHA256 = "6dc49bbef93ce2c3cbd6091a49925a952410ce526d3c698efc604db3a0ad3c28"
FACTOR_INVENTORY_SHA256 = "b9e4c038adfd2ec70fadcdbce46e6ed040cb8e2e855b0335c7bb6552140ad2cc"
SYMBOL = "BTCUSDT"
WARMUP_DAYS = 365
LINEAGE_TRIAL_NUMBER = 14
BASE_SEED = 61
MINIMUM_TOTAL_RETURN = 0.0
MINIMUM_PROFIT_FACTOR = 1.05
MINIMUM_ANNUALIZED_SHARPE = 0.50
MAXIMUM_DRAWDOWN = 0.25
MINIMUM_ACTIVE_DAYS = 60
MINIMUM_ALLOCATION_CHANGES = 8
SOURCE_UNSEEN_MINIMUM_TOTAL_RETURN = -0.05
SOURCE_UNSEEN_MAXIMUM_DRAWDOWN = 0.15
SOURCE_UNSEEN_MINIMUM_ALLOCATION_CHANGES = 1
_FOUR_HOURS_MS = 4 * 60 * 60 * 1_000
_DAY_MS = 24 * 60 * 60 * 1_000

WINDOWS = (
    DataWindow("selection", date(2024, 7, 1), date(2025, 7, 1), DataRole.SELECTION),
    DataWindow("robustness", date(2025, 7, 1), date(2026, 8, 1), DataRole.ROBUSTNESS),
)
SOURCE_UNSEEN_START = date(2026, 4, 1)

PROTOCOL = ResearchProtocol(
    protocol_name="four-hour-sma200-external-reproduction-v1",
    universe=(SYMBOL,),
    windows=WINDOWS,
    max_trials=1,
    maximum_holding_ms=(WINDOWS[-1].end - WINDOWS[0].start).days * _DAY_MS,
    maximum_label_horizon_ms=(WINDOWS[-1].end - WINDOWS[0].start).days * _DAY_MS,
    maximum_execution_latency_ms=0,
    warmup_ms=WARMUP_DAYS * _DAY_MS,
)


@dataclass(frozen=True, slots=True)
class CostScenario:
    transaction_cost_bps: float
    use_actual_funding: bool
    additional_funding_per_settlement: float


SCENARIOS = {
    "published_spot": CostScenario(10.0, False, 0.0),
    "futures_baseline": CostScenario(10.0, True, 0.0),
    "futures_stress": CostScenario(25.0, True, 0.0005),
}


@dataclass(frozen=True, slots=True)
class FourHourReturn:
    open_time_ms: int
    net_return: float
    gross_return: float
    actual_funding_return: float
    adverse_funding_return: float
    transaction_cost_return: float
    target_weight: float
    turnover: float
    funding_settlements: int


def expected_plan() -> dict[str, object]:
    config = FourHourSma200Config()
    return {
        "classification": "external_open_code_reproduction_reused_data_only",
        "data": {
            "field_profile": ArchiveFieldProfile.PRICE_ONLY.value,
            "funding_source": "official_binance_usdm_fundingRate",
            "preflight_file_sha256": PREFLIGHT_FILE_SHA256,
            "preflight_result_sha256": PREFLIGHT_RESULT_SHA256,
            "reused_data": True,
            "source_data_end_exclusive": SOURCE_DATA_END_EXCLUSIVE.isoformat(),
            "source_overlap_limitation": (
                "the external author observed data through 2026-03-31; only 2026-04-01 onward "
                "is source-unseen, and no interval is Kairos-blind"
            ),
            "source_unseen_subwindow": {
                "end_exclusive": WINDOWS[-1].end.isoformat(),
                "start": SOURCE_UNSEEN_START.isoformat(),
            },
            "symbol": SYMBOL,
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
            "maximum_drawdown": MAXIMUM_DRAWDOWN,
            "minimum_active_days": MINIMUM_ACTIVE_DAYS,
            "minimum_allocation_changes": MINIMUM_ALLOCATION_CHANGES,
            "minimum_annualized_sharpe": MINIMUM_ANNUALIZED_SHARPE,
            "minimum_profit_factor_strictly_greater_than": MINIMUM_PROFIT_FACTOR,
            "minimum_total_return_strictly_greater_than": MINIMUM_TOTAL_RETURN,
            "must_beat_buy_and_hold_drawdown": True,
            "must_beat_buy_and_hold_sharpe": True,
            "required_scenarios": list(SCENARIOS),
            "required_windows": [window.name for window in WINDOWS],
            "source_unseen": {
                "maximum_drawdown": SOURCE_UNSEEN_MAXIMUM_DRAWDOWN,
                "minimum_allocation_changes": SOURCE_UNSEEN_MINIMUM_ALLOCATION_CHANGES,
                "minimum_total_return_strictly_greater_than": SOURCE_UNSEEN_MINIMUM_TOTAL_RETURN,
            },
        },
        "forward_gate": {
            "blind_start_not_before": "2026-09-01",
            "minimum_days": 365,
            "minimum_allocation_changes": 25,
        },
        "hypothesis": {
            "action_timing": "signal_on_closed_4h_bar_target_effective_next_4h_bar",
            "economic_premise": "low_turnover_crash_avoidance_improves_btc_risk_adjusted_return",
            "entry_rule": "close_strictly_greater_than_simple_moving_average_200",
            "exit_rule": "close_less_than_or_equal_to_simple_moving_average_200",
            "external_parameter_search_disclosed": True,
            "kairos_parameter_search_allowed": False,
            "no_fixed_stop_target_or_timeout": True,
            "research_lineage_trial_number": LINEAGE_TRIAL_NUMBER,
            "source_commit": SOURCE_COMMIT,
            "source_url": SOURCE_URL,
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
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or _json_value(payload) != _json_value(expected_plan()):
        raise ValueError("committed SMA200 plan differs from the executable plan")
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
        raise RuntimeError("SMA200 screen refuses to open data from a dirty Git worktree")
    definition = get_allocation_strategy(STRATEGY_ID)
    if definition.status is not StrategyStatus.RESEARCH or definition.revision != "1":
        raise RuntimeError("SMA200 registration changed after preregistration")
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
        "classification": "external_open_code_reproduction_reused_data_only",
        "consumed_at": _now_utc(),
        "consumption_point": "after_preflight_receipt_before_first_price_or_funding_archive_access",
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
        raise RuntimeError("SMA200 attempt ledger changed after publication")
    return payload


def _validate_preflight_receipt(path: Path) -> dict[str, object]:
    raw = path.read_bytes()
    # Git may materialize the committed JSON with CRLF on Windows runners.  The
    # preregistered digest is for the repository's LF-normalized content; the
    # signed canonical JSON identity below remains the authoritative binding.
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
        or payload.get("classification") != "DATA_PREFLIGHT_PASSED"
    ):
        raise ValueError("data preflight receipt is invalid")
    evidence = payload.get("evidence")
    if not isinstance(evidence, list):
        raise ValueError("data preflight receipt has no evidence")
    btc = [
        item
        for item in evidence
        if isinstance(item, dict)
        and item.get("symbol") == SYMBOL
        and item.get("field_profile") == ArchiveFieldProfile.PRICE_ONLY.value
    ]
    if len(btc) != len(WINDOWS):
        raise ValueError("data preflight receipt does not qualify both BTC slices")
    return payload


def _utc_ms(day: date) -> int:
    return int(datetime.combine(day, datetime.min.time(), UTC).timestamp() * 1_000)


def _preflight_evidence(receipt: Mapping[str, object], purpose: str) -> Mapping[str, object]:
    evidence = receipt["evidence"]
    if not isinstance(evidence, list):
        raise TypeError("preflight evidence must be a list")
    matches = [
        item
        for item in evidence
        if isinstance(item, dict) and item.get("symbol") == SYMBOL and item.get("purpose") == purpose
    ]
    if len(matches) != 1:
        raise ValueError(f"preflight evidence is missing exact purpose {purpose}")
    return matches[0]


def _validate_manifest(
    manifest: DatasetManifest,
    candles: Sequence[Candle],
    *,
    start: date,
    end: date,
    evidence: Mapping[str, object],
) -> None:
    expected_rows = (end - start).days * 24 * 60
    expected_files = len(month_starts(start, end))
    checks = (
        manifest.symbol == SYMBOL,
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
        manifest.transport_verification == "zip_crc_and_profiled_rows_sha256",
        manifest.field_profile == ArchiveFieldProfile.PRICE_ONLY.value,
        manifest.sha256 == evidence.get("normalized_rows_sha256"),
        manifest.quarantined_optional_rows == evidence.get("quarantined_optional_rows"),
    )
    if not all(checks):
        raise ValueError(f"{SYMBOL} {start}..{end} differs from its price-only preflight evidence")


def _funding_by_four_hour(
    observations: Sequence[FundingObservation], start_ms: int, end_ms: int
) -> dict[int, tuple[float, int]]:
    result: dict[int, tuple[float, int]] = {}
    for item in observations:
        if start_ms <= item.timestamp_ms < end_ms:
            opened = item.timestamp_ms // _FOUR_HOURS_MS * _FOUR_HOURS_MS
            rate, count = result.get(opened, (0.0, 0))
            result[opened] = (rate + item.rate, count + 1)
    return result


def _replay(
    bars: Sequence[Candle],
    allocations: Sequence[TargetAllocation],
    funding: Mapping[int, tuple[float, int]],
    scenario: CostScenario,
    *,
    start_ms: int,
    end_ms: int,
    buy_and_hold: bool = False,
) -> tuple[FourHourReturn, ...]:
    targets = {item.effective_ts_ms: item.target_weight for item in allocations}
    current_weight = 0.0
    result: list[FourHourReturn] = []
    for previous, current in zip(bars, bars[1:], strict=False):
        if current.open_time_ms < start_ms:
            continue
        if current.open_time_ms >= end_ms:
            break
        if buy_and_hold:
            new_weight = 1.0
        else:
            if current.open_time_ms not in targets:
                raise ValueError(f"missing causal SMA200 target for bar {current.open_time_ms}")
            new_weight = targets[current.open_time_ms]
        turnover = abs(new_weight - current_weight)
        current_weight = new_weight
        asset_return = current.close / previous.close - 1
        gross = current_weight * asset_return
        actual_rate, settlements = funding.get(current.open_time_ms, (0.0, 0))
        actual_funding = current_weight * actual_rate if scenario.use_actual_funding else 0.0
        adverse_funding = current_weight * scenario.additional_funding_per_settlement * settlements
        transaction_cost = turnover * scenario.transaction_cost_bps / 10_000
        net = gross - actual_funding - adverse_funding - transaction_cost
        if not all(
            math.isfinite(value) for value in (gross, actual_funding, adverse_funding, transaction_cost, net)
        ):
            raise ValueError("non-finite SMA200 replay economics")
        result.append(
            FourHourReturn(
                current.open_time_ms,
                net,
                gross,
                actual_funding,
                adverse_funding,
                transaction_cost,
                current_weight,
                turnover,
                settlements,
            )
        )
    return tuple(result)


def _maximum_drawdown(equity: Sequence[float]) -> float:
    peak = equity[0]
    maximum = 0.0
    for value in equity:
        peak = max(peak, value)
        maximum = max(maximum, 1 - value / peak)
    return maximum


def _profit_factor(returns: Sequence[float]) -> float | None:
    profits = math.fsum(max(0.0, value) for value in returns)
    losses = -math.fsum(min(0.0, value) for value in returns)
    return profits / losses if losses > 0 else None


def _summarize(rows: Sequence[FourHourReturn]) -> dict[str, object]:
    if not rows:
        raise ValueError("SMA200 replay produced no evaluation rows")
    returns = [item.net_return for item in rows]
    equity = [1.0]
    for value in returns:
        if value <= -1:
            raise ValueError("SMA200 replay reached or crossed ruin")
        equity.append(equity[-1] * (1 + value))
    volatility = stdev(returns) if len(returns) > 1 else 0.0
    sharpe = mean(returns) / volatility * math.sqrt(6 * 365) if volatility > 0 else 0.0
    active_days = len({item.open_time_ms // _DAY_MS for item in rows if item.target_weight > 0})
    return {
        "active_days": active_days,
        "allocation_changes": sum(item.turnover > 0 for item in rows),
        "annualized_sharpe": sharpe,
        "average_target_weight": mean(item.target_weight for item in rows),
        "bars": len(rows),
        "funding_settlements_while_active": sum(
            item.funding_settlements for item in rows if item.target_weight > 0
        ),
        "gross_return_sum": math.fsum(item.gross_return for item in rows),
        "actual_funding_return": math.fsum(item.actual_funding_return for item in rows),
        "adverse_funding_return": math.fsum(item.adverse_funding_return for item in rows),
        "maximum_drawdown": _maximum_drawdown(equity),
        "profit_factor": _profit_factor(returns),
        "total_return": equity[-1] - 1,
        "transaction_cost_return": math.fsum(item.transaction_cost_return for item in rows),
        "turnover": math.fsum(item.turnover for item in rows),
    }


def _number(metrics: Mapping[str, object], name: str) -> float:
    value = metrics[name]
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise TypeError(f"{name} metric must be finite and numeric")
    return float(value)


def _integer(metrics: Mapping[str, object], name: str) -> int:
    value = metrics[name]
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{name} metric must be an integer")
    return value


def _gate_failures(windows: Mapping[str, Mapping[str, Mapping[str, object]]]) -> tuple[str, ...]:
    failures: list[str] = []
    for window_name in ("selection", "robustness"):
        for scenario_name in SCENARIOS:
            cell = windows[window_name][scenario_name]
            strategy = cell["strategy"]
            benchmark = cell["buy_and_hold"]
            if not isinstance(strategy, Mapping) or not isinstance(benchmark, Mapping):
                raise TypeError("screen cells require strategy and benchmark mappings")
            prefix = f"{window_name}.{scenario_name}"
            profit_factor = strategy["profit_factor"]
            checks = (
                (_number(strategy, "total_return") > MINIMUM_TOTAL_RETURN, "total_return_not_positive"),
                (
                    profit_factor is not None and float(profit_factor) > MINIMUM_PROFIT_FACTOR,
                    "profit_factor_below_minimum",
                ),
                (
                    _number(strategy, "annualized_sharpe") >= MINIMUM_ANNUALIZED_SHARPE,
                    "sharpe_below_minimum",
                ),
                (
                    _number(strategy, "maximum_drawdown") <= MAXIMUM_DRAWDOWN,
                    "drawdown_above_maximum",
                ),
                (
                    _integer(strategy, "active_days") >= MINIMUM_ACTIVE_DAYS,
                    "active_days_below_minimum",
                ),
                (
                    _integer(strategy, "allocation_changes") >= MINIMUM_ALLOCATION_CHANGES,
                    "allocation_changes_below_minimum",
                ),
                (
                    _number(strategy, "annualized_sharpe") > _number(benchmark, "annualized_sharpe"),
                    "does_not_beat_buy_and_hold_sharpe",
                ),
                (
                    _number(strategy, "maximum_drawdown") < _number(benchmark, "maximum_drawdown"),
                    "does_not_beat_buy_and_hold_drawdown",
                ),
            )
            failures.extend(f"{prefix}.{name}" for passed, name in checks if not passed)
    for scenario_name in SCENARIOS:
        cell = windows["source_unseen"][scenario_name]
        strategy = cell["strategy"]
        if not isinstance(strategy, Mapping):
            raise TypeError("source-unseen strategy metrics must be a mapping")
        prefix = f"source_unseen.{scenario_name}"
        source_checks = (
            (
                _number(strategy, "total_return") > SOURCE_UNSEEN_MINIMUM_TOTAL_RETURN,
                "total_return_below_floor",
            ),
            (
                _number(strategy, "maximum_drawdown") <= SOURCE_UNSEEN_MAXIMUM_DRAWDOWN,
                "drawdown_above_maximum",
            ),
            (
                _integer(strategy, "allocation_changes") >= SOURCE_UNSEEN_MINIMUM_ALLOCATION_CHANGES,
                "allocation_changes_below_minimum",
            ),
        )
        failures.extend(f"{prefix}.{name}" for passed, name in source_checks if not passed)
    return tuple(failures)


def _evaluate_window(
    candles: Sequence[Candle],
    factors: FactorDataset,
    window: DataWindow,
) -> dict[str, Mapping[str, object]]:
    allocations = generate_four_hour_sma200_allocations(list(candles), FourHourSma200Config())
    bars = aggregate(list(candles), "4h")
    start_ms = _utc_ms(window.start)
    end_ms = _utc_ms(window.end)
    funding = _funding_by_four_hour(factors.funding[SYMBOL], start_ms, end_ms)
    result: dict[str, Mapping[str, object]] = {}
    for name, scenario in SCENARIOS.items():
        strategy = _summarize(_replay(bars, allocations, funding, scenario, start_ms=start_ms, end_ms=end_ms))
        benchmark = _summarize(
            _replay(
                bars,
                allocations,
                funding,
                scenario,
                start_ms=start_ms,
                end_ms=end_ms,
                buy_and_hold=True,
            )
        )
        result[name] = {"buy_and_hold": benchmark, "strategy": strategy}
    return result


def run_sma200_screen(
    *,
    project_root: Path,
    price_cache: Path,
    factor_cache: Path,
    plan_path: Path,
    preflight_path: Path,
    attempt_path: Path,
    result_path: Path,
) -> dict[str, object]:
    if attempt_path.exists() or result_path.exists():
        raise FileExistsError("one-shot SMA200 attempt is unavailable")
    plan = load_preregistered_plan(plan_path)
    receipt = _validate_preflight_receipt(preflight_path)
    environment = _environment(project_root)
    attempt = _consume_attempt(plan, plan_path=plan_path, attempt_path=attempt_path)
    factors = load_factor_cache(factor_cache)
    if factors.inventory_sha256 != FACTOR_INVENTORY_SHA256:
        raise ValueError("factor inventory differs from preregistration")
    loader = BinanceArchiveLoader(
        price_cache,
        allow_download=False,
        field_profile=ArchiveFieldProfile.PRICE_ONLY,
    )
    loaded: dict[str, tuple[list[Candle], DatasetManifest]] = {}
    purposes = {
        "selection": "selection_with_365d_warmup",
        "robustness": "robustness_with_365d_warmup",
    }
    windows: dict[str, Mapping[str, Mapping[str, object]]] = {}
    datasets: list[dict[str, object]] = []
    for window in WINDOWS:
        load_start = window.start - timedelta(days=WARMUP_DAYS)
        candles, manifest = loader.load(SYMBOL, load_start, window.end)
        evidence = _preflight_evidence(receipt, purposes[window.name])
        _validate_manifest(manifest, candles, start=load_start, end=window.end, evidence=evidence)
        loaded[window.name] = (candles, manifest)
        datasets.append({"window": window.name, **asdict(manifest)})
        windows[window.name] = _evaluate_window(candles, factors, window)
    robustness_candles, _ = loaded["robustness"]
    source_unseen = DataWindow(
        "source_unseen",
        SOURCE_UNSEEN_START,
        WINDOWS[-1].end,
        DataRole.ROBUSTNESS,
    )
    windows["source_unseen"] = _evaluate_window(robustness_candles, factors, source_unseen)
    failures = _gate_failures(windows)
    summary: dict[str, object] = {
        "attempt": attempt,
        "classification": "FORWARD_FREEZE_CANDIDATE" if not failures else "REJECT_REUSED_DATA_SCREEN",
        "data_quality": {
            "factor_audit": asdict(next(item for item in factors.audits if item.symbol == SYMBOL)),
            "factor_inventory_sha256": factors.inventory_sha256,
            "preflight_file_sha256": PREFLIGHT_FILE_SHA256,
            "preflight_result_sha256": PREFLIGHT_RESULT_SHA256,
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
    parser = argparse.ArgumentParser(description="Run the preregistered external SMA200 reproduction")
    parser.add_argument("--price-cache", type=Path, default=Path("data/historical"))
    parser.add_argument("--factor-cache", type=Path, default=Path("data/historical-factors"))
    parser.add_argument("--plan", type=Path, default=Path(PLAN_FILENAME))
    parser.add_argument("--preflight", type=Path, default=Path(PREFLIGHT_FILENAME))
    parser.add_argument("--attempt", type=Path, default=Path(ATTEMPT_FILENAME))
    parser.add_argument("--result", type=Path, default=Path(RESULT_FILENAME))
    parser.add_argument("--verify-plan", action="store_true")
    arguments = parser.parse_args()
    plan = load_preregistered_plan(arguments.plan)
    if arguments.verify_plan:
        _validate_preflight_receipt(arguments.preflight)
        print(f"plan_sha256={_sha256(plan)}")
        print(f"preflight_result_sha256={PREFLIGHT_RESULT_SHA256}")
        return 0
    result = run_sma200_screen(
        project_root=Path(__file__).resolve().parents[1],
        price_cache=arguments.price_cache,
        factor_cache=arguments.factor_cache,
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
