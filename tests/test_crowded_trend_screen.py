from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

import kairos_backtest.crowded_trend_screen as screen
from kairos_backtest.crowded_trend_screen import (
    _consume_attempt,
    _factor_observations,
    _sha256,
    expected_plan,
    load_preregistered_plan,
)
from kairos_backtest.factor_data import (
    FactorDataset,
    FundingObservation,
    LeverageObservation,
    PremiumObservation,
)

HOUR_MS = 3_600_000


def test_committed_plan_exactly_matches_executable_post_hoc_plan():
    root = Path(__file__).resolve().parents[1]
    path = root / "reports" / "crowded-trend-screen" / "plan.json"

    loaded = load_preregistered_plan(path)

    assert loaded == json.loads(path.read_text(encoding="utf-8"))
    assert _sha256(loaded) == "169949a28b0608c880355ab004c9f0fdd7458d7f3e782b4793cd601a14585ccb"
    assert loaded["hypothesis"]["post_hoc_direction_disclosed"] is True
    assert loaded["hypothesis"]["parameter_search_allowed"] is False
    assert loaded["hypothesis"]["symbol_exclusions_allowed"] is False
    assert loaded["hypothesis"]["research_lineage_trial_number"] == 12
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


def test_attempt_is_consumed_once_before_both_archive_families(tmp_path, monkeypatch):
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

    assert attempt["lineage_trial_number"] == 12
    assert attempt["consumption_point"] == "before_first_price_or_factor_archive_access"
    assert attempt["rerun_allowed"] is False
    assert attempt["crash_or_failure_releases_attempt"] is False
    with pytest.raises(FileExistsError, match="already consumed"):
        _consume_attempt(plan, plan_path=plan_path, attempt_path=attempt_path)


def test_factor_adapter_uses_latest_causal_hourly_values():
    symbol = "BTCUSDT"
    factors = FactorDataset(
        funding={symbol: (FundingObservation(symbol, 0, 8, 0.0001),)},
        premium={
            symbol: (
                PremiumObservation(symbol, 0, HOUR_MS - 1, 0.0, 0.0, 0.0, 0.0005),
                PremiumObservation(symbol, HOUR_MS, 2 * HOUR_MS - 1, 0.0, 0.0, 0.0, 0.0006),
            )
        },
        leverage={
            symbol: (
                LeverageObservation(symbol, 5 * 60_000, 1.0, 100.0, None, None, None, None),
                LeverageObservation(symbol, 55 * 60_000, 1.0, 101.0, None, None, None, None),
                LeverageObservation(symbol, HOUR_MS + 55 * 60_000, 1.0, 102.0, None, None, None, None),
            )
        },
        audits=(),
        inventory_sha256="0" * 64,
    )

    observations = _factor_observations(factors, symbol, date(1970, 1, 1), date(1970, 1, 2))

    assert len(observations) == 2
    assert observations[0].open_interest_value == 101.0
    assert observations[0].open_interest_timestamp_ms == 55 * 60_000
    assert observations[1].funding_timestamp_ms == 0
    assert observations[1].premium_close == 0.0006


def test_preregistered_stress_cost_covers_maximum_holding_plus_grace():
    plan = expected_plan()

    assert plan["scenarios"]["stress"]["costs"]["adverse_funding_bps"] == 15.625
    assert _sha256(plan) == _sha256(
        load_preregistered_plan(
            Path(__file__).resolve().parents[1] / "reports" / "crowded-trend-screen" / "plan.json"
        )
    )
