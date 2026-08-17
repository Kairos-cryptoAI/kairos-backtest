"""Selection-aware robustness statistics for strategy research.

The functions in this module deliberately operate on non-overlapping daily
net returns.  They do not turn post-selection diagnostics into out-of-sample
evidence; callers must still enforce immutable data roles and trial logging.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, timedelta
from itertools import combinations
from statistics import NormalDist, fmean, median, pvariance
from typing import Literal


def _finite_number(value: object) -> bool:
    return not isinstance(value, bool) and isinstance(value, (int, float)) and math.isfinite(value)


@dataclass(frozen=True, slots=True)
class DailyReturnSeries:
    """A contiguous UTC-day index and its finite net returns."""

    dates: tuple[date, ...]
    returns: tuple[float, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.dates, tuple) or not isinstance(self.returns, tuple):
            raise TypeError("daily return dates and values must be tuples")
        if len(self.dates) < 2 or len(self.dates) != len(self.returns):
            raise ValueError("daily returns require at least two equally sized observations")
        if any(type(day) is not date for day in self.dates):
            raise TypeError("daily return timestamps must be date values, not datetimes")
        if any(
            current - previous != timedelta(days=1)
            for previous, current in zip(self.dates, self.dates[1:], strict=False)
        ):
            raise ValueError("daily return dates must be strictly increasing and contiguous")
        if any(not _finite_number(value) or value <= -1 for value in self.returns):
            raise ValueError("daily returns must be finite numbers greater than -1")

    @property
    def observations(self) -> int:
        return len(self.returns)


@dataclass(frozen=True, slots=True)
class SynchronousTrialMatrix:
    """Daily return columns for every tried configuration on one common index."""

    trial_ids: tuple[str, ...]
    series: tuple[DailyReturnSeries, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.trial_ids, tuple) or not isinstance(self.series, tuple):
            raise TypeError("trial identifiers and series must be tuples")
        if not self.trial_ids or len(self.trial_ids) != len(self.series):
            raise ValueError("a trial matrix requires at least one identified return series")
        if any(
            not isinstance(trial_id, str) or not trial_id or trial_id != trial_id.strip()
            for trial_id in self.trial_ids
        ):
            raise ValueError("trial identifiers must be non-empty normalized strings")
        if len(set(self.trial_ids)) != len(self.trial_ids):
            raise ValueError("trial identifiers must be unique")
        if any(not isinstance(item, DailyReturnSeries) for item in self.series):
            raise TypeError("every trial column must be a DailyReturnSeries")
        common_dates = self.series[0].dates
        if any(item.dates != common_dates for item in self.series[1:]):
            raise ValueError("trial return series must have a synchronous daily index")

    @property
    def observations(self) -> int:
        return self.series[0].observations

    @property
    def trials(self) -> int:
        return len(self.series)

    def selected(self, trial_id: str) -> DailyReturnSeries:
        if not isinstance(trial_id, str) or not trial_id:
            raise ValueError("selected trial identifier is required")
        try:
            return self.series[self.trial_ids.index(trial_id)]
        except ValueError as exc:
            raise KeyError(f"selected trial is absent from the matrix: {trial_id}") from exc


def _non_annualized_sharpe(values: tuple[float, ...]) -> float:
    if len(values) < 2:
        raise ValueError("Sharpe estimation requires at least two observations")
    average = fmean(values)
    squared_deviations = sum((value - average) ** 2 for value in values)
    variance = squared_deviations / (len(values) - 1)
    if not math.isfinite(variance) or variance <= 0:
        raise ValueError("Sharpe estimation requires positive finite variance")
    result = average / math.sqrt(variance)
    if not math.isfinite(result):
        raise ValueError("Sharpe estimate is not finite")
    return result


def non_annualized_sharpe(series: DailyReturnSeries) -> float:
    """Return the daily, non-annualized sample Sharpe ratio."""

    if not isinstance(series, DailyReturnSeries):
        raise TypeError("series must be a DailyReturnSeries")
    return _non_annualized_sharpe(series.returns)


def hac_sharpe(
    series: DailyReturnSeries,
    *,
    annualization_periods: int = 365,
    max_lag: int | None = None,
) -> float:
    """Estimate annualized Sharpe using a Bartlett/Newey-West long-run variance."""

    if not isinstance(series, DailyReturnSeries):
        raise TypeError("series must be a DailyReturnSeries")
    if series.observations < 3:
        raise ValueError("HAC Sharpe requires at least three daily observations")
    if (
        isinstance(annualization_periods, bool)
        or not isinstance(annualization_periods, int)
        or annualization_periods <= 0
    ):
        raise ValueError("annualization_periods must be a positive integer")
    if max_lag is None:
        max_lag = min(
            series.observations - 1,
            max(1, math.floor(4 * (series.observations / 100) ** (2 / 9))),
        )
    if isinstance(max_lag, bool) or not isinstance(max_lag, int) or not 0 <= max_lag < series.observations:
        raise ValueError("max_lag must be an integer within the observed return horizon")

    values = series.returns
    count = len(values)
    average = fmean(values)
    centered = tuple(value - average for value in values)
    autocovariance_zero = sum(value * value for value in centered) / count
    long_run_variance = autocovariance_zero
    for lag in range(1, max_lag + 1):
        covariance = sum(centered[index] * centered[index - lag] for index in range(lag, count)) / count
        weight = 1 - lag / (max_lag + 1)
        long_run_variance += 2 * weight * covariance
    if not math.isfinite(long_run_variance) or long_run_variance <= 0:
        raise ValueError("HAC long-run variance must be finite and positive")
    result = average / math.sqrt(long_run_variance) * math.sqrt(annualization_periods)
    if not math.isfinite(result):
        raise ValueError("HAC Sharpe estimate is not finite")
    return result


def _skewness_and_kurtosis(values: tuple[float, ...]) -> tuple[float, float]:
    if len(values) < 4:
        raise ValueError("skewness and kurtosis require at least four observations")
    average = fmean(values)
    centered = tuple(value - average for value in values)
    second = fmean(value**2 for value in centered)
    if not math.isfinite(second) or second <= 0:
        raise ValueError("return moments require positive finite variance")
    skewness = fmean(value**3 for value in centered) / second**1.5
    kurtosis = fmean(value**4 for value in centered) / second**2
    if not math.isfinite(skewness) or not math.isfinite(kurtosis):
        raise ValueError("return moments must be finite")
    return skewness, kurtosis


@dataclass(frozen=True, slots=True)
class DeflatedSharpeResult:
    probability: float
    observed_sharpe: float
    expected_maximum_sharpe: float
    observations: int
    trials: int
    skewness: float
    kurtosis: float
    trial_sharpe_variance: float


def deflated_sharpe_probability(
    observed_sharpe: float,
    *,
    observations: int,
    skewness: float,
    kurtosis: float,
    trial_sharpes: tuple[float, ...],
) -> DeflatedSharpeResult:
    """Compute Bailey--Lopez de Prado DSR from non-annualized Sharpe inputs."""

    numeric_inputs = (observed_sharpe, skewness, kurtosis)
    if any(not _finite_number(value) for value in numeric_inputs):
        raise ValueError("DSR inputs must be finite numbers")
    if isinstance(observations, bool) or not isinstance(observations, int) or observations < 4:
        raise ValueError("DSR requires at least four return observations")
    if not isinstance(trial_sharpes, tuple) or not trial_sharpes:
        raise ValueError("DSR requires the complete tuple of tried Sharpe ratios")
    if any(not _finite_number(value) for value in trial_sharpes):
        raise ValueError("trial Sharpe ratios must be finite numbers")
    if not any(math.isclose(observed_sharpe, value, rel_tol=1e-12, abs_tol=1e-15) for value in trial_sharpes):
        raise ValueError("the selected Sharpe ratio must be present in the trial inventory")
    if kurtosis < 1 or kurtosis + 1e-12 < skewness**2 + 1:
        raise ValueError("skewness and Pearson kurtosis are mutually inconsistent")

    trials = len(trial_sharpes)
    sharpe_variance = pvariance(trial_sharpes)
    if not math.isfinite(sharpe_variance) or sharpe_variance < 0:
        raise ValueError("trial Sharpe variance must be finite and non-negative")
    expected_maximum = 0.0
    if trials > 1:
        normal = NormalDist()
        euler_mascheroni = 0.5772156649015329
        expected_standard_maximum = (1 - euler_mascheroni) * normal.inv_cdf(
            1 - 1 / trials
        ) + euler_mascheroni * normal.inv_cdf(1 - 1 / (trials * math.e))
        expected_maximum = math.sqrt(sharpe_variance) * expected_standard_maximum

    denominator_squared = 1 - skewness * observed_sharpe + (kurtosis - 1) / 4 * observed_sharpe**2
    if not math.isfinite(denominator_squared) or denominator_squared <= 0:
        raise ValueError("DSR sampling variance must be finite and positive")
    score = (
        (observed_sharpe - expected_maximum) * math.sqrt(observations - 1) / math.sqrt(denominator_squared)
    )
    if not math.isfinite(score):
        raise ValueError("DSR score is not finite")
    probability = NormalDist().cdf(score)
    if not math.isfinite(probability) or not 0 <= probability <= 1:
        raise ValueError("DSR probability is invalid")
    return DeflatedSharpeResult(
        probability=probability,
        observed_sharpe=observed_sharpe,
        expected_maximum_sharpe=expected_maximum,
        observations=observations,
        trials=trials,
        skewness=skewness,
        kurtosis=kurtosis,
        trial_sharpe_variance=sharpe_variance,
    )


def deflated_sharpe_ratio(
    matrix: SynchronousTrialMatrix,
    selected_trial_id: str,
) -> DeflatedSharpeResult:
    """Calculate DSR for one selected column using the complete trial matrix."""

    if not isinstance(matrix, SynchronousTrialMatrix):
        raise TypeError("matrix must be a SynchronousTrialMatrix")
    selected = matrix.selected(selected_trial_id)
    trial_sharpes = tuple(non_annualized_sharpe(item) for item in matrix.series)
    observed = non_annualized_sharpe(selected)
    skewness, kurtosis = _skewness_and_kurtosis(selected.returns)
    return deflated_sharpe_probability(
        observed,
        observations=matrix.observations,
        skewness=skewness,
        kurtosis=kurtosis,
        trial_sharpes=trial_sharpes,
    )


PerformanceMeasure = Literal["mean", "sharpe"]
_SCORE_RELATIVE_TOLERANCE = 1e-12
_SCORE_ABSOLUTE_TOLERANCE = 1e-15


@dataclass(frozen=True, slots=True)
class CSCVResult:
    pbo: float
    probability_of_loss: float
    logits: tuple[float, ...]
    selected_trial_ids: tuple[str, ...]
    oos_log_growth: tuple[float, ...]
    blocks: int
    combinations: int
    performance_measure: PerformanceMeasure


def _performance(values: tuple[float, ...], measure: PerformanceMeasure) -> float:
    if not values:
        raise ValueError("performance samples cannot be empty")
    if measure == "mean":
        result = fmean(values)
    elif measure == "sharpe":
        result = _non_annualized_sharpe(values)
    else:
        raise ValueError("performance_measure must be 'mean' or 'sharpe'")
    if not math.isfinite(result):
        raise ValueError("performance estimate is not finite")
    return result


def _unique_maximum_index(values: tuple[float, ...]) -> int:
    best = max(values)
    winners = [
        index
        for index, value in enumerate(values)
        if math.isclose(
            value,
            best,
            rel_tol=_SCORE_RELATIVE_TOLERANCE,
            abs_tol=_SCORE_ABSOLUTE_TOLERANCE,
        )
    ]
    if len(winners) != 1:
        raise ValueError("CSCV requires a unique in-sample winner for every combination")
    return winners[0]


def _midrank(values: tuple[float, ...], selected_index: int) -> float:
    selected = values[selected_index]
    equal_scores = tuple(
        math.isclose(
            value,
            selected,
            rel_tol=_SCORE_RELATIVE_TOLERANCE,
            abs_tol=_SCORE_ABSOLUTE_TOLERANCE,
        )
        for value in values
    )
    lower = sum(value < selected and not equal for value, equal in zip(values, equal_scores, strict=True))
    equal = sum(equal_scores)
    return lower + (equal + 1) / 2


def cscv_pbo(
    matrix: SynchronousTrialMatrix,
    *,
    blocks: int,
    performance_measure: PerformanceMeasure = "sharpe",
) -> CSCVResult:
    """Estimate CSCV probability of backtest overfitting and OOS loss."""

    if not isinstance(matrix, SynchronousTrialMatrix):
        raise TypeError("matrix must be a SynchronousTrialMatrix")
    if matrix.trials < 2:
        raise ValueError("CSCV requires at least two trials")
    if performance_measure not in {"mean", "sharpe"}:
        raise ValueError("performance_measure must be 'mean' or 'sharpe'")
    if isinstance(blocks, bool) or not isinstance(blocks, int) or blocks < 4 or blocks % 2 or blocks > 20:
        raise ValueError("CSCV blocks must be an even integer within [4, 20]")
    if matrix.observations % blocks:
        raise ValueError("CSCV requires equally sized contiguous blocks")
    block_size = matrix.observations // blocks
    if block_size < 1 or (performance_measure == "sharpe" and matrix.observations // 2 < 2):
        raise ValueError("CSCV block configuration has insufficient observations")

    block_indices = tuple(
        tuple(range(block * block_size, (block + 1) * block_size)) for block in range(blocks)
    )
    logits: list[float] = []
    selected_ids: list[str] = []
    oos_growth: list[float] = []
    for in_sample_blocks in combinations(range(blocks), blocks // 2):
        in_sample_set = set(in_sample_blocks)
        in_sample_indices = tuple(
            index for block in range(blocks) if block in in_sample_set for index in block_indices[block]
        )
        out_of_sample_indices = tuple(
            index for block in range(blocks) if block not in in_sample_set for index in block_indices[block]
        )
        in_sample_scores = tuple(
            _performance(tuple(column.returns[index] for index in in_sample_indices), performance_measure)
            for column in matrix.series
        )
        selected_index = _unique_maximum_index(in_sample_scores)
        out_of_sample_scores = tuple(
            _performance(
                tuple(column.returns[index] for index in out_of_sample_indices),
                performance_measure,
            )
            for column in matrix.series
        )
        rank = _midrank(out_of_sample_scores, selected_index)
        relative_rank = rank / (matrix.trials + 1)
        logit = math.log(relative_rank / (1 - relative_rank))
        growth = sum(
            math.log1p(matrix.series[selected_index].returns[index]) for index in out_of_sample_indices
        )
        if not math.isfinite(logit) or not math.isfinite(growth):
            raise ValueError("CSCV produced non-finite evidence")
        logits.append(logit)
        selected_ids.append(matrix.trial_ids[selected_index])
        oos_growth.append(growth)

    if not logits:
        raise ValueError("CSCV produced no symmetric combinations")
    pbo = sum(value <= 0 for value in logits) / len(logits)
    probability_of_loss = sum(value < 0 for value in oos_growth) / len(oos_growth)
    return CSCVResult(
        pbo=pbo,
        probability_of_loss=probability_of_loss,
        logits=tuple(logits),
        selected_trial_ids=tuple(selected_ids),
        oos_log_growth=tuple(oos_growth),
        blocks=blocks,
        combinations=len(logits),
        performance_measure=performance_measure,
    )


@dataclass(frozen=True, slots=True)
class ParameterOutcome:
    parameter_id: str
    baseline_log_growth: float
    stress_log_growth: float
    profit_factor: float

    def __post_init__(self) -> None:
        if (
            not isinstance(self.parameter_id, str)
            or not self.parameter_id
            or self.parameter_id != self.parameter_id.strip()
        ):
            raise ValueError("parameter_id must be a non-empty normalized string")
        values = (self.baseline_log_growth, self.stress_log_growth, self.profit_factor)
        if any(not _finite_number(value) for value in values) or self.profit_factor < 0:
            raise ValueError("parameter outcomes must contain finite growth and profit factor")


@dataclass(frozen=True, slots=True)
class ParameterPlateauPolicy:
    minimum_neighbors: int = 8
    minimum_positive_fraction: float = 0.70
    minimum_stress_positive_fraction: float = 0.60
    minimum_median_profit_factor: float = 1.10
    minimum_median_growth_ratio: float = 0.70

    def __post_init__(self) -> None:
        if (
            isinstance(self.minimum_neighbors, bool)
            or not isinstance(self.minimum_neighbors, int)
            or self.minimum_neighbors < 1
        ):
            raise ValueError("minimum_neighbors must be a positive integer")
        fractions = (
            self.minimum_positive_fraction,
            self.minimum_stress_positive_fraction,
        )
        if any(not _finite_number(value) or not 0 <= value <= 1 for value in fractions):
            raise ValueError("plateau fractions must be finite values within [0, 1]")
        thresholds = (
            self.minimum_median_profit_factor,
            self.minimum_median_growth_ratio,
        )
        if any(not _finite_number(value) or value < 0 for value in thresholds):
            raise ValueError("plateau thresholds must be finite and non-negative")


@dataclass(frozen=True, slots=True)
class ParameterPlateauReport:
    stable: bool
    reasons: tuple[str, ...]
    selected_parameter_id: str
    neighbors: int
    positive_fraction: float
    stress_positive_fraction: float
    median_profit_factor: float
    median_growth_ratio: float
    selected_on_boundary: bool


def parameter_plateau_report(
    selected: ParameterOutcome,
    neighbors: tuple[ParameterOutcome, ...],
    *,
    selected_on_boundary: bool = False,
    policy: ParameterPlateauPolicy | None = None,
) -> ParameterPlateauReport:
    """Summarize whether a selected configuration lies on a robust local plateau."""

    if not isinstance(selected, ParameterOutcome):
        raise TypeError("selected must be a ParameterOutcome")
    if not isinstance(neighbors, tuple) or any(not isinstance(item, ParameterOutcome) for item in neighbors):
        raise TypeError("neighbors must be a tuple of ParameterOutcome values")
    if not isinstance(selected_on_boundary, bool):
        raise TypeError("selected_on_boundary must be boolean")
    settings = ParameterPlateauPolicy() if policy is None else policy
    if not isinstance(settings, ParameterPlateauPolicy):
        raise TypeError("policy must be a ParameterPlateauPolicy")
    identifiers = tuple(item.parameter_id for item in neighbors)
    if selected.parameter_id in identifiers or len(set(identifiers)) != len(identifiers):
        raise ValueError("selected and neighboring parameter identifiers must be unique")

    count = len(neighbors)
    positive_fraction = sum(item.baseline_log_growth > 0 for item in neighbors) / count if count else 0.0
    stress_positive_fraction = sum(item.stress_log_growth > 0 for item in neighbors) / count if count else 0.0
    median_profit_factor = median(item.profit_factor for item in neighbors) if count else 0.0
    median_growth = median(item.baseline_log_growth for item in neighbors) if count else 0.0
    median_growth_ratio = (
        median_growth / selected.baseline_log_growth if selected.baseline_log_growth > 0 else 0.0
    )
    values = (
        positive_fraction,
        stress_positive_fraction,
        median_profit_factor,
        median_growth_ratio,
    )
    if any(not math.isfinite(value) for value in values):
        raise ValueError("plateau report produced non-finite evidence")

    reasons: list[str] = []
    if count < settings.minimum_neighbors:
        reasons.append("insufficient_neighbors")
    if selected_on_boundary:
        reasons.append("selected_on_search_boundary")
    if selected.baseline_log_growth <= 0:
        reasons.append("non_positive_selected_growth")
    if selected.stress_log_growth <= 0:
        reasons.append("non_positive_selected_stress_growth")
    if selected.profit_factor < settings.minimum_median_profit_factor:
        reasons.append("weak_selected_profit_factor")
    if positive_fraction < settings.minimum_positive_fraction:
        reasons.append("insufficient_positive_neighbors")
    if stress_positive_fraction < settings.minimum_stress_positive_fraction:
        reasons.append("insufficient_stress_positive_neighbors")
    if median_profit_factor < settings.minimum_median_profit_factor:
        reasons.append("weak_neighbor_profit_factor")
    if median_growth_ratio < settings.minimum_median_growth_ratio:
        reasons.append("isolated_selected_growth")
    return ParameterPlateauReport(
        stable=not reasons,
        reasons=tuple(reasons),
        selected_parameter_id=selected.parameter_id,
        neighbors=count,
        positive_fraction=positive_fraction,
        stress_positive_fraction=stress_positive_fraction,
        median_profit_factor=median_profit_factor,
        median_growth_ratio=median_growth_ratio,
        selected_on_boundary=selected_on_boundary,
    )
