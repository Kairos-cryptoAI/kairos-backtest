from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import numpy as np
import pytest

from kairos_backtest.quarter_hour_lag_model import (
    LAG_COUNT,
    LAMBDA_COUNT,
    LAMBDA_GRID,
    LAMBDA_MAXIMUM,
    LAMBDA_MINIMUM,
    QUARTER_HOUR_MS,
    LagDataset,
    build_lag_dataset,
    fit_standardized_lasso,
    forecast_metrics,
    rolling_monthly_forecast,
    tune_lasso,
)


def _utc_ms(value: date) -> int:
    return int(datetime(value.year, value.month, value.day, tzinfo=UTC).timestamp() * 1_000)


def test_lag_dataset_requires_all_twelve_causal_boundary_lags() -> None:
    observations = tuple((index * QUARTER_HOUR_MS, float(index)) for index in range(15))
    dataset = build_lag_dataset(observations)

    assert dataset.timestamps_ms.tolist() == [
        12 * QUARTER_HOUR_MS,
        13 * QUARTER_HOUR_MS,
        14 * QUARTER_HOUR_MS,
    ]
    assert dataset.predictors[0].tolist() == [float(index) for index in range(11, -1, -1)]
    assert dataset.responses.tolist() == [12.0, 13.0, 14.0]

    missing = tuple(row for row in observations if row[0] != 11 * QUARTER_HOUR_MS)
    incomplete = build_lag_dataset(missing)
    assert incomplete.timestamps_ms.size == 0


def test_standardized_lasso_recovers_sparse_signal_and_zeroes_constant_column() -> None:
    generator = np.random.default_rng(42)
    predictors = generator.normal(size=(500, LAG_COUNT))
    predictors[:, -1] = 7.0
    response = 0.8 * predictors[:, 0] - 0.5 * predictors[:, 1]

    model = fit_standardized_lasso(predictors, response, penalty=1e-5)
    predicted = model.predict(predictors)

    assert float(np.mean(np.square(response - predicted))) < 1e-7
    assert model.coefficients[-1] == 0.0
    assert model.iterations < 100


def test_tuning_tie_breaks_to_largest_preregistered_penalty() -> None:
    predictors = np.zeros((100, LAG_COUNT), dtype=np.float64)
    response = np.linspace(-1.0, 1.0, 100)

    selected = tune_lasso(
        predictors[:80],
        response[:80],
        predictors[80:],
        response[80:],
    )

    assert selected == max(LAMBDA_GRID)


def test_lambda_grid_has_exact_cross_platform_preregistered_endpoints() -> None:
    assert len(LAMBDA_GRID) == LAMBDA_COUNT == 21
    assert LAMBDA_GRID[0] == LAMBDA_MINIMUM == 1e-5
    assert LAMBDA_GRID[-1] == LAMBDA_MAXIMUM == 0.1
    assert all(LAMBDA_GRID[index] < LAMBDA_GRID[index + 1] for index in range(len(LAMBDA_GRID) - 1))


def test_rolling_monthly_forecast_is_causal_deterministic_and_monthly() -> None:
    start = date(2021, 1, 1)
    end = date(2021, 9, 1)
    days = (end - start).days
    timestamps = np.asarray(
        [_utc_ms(start + timedelta(days=offset)) for offset in range(days)],
        dtype=np.int64,
    )
    time = np.arange(days, dtype=np.float64)
    predictors = np.column_stack([np.sin(time / (column + 2)) + column * 0.01 for column in range(LAG_COUNT)])
    response = 0.002 * predictors[:, 0] - 0.001 * predictors[:, 3]
    dataset = LagDataset(timestamps, predictors, response)

    first = rolling_monthly_forecast(
        dataset,
        oos_start=date(2021, 7, 1),
        end_exclusive=end,
    )
    second = rolling_monthly_forecast(
        dataset,
        oos_start=date(2021, 7, 1),
        end_exclusive=end,
    )

    assert [refit.test_month for refit in first.refits] == ["2021-07", "2021-08"]
    assert first.forecast_sha256 == second.forecast_sha256
    assert np.array_equal(first.predicted, second.predicted)
    assert first.timestamps_ms[0] == _utc_ms(date(2021, 7, 1))
    assert first.timestamps_ms[-1] == _utc_ms(date(2021, 8, 31))
    assert forecast_metrics(first.actual, first.predicted)["oos_r2_vs_zero"] > 0.99


def test_forecast_metrics_report_perfect_ranking_and_reject_non_finite() -> None:
    actual = np.linspace(-0.04, 0.04, 100, dtype=np.float64)
    predicted = actual.copy()

    metrics = forecast_metrics(actual, predicted)

    assert metrics["oos_r2_vs_zero"] == 1.0
    assert metrics["direction_accuracy"] == 1.0
    assert metrics["continuous_score_auc"] == 1.0
    assert metrics["gross_sign_weighted_response_bps"] == pytest.approx(
        float(np.mean(np.abs(actual)) * 10_000)
    )
    assert metrics["mincer_zarnowitz_slope"] == pytest.approx(1.0)
    assert metrics["dm_t_statistic"] > 0
    assert metrics["dm_one_sided_p_value"] < 0.5

    with pytest.raises(ValueError, match="non-finite"):
        invalid = predicted.copy()
        invalid[-1] = np.nan
        forecast_metrics(actual, invalid)
