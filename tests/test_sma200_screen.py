from __future__ import annotations

import json
from pathlib import Path

import pytest
from kairos_strategy.allocation import AllocationReason, TargetAllocation
from kairos_strategy.candles import Candle

import kairos_backtest.sma200_screen as screen
from kairos_backtest.sma200_screen import (
    CostScenario,
    _consume_attempt,
    _gate_failures,
    _replay,
    _sha256,
    _validate_preflight_receipt,
    expected_plan,
    load_preregistered_plan,
)


def _metrics(**overrides: object) -> dict[str, object]:
    result: dict[str, object] = {
        "active_days": 200,
        "allocation_changes": 20,
        "annualized_sharpe": 1.0,
        "maximum_drawdown": 0.10,
        "profit_factor": 1.2,
        "total_return": 0.10,
    }
    result.update(overrides)
    return result


def _cell(**strategy_overrides: object) -> dict[str, object]:
    return {
        "buy_and_hold": _metrics(annualized_sharpe=0.5, maximum_drawdown=0.20),
        "strategy": _metrics(**strategy_overrides),
    }


def test_committed_plan_matches_exact_external_rule_and_discloses_overlap():
    root = Path(__file__).resolve().parents[1]
    path = root / "reports" / "sma200-screen" / "plan.json"

    loaded = load_preregistered_plan(path)

    assert loaded == json.loads(path.read_text(encoding="utf-8"))
    assert loaded["strategy"]["config"] == {"sma_bars": 200, "target_weight": 1.0}
    assert loaded["hypothesis"]["kairos_parameter_search_allowed"] is False
    assert loaded["hypothesis"]["external_parameter_search_disclosed"] is True
    assert loaded["hypothesis"]["research_lineage_trial_number"] == 14
    assert "only 2026-04-01 onward is source-unseen" in loaded["data"]["source_overlap_limitation"]
    assert loaded["permissions"] == {
        "alpha_ready": False,
        "live_allowed": False,
        "paper_allowed": False,
        "promotion_eligible": False,
    }


def test_preflight_receipt_is_bound_to_exact_qualified_price_only_slices():
    root = Path(__file__).resolve().parents[1]

    receipt = _validate_preflight_receipt(root / "reports" / "data-field-preflight" / "result-v2.json")

    assert receipt["result_sha256"] == screen.PREFLIGHT_RESULT_SHA256


def test_plan_mutation_is_rejected(tmp_path):
    plan = expected_plan()
    plan["classification"] = "promotion"
    path = tmp_path / "plan.json"
    path.write_text(json.dumps(plan), encoding="utf-8")

    with pytest.raises(ValueError, match="differs"):
        load_preregistered_plan(path)


def test_attempt_is_consumed_after_preflight_and_before_market_data(tmp_path, monkeypatch):
    plan = expected_plan()
    plan_path = tmp_path / "plan.json"
    attempt_path = tmp_path / "attempt.json"
    plan_path.write_text(json.dumps(plan, sort_keys=True), encoding="utf-8")
    monkeypatch.setattr(screen, "_now_utc", lambda: screen.datetime(2026, 8, 27, 12, tzinfo=screen.UTC))

    attempt = _consume_attempt(plan, plan_path=plan_path, attempt_path=attempt_path)

    assert attempt["lineage_trial_number"] == 14
    assert attempt["consumption_point"].startswith("after_preflight_receipt")
    assert attempt["rerun_allowed"] is False
    with pytest.raises(FileExistsError, match="already consumed"):
        _consume_attempt(plan, plan_path=plan_path, attempt_path=attempt_path)


def test_gates_require_costed_outperformance_in_both_windows_and_source_unseen_safety():
    passing = {
        window: {scenario: _cell() for scenario in screen.SCENARIOS}
        for window in ("selection", "robustness", "source_unseen")
    }
    assert _gate_failures(passing) == ()

    failing = {
        window: {scenario: _cell() for scenario in screen.SCENARIOS}
        for window in ("selection", "robustness", "source_unseen")
    }
    failing["robustness"]["futures_stress"] = _cell(
        total_return=-0.01,
        profit_factor=1.0,
        annualized_sharpe=0.2,
        maximum_drawdown=0.30,
        active_days=20,
        allocation_changes=2,
    )
    failing["source_unseen"]["published_spot"] = _cell(
        total_return=-0.06,
        maximum_drawdown=0.16,
        allocation_changes=0,
    )

    failures = _gate_failures(failing)

    assert "robustness.futures_stress.total_return_not_positive" in failures
    assert "robustness.futures_stress.profit_factor_below_minimum" in failures
    assert "robustness.futures_stress.sharpe_below_minimum" in failures
    assert "robustness.futures_stress.drawdown_above_maximum" in failures
    assert "robustness.futures_stress.active_days_below_minimum" in failures
    assert "robustness.futures_stress.allocation_changes_below_minimum" in failures
    assert "robustness.futures_stress.does_not_beat_buy_and_hold_sharpe" in failures
    assert "robustness.futures_stress.does_not_beat_buy_and_hold_drawdown" in failures
    assert "source_unseen.published_spot.total_return_below_floor" in failures
    assert "source_unseen.published_spot.drawdown_above_maximum" in failures
    assert "source_unseen.published_spot.allocation_changes_below_minimum" in failures


def test_replay_applies_next_bar_target_cost_and_funding_once():
    period = 14_400_000
    bars = [
        Candle("BTCUSDT", "4h", 0, period - 1, 100, 100, 100, 100, 0),
        Candle("BTCUSDT", "4h", period, 2 * period - 1, 100, 110, 100, 110, 0),
        Candle("BTCUSDT", "4h", 2 * period, 3 * period - 1, 110, 121, 110, 121, 0),
    ]
    allocations = [
        TargetAllocation(
            "four_hour_sma200_long_v1",
            "BTCUSDT",
            period - 1,
            period,
            1.0,
            None,
            (200,),
            ((200, 90.0),),
            AllocationReason.SIGNAL,
        ),
        TargetAllocation(
            "four_hour_sma200_long_v1",
            "BTCUSDT",
            2 * period - 1,
            2 * period,
            0.0,
            None,
            (),
            (),
            AllocationReason.SIGNAL,
        ),
    ]

    rows = _replay(
        bars,
        allocations,
        {period: (0.001, 1)},
        CostScenario(10.0, True, 0.0005),
        start_ms=period,
        end_ms=3 * period,
    )

    assert len(rows) == 2
    assert rows[0].gross_return == pytest.approx(0.10)
    assert rows[0].actual_funding_return == pytest.approx(0.001)
    assert rows[0].adverse_funding_return == pytest.approx(0.0005)
    assert rows[0].transaction_cost_return == pytest.approx(0.001)
    assert rows[0].net_return == pytest.approx(0.0975)
    assert rows[1].gross_return == 0.0
    assert rows[1].transaction_cost_return == pytest.approx(0.001)


def test_committed_plan_has_stable_canonical_hash():
    root = Path(__file__).resolve().parents[1]
    loaded = load_preregistered_plan(root / "reports" / "sma200-screen" / "plan.json")

    assert _sha256(loaded) == "15446c0f1bcb9edc94bf6032831c9d9880d9c9881b81846196c220f682d0584a"
    assert _sha256(loaded) == _sha256(expected_plan())
