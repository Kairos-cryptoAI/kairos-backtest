import math
from datetime import date, datetime, timedelta
from statistics import fmean, stdev

import pytest

from kairos_backtest.robustness import (
    DailyReturnSeries,
    ParameterOutcome,
    ParameterPlateauPolicy,
    SynchronousTrialMatrix,
    cscv_pbo,
    deflated_sharpe_probability,
    deflated_sharpe_ratio,
    hac_sharpe,
    parameter_plateau_report,
)


def days(count: int, *, offset: int = 0) -> tuple[date, ...]:
    start = date(2025, 1, 1) + timedelta(days=offset)
    return tuple(start + timedelta(days=index) for index in range(count))


def series(values: tuple[float, ...], *, offset: int = 0) -> DailyReturnSeries:
    return DailyReturnSeries(days(len(values), offset=offset), values)


def matrix(**trials: tuple[float, ...]) -> SynchronousTrialMatrix:
    return SynchronousTrialMatrix(
        tuple(trials),
        tuple(series(values) for values in trials.values()),
    )


def test_daily_returns_fail_closed_on_invalid_shape_index_and_values():
    with pytest.raises(ValueError, match="at least two"):
        DailyReturnSeries((date(2025, 1, 1),), (0.01,))
    with pytest.raises(ValueError, match="equally sized"):
        DailyReturnSeries(days(2), (0.01,))
    with pytest.raises(ValueError, match="contiguous"):
        DailyReturnSeries((date(2025, 1, 1), date(2025, 1, 3)), (0.01, 0.02))
    with pytest.raises(TypeError, match="not datetimes"):
        DailyReturnSeries((datetime(2025, 1, 1), datetime(2025, 1, 2)), (0.01, 0.02))
    for invalid in (float("nan"), float("inf"), -1.0, True):
        with pytest.raises(ValueError, match="finite"):
            DailyReturnSeries(days(2), (0.01, invalid))  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="tuples"):
        DailyReturnSeries(list(days(2)), [0.01, 0.02])  # type: ignore[arg-type]


def test_trial_matrix_requires_complete_unique_synchronous_columns():
    first = series((0.01, -0.01, 0.02, -0.01))
    shifted = series((0.01, -0.01, 0.02, -0.01), offset=1)

    with pytest.raises(ValueError, match="at least one"):
        SynchronousTrialMatrix((), ())
    with pytest.raises(ValueError, match="unique"):
        SynchronousTrialMatrix(("same", "same"), (first, first))
    with pytest.raises(ValueError, match="synchronous"):
        SynchronousTrialMatrix(("first", "shifted"), (first, shifted))
    with pytest.raises(KeyError, match="absent"):
        SynchronousTrialMatrix(("first",), (first,)).selected("missing")


def test_hac_sharpe_penalizes_positive_serial_correlation():
    values = tuple(([-0.02] * 5 + [0.03] * 5) * 20)
    candidate = series(values)
    naive = fmean(values) / stdev(values) * math.sqrt(365)

    robust = hac_sharpe(candidate, max_lag=4)

    assert robust < naive
    assert math.isfinite(robust)


def test_hac_sharpe_rejects_insufficient_or_degenerate_evidence():
    with pytest.raises(ValueError, match="at least three"):
        hac_sharpe(series((0.01, -0.01)))
    with pytest.raises(ValueError, match="positive"):
        hac_sharpe(series((0.01, 0.01, 0.01, 0.01)), max_lag=0)
    with pytest.raises(ValueError, match="max_lag"):
        hac_sharpe(series((0.01, -0.01, 0.02, -0.02)), max_lag=4)
    with pytest.raises(ValueError, match="annualization_periods"):
        hac_sharpe(series((0.01, -0.01, 0.02, -0.02)), annualization_periods=False)


def test_single_trial_zero_sharpe_has_half_deflated_probability():
    candidate = matrix(only=(-0.01, 0.01, -0.02, 0.02))

    result = deflated_sharpe_ratio(candidate, "only")

    assert result.observed_sharpe == pytest.approx(0.0)
    assert result.expected_maximum_sharpe == 0.0
    assert result.probability == pytest.approx(0.5)
    assert result.trials == 1


def test_more_trials_reduce_dsr_when_trial_dispersion_is_held_constant():
    single = deflated_sharpe_probability(
        1.0,
        observations=100,
        skewness=0.0,
        kurtosis=3.0,
        trial_sharpes=(1.0,),
    )
    many = deflated_sharpe_probability(
        1.0,
        observations=100,
        skewness=0.0,
        kurtosis=3.0,
        trial_sharpes=(1.0, -1.0, 1.0, -1.0, 1.0, -1.0, 1.0, -1.0),
    )

    assert many.trial_sharpe_variance == pytest.approx(1.0)
    assert many.expected_maximum_sharpe > single.expected_maximum_sharpe
    assert many.probability < single.probability


def test_adverse_non_normality_reduces_dsr_for_a_positive_discovery():
    trials = (0.8, -0.8, 0.4, -0.4)
    normal = deflated_sharpe_probability(
        0.8,
        observations=200,
        skewness=0.0,
        kurtosis=3.0,
        trial_sharpes=trials,
    )
    adverse = deflated_sharpe_probability(
        0.8,
        observations=200,
        skewness=-1.0,
        kurtosis=6.0,
        trial_sharpes=trials,
    )

    assert adverse.probability < normal.probability


