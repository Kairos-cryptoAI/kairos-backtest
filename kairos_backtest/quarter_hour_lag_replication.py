"""Run the preregistered lag-only quarter-hour statistical replication."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import sys
import tempfile
from collections import Counter
from collections.abc import Mapping, Sequence
from datetime import UTC, date, datetime
from pathlib import Path
from typing import cast

import numpy as np

from . import quarter_hour_lag_model as lag_model
from .quarter_hour_features import (
    DATA_END_EXCLUSIVE,
    DATA_START,
    PHASE_OFFSETS_MINUTES,
    PLAN_FILENAME,
    QuarterHourFeatureIntegrityError,
    QuarterHourFeatureLedger,
    _assert_clean,
    _json_bytes,
    _json_value,
    _logical_sha256,
    _plan_value,
    expected_sequence,
    load_plan,
)
from .quarter_hour_features import source_sha256 as feature_source_sha256
from .quarter_hour_lag_model import (
    RollingForecast,
    build_lag_dataset,
    forecast_metrics,
    rolling_monthly_forecast,
)
from .scenarios import SYMBOLS

SCHEMA_VERSION = "kairos.quarter-hour-lag-replication-result.v2"
OOS_START = date(2021, 7, 1)
PAPER_END_EXCLUSIVE = date(2024, 11, 1)
POST_SAMPLE_START = PAPER_END_EXCLUSIVE
PAPER_OVERLAP_ASSETS = ("BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT")
PRIMARY_QUALITY = "clean_targets"
RESULT_FILENAME = "reports/quarter-hour-lag-replication-v2/result.json"


def _utc_ms(value: date) -> int:
    return int(datetime(value.year, value.month, value.day, tzinfo=UTC).timestamp() * 1_000)


def _model_source_sha256() -> str:
    digest = hashlib.sha256()
    paths = sorted((Path(cast(str, lag_model.__file__)), Path(__file__)), key=str)
    for path in paths:
        name = path.name.encode("ascii")
        content = path.read_bytes()
        digest.update(len(name).to_bytes(4, "big"))
        digest.update(name)
        digest.update(len(content).to_bytes(8, "big"))
        digest.update(content)
    return digest.hexdigest()


def _validate_model_plan(plan: Mapping[str, object]) -> None:
    expected: tuple[tuple[tuple[str, ...], object], ...] = (
        (("measurement", "lag_count"), lag_model.LAG_COUNT),
        (("measurement", "phase_offsets_minutes"), list(PHASE_OFFSETS_MINUTES)),
        (("model", "lasso_max_iterations"), lag_model.LASSO_MAX_ITERATIONS),
        (("model", "lasso_tolerance"), lag_model.LASSO_TOLERANCE),
        (("model", "monthly_refit"), True),
        (("model", "refit_training_window_calendar_months"), lag_model.TRAINING_MONTHS),
        (
            ("model", "tie_break"),
            "largest lambda within an absolute tuning-MSE tolerance of 1e-15",
        ),
        (("model", "lambda_grid", "count"), lag_model.LAMBDA_COUNT),
        (("model", "lambda_grid", "maximum"), lag_model.LAMBDA_MAXIMUM),
        (("model", "lambda_grid", "minimum"), lag_model.LAMBDA_MINIMUM),
        (("model", "lambda_grid", "spacing"), "base-10 logarithmic inclusive"),
        (("protocol", "paper_replication_end_exclusive"), PAPER_END_EXCLUSIVE.isoformat()),
        (("protocol", "paper_replication_oos_start"), OOS_START.isoformat()),
        (("protocol", "post_sample_end_exclusive"), DATA_END_EXCLUSIVE.isoformat()),
        (("protocol", "post_sample_start"), POST_SAMPLE_START.isoformat()),
        (("data", "paper_overlap_assets"), list(PAPER_OVERLAP_ASSETS)),
    )
    for path, expected_value in expected:
        actual = _plan_value(plan, path)
        if type(actual) is not type(expected_value) or actual != expected_value:
            raise QuarterHourFeatureIntegrityError(
                f"committed plan field {'.'.join(path)} does not match the model contract"
            )
    grid = tuple(
        float(value)
        for value in np.logspace(
            np.log10(lag_model.LAMBDA_MINIMUM),
            np.log10(lag_model.LAMBDA_MAXIMUM),
            lag_model.LAMBDA_COUNT,
        )
    )
    if (
        len(lag_model.LAMBDA_GRID) != lag_model.LAMBDA_COUNT
        or lag_model.LAMBDA_GRID[0] != lag_model.LAMBDA_MINIMUM
        or lag_model.LAMBDA_GRID[-1] != lag_model.LAMBDA_MAXIMUM
        or not np.allclose(
            lag_model.LAMBDA_GRID[1:-1],
            grid[1:-1],
            rtol=0.0,
            atol=np.finfo(np.float64).eps,
        )
        or lag_model.TUNING_MSE_TOLERANCE != 1e-15
    ):
        raise QuarterHourFeatureIntegrityError(
            "executable lag-model grid or tuning tie-break differs from the committed plan"
        )
    if PRIMARY_QUALITY != "clean_targets":
        raise QuarterHourFeatureIntegrityError("authoritative replication gates must use clean targets")


def _slice_forecast(
    forecast: RollingForecast,
    *,
    start: date,
    end_exclusive: date,
) -> tuple[np.ndarray, np.ndarray]:
    mask = (forecast.timestamps_ms >= _utc_ms(start)) & (forecast.timestamps_ms < _utc_ms(end_exclusive))
    actual = forecast.actual[mask]
    predicted = forecast.predicted[mask]
    if len(actual) == 0:
        raise ValueError(f"forecast slice contains no observations: {start}..{end_exclusive}")
    return actual, predicted


def _refit_summary(forecast: RollingForecast) -> dict[str, object]:
    penalties = Counter(format(refit.selected_penalty, ".17g") for refit in forecast.refits)
    return {
        "forecast_sha256": forecast.forecast_sha256,
        "maximum_test_rows": max(refit.test_rows for refit in forecast.refits),
        "maximum_training_rows": max(refit.training_rows for refit in forecast.refits),
        "minimum_test_rows": min(refit.test_rows for refit in forecast.refits),
        "minimum_training_rows": min(refit.training_rows for refit in forecast.refits),
        "refit_count": len(forecast.refits),
        "selected_penalty_counts": dict(sorted(penalties.items())),
    }


def _window_metrics(
    forecasts: Mapping[tuple[str, int], RollingForecast],
    *,
    start: date,
    end_exclusive: date,
) -> dict[str, object]:
    phases: dict[str, object] = {}
    for phase in PHASE_OFFSETS_MINUTES:
        per_symbol: dict[str, object] = {}
        actual_parts: list[np.ndarray] = []
        predicted_parts: list[np.ndarray] = []
        for symbol in SYMBOLS:
            actual, predicted = _slice_forecast(
                forecasts[(symbol, phase)],
                start=start,
                end_exclusive=end_exclusive,
            )
            actual_parts.append(actual)
            predicted_parts.append(predicted)
            per_symbol[symbol] = forecast_metrics(actual, predicted)
        phases[str(phase)] = {
            "per_symbol": per_symbol,
            "pooled": forecast_metrics(
                np.concatenate(actual_parts),
                np.concatenate(predicted_parts),
            ),
        }
    return {
        "end_exclusive": end_exclusive.isoformat(),
        "phases": phases,
        "start": start.isoformat(),
    }


def _evaluate_quality(
    ledger: QuarterHourFeatureLedger,
    *,
    clean_only: bool,
) -> dict[str, object]:
    forecasts: dict[tuple[str, int], RollingForecast] = {}
    cells: dict[str, object] = {}
    for symbol in SYMBOLS:
        for phase in PHASE_OFFSETS_MINUTES:
            observations = ledger.phase_returns(
                symbol=symbol,
                phase_offset_minutes=phase,
                start_ms=_utc_ms(DATA_START),
                end_ms=_utc_ms(DATA_END_EXCLUSIVE),
                clean_only=clean_only,
            )
            dataset = build_lag_dataset(observations)
            forecast = rolling_monthly_forecast(
                dataset,
                oos_start=OOS_START,
                end_exclusive=DATA_END_EXCLUSIVE,
            )
            forecasts[(symbol, phase)] = forecast
            cells[f"{symbol}:{phase}"] = {
                "complete_lag_rows": len(dataset.responses),
                "feature_rows": len(observations),
                **_refit_summary(forecast),
            }
            print(
                f"lag_cell symbol={symbol} phase={phase} clean={str(clean_only).lower()} "
                f"features={len(observations)} forecasts={len(forecast.actual)}"
            )
    return {
        "cells": cells,
        "paper_replication": _window_metrics(
            forecasts,
            start=OOS_START,
            end_exclusive=PAPER_END_EXCLUSIVE,
        ),
        "post_sample_robustness": _window_metrics(
            forecasts,
            start=POST_SAMPLE_START,
            end_exclusive=DATA_END_EXCLUSIVE,
        ),
    }


def _metric(
    evaluations: Mapping[str, object],
    quality: str,
    window: str,
    phase: int,
    symbol: str | None,
    name: str,
) -> float | None:
    quality_payload = cast(Mapping[str, object], evaluations[quality])
    window_payload = cast(Mapping[str, object], quality_payload[window])
    phases = cast(Mapping[str, object], window_payload["phases"])
    phase_payload = cast(Mapping[str, object], phases[str(phase)])
    if symbol is None:
        metrics = cast(Mapping[str, object], phase_payload["pooled"])
    else:
        per_symbol = cast(Mapping[str, object], phase_payload["per_symbol"])
        metrics = cast(Mapping[str, object], per_symbol[symbol])
    value = metrics[name]
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"{name} must be numeric or null")
    return float(value)


def gate_failures(evaluations: Mapping[str, object]) -> tuple[str, ...]:
    failures: list[str] = []
    for symbol in PAPER_OVERLAP_ASSETS:
        r2 = _metric(
            evaluations,
            PRIMARY_QUALITY,
            "paper_replication",
            0,
            symbol,
            "oos_r2_vs_zero",
        )
        if r2 is None or r2 <= 0:
            failures.append(f"paper_replication.{symbol}.oos_r2_not_positive")
    significant = sum(
        (
            (
                p_value := _metric(
                    evaluations,
                    PRIMARY_QUALITY,
                    "paper_replication",
                    0,
                    symbol,
                    "dm_one_sided_p_value",
                )
            )
            is not None
            and p_value < 0.05
        )
        for symbol in PAPER_OVERLAP_ASSETS
    )
    if significant < 3:
        failures.append("paper_replication.dm_significant_assets_below_three")

    post_positive = sum(
        (
            r2 := _metric(
                evaluations,
                PRIMARY_QUALITY,
                "post_sample_robustness",
                0,
                symbol,
                "oos_r2_vs_zero",
            )
        )
        is not None
        and r2 > 0
        for symbol in SYMBOLS
    )
    if post_positive < 3:
        failures.append("post_sample_robustness.positive_assets_below_three")
    pooled_post = _metric(
        evaluations,
        PRIMARY_QUALITY,
        "post_sample_robustness",
        0,
        None,
        "oos_r2_vs_zero",
    )
    if pooled_post is None or pooled_post <= 0:
        failures.append("post_sample_robustness.pooled_oos_r2_not_positive")

    for window in ("paper_replication", "post_sample_robustness"):
        primary = _metric(
            evaluations,
            PRIMARY_QUALITY,
            window,
            0,
            None,
            "oos_r2_vs_zero",
        )
        for placebo in PHASE_OFFSETS_MINUTES[1:]:
            placebo_r2 = _metric(
                evaluations,
                PRIMARY_QUALITY,
                window,
                placebo,
                None,
                "oos_r2_vs_zero",
            )
            if primary is None or placebo_r2 is None or primary <= placebo_r2:
                failures.append(f"{window}.primary_phase_not_above_placebo_{placebo}")

    return tuple(failures)


def _atomic_create(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError as exc:
            raise FileExistsError(f"refusing to overwrite replication result: {path}") from exc
        temporary.unlink()
    finally:
        temporary.unlink(missing_ok=True)


def run_replication(
    *,
    project_root: Path,
    plan_path: Path,
    ledger_path: Path,
    result_path: Path,
) -> dict[str, object]:
    plan = load_plan(plan_path)
    _validate_model_plan(plan)
    plan_sha = _logical_sha256(plan)
    git_head = _assert_clean(project_root)
    ledger_source = feature_source_sha256()
    with QuarterHourFeatureLedger(
        ledger_path,
        plan_sha256=plan_sha,
        feature_source_sha256=ledger_source,
    ) as ledger:
        ledger_chain = ledger.verify(require_complete=True, deep=True)
        evaluations = {
            "all_targets": _evaluate_quality(ledger, clean_only=False),
            "clean_targets": _evaluate_quality(ledger, clean_only=True),
        }
    failures = gate_failures(evaluations)
    result: dict[str, object] = {
        "classification": (
            "STATISTICAL_COMPONENT_CONFIRMED" if not failures else "REJECT_STATISTICAL_COMPONENT"
        ),
        "completed_at": datetime.now(UTC).isoformat(),
        "data": {
            "archive_batches": len(expected_sequence()),
            "end_exclusive": DATA_END_EXCLUSIVE.isoformat(),
            "feature_ledger_chain_sha256": ledger_chain,
            "feature_ledger_path_is_runtime_only": True,
            "start": DATA_START.isoformat(),
        },
        "evaluations": evaluations,
        "gate_failures": failures,
        "permissions": {
            "alpha_ready": False,
            "live_allowed": False,
            "paper_allowed": False,
            "promotion_eligible": False,
        },
        "plan_sha256": plan_sha,
        "provenance": {
            "feature_source_sha256": ledger_source,
            "git_head_sha": git_head,
            "model_source_sha256": _model_source_sha256(),
            "numpy": np.__version__,
            "platform": platform.platform(),
            "python": sys.version.split()[0],
        },
        "result_schema_version": SCHEMA_VERSION,
        "trading_interpretation": (
            "clean-target forecast evidence only; no entry, exit, sizing, cost, PnL, or trading permission"
        ),
    }
    result["result_sha256"] = _logical_sha256(result)
    _atomic_create(result_path, _json_bytes(result))
    return result


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the preregistered quarter-hour lag-only replication")
    parser.add_argument("--plan", type=Path, default=Path(PLAN_FILENAME))
    parser.add_argument("--ledger", type=Path, required=True)
    parser.add_argument("--result", type=Path, default=Path(RESULT_FILENAME))
    arguments = parser.parse_args(argv)
    result = run_replication(
        project_root=Path(__file__).resolve().parents[1],
        plan_path=arguments.plan,
        ledger_path=arguments.ledger,
        result_path=arguments.result,
    )
    print(
        json.dumps(
            _json_value(
                {
                    "classification": result["classification"],
                    "gate_failures": result["gate_failures"],
                    "result_sha256": result["result_sha256"],
                }
            ),
            separators=(",", ":"),
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
