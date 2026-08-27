from __future__ import annotations

import json
from pathlib import Path

import pytest

import kairos_backtest.regime_aligned_screen as screen
from kairos_backtest.regime_aligned_screen import (
    _consume_attempt,
    _gate_failures,
    _validate_preflight_receipt,
    expected_plan,
    load_preregistered_plan,
)


def _metrics(*, trades: int = 120, profit_factor: float = 1.2, drawdown: float = 0.02):
    return {
        "active_symbols": 5,
        "direction_trades": {"LONG": max(25, trades // 2), "SHORT": max(25, trades // 2)},
        "expectancy_usd_per_trade": 2.0,
        "hac_sharpe": 0.5,
        "maximum_drawdown": drawdown,
        "maximum_one_symbol_trade_share": 0.2,
        "per_symbol": [
            {"expectancy_usd_per_trade": 1.0, "symbol": symbol, "trades": trades // 5}
            for symbol in ("BTC", "ETH", "SOL", "BNB", "XRP")
        ],
        "positive_expectancy_symbols": 5,
        "profit_factor": profit_factor,
        "total_return": 0.02,
        "trades": trades,
    }


def test_committed_plan_matches_exact_synthesis_and_preserves_permissions():
    root = Path(__file__).resolve().parents[1]
    path = root / "reports" / "regime-aligned-screen" / "plan.json"

    loaded = load_preregistered_plan(path)

    assert loaded == json.loads(path.read_text(encoding="utf-8"))
    assert screen._sha256(loaded) == "aae0730019cb5f78099b0b3e89afbe21fe1d4bb9ef8f247c74e53f349fc31730"
    assert loaded["hypothesis"]["research_lineage_trial_number"] == 15
    assert loaded["hypothesis"]["post_hoc_synthesis"] is True
    assert loaded["hypothesis"]["base_lifecycle_changed"] is False
    assert loaded["data"]["field_profile"] == "price_volume"
    assert loaded["data"]["warmup_days"] == 40
    assert loaded["strategy"]["config"] == {
        "atr_period_hours": 24,
        "decision_interval_hours": 24,
        "intent_valid_hours": 1,
        "max_hold_hours": 72,
        "minimum_trend_score": 1.0,
        "regime_sma_bars": 200,
        "stop_atr_multiple": 2.0,
        "target_reward_to_risk": 4.0,
        "trend_lookback_hours": 24,
    }
    assert loaded["permissions"] == {
        "alpha_ready": False,
        "live_allowed": False,
        "paper_allowed": False,
        "promotion_eligible": False,
    }


def test_preflight_receipt_binds_all_ten_price_volume_slices():
    root = Path(__file__).resolve().parents[1]

    receipt = _validate_preflight_receipt(root / "reports" / "data-field-preflight" / "result-v3.json")

    assert receipt["result_sha256"] == screen.PREFLIGHT_RESULT_SHA256
    assert len(receipt["evidence"]) == 10


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

    assert attempt["lineage_trial_number"] == 15
    assert attempt["consumption_point"].startswith("after_preflight_receipt")
    assert attempt["rerun_allowed"] is False
    with pytest.raises(FileExistsError, match="already consumed"):
        _consume_attempt(plan, plan_path=plan_path, attempt_path=attempt_path)


def test_gate_requires_absolute_quality_and_improvement_over_base():
    candidate = {
        name: {
            "baseline": _metrics(profit_factor=1.25),
            "stress": _metrics(trades=100, profit_factor=1.15, drawdown=0.015),
        }
        for name in ("selection", "robustness")
    }
    base = {
        name: {
            "baseline": _metrics(),
            "stress": _metrics(trades=120, profit_factor=1.04, drawdown=0.02),
        }
        for name in ("selection", "robustness")
    }

    assert _gate_failures(candidate, base) == ()

    candidate["robustness"]["stress"] = _metrics(
        trades=50,
        profit_factor=1.03,
        drawdown=0.03,
    )
    failures = _gate_failures(candidate, base)

    assert "robustness.stress.trades_below_minimum" in failures
    assert "robustness.stress.profit_factor_below_minimum" in failures
    assert "robustness.stress.base_trade_retention_below_minimum" in failures
    assert "robustness.stress.profit_factor_not_above_base" in failures
    assert "robustness.stress.drawdown_above_base" in failures


def test_committed_attempt_and_result_are_immutable_forward_freeze_evidence():
    root = Path(__file__).resolve().parents[1]
    attempt = json.loads(
        (root / "reports" / "regime-aligned-screen" / "attempt.json").read_text(encoding="utf-8")
    )
    result = json.loads(
        (root / "reports" / "regime-aligned-screen" / "summary.json").read_text(encoding="utf-8")
    )

    assert screen._sha256(attempt) == "e7c16126287f4adab5c63d76f63df82e6f03596ee7dd4bde9c262bf71259dbff"
    assert screen._sha256(result) == "bc31c3134b296a80a234ed2d87a3851a5e6f409666f87ffe4fb8646a5367fd53"
    assert attempt["attempt_sha256"] == "4d5f0f83f8e5c12c2e026058f9f303e9152638e7b00770ef3925abff44289e5f"
    assert result["classification"] == "FORWARD_FREEZE_CANDIDATE"
    assert result["gate_failures"] == []
    assert result["windows"]["robustness"]["stress"]["profit_factor"] > 1.05
    assert result["permissions"] == {
        "alpha_ready": False,
        "live_allowed": False,
        "paper_allowed": False,
        "promotion_eligible": False,
    }
