"""Fail-closed data roles and preregistration metadata for strategy research."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime, time, timedelta
from enum import StrEnum


class DataRole(StrEnum):
    """A dataset's immutable role in one research cycle."""

    RESEARCH = "research"
    SELECTION = "selection"
    ROBUSTNESS = "robustness"
    BLIND = "blind"


class ResearchPurpose(StrEnum):
    """The only operations that may consume a registered data window."""

    FIT = "fit"
    SELECT = "select"
    DIAGNOSE = "diagnose"
    PROMOTE = "promote"


_ALLOWED_PURPOSES = {
    DataRole.RESEARCH: frozenset({ResearchPurpose.FIT, ResearchPurpose.DIAGNOSE}),
    DataRole.SELECTION: frozenset({ResearchPurpose.SELECT}),
    DataRole.ROBUSTNESS: frozenset({ResearchPurpose.DIAGNOSE}),
    DataRole.BLIND: frozenset({ResearchPurpose.PROMOTE}),
}


def _is_lower_hex(value: str, length: int) -> bool:
    return (
        len(value) == length
        and value == value.lower()
        and all(character in "0123456789abcdef" for character in value)
    )


@dataclass(frozen=True, slots=True)
class DataWindow:
    """Inclusive UTC start and exclusive UTC end with a fixed research role."""

    name: str
    start: date
    end: date
    role: DataRole

    def __post_init__(self) -> None:
        if not self.name.strip() or self.start >= self.end:
            raise ValueError("data window requires a name and ordered boundaries")
        if self.start.day != 1 or self.end.day != 1:
            raise ValueError("data windows must use UTC month boundaries")


