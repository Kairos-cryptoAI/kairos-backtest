from __future__ import annotations

import json
from copy import deepcopy
from datetime import date
from pathlib import Path

import numpy as np
import pytest

from kairos_backtest.quarter_hour_features import (
    PLAN_FILENAME,
    QuarterHourFeatureIntegrityError,
    load_plan,
)
from kairos_backtest.quarter_hour_lag_model import RollingForecast
from kairos_backtest.quarter_hour_lag_replication import (
    _atomic_create,
    _slice_forecast,
    _validate_model_plan,
    gate_failures,
)


def _metrics(r2: float, *, p_value: float = 0.01) -> dict[str, object]:
    return {
        "continuous_score_auc": 0.6,
        "direction_accuracy": 0.56,
        "dm_hac_lag": 6,
        "dm_one_sided_p_value": p_value,
        "dm_t_statistic": 2.5,
        "gross_sign_weighted_response_bps": 0.5,
        "mincer_zarnowitz_slope": 0.85,
        "observations": 100,
        "oos_r2_vs_zero": r2,
    }


def _quality() -> dict[str, object]:
    phases: dict[str, object] = {}
    phase_r2 = {0: 0.03, 2: 0.01, 5: 0.005, 7: -0.001}
    for phase, r2 in phase_r2.items():
        phases[str(phase)] = {
            "per_symbol": {
                symbol: _metrics(r2) for symbol in ("BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT")
            },
            "pooled": _metrics(r2),
        }
    return {
        "paper_replication": {"phases": deepcopy(phases)},
        "post_sample_robustness": {"phases": deepcopy(phases)},
    }


def test_preregistered_gates_require_replication_robustness_phase_and_clean_signs() -> None:
    evaluations = {
        "all_targets": _quality(),
        "clean_targets": _quality(),
    }
    assert gate_failures(evaluations) == ()

    failing = deepcopy(evaluations)
    primary = failing["clean_targets"]["paper_replication"]["phases"]["0"]
    primary["per_symbol"]["BTCUSDT"]["oos_r2_vs_zero"] = -0.01
    primary["pooled"]["oos_r2_vs_zero"] = 0.0
    failures = gate_failures(failing)
    assert "paper_replication.BTCUSDT.oos_r2_not_positive" in failures
    assert "paper_replication.primary_phase_not_above_placebo_2" in failures


def test_preregistered_gates_cover_every_failure_class() -> None:
    insufficient_dm = {
        "all_targets": _quality(),
        "clean_targets": _quality(),
    }
    for symbol in ("BTCUSDT", "ETHUSDT"):
        insufficient_dm["clean_targets"]["paper_replication"]["phases"]["0"]["per_symbol"][symbol][
            "dm_one_sided_p_value"
        ] = 0.5
    assert "paper_replication.dm_significant_assets_below_three" in gate_failures(insufficient_dm)

    weak_post_sample = {
        "all_targets": _quality(),
        "clean_targets": _quality(),
    }
    post_primary = weak_post_sample["clean_targets"]["post_sample_robustness"]["phases"]["0"]
    for symbol in ("BTCUSDT", "ETHUSDT", "SOLUSDT"):
        post_primary["per_symbol"][symbol]["oos_r2_vs_zero"] = -0.01
    post_primary["pooled"]["oos_r2_vs_zero"] = -0.01
    post_failures = gate_failures(weak_post_sample)
    assert "post_sample_robustness.positive_assets_below_three" in post_failures
    assert "post_sample_robustness.pooled_oos_r2_not_positive" in post_failures
    assert "post_sample_robustness.primary_phase_not_above_placebo_2" in post_failures

    diagnostic_only = {
        "all_targets": _quality(),
        "clean_targets": _quality(),
    }
    diagnostic_only["all_targets"]["paper_replication"]["phases"]["0"]["pooled"]["oos_r2_vs_zero"] = -10.0
    assert gate_failures(diagnostic_only) == ()


def test_forecast_slice_uses_exact_half_open_calendar_window() -> None:
    timestamps = np.asarray(
        [
            1_609_459_200_000,
            1_612_137_600_000,
            1_614_556_800_000,
        ],
        dtype=np.int64,
    )
    forecast = RollingForecast(
        timestamps_ms=timestamps,
        actual=np.asarray([1.0, 2.0, 3.0]),
        predicted=np.asarray([0.5, 1.5, 2.5]),
        refits=(),
        forecast_sha256="a" * 64,
    )

    actual, predicted = _slice_forecast(
        forecast,
        start=date(2021, 2, 1),
        end_exclusive=date(2021, 3, 1),
    )

    assert actual.tolist() == [2.0]
    assert predicted.tolist() == [1.5]


def test_replication_result_writer_is_create_only(tmp_path: Path) -> None:
    path = tmp_path / "result.json"
    payload = (json.dumps({"classification": "REJECT"}) + "\n").encode("ascii")
    _atomic_create(path, payload)
    assert path.read_bytes() == payload

    with pytest.raises(FileExistsError, match="refusing to overwrite"):
        _atomic_create(path, payload)


def test_executable_model_contract_is_bound_to_plan_fields() -> None:
    root = Path(__file__).resolve().parents[1]
    plan = load_plan(root / PLAN_FILENAME)
    _validate_model_plan(plan)

    mutated = deepcopy(plan)
    mutated["model"]["refit_training_window_calendar_months"] = 12
    with pytest.raises(QuarterHourFeatureIntegrityError, match="model contract"):
        _validate_model_plan(mutated)