def test_dsr_rejects_incomplete_nonfinite_or_inconsistent_inputs():
    with pytest.raises(ValueError, match="at least four"):
        deflated_sharpe_probability(
            0.5,
            observations=3,
            skewness=0,
            kurtosis=3,
            trial_sharpes=(0.5,),
        )
    with pytest.raises(ValueError, match="present"):
        deflated_sharpe_probability(
            0.5,
            observations=100,
            skewness=0,
            kurtosis=3,
            trial_sharpes=(0.1, 0.2),
        )
    with pytest.raises(ValueError, match="finite"):
        deflated_sharpe_probability(
            float("nan"),
            observations=100,
            skewness=0,
            kurtosis=3,
            trial_sharpes=(0.0,),
        )
    with pytest.raises(ValueError, match="inconsistent"):
        deflated_sharpe_probability(
            0.5,
            observations=100,
            skewness=2,
            kurtosis=3,
            trial_sharpes=(0.5,),
        )


def test_cscv_persistent_winner_has_zero_pbo_and_loss_probability():
    common = (0.04, 0.03, 0.05, 0.02, 0.06, 0.01, 0.04, 0.03)
    candidate = matrix(
        best=common,
        middle=tuple(value - 0.01 for value in common),
        worst=tuple(value - 0.02 for value in common),
    )

    result = cscv_pbo(candidate, blocks=4, performance_measure="mean")

    assert result.combinations == 6
    assert result.pbo == 0.0
    assert result.probability_of_loss == 0.0
    assert set(result.selected_trial_ids) == {"best"}


def test_cscv_antiselection_has_certain_overfit_and_loss():
    first = (0.01, 0.02, 0.04, -0.07)
    candidate = matrix(a=first, b=tuple(-value for value in first))

    result = cscv_pbo(candidate, blocks=4, performance_measure="mean")

    assert result.combinations == 6
    assert result.pbo == 1.0
    assert result.probability_of_loss == 1.0
    assert all(value < 0 for value in result.logits)


def test_cscv_is_invariant_to_trial_column_permutation():
    first = (0.01, 0.02, 0.04, -0.07)
    direct = cscv_pbo(
        matrix(a=first, b=tuple(-value for value in first)),
        blocks=4,
        performance_measure="mean",
    )
    reversed_columns = cscv_pbo(
        matrix(b=tuple(-value for value in first), a=first),
        blocks=4,
        performance_measure="mean",
    )

    assert reversed_columns.pbo == direct.pbo
    assert reversed_columns.probability_of_loss == direct.probability_of_loss
    assert sorted(reversed_columns.logits) == pytest.approx(sorted(direct.logits))


def test_cscv_rejects_odd_blocks_missing_trials_and_ambiguous_winners():
    values = (0.01, -0.01, 0.02, -0.02)
    with pytest.raises(ValueError, match="even"):
        cscv_pbo(matrix(a=values, b=tuple(-value for value in values)), blocks=3)
    with pytest.raises(ValueError, match="at least two"):
        cscv_pbo(matrix(a=values), blocks=4, performance_measure="mean")
    with pytest.raises(ValueError, match="unique"):
        cscv_pbo(matrix(a=values, b=values), blocks=4, performance_measure="mean")
    six_values = (0.01, -0.01, 0.02, -0.02, 0.03, -0.03)
    with pytest.raises(ValueError, match="equally sized"):
        cscv_pbo(
            matrix(a=six_values, b=tuple(-value for value in six_values)),
            blocks=4,
            performance_measure="mean",
        )


def test_parameter_plateau_reports_a_stable_neighborhood():
    selected = ParameterOutcome("selected", 1.0, 0.7, 1.5)
    neighbors = tuple(
        ParameterOutcome(f"neighbor-{index}", 0.8 + index / 100, 0.2, 1.2) for index in range(8)
    )

    report = parameter_plateau_report(selected, neighbors)

    assert report.stable is True
    assert report.reasons == ()
    assert report.positive_fraction == 1.0
    assert report.stress_positive_fraction == 1.0
    assert report.median_profit_factor == pytest.approx(1.2)
    assert report.median_growth_ratio >= 0.7


def test_parameter_plateau_fails_closed_on_thin_boundary_or_isolated_optimum():
    selected = ParameterOutcome("selected", 1.0, 0.4, 1.4)
    neighbors = tuple(ParameterOutcome(f"neighbor-{index}", 0.1, -0.1, 0.8) for index in range(7))

    report = parameter_plateau_report(selected, neighbors, selected_on_boundary=True)

    assert report.stable is False
    assert set(report.reasons) == {
        "insufficient_neighbors",
        "selected_on_search_boundary",
        "insufficient_stress_positive_neighbors",
        "weak_neighbor_profit_factor",
        "isolated_selected_growth",
    }


def test_parameter_plateau_requires_selected_stress_growth_and_profit_factor():
    selected = ParameterOutcome("selected", 1.0, -0.1, 1.0)
    neighbors = tuple(ParameterOutcome(f"neighbor-{index}", 0.8, 0.2, 1.2) for index in range(8))

    report = parameter_plateau_report(selected, neighbors)

    assert report.stable is False
    assert report.reasons == (
        "non_positive_selected_stress_growth",
        "weak_selected_profit_factor",
    )


def test_parameter_plateau_validates_policy_and_outcome_inventory():
    selected = ParameterOutcome("selected", 1.0, 0.4, 1.4)
    neighbor = ParameterOutcome("neighbor", 0.8, 0.2, 1.2)

    with pytest.raises(ValueError, match="unique"):
        parameter_plateau_report(selected, (neighbor, neighbor))
    with pytest.raises(ValueError, match="finite"):
        ParameterOutcome("invalid", float("nan"), 0.1, 1.2)
    with pytest.raises(ValueError, match="finite"):
        ParameterOutcome("missing-pf", 0.2, 0.1, None)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="fractions"):
        ParameterPlateauPolicy(minimum_positive_fraction=1.1)
    with pytest.raises(TypeError, match="ParameterPlateauPolicy"):
        parameter_plateau_report(selected, (neighbor,), policy=False)  # type: ignore[arg-type]