@dataclass(frozen=True, slots=True)
class ResearchProtocol:
    """Bind one candidate to its data roles, purge and trial budget.

    This is an application-level guard, not a substitute for keeping future
    blind data outside the research process. A blind window stays inaccessible
    unless the candidate is frozen and the caller supplies explicit
    authorization. Persisting and consuming that authorization exactly once
    belongs to the isolated evaluator, not this immutable registration object.
    """

    protocol_name: str
    universe: tuple[str, ...]
    windows: tuple[DataWindow, ...]
    max_trials: int
    maximum_holding_ms: int
    maximum_label_horizon_ms: int
    maximum_execution_latency_ms: int
    warmup_ms: int
    candidate_commit: str | None = None
    parameter_set_sha256: str | None = None
    frozen_at: datetime | None = None

    def __post_init__(self) -> None:
        if not self.protocol_name.strip():
            raise ValueError("protocol_name is required")
        if (
            not self.universe
            or len(set(self.universe)) != len(self.universe)
            or any(not symbol or symbol != symbol.strip().upper() for symbol in self.universe)
        ):
            raise ValueError("universe must contain unique normalized symbols")
        if not self.windows or len({window.name for window in self.windows}) != len(self.windows):
            raise ValueError("protocol requires uniquely named data windows")
        ordered = sorted(self.windows, key=lambda window: (window.start, window.end, window.name))
        if any(current.start < previous.end for previous, current in zip(ordered, ordered[1:], strict=False)):
            raise ValueError("data windows must not overlap")
        if isinstance(self.max_trials, bool) or not isinstance(self.max_trials, int) or self.max_trials <= 0:
            raise ValueError("max_trials must be positive")
        horizons = (
            self.maximum_holding_ms,
            self.maximum_label_horizon_ms,
            self.maximum_execution_latency_ms,
            self.warmup_ms,
        )
        if any(isinstance(value, bool) or not isinstance(value, int) or value < 0 for value in horizons):
            raise ValueError("research horizons must be non-negative integer milliseconds")
        if self.maximum_holding_ms <= 0 or self.maximum_label_horizon_ms <= 0:
            raise ValueError("holding and label horizons must be bounded and positive")
        freeze_fields = (self.candidate_commit, self.parameter_set_sha256, self.frozen_at)
        if any(value is not None for value in freeze_fields) and not all(
            value is not None for value in freeze_fields
        ):
            raise ValueError("candidate freeze metadata must be complete or absent")
        if self.candidate_commit is not None and not _is_lower_hex(self.candidate_commit, 40):
            raise ValueError("candidate_commit must be a full lowercase Git SHA")
        if self.parameter_set_sha256 is not None and not _is_lower_hex(self.parameter_set_sha256, 64):
            raise ValueError("parameter_set_sha256 must be a lowercase SHA-256")
        if self.frozen_at is not None:
            if self.frozen_at.tzinfo is None or self.frozen_at.utcoffset() is None:
                raise ValueError("frozen_at must be timezone-aware")
            if self.frozen_at.utcoffset() != timedelta(0):
                raise ValueError("frozen_at must be expressed in UTC")
        if self.frozen_at is not None:
            for window in self.windows:
                if window.role is DataRole.BLIND and self.frozen_at >= datetime.combine(
                    window.start, time.min, UTC
                ):
                    raise ValueError("candidate must be frozen before every blind window starts")

    @property
    def purge_ms(self) -> int:
        """Minimum purge: the longest outcome horizon plus execution latency."""

        return max(self.maximum_holding_ms, self.maximum_label_horizon_ms) + self.maximum_execution_latency_ms

    @property
    def is_frozen(self) -> bool:
        return self.frozen_at is not None

    def window(self, name: str) -> DataWindow:
        try:
            return next(window for window in self.windows if window.name == name)
        except StopIteration as exc:
            raise KeyError(f"unregistered data window: {name}") from exc

    def assert_access(
        self,
        name: str,
        purpose: ResearchPurpose,
        *,
        blind_authorized_at: datetime | None = None,
    ) -> DataWindow:
        """Return the window only when its immutable role permits the purpose."""

        window = self.window(name)
        if purpose not in _ALLOWED_PURPOSES[window.role]:
            raise PermissionError(f"{window.role.value} data cannot be used to {purpose.value}")
        if window.role is DataRole.BLIND:
            if not self.is_frozen or blind_authorized_at is None:
                raise PermissionError("blind promotion data is locked")
            if blind_authorized_at.tzinfo is None or blind_authorized_at.utcoffset() != timedelta(0):
                raise ValueError("blind authorization timestamp must be expressed in UTC")
            if blind_authorized_at < datetime.combine(window.end, time.min, UTC):
                raise PermissionError("blind observation period has not completed")
        return window

    def fingerprint(self) -> str:
        """Hash the complete object, including any post-selection freeze."""

        payload = asdict(self)
        payload["frozen_at"] = self.frozen_at.isoformat() if self.frozen_at is not None else None
        payload["windows"] = [
            {
                "name": window.name,
                "start": window.start.isoformat(),
                "end": window.end.isoformat(),
                "role": window.role.value,
            }
            for window in self.windows
        ]
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        return hashlib.sha256(encoded).hexdigest()

    def preregistration_fingerprint(self) -> str:
        """Hash only facts that can be fixed before any trial is evaluated.

        Candidate identity and its freeze timestamp are deliberately excluded:
        those facts can exist only after selection.  This digest is therefore
        suitable for binding every trial in a causally ordered experiment plan.
        A hash is an integrity identifier, not an external signature.
        """

        payload = asdict(self)
        for field_name in ("candidate_commit", "parameter_set_sha256", "frozen_at"):
            payload.pop(field_name)
        payload["windows"] = [
            {
                "name": window.name,
                "start": window.start.isoformat(),
                "end": window.end.isoformat(),
                "role": window.role.value,
            }
            for window in self.windows
        ]
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        return hashlib.sha256(encoded).hexdigest()


DEVELOPMENT_WINDOWS = (
    DataWindow("research", date(2021, 7, 1), date(2024, 7, 1), DataRole.RESEARCH),
    DataWindow("selection", date(2024, 7, 1), date(2025, 7, 1), DataRole.SELECTION),
    DataWindow("robustness", date(2025, 7, 1), date(2026, 8, 1), DataRole.ROBUSTNESS),
)
