"""Fail-closed controls for the preregistered quarter-hour execution overlay."""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from kairos_core.enums import Side

PLAN_PATH = Path("reports/quarter-hour-execution-overlay/plan.json")
PLAN_SHA256 = "637e9240545f7dfcd10a21989bff761ce55ef4eed9f51d7eea4e6415af0ff073"
PARENT_PLAN_SHA256 = "2c5d91f76dcf5fd2f8c5bcc1ccec1032fb56b967e131d6136fb9b437c86f425f"
PARENT_RESULT_SCHEMA = "kairos.quarter-hour-lag-replication-result.v2"
PARENT_CONFIRMED = "STATISTICAL_COMPONENT_CONFIRMED"
PARENT_REJECTED = "REJECT_STATISTICAL_COMPONENT"
DELAY_MS = 10_000
QUARTER_HOUR_MS = 15 * 60 * 1_000
_BPS = 10_000.0


class TimingAction(StrEnum):
    """The only two entry-clock actions permitted by the frozen plan."""

    IMMEDIATE = "IMMEDIATE"
    DELAY = "DELAY"


class ParentDisposition(StrEnum):
    """Whether the conditional overlay may open its reused-data evaluation."""

    PENDING = "PENDING"
    ELIGIBLE = "ELIGIBLE"
    NOT_RUN_PARENT_COMPONENT_REJECTED = "NOT_RUN_PARENT_COMPONENT_REJECTED"


@dataclass(frozen=True, slots=True)
class TimingDecision:
    """Auditable entry-clock decision without any intent mutation."""

    action: TimingAction
    boundary_timestamp_ms: int
    scenario_latency_ms: int
    submission_timestamp_ms: int
    forecast: float | None
    clean_forecast: bool
    reason: str

    def __post_init__(self) -> None:
        if not isinstance(self.action, TimingAction):
            raise TypeError("timing action must be a TimingAction")
        for name in ("boundary_timestamp_ms", "scenario_latency_ms", "submission_timestamp_ms"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{name} must be a non-negative integer")
        if self.boundary_timestamp_ms % QUARTER_HOUR_MS:
            raise ValueError("timing boundary must be aligned to a UTC quarter hour")
        if self.submission_timestamp_ms < self.boundary_timestamp_ms:
            raise ValueError("submission cannot predate its boundary")
        expected_submission = (
            self.boundary_timestamp_ms
            + self.scenario_latency_ms
            + (DELAY_MS if self.action is TimingAction.DELAY else 0)
        )
        if self.submission_timestamp_ms != expected_submission:
            raise ValueError("submission timestamp contradicts the frozen timing action")
        if self.forecast is not None and (
            isinstance(self.forecast, bool)
            or not isinstance(self.forecast, (int, float))
            or not math.isfinite(self.forecast)
        ):
            raise ValueError("timing forecast must be finite or absent")
        if not isinstance(self.clean_forecast, bool):
            raise TypeError("clean_forecast must be a boolean")
        if not isinstance(self.reason, str) or not self.reason:
            raise ValueError("timing reason must be a non-empty string")


def _logical_sha256(payload: object) -> str:
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def load_plan(path: Path = PLAN_PATH) -> dict[str, object]:
    """Load and verify the exact preregistered overlay plan."""

    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or _logical_sha256(payload) != PLAN_SHA256:
        raise ValueError("quarter-hour execution overlay plan hash mismatch")
    model = payload.get("model")
    if not isinstance(model, dict) or model.get("parent_plan_sha256") != PARENT_PLAN_SHA256:
        raise ValueError("overlay plan does not bind the exact parent plan")
    permissions = payload.get("permissions")
    if (
        not isinstance(permissions, dict)
        or set(permissions) != {"alpha_ready", "live_allowed", "paper_allowed", "promotion_eligible"}
        or any(value is not False for value in permissions.values())
    ):
        raise ValueError("overlay plan permissions must remain fail-closed")
    return payload


def decide_timing(
    *,
    side: Side,
    boundary_timestamp_ms: int,
    scenario_latency_ms: int,
    forecast: float | None,
    clean_forecast: bool,
) -> TimingDecision:
    """Apply the frozen symmetric timing rule to an existing immutable intent."""

    if not isinstance(side, Side) or side not in (Side.LONG, Side.SHORT):
        raise ValueError("timing overlay supports only LONG and SHORT intents")
    if (
        isinstance(scenario_latency_ms, bool)
        or not isinstance(scenario_latency_ms, int)
        or scenario_latency_ms < 0
    ):
        raise ValueError("scenario latency must be a non-negative integer")
    if isinstance(boundary_timestamp_ms, bool) or not isinstance(boundary_timestamp_ms, int):
        raise ValueError("boundary timestamp must be an integer")
    if boundary_timestamp_ms < 0 or boundary_timestamp_ms % QUARTER_HOUR_MS:
        raise ValueError("boundary timestamp must be a non-negative UTC quarter hour")
    if not isinstance(clean_forecast, bool):
        raise TypeError("clean_forecast must be a boolean")
    if forecast is not None and (
        isinstance(forecast, bool) or not isinstance(forecast, (int, float)) or not math.isfinite(forecast)
    ):
        raise ValueError("forecast must be finite or absent")

    if forecast is None:
        action = TimingAction.IMMEDIATE
        reason = "forecast_unavailable_base_fallback"
    elif not clean_forecast:
        action = TimingAction.IMMEDIATE
        reason = "forecast_dirty_base_fallback"
    else:
        direction = 1.0 if side is Side.LONG else -1.0
        if direction * float(forecast) < 0:
            action = TimingAction.DELAY
            reason = "clean_forecast_adverse"
        else:
            action = TimingAction.IMMEDIATE
            reason = "clean_forecast_aligned_or_zero"
    delay = DELAY_MS if action is TimingAction.DELAY else 0
    return TimingDecision(
        action=action,
        boundary_timestamp_ms=boundary_timestamp_ms,
        scenario_latency_ms=scenario_latency_ms,
        submission_timestamp_ms=boundary_timestamp_ms + delay + scenario_latency_ms,
        forecast=None if forecast is None else float(forecast),
        clean_forecast=clean_forecast,
        reason=reason,
    )


def direction_adjusted_entry_improvement_bps(
    *,
    side: Side,
    base_entry_price: float,
    candidate_entry_price: float,
) -> float:
    """Return positive bps only when the candidate receives the better entry."""

    if not isinstance(side, Side) or side not in (Side.LONG, Side.SHORT):
        raise ValueError("paired TCA supports only LONG and SHORT")
    for name, value in (
        ("base_entry_price", base_entry_price),
        ("candidate_entry_price", candidate_entry_price),
    ):
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
            or value <= 0
        ):
            raise ValueError(f"{name} must be finite and positive")
    direction = 1.0 if side is Side.LONG else -1.0
    result = (
        direction * (float(base_entry_price) - float(candidate_entry_price)) / float(base_entry_price) * _BPS
    )
    return 0.0 if result == 0 else result


