"""Frozen, offline strategy validation campaign used before any real API test."""

from __future__ import annotations

import argparse
import hashlib
import json
import tomllib
from bisect import bisect_left
from dataclasses import asdict
from datetime import UTC, date, datetime
from importlib.metadata import PackageNotFoundError, distribution
from pathlib import Path
from statistics import fmean
from typing import cast

from .data import BinanceArchiveLoader, audit_cached_archives, month_starts
from .evaluation import EvaluationResult, aggregate_results
from .harness import (
    EvaluationScenario,
    ScenarioResult,
    evaluate_sensitivity,
    evaluate_walk_forward,
    evaluate_window_sensitivity,
)
from .provenance import runtime_manifest, source_fingerprint
from .readiness import PromotionReadiness, evaluate_promotion
from .scenarios import BASELINE, STRESS, SYMBOLS
from .seeding import derive_seed
from .strategy import StrategyConfig, generate_signals

RESEARCH_CACHE_START = date(2021, 7, 1)
TRAIN_START = date(2025, 7, 1)
TRAIN_END = date(2026, 7, 1)
HOLDOUT_WARMUP_START = date(2026, 5, 1)
HOLDOUT_START = date(2026, 7, 1)
HOLDOUT_END = date(2026, 8, 1)
FROZEN_STRATEGY = StrategyConfig(
    confirmation_bars=12,
    minimum_hold_bars=48,
    minimum_confidence=0.67,
)
FROZEN_QUANT_SHA = "c74b9853bd97597b2104b2d9c4bcd5b7c6cefb24"
RUNTIME_QUANT_SHA = "8474a25bd0afa58f4182ea69aaa0af71c7a01643"
FROZEN_QUANT_URL = "https://github.com/Kairos-cryptoAI/kairos-quant-scouts.git"
INITIAL_EQUITY = 10_000.0
ALLOCATION = 0.25
WALK_FORWARD_TRAIN_ROWS = 180 * 24 * 60
WALK_FORWARD_PURGE_ROWS = 24 * 60
WALK_FORWARD_TEST_ROWS = 60 * 24 * 60


def _scenario_result(row: ScenarioResult) -> dict[str, object]:
    return {"scenario": row.scenario, **row.result.to_dict()}


def _summary(results: list[EvaluationResult]) -> dict[str, object]:
    """Equal-weight symbol summary; drawdown remains a worst-symbol diagnostic."""
    if not results:
        raise ValueError("summary requires at least one result")
    return {
        "symbols": len(results),
        "equal_weight_mean_return_pct": fmean(result.return_pct for result in results),
        "equal_weight_mean_benchmark_return_pct": fmean(result.benchmark_return_pct for result in results),
        "positive_symbols": sum(result.return_pct > 0 for result in results),
        "benchmark_outperforming_symbols": sum(
            result.return_pct > result.benchmark_return_pct for result in results
        ),
        "worst_symbol_drawdown": max(result.metrics.max_drawdown for result in results),
        "trades": sum(result.trades for result in results),
        "fees_usd_across_independent_symbol_accounts": sum(result.fees_usd for result in results),
        "funding_usd_across_independent_symbol_accounts": sum(result.funding_usd for result in results),
        "portfolio_sharpe_or_drawdown_available": False,
    }


def _readiness_dict(readiness: PromotionReadiness) -> dict[str, object]:
    return {
        "status": readiness.status,
        "real_api_allowed": readiness.real_api_allowed,
        "reasons": list(readiness.reasons),
    }


