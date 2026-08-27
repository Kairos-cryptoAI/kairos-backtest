from __future__ import annotations

import json
from pathlib import Path

import pytest
from kairos_strategy.allocation import AllocationReason, TargetAllocation
from kairos_strategy.candles import Candle

import kairos_backtest.donchian_screen as screen
from kairos_backtest.donchian_screen import (
    AllocationCostScenario,
    _consume_attempt,
    _gate_failures,
    _sha256,
    _symbol_days,
    expected_plan,
    load_preregistered_plan,
)


def _metrics(**overrides: object) -> dict[str, object]:
    result: dict[str, object] = {
        "active_days": 300,
        "allocation_changes": 80,
        "annualized_sharpe": 1.0,
        "maximum_drawdown": 0.10,
        "positive_symbols": 4,
        "profit_factor": 1.2,
        "total_return": 0.10,
    }
    result.update(overrides)
    return result


def test_committed_plan_matches_published_model_and_discloses_interpretations():
    root = Path(__file__).resolve().parents[1]
    path = root / "reports" / "donchian-screen" / "plan.json"

    loaded = load_preregistered_plan(path)

    assert loaded == json.loads(path.read_text(encoding="utf-8"))
    assert loaded["hypothesis"]["published_model_transcribed_without_horizon_search"] is True
    assert loaded["hypothesis"]["parameter_search_allowed"] is False
    assert loaded["hypothesis"]["research_lineage_trial_number"] == 13
    assert loaded["strategy"]["config"]["horizons_days"] == [5, 10, 20, 30, 60, 90, 150, 250, 360]
    assert loaded["permissions"] == {
        "alpha_ready": False,
        "live_allowed": False,
        "paper_allowed": False,
        "promotion_eligible": False,
    }


def test_plan_mutation_is_rejected(tmp_path):
    plan = expected_plan()
    plan["classification"] = "promotion"
    path = tmp_path / "plan.json"
    path.write_text(json.dumps(plan), encoding="utf-8")

    with pytest.raises(ValueError, match="differs"):
        load_preregistered_plan(path)


def test_attempt_is_consumed_before_price_or_funding_access(tmp_path, monkeypatch):
    plan = expected_plan()
    plan_path = tmp_path / "plan.json"
    attempt_path = tmp_path / "attempt.json"
    plan_path.write_text(json.dumps(plan, sort_keys=True), encoding="utf-8")
    monkeypatch.setattr(
        screen,
        "_now_utc",
        lambda: screen.datetime(2026, 8, 27, 12, tzinfo=screen.UTC),
    )

    attempt = _consume_attempt(plan, plan_path=plan_path, attempt_path=attempt_path)

    assert attempt["lineage_trial_number"] == 13
    assert attempt["consumption_point"] == "before_first_price_or_funding_archive_access"
    assert attempt["rerun_allowed"] is False
    with pytest.raises(FileExistsError, match="already consumed"):
        _consume_attempt(plan, plan_path=plan_path, attempt_path=attempt_path)


def test_gates_require_positive_costed_breadth_in_every_cell():
    passing = {
        window: {scenario: _metrics() for scenario in ("baseline", "stress")}
        for window in ("selection", "robustness")
    }
    assert _gate_failures(passing) == ()

    failing = {
        window: {scenario: _metrics() for scenario in ("baseline", "stress")}
        for window in ("selection", "robustness")
    }
    failing["robustness"]["stress"] = _metrics(
        total_return=-0.01,
        profit_factor=1.0,
        annualized_sharpe=0.2,
        positive_symbols=2,
        active_days=50,
        allocation_changes=10,
        maximum_drawdown=0.25,
    )

    failures = _gate_failures(failing)

    assert "robustness.stress.total_return_not_positive" in failures
    assert "robustness.stress.profit_factor_below_minimum" in failures
    assert "robustness.stress.sharpe_below_minimum" in failures
    assert "robustness.stress.positive_symbols_below_minimum" in failures
    assert "robustness.stress.active_days_below_minimum" in failures
    assert "robustness.stress.allocation_changes_below_minimum" in failures
    assert "robustness.stress.drawdown_above_maximum" in failures


def test_committed_plan_has_stable_canonical_hash():
    root = Path(__file__).resolve().parents[1]
    loaded = load_preregistered_plan(root / "reports" / "donchian-screen" / "plan.json")

    assert _sha256(loaded) == "af901d350c1ebdc2eedf89229859c8934ecaa85bc44deff698720f01ae208b9a"
    assert _sha256(loaded) == _sha256(expected_plan())


def test_consumed_trial_is_closed_as_inconclusive_without_performance_result():
    root = Path(__file__).resolve().parents[1]
    failure = json.loads((root / "reports" / "donchian-screen" / "failure.json").read_text(encoding="utf-8"))

    assert failure["classification"] == "INCONCLUSIVE_DATA_INTEGRITY"
    assert failure["rerun_allowed"] is False
    assert failure["failure"]["taker_buy_volume"] > failure["failure"]["total_volume"]
    assert failure["archive"]["official_sha256_verified"] is True
    assert failure["observability"] == {
        "operator_observed_performance_metrics": False,
        "partial_in_memory_symbol_replays_persisted": False,
        "result_file_written": False,
    }
    assert failure["permissions"] == {
        "alpha_ready": False,
        "live_allowed": False,
        "paper_allowed": False,
        "promotion_eligible": False,
    }
    assert _sha256(failure) == "02bf8eff39c03a756aaea48642a45973bb8b20ee0c4ae1751b3e07e2e1c8fd82"
    assert not (root / "reports" / "donchian-screen" / "summary.json").exists()


def test_symbol_replay_applies_next_day_weight_turnover_and_funding_once():
    day_ms = 86_400_000
    daily = [
        Candle("BTCUSDT", "1d", 0, day_ms - 1, 100, 100, 100, 100, 1),
        Candle("BTCUSDT", "1d", day_ms, 2 * day_ms - 1, 100, 110, 100, 110, 1),
        Candle("BTCUSDT", "1d", 2 * day_ms, 3 * day_ms - 1, 110, 121, 110, 121, 1),
    ]
    allocations = [
        TargetAllocation(
            "donchian_ensemble_long_v1",
            "BTCUSDT",
            day_ms - 1,
            day_ms,
            1.0,
            0.5,
            (5,),
            ((5, 90.0),),
            AllocationReason.SIGNAL,
        ),
        TargetAllocation(
            "donchian_ensemble_long_v1",
            "BTCUSDT",
            2 * day_ms - 1,
            2 * day_ms,
            0.5,
            0.5,
            (5,),
            ((5, 95.0),),
            AllocationReason.VOLATILITY,
        ),
    ]

    days = _symbol_days(
        daily,
        allocations,
        {day_ms: (0.001, 1)},
        AllocationCostScenario(10.0, 0.0),
        day_ms,
        3 * day_ms,
    )

    assert len(days) == 2
    assert days[0].gross_return == pytest.approx(0.10)
    assert days[0].funding_return == pytest.approx(0.001)
    assert days[0].transaction_cost_return == pytest.approx(0.001)
    assert days[0].net_return == pytest.approx(0.098)
    assert days[1].gross_return == pytest.approx(0.05)
    assert days[1].transaction_cost_return == pytest.approx(0.0005)
    assert days[1].net_return == pytest.approx(0.0495)