def verify_parent_result(path: Path) -> ParentDisposition:
    """Verify immutable parent evidence before the overlay may access performance data."""

    load_plan()
    if not path.exists():
        return ParentDisposition.PENDING
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("parent result must be a JSON object")
    embedded_sha = payload.get("result_sha256")
    unsigned = dict(payload)
    unsigned.pop("result_sha256", None)
    if not isinstance(embedded_sha, str) or _logical_sha256(unsigned) != embedded_sha:
        raise ValueError("parent result logical hash mismatch")
    if payload.get("result_schema_version") != PARENT_RESULT_SCHEMA:
        raise ValueError("parent result schema mismatch")
    if payload.get("plan_sha256") != PARENT_PLAN_SHA256:
        raise ValueError("parent result plan binding mismatch")
    permissions = payload.get("permissions")
    if not isinstance(permissions, dict) or any(value is not False for value in permissions.values()):
        raise ValueError("parent result permissions must remain fail-closed")
    classification = payload.get("classification")
    if classification == PARENT_CONFIRMED:
        return ParentDisposition.ELIGIBLE
    if classification == PARENT_REJECTED:
        return ParentDisposition.NOT_RUN_PARENT_COMPONENT_REJECTED
    raise ValueError("parent result classification is not permitted by the v2 protocol")


__all__ = [
    "ParentDisposition",
    "TimingAction",
    "TimingDecision",
    "decide_timing",
    "direction_adjusted_entry_improvement_bps",
    "load_plan",
    "verify_parent_result",
]