def _validate_installed_quant_direct_url(
    raw: str | None,
    *,
    expected_sha: str = FROZEN_QUANT_SHA,
) -> dict[str, str]:
    if raw is None:
        raise RuntimeError("installed kairos-quant-scouts is missing direct_url.json")
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError("installed kairos-quant-scouts has malformed direct_url.json") from exc
    if not isinstance(payload, dict):
        raise RuntimeError("installed kairos-quant-scouts has malformed direct_url.json")
    vcs_info = payload.get("vcs_info")
    if not isinstance(vcs_info, dict):
        raise RuntimeError("installed kairos-quant-scouts has no VCS provenance")
    provenance = {
        "url": payload.get("url"),
        "vcs": vcs_info.get("vcs"),
        "commit_id": vcs_info.get("commit_id"),
        "requested_revision": vcs_info.get("requested_revision"),
    }
    expected = {
        "url": FROZEN_QUANT_URL,
        "vcs": "git",
        "commit_id": expected_sha,
        "requested_revision": expected_sha,
    }
    if provenance != expected:
        raise RuntimeError("installed kairos-quant-scouts does not match the frozen URL and commit")
    return cast(dict[str, str], provenance)


def _assert_quant_dependency(project_root: Path, *, expected_sha: str) -> dict[str, object]:
    project = tomllib.loads((project_root / "pyproject.toml").read_text(encoding="utf-8"))
    quant_source = cast(dict[str, object], project["tool"]["uv"]["sources"])["kairos-quant-scouts"]
    if (
        not isinstance(quant_source, dict)
        or quant_source.get("git") != FROZEN_QUANT_URL
        or quant_source.get("rev") != expected_sha
    ):
        raise RuntimeError("kairos-quant-scouts is not pinned to the expected commit")

    lock = tomllib.loads((project_root / "uv.lock").read_text(encoding="utf-8"))
    packages = lock.get("package")
    if not isinstance(packages, list):
        raise RuntimeError("uv.lock has no package inventory")
    quant_packages = [
        package
        for package in packages
        if isinstance(package, dict) and package.get("name") == "kairos-quant-scouts"
    ]
    expected_lock_source = f"{FROZEN_QUANT_URL}?rev={expected_sha}#{expected_sha}"
    if len(quant_packages) != 1:
        raise RuntimeError("uv.lock must contain exactly one kairos-quant-scouts distribution")
    lock_source = quant_packages[0].get("source")
    if not isinstance(lock_source, dict) or lock_source.get("git") != expected_lock_source:
        raise RuntimeError("uv.lock does not resolve kairos-quant-scouts to the frozen commit")

    try:
        installed = distribution("kairos-quant-scouts")
    except PackageNotFoundError as exc:
        raise RuntimeError("kairos-quant-scouts is not installed") from exc
    installed_provenance = _validate_installed_quant_direct_url(
        installed.read_text("direct_url.json"),
        expected_sha=expected_sha,
    )
    return {
        "distribution": "kairos-quant-scouts",
        "version": installed.version,
        "declared_url": FROZEN_QUANT_URL,
        "declared_revision": expected_sha,
        "locked_source": expected_lock_source,
        "installed_direct_url": installed_provenance,
    }


def _assert_frozen_dependency(project_root: Path) -> dict[str, object]:
    """Require the immutable dependency used by the archived legacy campaign."""

    return _assert_quant_dependency(project_root, expected_sha=FROZEN_QUANT_SHA)


def _assert_runtime_dependency(project_root: Path) -> dict[str, object]:
    """Require the dependency used by the current strategy-parity runtime."""

    return _assert_quant_dependency(project_root, expected_sha=RUNTIME_QUANT_SHA)


def _cache_snapshot(
    cache_dir: Path,
    start: date,
    end: date,
    symbols: tuple[str, ...] = SYMBOLS,
) -> str:
    """Hash every frozen archive and checksum sidecar without parsing rows."""
    digest = hashlib.sha256()
    for symbol in symbols:
        for month in month_starts(start, end):
            archive = cache_dir / symbol / "1m" / f"{symbol}-1m-{month:%Y-%m}.zip"
            for path in (archive, archive.with_name(f"{archive.name}.CHECKSUM")):
                if not path.is_file():
                    raise FileNotFoundError(f"missing frozen cache artifact: {path}")
                digest.update(path.relative_to(cache_dir).as_posix().encode("utf-8"))
                digest.update(b"\0")
                with path.open("rb") as handle:
                    while chunk := handle.read(1024 * 1024):
                        digest.update(chunk)
                digest.update(b"\0")
    return digest.hexdigest()


