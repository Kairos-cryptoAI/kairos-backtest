"""Deterministic causal lag-only forecasting primitives for quarter-hour research."""

from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass
from datetime import UTC, date, datetime

import numpy as np
from numpy.typing import NDArray

LAG_COUNT = 12
QUARTER_HOUR_MS = 15 * 60_000
TRAINING_MONTHS = 6
LASSO_TOLERANCE = 1e-10
LASSO_MAX_ITERATIONS = 10_000
TUNING_MSE_TOLERANCE = 1e-15
LAMBDA_GRID = tuple(float(value) for value in np.logspace(-5, -1, 21))

FloatArray = NDArray[np.float64]
IntArray = NDArray[np.int64]


class LagModelError(RuntimeError):
    """The causal dataset or deterministic estimator is invalid."""


@dataclass(frozen=True, slots=True)
class LagDataset:
    timestamps_ms: IntArray
    predictors: FloatArray
    responses: FloatArray

    def __post_init__(self) -> None:
        if self.timestamps_ms.ndim != 1 or self.responses.ndim != 1:
            raise ValueError("lag timestamps and responses must be one-dimensional")
        if self.predictors.ndim != 2 or self.predictors.shape[1] != LAG_COUNT:
            raise ValueError(f"lag predictors must have exactly {LAG_COUNT} columns")
        if len(self.timestamps_ms) != len(self.predictors) or len(self.responses) != len(self.predictors):
            raise ValueError("lag dataset arrays must have the same row count")
        if len(self.timestamps_ms) and np.any(np.diff(self.timestamps_ms) <= 0):
            raise ValueError("lag dataset timestamps must be strictly increasing")
        if not np.all(np.isfinite(self.predictors)) or not np.all(np.isfinite(self.responses)):
            raise ValueError("lag dataset cannot contain non-finite values")


@dataclass(frozen=True, slots=True)
class StandardizedLasso:
    coefficients: FloatArray
    predictor_mean: FloatArray
    predictor_scale: FloatArray
    response_mean: float
    response_scale: float
    penalty: float
    iterations: int

    def predict(self, predictors: FloatArray) -> FloatArray:
        values = np.asarray(predictors, dtype=np.float64)
        if values.ndim != 2 or values.shape[1] != len(self.coefficients):
            raise ValueError("prediction matrix has the wrong shape")
        if not np.all(np.isfinite(values)):
            raise ValueError("prediction matrix contains non-finite values")
        standardized = (values - self.predictor_mean) / self.predictor_scale
        return self.response_mean + self.response_scale * (standardized @ self.coefficients)


@dataclass(frozen=True, slots=True)
class MonthlyRefit:
    test_month: str
    training_rows: int
    fit_rows: int
    tuning_rows: int
    test_rows: int
    selected_penalty: float
    iterations: int


@dataclass(frozen=True, slots=True)
class RollingForecast:
    timestamps_ms: IntArray
    actual: FloatArray
    predicted: FloatArray
    refits: tuple[MonthlyRefit, ...]
    forecast_sha256: str

    def __post_init__(self) -> None:
        if self.timestamps_ms.ndim != 1 or self.actual.ndim != 1 or self.predicted.ndim != 1:
            raise ValueError("forecast arrays must be one-dimensional")
        if not len(self.timestamps_ms) == len(self.actual) == len(self.predicted):
            raise ValueError("forecast arrays must have the same length")
        if len(self.timestamps_ms) and np.any(np.diff(self.timestamps_ms) <= 0):
            raise ValueError("forecast timestamps must be strictly increasing")
        if not np.all(np.isfinite(self.actual)) or not np.all(np.isfinite(self.predicted)):
            raise ValueError("forecast arrays cannot contain non-finite values")


def _utc_ms(value: date) -> int:
    return int(datetime(value.year, value.month, value.day, tzinfo=UTC).timestamp() * 1_000)


def _next_month(value: date) -> date:
    if value.day != 1:
        raise ValueError("rolling forecast months must start on day one")
    return date(value.year + (value.month == 12), value.month % 12 + 1, 1)


