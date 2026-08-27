from __future__ import annotations

import json
from pathlib import Path

import pytest
from kairos_core.enums import Side

from kairos_backtest.quarter_hour_execution_overlay import (
    PARENT_PLAN_SHA256,
    ParentDisposition,
    TimingAction,
    decide_timing,
    direction_adjusted_entry_improvement_bps,
    load_plan,
    verify_parent_result,
)

BOUNDARY_MS = 1_800_000


@pytest.mark.parametrize(
    ("side", "forecast", "expected"),
    [
        (Side.LONG, 0.0001, TimingAction.IMMEDIATE),
        (Side.LONG, -0.0001, TimingAction.DELAY),
        (Side.SHORT, -0.0001, TimingAction.IMMEDIATE),
        (Side.SHORT, 0.0001, TimingAction.DELAY),
        (Side.LONG, 0.0, TimingAction.IMMEDIATE),
        (Side.SHORT, 0.0, TimingAction.IMMEDIATE),
    ],
)
def test_clean_forecast_rule_is_symmetric(
    side: Side,
    forecast: float,
    expected: TimingAction,
) -> None:
    decision = decide_timing(
        side=side,
        boundary_timestamp_ms=BOUNDARY_MS,
        scenario_latency_ms=250,
        forecast=forecast,
        clean_forecast=True,
    )

    assert decision.action is expected
    expected_delay = 10_000 if expected is TimingAction.DELAY else 0
    assert decision.submission_timestamp_ms == BOUNDARY_MS + expected_delay + 250


@pytest.mark.parametrize(
    ("forecast", "clean", "reason"),
    [
        (None, False, "forecast_unavailable_base_fallback"),
        (-0.0001, False, "forecast_dirty_base_fallback"),
    ],
)
def test_missing_or_dirty_forecast_preserves_base_clock(
    forecast: float | None,
    clean: bool,
    reason: str,
) -> None:
    decision = decide_timing(
        side=Side.LONG,
        boundary_timestamp_ms=BOUNDARY_MS,
        scenario_latency_ms=500,
        forecast=forecast,
        clean_forecast=clean,
    )

    assert decision.action is TimingAction.IMMEDIATE
    assert decision.submission_timestamp_ms == BOUNDARY_MS + 500
    assert decision.reason == reason


def test_direction_adjusted_tca_uses_the_correct_side_sign() -> None:
    long_better = direction_adjusted_entry_improvement_bps(
        side=Side.LONG,
        base_entry_price=100.0,
        candidate_entry_price=99.0,
    )
    short_better = direction_adjusted_entry_improvement_bps(
        side=Side.SHORT,
        base_entry_price=100.0,
        candidate_entry_price=101.0,
    )

    assert long_better == pytest.approx(100.0)
    assert short_better == pytest.approx(100.0)


def _parent_result(classification: str) -> dict[str, object]:
    from kairos_backtest.quarter_hour_execution_overlay import _logical_sha256

    payload: dict[str, object] = {
        "classification": classification,
        "permissions": {
            "alpha_ready": False,
            "live_allowed": False,
            "paper_allowed": False,
            "promotion_eligible": False,
        },
        "plan_sha256": PARENT_PLAN_SHA256,
        "result_schema_version": "kairos.quarter-hour-lag-replication-result.v2",
    }
    payload["result_sha256"] = _logical_sha256(payload)
    return payload


@pytest.mark.parametrize(
    ("classification", "expected"),
    [
        ("STATISTICAL_COMPONENT_CONFIRMED", ParentDisposition.ELIGIBLE),
        (
            "REJECT_STATISTICAL_COMPONENT",
            ParentDisposition.NOT_RUN_PARENT_COMPONENT_REJECTED,
        ),
    ],
)
def test_parent_result_gate_is_fail_closed(
    tmp_path: Path,
    classification: str,
    expected: ParentDisposition,
) -> None:
    result = tmp_path / "result.json"
    result.write_text(json.dumps(_parent_result(classification)), encoding="utf-8")

    assert verify_parent_result(result) is expected


def test_parent_result_gate_rejects_mutation(tmp_path: Path) -> None:
    result = tmp_path / "result.json"
    payload = _parent_result("STATISTICAL_COMPONENT_CONFIRMED")
    payload["classification"] = "REJECT_STATISTICAL_COMPONENT"
    result.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError, match="logical hash mismatch"):
        verify_parent_result(result)


def test_plan_and_absent_parent_state() -> None:
    assert load_plan()["schema_version"] == "kairos.quarter-hour-execution-overlay-plan.v1"
    assert verify_parent_result(Path("does-not-exist.json")) is ParentDisposition.PENDING


@pytest.mark.parametrize(
    ("boundary", "latency", "forecast", "clean"),
    [
        (1, 0, 0.1, True),
        (BOUNDARY_MS, -1, 0.1, True),
        (BOUNDARY_MS, 0, float("nan"), True),
        (BOUNDARY_MS, 0, 0.1, 1),
    ],
)
def test_timing_rule_rejects_malformed_inputs(
    boundary: int,
    latency: int,
    forecast: float,
    clean: bool,
) -> None:
    with pytest.raises((TypeError, ValueError)):
        decide_timing(
            side=Side.LONG,
            boundary_timestamp_ms=boundary,
            scenario_latency_ms=latency,
            forecast=forecast,
            clean_forecast=clean,
        )
