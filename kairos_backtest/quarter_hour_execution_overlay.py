"""Fail-closed controls for the preregistered quarter-hour execution overlay."""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from pathlib import Path

from kairos_core.enums import Side

from .aggtrades import AggTrade

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


class EntryTickStatus(StrEnum):
    """Why an exact historical entry tick is or is not usable."""

    FOUND = "FOUND"
    GAP_TAINTED = "GAP_TAINTED"
    TIMEOUT = "TIMEOUT"


@dataclass(frozen=True, slots=True)
class EntryTickRequest:
    """One exact first-trade lookup within the frozen one-second deadline."""

    request_id: str
    submission_timestamp_ms: int

    def __post_init__(self) -> None:
        if not isinstance(self.request_id, str) or not self.request_id:
            raise ValueError("entry tick request_id must be a non-empty string")
        if (
            isinstance(self.submission_timestamp_ms, bool)
            or not isinstance(self.submission_timestamp_ms, int)
            or self.submission_timestamp_ms < 0
        ):
            raise ValueError("entry submission timestamp must be a non-negative integer")


@dataclass(frozen=True, slots=True)
class EntryTickResolution:
    """Immutable exact-tick evidence aligned one-to-one with a request."""

    request: EntryTickRequest
    status: EntryTickStatus
    aggregate_trade_id: int | None = None
    transact_time_ms: int | None = None
    price: Decimal | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.request, EntryTickRequest):
            raise TypeError("entry tick resolution requires an EntryTickRequest")
        if not isinstance(self.status, EntryTickStatus):
            raise TypeError("entry tick status must be an EntryTickStatus")
        evidence = (self.aggregate_trade_id, self.transact_time_ms, self.price)
        if self.status is EntryTickStatus.FOUND:
            if (
                isinstance(self.aggregate_trade_id, bool)
                or not isinstance(self.aggregate_trade_id, int)
                or self.aggregate_trade_id < 0
                or isinstance(self.transact_time_ms, bool)
                or not isinstance(self.transact_time_ms, int)
                or not self.request.submission_timestamp_ms
                <= self.transact_time_ms
                <= self.request.submission_timestamp_ms + 1_000
                or not isinstance(self.price, Decimal)
                or not self.price.is_finite()
                or self.price <= 0
            ):
                raise ValueError("found entry tick evidence is malformed or outside its deadline")
        elif any(value is not None for value in evidence):
            raise ValueError("unavailable entry ticks cannot contain synthetic evidence")


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


def extract_exact_entry_ticks(
    trades: Iterable[AggTrade],
    requests: Sequence[EntryTickRequest],
    *,
    coverage_end_ms: int,
) -> tuple[EntryTickResolution, ...]:
    """Resolve the first validated aggregate trade after each submission.

    The stream is consumed once. Aggregate-ID or raw-trade-ID gaps that could
    hide an earlier eligible trade mark that request as tainted; no value is
    filled, interpolated, or inferred from a later return window.
    """

    if isinstance(coverage_end_ms, bool) or not isinstance(coverage_end_ms, int) or coverage_end_ms < 0:
        raise ValueError("entry tick coverage end must be a non-negative integer")
    ordered_requests = tuple(
        sorted(requests, key=lambda item: (item.submission_timestamp_ms, item.request_id))
    )
    if len({item.request_id for item in ordered_requests}) != len(ordered_requests):
        raise ValueError("entry tick request IDs must be unique")
    if tuple(requests) != ordered_requests:
        raise ValueError("entry tick requests must be sorted by submission time and identity")
    if any(item.submission_timestamp_ms + 1_000 > coverage_end_ms for item in ordered_requests):
        raise ValueError("entry tick coverage must include every request deadline")

    resolutions: list[EntryTickResolution] = []
    request_index = 0
    previous: AggTrade | None = None
    for trade in trades:
        if not isinstance(trade, AggTrade):
            raise TypeError("entry tick stream must contain AggTrade values")
        if trade.transact_time_ms >= coverage_end_ms:
            raise ValueError("entry tick trade lies outside declared coverage")
        if previous is not None:
            if trade.aggregate_trade_id <= previous.aggregate_trade_id:
                raise ValueError("entry tick aggregate trades must be strictly ID ordered")
            if trade.transact_time_ms < previous.transact_time_ms:
                raise ValueError("entry tick aggregate trades must be timestamp ordered")
            if trade.first_trade_id <= previous.last_trade_id:
                raise ValueError("entry tick raw trade ranges must be strictly ordered")

        while (
            request_index < len(ordered_requests)
            and ordered_requests[request_index].submission_timestamp_ms <= trade.transact_time_ms
        ):
            request = ordered_requests[request_index]
            deadline = request.submission_timestamp_ms + 1_000
            missing_aggregate = (
                previous is not None and trade.aggregate_trade_id > previous.aggregate_trade_id + 1
            )
            missing_raw = previous is not None and trade.first_trade_id > previous.last_trade_id + 1
            predecessor_unproven = previous is None
            gap_crosses_request_window = (
                previous is not None
                and previous.transact_time_ms < deadline
                and trade.transact_time_ms >= request.submission_timestamp_ms
                and (missing_aggregate or missing_raw)
            )
            if predecessor_unproven or gap_crosses_request_window:
                status = EntryTickStatus.GAP_TAINTED
            elif trade.transact_time_ms > deadline:
                status = EntryTickStatus.TIMEOUT
            else:
                resolutions.append(
                    EntryTickResolution(
                        request=request,
                        status=EntryTickStatus.FOUND,
                        aggregate_trade_id=trade.aggregate_trade_id,
                        transact_time_ms=trade.transact_time_ms,
                        price=trade.price,
                    )
                )
                request_index += 1
                continue
            resolutions.append(EntryTickResolution(request=request, status=status))
            request_index += 1
        previous = trade

    resolutions.extend(
        EntryTickResolution(request=request, status=EntryTickStatus.TIMEOUT)
        for request in ordered_requests[request_index:]
    )
    if tuple(item.request for item in resolutions) != ordered_requests:
        raise RuntimeError("entry tick extraction lost request ordering")
    return tuple(resolutions)


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
    "EntryTickRequest",
    "EntryTickResolution",
    "EntryTickStatus",
    "ParentDisposition",
    "TimingAction",
    "TimingDecision",
    "decide_timing",
    "direction_adjusted_entry_improvement_bps",
    "extract_exact_entry_ticks",
    "load_plan",
    "verify_parent_result",
]