def _subtract_months(value: date, count: int) -> date:
    if value.day != 1 or isinstance(count, bool) or not isinstance(count, int) or count < 0:
        raise ValueError("calendar-month subtraction requires a month start and non-negative count")
    total = value.year * 12 + value.month - 1 - count
    return date(total // 12, total % 12 + 1, 1)


def build_lag_dataset(
    observations: tuple[tuple[int, float], ...],
    *,
    lag_count: int = LAG_COUNT,
    spacing_ms: int = QUARTER_HOUR_MS,
) -> LagDataset:
    if lag_count != LAG_COUNT:
        raise ValueError(f"preregistered lag count is fixed at {LAG_COUNT}")
    if spacing_ms != QUARTER_HOUR_MS:
        raise ValueError("preregistered lag spacing is fixed at fifteen minutes")
    values: dict[int, float] = {}
    previous: int | None = None
    for timestamp_ms, response in observations:
        if isinstance(timestamp_ms, bool) or not isinstance(timestamp_ms, int) or timestamp_ms < 0:
            raise ValueError("observation timestamp must be a non-negative integer")
        if previous is not None and timestamp_ms <= previous:
            raise LagModelError("quarter-hour observations must be strictly ordered")
        if not math.isfinite(response):
            raise ValueError("quarter-hour response must be finite")
        values[timestamp_ms] = response
        previous = timestamp_ms
    timestamps: list[int] = []
    predictors: list[list[float]] = []
    responses: list[float] = []
    for timestamp_ms, response in observations:
        lags = [values.get(timestamp_ms - index * spacing_ms) for index in range(1, lag_count + 1)]
        if any(value is None for value in lags):
            continue
        timestamps.append(timestamp_ms)
        predictors.append([float(value) for value in lags if value is not None])
        responses.append(response)
    return LagDataset(
        timestamps_ms=np.asarray(timestamps, dtype=np.int64),
        predictors=np.asarray(predictors, dtype=np.float64).reshape((-1, LAG_COUNT)),
        responses=np.asarray(responses, dtype=np.float64),
    )


def _soft_threshold(value: float, penalty: float) -> float:
    if value > penalty:
        return value - penalty
    if value < -penalty:
        return value + penalty
    return 0.0


def _coordinate_descent(
    predictors: FloatArray,
    response: FloatArray,
    *,
    penalty: float,
    initial: FloatArray | None = None,
) -> tuple[FloatArray, int]:
    if penalty < 0 or not math.isfinite(penalty):
        raise ValueError("LASSO penalty must be finite and non-negative")
    rows, columns = predictors.shape
    if rows == 0 or response.shape != (rows,):
        raise ValueError("LASSO fit data has an invalid shape")
    coefficients = (
        np.zeros(columns, dtype=np.float64)
        if initial is None
        else np.asarray(initial, dtype=np.float64).copy()
    )
    if coefficients.shape != (columns,) or not np.all(np.isfinite(coefficients)):
        raise ValueError("initial LASSO coefficients are invalid")
    residual = response - predictors @ coefficients
    column_moments = np.mean(predictors * predictors, axis=0)
    for iteration in range(1, LASSO_MAX_ITERATIONS + 1):
        maximum_change = 0.0
        for column in range(columns):
            moment = float(column_moments[column])
            old = float(coefficients[column])
            if moment <= np.finfo(np.float64).eps:
                new = 0.0
            else:
                partial = residual + predictors[:, column] * old
                correlation = float(np.mean(predictors[:, column] * partial))
                new = _soft_threshold(correlation, penalty) / moment
            if new != old:
                residual -= predictors[:, column] * (new - old)
                coefficients[column] = new
                maximum_change = max(maximum_change, abs(new - old))
        if maximum_change <= LASSO_TOLERANCE:
            return coefficients, iteration
    raise LagModelError("deterministic LASSO coordinate descent did not converge")


def _standardize(predictors: FloatArray, response: FloatArray) -> tuple[FloatArray, ...]:
    values = np.asarray(predictors, dtype=np.float64)
    target = np.asarray(response, dtype=np.float64)
    if values.ndim != 2 or target.shape != (len(values),) or len(values) < 2:
        raise ValueError("standardization requires at least two aligned rows")
    if not np.all(np.isfinite(values)) or not np.all(np.isfinite(target)):
        raise ValueError("standardization data cannot contain non-finite values")
    predictor_mean = np.mean(values, axis=0)
    predictor_scale = np.std(values, axis=0)
    constant = predictor_scale <= np.finfo(np.float64).eps
    predictor_scale = predictor_scale.copy()
    predictor_scale[constant] = 1.0
    standardized_predictors = (values - predictor_mean) / predictor_scale
    standardized_predictors[:, constant] = 0.0
    response_mean = float(np.mean(target))
    response_scale = float(np.std(target))
    if response_scale <= np.finfo(np.float64).eps:
        raise LagModelError("training response has zero variance")
    standardized_response = (target - response_mean) / response_scale
    return (
        standardized_predictors,
        standardized_response,
        predictor_mean,
        predictor_scale,
        np.asarray([response_mean, response_scale], dtype=np.float64),
    )


def fit_standardized_lasso(
    predictors: FloatArray,
    response: FloatArray,
    *,
    penalty: float,
) -> StandardizedLasso:
    standardized_x, standardized_y, x_mean, x_scale, y_moments = _standardize(predictors, response)
    coefficients, iterations = _coordinate_descent(
        standardized_x,
        standardized_y,
        penalty=penalty,
    )
    return StandardizedLasso(
        coefficients=coefficients,
        predictor_mean=x_mean,
        predictor_scale=x_scale,
        response_mean=float(y_moments[0]),
        response_scale=float(y_moments[1]),
        penalty=penalty,
        iterations=iterations,
    )


def tune_lasso(
    fit_predictors: FloatArray,
    fit_response: FloatArray,
    tuning_predictors: FloatArray,
    tuning_response: FloatArray,
) -> float:
    standardized_x, standardized_y, x_mean, x_scale, y_moments = _standardize(fit_predictors, fit_response)
    tuning = np.asarray(tuning_predictors, dtype=np.float64)
    tuning_target = np.asarray(tuning_response, dtype=np.float64)
    if tuning.ndim != 2 or tuning.shape[1] != standardized_x.shape[1]:
        raise ValueError("tuning predictor matrix has the wrong shape")
    if tuning_target.shape != (len(tuning),) or len(tuning) == 0:
        raise ValueError("tuning response has the wrong shape")
    if not np.all(np.isfinite(tuning)) or not np.all(np.isfinite(tuning_target)):
        raise ValueError("tuning data cannot contain non-finite values")
    standardized_tuning = (tuning - x_mean) / x_scale
    best_penalty: float | None = None
    best_mse = math.inf
    coefficients = np.zeros(standardized_x.shape[1], dtype=np.float64)
    for penalty in reversed(LAMBDA_GRID):
        coefficients, _ = _coordinate_descent(
            standardized_x,
            standardized_y,
            penalty=penalty,
            initial=coefficients,
        )
        prediction = float(y_moments[0]) + float(y_moments[1]) * (standardized_tuning @ coefficients)
        mse = float(np.mean(np.square(tuning_target - prediction)))
        if mse < best_mse - TUNING_MSE_TOLERANCE:
            best_mse = mse
            best_penalty = penalty
    if best_penalty is None:
        raise LagModelError("LASSO tuning failed to select a finite penalty")
    return best_penalty


def rolling_monthly_forecast(
    dataset: LagDataset,
    *,
    oos_start: date,
    end_exclusive: date,
) -> RollingForecast:
    if oos_start.day != 1 or end_exclusive.day != 1 or oos_start >= end_exclusive:
        raise ValueError("rolling OOS range must be non-empty and month-aligned")
    timestamps: list[IntArray] = []
    actual: list[FloatArray] = []
    predicted: list[FloatArray] = []
    refits: list[MonthlyRefit] = []
    month = oos_start
    while month < end_exclusive:
        next_month = _next_month(month)
        training_start = _subtract_months(month, TRAINING_MONTHS)
        train_mask = (dataset.timestamps_ms >= _utc_ms(training_start)) & (
            dataset.timestamps_ms < _utc_ms(month)
        )
        test_mask = (dataset.timestamps_ms >= _utc_ms(month)) & (dataset.timestamps_ms < _utc_ms(next_month))
        train_x = dataset.predictors[train_mask]
        train_y = dataset.responses[train_mask]
        test_x = dataset.predictors[test_mask]
        test_y = dataset.responses[test_mask]
        test_timestamps = dataset.timestamps_ms[test_mask]
        if len(train_x) < 20:
            raise LagModelError(f"insufficient six-month training rows for {month:%Y-%m}")
        if len(test_x) == 0:
            raise LagModelError(f"test month contains no complete lag rows: {month:%Y-%m}")
        split = int(math.floor(len(train_x) * 0.8))
        if split < 2 or len(train_x) - split < 1:
            raise LagModelError(f"chronological tuning split is empty for {month:%Y-%m}")
        penalty = tune_lasso(
            train_x[:split],
            train_y[:split],
            train_x[split:],
            train_y[split:],
        )
        model = fit_standardized_lasso(train_x, train_y, penalty=penalty)
        forecast = model.predict(test_x)
        timestamps.append(test_timestamps)
        actual.append(test_y)
        predicted.append(forecast)
        refits.append(
            MonthlyRefit(
                test_month=month.strftime("%Y-%m"),
                training_rows=len(train_x),
                fit_rows=split,
                tuning_rows=len(train_x) - split,
                test_rows=len(test_x),
                selected_penalty=penalty,
                iterations=model.iterations,
            )
        )
        month = next_month
    all_timestamps = np.concatenate(timestamps).astype(np.int64, copy=False)
    all_actual = np.concatenate(actual).astype(np.float64, copy=False)
    all_predicted = np.concatenate(predicted).astype(np.float64, copy=False)
    digest = hashlib.sha256()
    for timestamp_ms, realized, forecast in zip(
        all_timestamps,
        all_actual,
        all_predicted,
        strict=True,
    ):
        digest.update(
            f"{int(timestamp_ms)},{float(realized).hex()},{float(forecast).hex()}\n".encode("ascii")
        )
    return RollingForecast(
        timestamps_ms=all_timestamps,
        actual=all_actual,
        predicted=all_predicted,
        refits=tuple(refits),
        forecast_sha256=digest.hexdigest(),
    )


def _auc(labels: NDArray[np.bool_], scores: FloatArray) -> float | None:
    positives = int(np.sum(labels))
    negatives = len(labels) - positives
    if positives == 0 or negatives == 0:
        return None
    order = np.argsort(scores, kind="mergesort")
    sorted_scores = scores[order]
    ranks = np.empty(len(scores), dtype=np.float64)
    index = 0
    while index < len(scores):
        end = index + 1
        while end < len(scores) and sorted_scores[end] == sorted_scores[index]:
            end += 1
        average_rank = (index + 1 + end) / 2.0
        ranks[order[index:end]] = average_rank
        index = end
    rank_sum = float(np.sum(ranks[labels]))
    return (rank_sum - positives * (positives + 1) / 2.0) / (positives * negatives)


def forecast_metrics(actual: FloatArray, predicted: FloatArray, *, hac_lag: int = 6) -> dict[str, object]:
    realized = np.asarray(actual, dtype=np.float64)
    forecast = np.asarray(predicted, dtype=np.float64)
    if realized.ndim != 1 or forecast.shape != realized.shape or len(realized) == 0:
        raise ValueError("forecast metrics require aligned non-empty vectors")
    if not np.all(np.isfinite(realized)) or not np.all(np.isfinite(forecast)):
        raise ValueError("forecast metrics cannot contain non-finite values")
    denominator = float(np.sum(np.square(realized)))
    if denominator <= 0:
        raise LagModelError("OOS R2 zero-forecast denominator is zero")
    residual = realized - forecast
    oos_r2 = 1.0 - float(np.sum(np.square(residual))) / denominator
    actual_up = realized > 0
    predicted_up = forecast > 0
    accuracy = float(np.mean(actual_up == predicted_up))
    auc = _auc(actual_up, forecast)
    gross_bps = float(np.mean(np.sign(forecast) * realized) * 10_000)
    centered_forecast = forecast - float(np.mean(forecast))
    forecast_variance = float(np.sum(np.square(centered_forecast)))
    mz_slope = None
    if forecast_variance > np.finfo(np.float64).eps:
        mz_slope = float(
            np.sum(centered_forecast * (realized - float(np.mean(realized)))) / forecast_variance
        )
    loss_advantage = np.square(realized) - np.square(residual)
    mean_advantage = float(np.mean(loss_advantage))
    centered_loss = loss_advantage - mean_advantage
    variance = float(np.mean(np.square(centered_loss)))
    maximum_lag = min(hac_lag, len(centered_loss) - 1)
    for lag in range(1, maximum_lag + 1):
        covariance = float(np.sum(centered_loss[lag:] * centered_loss[:-lag]) / len(centered_loss))
        variance += 2.0 * (1.0 - lag / (maximum_lag + 1)) * covariance
    dm_t = None
    dm_p = None
    if variance > np.finfo(np.float64).eps:
        dm_t = mean_advantage / math.sqrt(variance / len(centered_loss))
        dm_p = 0.5 * math.erfc(dm_t / math.sqrt(2.0))
    return {
        "continuous_score_auc": auc,
        "direction_accuracy": accuracy,
        "dm_hac_lag": hac_lag,
        "dm_one_sided_p_value": dm_p,
        "dm_t_statistic": dm_t,
        "gross_sign_weighted_response_bps": gross_bps,
        "mincer_zarnowitz_slope": mz_slope,
        "observations": len(realized),
        "oos_r2_vs_zero": oos_r2,
    }
