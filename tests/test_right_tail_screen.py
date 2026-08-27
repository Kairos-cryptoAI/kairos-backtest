from __future__ import annotations

import json
from pathlib import Path

import pytest

import kairos_backtest.right_tail_screen as screen
from kairos_backtest.right_tail_screen import (
    _consume_attempt,
    _gate_failures,
    _sha256,
    expected_plan,
    load_preregistered_plan,
)


def _metrics(*, trades: int = 120, total_return: float = 0.02) -> dict[str, object]:
    per_symbol_trades = trades // 5
    return {
        "active_symbols": 5,
        "direction_trades": {"LONG": max(25, trades // 2), "SHORT": max(25, trades // 2)},
        "expectancy_usd_per_trade": 2.0,
        "hac_sharpe": 0.5,
        "maximum_drawdown": 0.02,
        "maximum_one_symbol_trade_share": 0.2,
        "per_symbol": [
            {"expectancy_usd_per_trade": 1.0, "symbol": symbol, "trades": per_symbol_trades}
            for symbol in ("BTC", "ETH", "SOL", "BNB", "XRP")
        ],
        "positive_expectancy_symbols": 5,
        "profit_factor": 1.2,
        "total_return": total_return,
        "trades": trades,
    }


def test_committed_plan_exactly_matches_executable_plan_and_preserves_failed_anatomy_decision():
    root = Path(__file__).resolve().parents[1]
    path = root / "reports" / "right-tail-screen" / "plan.json"
    loaded = load_preregistered_plan(path)

    assert loaded == json.loads(path.read_text(encoding="utf-8"))
    assert _sha256(loaded) == "4b98938b7880c4a799a528a1f7f3e0a83fbd4bf2b4cb606ff51bf6daec1ecef4"
    hypothesis = loaded["hypothesis"]
    assert hypothesis["market_anatomy_decision"] == "NO_PROTOTYPE_PASSED_DESCRIPTIVE_GATES"
    assert hypothesis["market_anatomy_reinterpreted_as_authorization"] is False
    assert hypothesis["parameter_search_allowed"] is False
    assert loaded["permissions"] == {
        "alpha_ready": False,
        "live_allowed": False,
        "paper_allowed": False,
        "promotion_eligible": False,
    }


def test_plan_mutation_is_rejected(tmp_path):
    mutated = expected_plan()
    mutated["classification"] = "promotion"
    path = tmp_path / "plan.json"
    path.write_text(json.dumps(mutated), encoding="utf-8")

    with pytest.raises(ValueError, match="differs"):
        load_preregistered_plan(path)


def test_attempt_is_consumed_once_before_data_access(tmp_path, monkeypatch):
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

    assert attempt["status"] == "consumed"
    assert attempt["lineage_trial_number"] == 11
    assert attempt["rerun_allowed"] is False
    assert attempt["crash_or_failure_releases_attempt"] is False
    assert attempt_path.exists()
    with pytest.raises(FileExistsError, match="already consumed"):
        _consume_attempt(plan, plan_path=plan_path, attempt_path=attempt_path)


def test_gates_require_positive_diversified_results_in_both_later_windows_and_scenarios():
    passing = {
        name: {scenario: _metrics() for scenario in ("baseline", "stress")}
        for name in ("research", "selection", "robustness")
    }
    assert _gate_failures(passing) == ()

    failing = {
        name: {scenario: _metrics() for scenario in ("baseline", "stress")}
        for name in ("research", "selection", "robustness")
    }
    failing["robustness"]["stress"] = {
        **_metrics(trades=60, total_return=-0.01),
        "expectancy_usd_per_trade": -1.0,
        "hac_sharpe": -0.1,
        "positive_expectancy_symbols": 2,
        "profit_factor": 0.9,
    }
    failures = _gate_failures(failing)

    assert "robustness.stress_trade_retention_below_minimum" in failures
    assert "robustness.stress.trades_below_minimum" in failures
    assert "robustness.stress.total_return_not_positive" in failures
    assert "robustness.stress.expectancy_not_positive" in failures
    assert "robustness.stress.hac_sharpe_not_positive" in failures
    assert "robustness.stress.profit_factor_below_minimum" in failures
    assert "robustness.stress.positive_expectancy_symbols_below_minimum" in failures


def test_gate_rejects_one_sided_or_concentrated_candidate():
    windows = {
        name: {scenario: _metrics() for scenario in ("baseline", "stress")}
        for name in ("research", "selection", "robustness")
    }
    windows["selection"]["baseline"] = {
        **_metrics(),
        "direction_trades": {"LONG": 119, "SHORT": 1},
        "maximum_one_symbol_trade_share": 0.75,
    }

    failures = _gate_failures(windows)

    assert "selection.baseline.short_trades_below_minimum" in failures
    assert "selection.baseline.one_symbol_trade_share_above_maximum" in failures


def test_committed_attempt_and_result_are_immutable_rejection_evidence():
    root = Path(__file__).resolve().parents[1]
    attempt_path = root / "reports" / "right-tail-screen" / "attempt.json"
    result_path = root / "reports" / "right-tail-screen" / "summary.json"

    attempt = json.loads(attempt_path.read_text(encoding="utf-8"))
    result = json.loads(result_path.read_text(encoding="utf-8"))
    assert _sha256(attempt) == "22dbf4420c45478ec0f42b348dbdae11a1d1bbb638046f2e34b6afc573139739"
    assert _sha256(result) == "b3b62e262d2a60be8fd9f1a101b4df3d1ee9d342907bb2e21598d8eb17642a0b"
    assert attempt["attempt_sha256"] == "9e01b505f701b336db861db84c57e07c85dd2bec560134aa84db71e76240aa40"
    assert result["classification"] == "REJECT_REUSED_DATA_SCREEN"
    assert result["gate_failures"] == ["robustness.stress.profit_factor_below_minimum"]
    assert result["permissions"] == {
        "alpha_ready": False,
        "live_allowed": False,
        "paper_allowed": False,
        "promotion_eligible": False,
    }