def run_frozen_validation(
    cache_dir: Path,
    output_path: Path,
    *,
    seed: int = 42,
) -> dict[str, object]:
    """Run 12m diagnostics plus a July holdout without network access."""
    project_root = Path(__file__).resolve().parent.parent
    source_sha256 = source_fingerprint()
    dependency_provenance = _assert_frozen_dependency(project_root)
    cache_snapshots = {
        "research_pre_holdout": _cache_snapshot(cache_dir, RESEARCH_CACHE_START, TRAIN_END),
        "untouched_holdout": _cache_snapshot(cache_dir, HOLDOUT_START, HOLDOUT_END),
    }
    loader = BinanceArchiveLoader(cache_dir, allow_download=False)
    scenarios = (
        EvaluationScenario("baseline", BASELINE, ALLOCATION),
        EvaluationScenario("stress", STRESS, ALLOCATION),
    )

    # Audit all 300 pre-freeze archives and the five untouched holdout archives.
    research_audit = audit_cached_archives(
        cache_dir,
        SYMBOLS,
        RESEARCH_CACHE_START,
        TRAIN_END,
    )
    holdout_audit = audit_cached_archives(
        cache_dir,
        SYMBOLS,
        HOLDOUT_START,
        HOLDOUT_END,
    )

    symbol_reports: list[dict[str, object]] = []
    train_by_scenario: dict[str, list[EvaluationResult]] = {scenario.name: [] for scenario in scenarios}
    holdout_by_scenario: dict[str, list[EvaluationResult]] = {scenario.name: [] for scenario in scenarios}
    readiness_rows: list[PromotionReadiness] = []

    for symbol in SYMBOLS:
        train_candles, train_manifest = loader.load(symbol, TRAIN_START, TRAIN_END)
        train_signals = generate_signals(train_candles, FROZEN_STRATEGY)
        train_seed = derive_seed(seed, "frozen-validation", symbol, "train-12m")
        training = evaluate_sensitivity(
            train_candles,
            train_signals,
            scenarios,
            initial_equity=INITIAL_EQUITY,
            seed=train_seed,
            allow_incomplete_terminal=True,
        )
        for result in training:
            train_by_scenario[result.scenario].append(result.result)

        walk_forward: dict[str, object] = {}
        for scenario in scenarios:
            folds = evaluate_walk_forward(
                train_candles,
                train_signals,
                train_size=WALK_FORWARD_TRAIN_ROWS,
                test_size=WALK_FORWARD_TEST_ROWS,
                purge_size=WALK_FORWARD_PURGE_ROWS,
                execution=scenario.execution,
                initial_equity=INITIAL_EQUITY,
                allocation=scenario.allocation,
                seed=derive_seed(seed, "frozen-validation", symbol, "walk-forward", scenario.name),
                allow_incomplete_terminal=True,
            )
            fold_results = tuple(fold.result for fold in folds)
            walk_forward[scenario.name] = {
                "folds": [
                    {
                        "fold": asdict(fold.fold),
                        "train_start_ms": train_candles[fold.fold.train_start].open_time_ms,
                        "train_end_ms_exclusive": train_candles[fold.fold.train_end].open_time_ms,
                        "test_start_ms": train_candles[fold.fold.test_start].open_time_ms,
                        "test_end_ms_exclusive": train_candles[fold.fold.test_end].open_time_ms,
                        "result": fold.result.to_dict(),
                    }
                    for fold in folds
                ],
                "compounded_post_selection_temporal_folds": aggregate_results(
                    list(fold_results),
                    initial_equity=INITIAL_EQUITY,
                ).to_dict(),
            }

        holdout_candles, holdout_warmup_manifest = loader.load(
            symbol,
            HOLDOUT_WARMUP_START,
            HOLDOUT_END,
        )
        holdout_signals = generate_signals(holdout_candles, FROZEN_STRATEGY)
        open_times = [candle.open_time_ms for candle in holdout_candles]
        holdout_start_ms = int(datetime.combine(HOLDOUT_START, datetime.min.time(), UTC).timestamp() * 1000)
        holdout_end_ms = int(datetime.combine(HOLDOUT_END, datetime.min.time(), UTC).timestamp() * 1000)
        start_index = bisect_left(open_times, holdout_start_ms)
        end_index = bisect_left(open_times, holdout_end_ms)
        if (
            start_index >= len(holdout_candles)
            or holdout_candles[start_index].open_time_ms != holdout_start_ms
            or end_index != len(holdout_candles)
        ):
            raise ValueError(f"holdout boundaries are incomplete for {symbol}")
        holdout = evaluate_window_sensitivity(
            holdout_candles,
            holdout_signals,
            scenarios,
            start_index=start_index,
            end_index=end_index,
            initial_equity=INITIAL_EQUITY,
            seed=derive_seed(seed, "frozen-validation", symbol, "untouched-2026-07"),
            allow_incomplete_terminal=True,
        )
        for result in holdout:
            holdout_by_scenario[result.scenario].append(result.result)

        baseline_holdout = next(result.result for result in holdout if result.scenario == "baseline")
        readiness = evaluate_promotion(
            (baseline_holdout,),
            tuple(result.result for result in holdout),
            data_audits=(research_audit, holdout_audit),
        )
        readiness_rows.append(readiness)
        baseline_result = next(result.result for result in holdout if result.scenario == "baseline")
        stress_result = next(result.result for result in holdout if result.scenario == "stress")
        symbol_reports.append(
            {
                "symbol": symbol,
                "strategy_signal_counts": {
                    "train_12m": len(train_signals),
                    "warmup_plus_holdout": len(holdout_signals),
                },
                "datasets": {
                    "train_12m": asdict(train_manifest),
                    "holdout_with_warmup": asdict(holdout_warmup_manifest),
                },
                "train_12m_sensitivity": [_scenario_result(result) for result in training],
                "walk_forward": walk_forward,
                "holdout_2026_07": [_scenario_result(result) for result in holdout],
                "holdout_cost_sensitivity_delta_pct": (stress_result.return_pct - baseline_result.return_pct),
                "readiness": _readiness_dict(readiness),
            }
        )

    reasons = tuple(dict.fromkeys(reason for readiness in readiness_rows for reason in readiness.reasons))
    overall_readiness = {
        "status": "ready" if all(item.real_api_allowed for item in readiness_rows) else "needs_revision",
        "real_api_allowed": all(item.real_api_allowed for item in readiness_rows),
        "reasons": list(reasons),
        "required_scope": "every symbol must pass; no averaging away a failed symbol",
    }
    if source_fingerprint() != source_sha256:
        raise RuntimeError("source tree changed during frozen validation")
    final_cache_snapshots = {
        "research_pre_holdout": _cache_snapshot(cache_dir, RESEARCH_CACHE_START, TRAIN_END),
        "untouched_holdout": _cache_snapshot(cache_dir, HOLDOUT_START, HOLDOUT_END),
    }
    if final_cache_snapshots != cache_snapshots:
        raise RuntimeError("historical cache changed during frozen validation")

    payload: dict[str, object] = {
        "schema_version": 2,
        "evaluation_as_of": HOLDOUT_END.isoformat(),
        "source_sha256": source_sha256,
        "runtime": {
            **runtime_manifest(),
            "installed_frozen_dependency": dependency_provenance,
        },
        "seed": seed,
        "no_network_access": True,
        "no_live_orders": True,
        "reproducibility_guard": {
            "source_unchanged_during_run": True,
            "cache_unchanged_during_run": True,
            "cache_snapshot_sha256": cache_snapshots,
        },
        "frozen_before_holdout": {
            "strategy": asdict(FROZEN_STRATEGY),
            "kairos_quant_scouts_git_sha": FROZEN_QUANT_SHA,
            "kairos_quant_scouts_provenance": dependency_provenance,
            "allocation_per_independent_symbol_account": ALLOCATION,
        },
        "windows": {
            "research_cache": [RESEARCH_CACHE_START.isoformat(), TRAIN_END.isoformat()],
            "train_12m": [TRAIN_START.isoformat(), TRAIN_END.isoformat()],
            "holdout_warmup": [HOLDOUT_WARMUP_START.isoformat(), HOLDOUT_START.isoformat()],
            "untouched_holdout": [HOLDOUT_START.isoformat(), HOLDOUT_END.isoformat()],
            "timezone": "UTC",
            "end_boundaries": "exclusive",
        },
        "archive_audits": {
            "research_pre_holdout_300_archives": asdict(research_audit),
            "untouched_holdout_5_archives": asdict(holdout_audit),
        },
        "execution_scenarios": {scenario.name: asdict(scenario.execution) for scenario in scenarios},
        "execution_methodology": {
            "eligibility": "closed-candle signal timestamp plus configured latency",
            "fill_time": "first one-minute candle open at or after eligibility",
            "liquidity_proxy": "volume of the preceding fully closed one-minute candle",
            "execution_candle_total_volume_used": False,
            "target_order_semantics": (
                "immediate-or-cancel; unfilled target remainder is cancelled and fill ratios are reported"
            ),
            "terminal_liquidation": (
                "last close using its then-fully-observed candle volume; the public evaluator "
                "fails incomplete fills, while this research campaign records residual exposure "
                "and promotion rejects it"
            ),
        },
        "walk_forward_method": {
            "train_rows": WALK_FORWARD_TRAIN_ROWS,
            "purge_rows": WALK_FORWARD_PURGE_ROWS,
            "test_rows": WALK_FORWARD_TEST_ROWS,
            "row_timeframe": "1m",
            "configuration_refit": False,
            "out_of_sample_for_promotion": False,
            "interpretation": (
                "post-selection temporal stability diagnostic only; the frozen parameters had "
                "already been selected after inspecting this 12-month window"
            ),
        },
        "symbols": symbol_reports,
        "cross_symbol_summaries": {
            "train_12m": {scenario: _summary(results) for scenario, results in train_by_scenario.items()},
            "holdout_2026_07": {
                scenario: _summary(results) for scenario, results in holdout_by_scenario.items()
            },
        },
        "promotion_readiness": overall_readiness,
        "limitations": [
            (
                "Historical EVEDEX funding observations are unavailable; assumed stress funding "
                "is not accepted as historical evidence."
            ),
            (
                "One-minute OHLCV cannot reproduce intrabar queue position, order-book depth, "
                "or liquidation mechanics."
            ),
            (
                "Cross-symbol summaries are equal-weight independent-account diagnostics; "
                "portfolio Sharpe and drawdown are not inferred from averages."
            ),
            (
                "Walk-forward uses frozen parameters and is a temporal diagnostic, not evidence "
                "of refitting or causal alpha discovery."
            ),
        ],
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_output = output_path.with_suffix(f"{output_path.suffix}.tmp")
    temporary_output.write_text(
        json.dumps(payload, indent=2, sort_keys=True, allow_nan=False) + "\n",
        encoding="utf-8",
    )
    temporary_output.replace(output_path)
    return payload


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the frozen offline strategy validation campaign")
    parser.add_argument("--cache-dir", type=Path, default=Path("data/historical"))
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("reports/strategy-validation-2026-08-01/evaluation.json"),
    )
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args()
    run_frozen_validation(args.cache_dir, args.output, seed=args.seed)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
