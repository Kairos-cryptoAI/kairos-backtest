"""Append-only, rollback-detecting registry for strategy research trials.

A final selection or explicit rejection of every trial creates a durable
sidecar seal.  The seal detects deletion or rollback of the registry alone and
an OS lock serializes cooperating writers.
It is not an adversarial signature: an actor able to rewrite both files can
forge both hash chains.  Long-lived evidence therefore still needs its exported
seal anchored by signed Git history or another external immutable store.
"""

from __future__ import annotations

import hashlib
import importlib
import json
import math
import os
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date
from enum import StrEnum
from pathlib import Path
from typing import Protocol, cast

from .robustness import DailyReturnSeries

SCHEMA_VERSION = 1
REJECTION_RECORD_SCHEMA_VERSION = 2
ANCHOR_SCHEMA_VERSION = 2
GENESIS_HASH = "0" * 64
_ENVELOPE_KEYS = {
    "schema_version",
    "line_sequence",
    "record_type",
    "previous_hash",
    "payload",
    "record_hash",
}
_TRIAL_PAYLOAD_KEYS = {
    "trial_id",
    "status",
    "fingerprints",
    "daily_returns",
    "failure_class",
}


class _MsvcrtModule(Protocol):
    LK_LOCK: int
    LK_UNLCK: int

    @staticmethod
    def locking(descriptor: int, mode: int, byte_count: int) -> None: ...


_SELECTION_PAYLOAD_KEYS = {
    "selected_trial_id",
    "selected_trial_record_hash",
    "candidate_sha256",
}
_REJECTION_PAYLOAD_KEYS = {
    "outcome",
    "trial_count",
    "trial_head_hash",
    "rejection_sha256",
}
_FINGERPRINT_KEYS = {
    "protocol_sha256",
    "config_sha256",
    "code_sha256",
    "data_sha256",
    "dependency_sha256",
    "container_sha256",
}
_DAILY_RETURN_KEYS = {"dates", "returns"}
_LEGACY_ANCHOR_KEYS = {"schema_version", "line_count", "head_hash", "candidate_sha256"}
_ANCHOR_KEYS = {
    "schema_version",
    "line_count",
    "head_hash",
    "outcome",
    "candidate_sha256",
    "rejection_sha256",
}


class RegistryIntegrityError(ValueError):
    """Raised when persisted registry evidence cannot be verified."""


class RegistryFrozenError(RuntimeError):
    """Raised when a caller attempts to append after a terminal outcome."""


class RegistryConcurrentModificationError(RuntimeError):
    """Raised when the file changed after its chain was verified."""


class TrialStatus(StrEnum):
    SUCCESS = "SUCCESS"
    FAILURE = "FAILURE"


class RegistryOutcome(StrEnum):
    """Typed terminal outcomes for a completed trial registry."""

    SELECTION = "SELECTION"
    REJECT_ALL = "REJECT_ALL"


class FailureClass(StrEnum):
    """Sanitized failure categories; exception messages are never persisted."""

    DATA = "DATA"
    CONFIGURATION = "CONFIGURATION"
    STRATEGY = "STRATEGY"
    NUMERICAL = "NUMERICAL"
    EXECUTION_SIMULATION = "EXECUTION_SIMULATION"
    RESOURCE = "RESOURCE"
    DEPENDENCY = "DEPENDENCY"
    INFRASTRUCTURE = "INFRASTRUCTURE"
    VALIDATION = "VALIDATION"
    UNKNOWN = "UNKNOWN"


class _RecordType(StrEnum):
    TRIAL = "TRIAL"
    SELECTION = "SELECTION"
    REJECTION = "REJECTION"


def _is_sha256(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and value == value.lower()
        and all(character in "0123456789abcdef" for character in value)
    )


def _canonical_json(value: object) -> bytes:
    try:
        rendered = json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise ValueError("registry values must be canonically JSON serializable") from exc
    return rendered.encode("utf-8")


def _sha256(value: object) -> str:
    return hashlib.sha256(_canonical_json(value)).hexdigest()


def _mapping(value: object, *, name: str) -> dict[str, object]:
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise RegistryIntegrityError(f"{name} must be a JSON object")
    return cast(dict[str, object], value)


def _exact_keys(value: dict[str, object], expected: set[str], *, name: str) -> None:
    if set(value) != expected:
        raise RegistryIntegrityError(f"{name} has an unexpected schema")


@dataclass(frozen=True, slots=True)
class TrialFingerprints:
    protocol_sha256: str
    config_sha256: str
    code_sha256: str
    data_sha256: str
    dependency_sha256: str
    container_sha256: str

    def __post_init__(self) -> None:
        if any(not _is_sha256(value) for value in self.as_dict().values()):
            raise ValueError("every trial fingerprint must be a full lowercase SHA-256")

    def as_dict(self) -> dict[str, str]:
        return {
            "protocol_sha256": self.protocol_sha256,
            "config_sha256": self.config_sha256,
            "code_sha256": self.code_sha256,
            "data_sha256": self.data_sha256,
            "dependency_sha256": self.dependency_sha256,
            "container_sha256": self.container_sha256,
        }


@dataclass(frozen=True, slots=True)
class TrialRecord:
    line_sequence: int
    trial_id: int
    status: TrialStatus
    fingerprints: TrialFingerprints
    daily_returns: DailyReturnSeries | None
    failure_class: FailureClass | None
    previous_hash: str
    record_hash: str


@dataclass(frozen=True, slots=True)
class SelectionRecord:
    line_sequence: int
    selected_trial_id: int
    selected_trial_record_hash: str
    candidate_sha256: str
    previous_hash: str
    record_hash: str

    @property
    def outcome(self) -> RegistryOutcome:
        return RegistryOutcome.SELECTION


@dataclass(frozen=True, slots=True)
class RejectionRecord:
    """Terminal proof that the recorded trial set produced no candidate."""

    line_sequence: int
    trial_count: int
    trial_head_hash: str
    rejection_sha256: str
    previous_hash: str
    record_hash: str

    @property
    def outcome(self) -> RegistryOutcome:
        return RegistryOutcome.REJECT_ALL


@dataclass(frozen=True, slots=True)
class RegistrySnapshot:
    trials: tuple[TrialRecord, ...]
    selection: SelectionRecord | None
    head_hash: str
    byte_length: int
    sealed_anchor: SealedRegistryAnchor | None = None
    rejection: RejectionRecord | None = None

    @property
    def terminal_record(self) -> SelectionRecord | RejectionRecord | None:
        return self.selection if self.selection is not None else self.rejection

    @property
    def outcome(self) -> RegistryOutcome | None:
        terminal = self.terminal_record
        return terminal.outcome if terminal is not None else None

    @property
    def frozen(self) -> bool:
        return self.terminal_record is not None


@dataclass(frozen=True, slots=True)
class SealedRegistryAnchor:
    """Durable local seal suitable for export into signed external history."""

    schema_version: int
    line_count: int
    head_hash: str
    candidate_sha256: str | None
    outcome: RegistryOutcome = RegistryOutcome.SELECTION
    rejection_sha256: str | None = None

    def __post_init__(self) -> None:
        if (
            isinstance(self.schema_version, bool)
            or not isinstance(self.schema_version, int)
            or self.schema_version not in {SCHEMA_VERSION, ANCHOR_SCHEMA_VERSION}
        ):
            raise ValueError("sealed anchor schema version is unsupported")
        if isinstance(self.line_count, bool) or not isinstance(self.line_count, int) or self.line_count < 1:
            raise ValueError("sealed anchor line_count must be a positive integer")
        if not _is_sha256(self.head_hash):
            raise ValueError("sealed anchor head hash must be a full lowercase SHA-256")
        if not isinstance(self.outcome, RegistryOutcome):
            raise TypeError("sealed anchor outcome must be a RegistryOutcome")
        if self.schema_version == SCHEMA_VERSION and (
            self.outcome is not RegistryOutcome.SELECTION or self.rejection_sha256 is not None
        ):
            raise ValueError("legacy sealed anchors support selection only")
        if self.outcome is RegistryOutcome.SELECTION:
            if not _is_sha256(self.candidate_sha256) or self.rejection_sha256 is not None:
                raise ValueError("selection anchors require only a candidate SHA-256")
        elif self.candidate_sha256 is not None or not _is_sha256(self.rejection_sha256):
            raise ValueError("reject-all anchors require only a rejection SHA-256")

    @property
    def terminal_sha256(self) -> str:
        """Return the digest for the typed terminal outcome."""

        if self.outcome is RegistryOutcome.SELECTION:
            return cast(str, self.candidate_sha256)
        return cast(str, self.rejection_sha256)

    def as_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "schema_version": self.schema_version,
            "line_count": self.line_count,
            "head_hash": self.head_hash,
            "candidate_sha256": self.candidate_sha256,
        }
        if self.schema_version == ANCHOR_SCHEMA_VERSION:
            payload.update(
                {
                    "outcome": self.outcome.value,
                    "rejection_sha256": self.rejection_sha256,
                }
            )
        return payload


def _fingerprints_from_json(value: object) -> TrialFingerprints:
    payload = _mapping(value, name="fingerprints")
    _exact_keys(payload, _FINGERPRINT_KEYS, name="fingerprints")
    if any(not isinstance(payload[key], str) for key in _FINGERPRINT_KEYS):
        raise RegistryIntegrityError("fingerprints must be strings")
    try:
        return TrialFingerprints(
            protocol_sha256=cast(str, payload["protocol_sha256"]),
            config_sha256=cast(str, payload["config_sha256"]),
            code_sha256=cast(str, payload["code_sha256"]),
            data_sha256=cast(str, payload["data_sha256"]),
            dependency_sha256=cast(str, payload["dependency_sha256"]),
            container_sha256=cast(str, payload["container_sha256"]),
        )
    except ValueError as exc:
        raise RegistryIntegrityError(str(exc)) from exc


def _anchor_from_json(value: object) -> SealedRegistryAnchor:
    payload = _mapping(value, name="sealed anchor")
    keys = set(payload)
    if keys == _LEGACY_ANCHOR_KEYS:
        if payload["schema_version"] != SCHEMA_VERSION:
            raise RegistryIntegrityError("sealed anchor schema does not match its version")
        outcome = RegistryOutcome.SELECTION
        rejection_sha256 = None
    elif keys == _ANCHOR_KEYS:
        if payload["schema_version"] != ANCHOR_SCHEMA_VERSION:
            raise RegistryIntegrityError("sealed anchor schema does not match its version")
        raw_outcome = payload["outcome"]
        if not isinstance(raw_outcome, str):
            raise RegistryIntegrityError("sealed anchor outcome is invalid")
        try:
            outcome = RegistryOutcome(raw_outcome)
        except ValueError as exc:
            raise RegistryIntegrityError("sealed anchor outcome is invalid") from exc
        raw_rejection_sha256 = payload["rejection_sha256"]
        if raw_rejection_sha256 is not None and not isinstance(raw_rejection_sha256, str):
            raise RegistryIntegrityError("sealed anchor rejection hash is invalid")
        rejection_sha256 = cast(str | None, raw_rejection_sha256)
    else:
        raise RegistryIntegrityError("sealed anchor has an unexpected schema")
    raw_candidate_sha256 = payload["candidate_sha256"]
    if raw_candidate_sha256 is not None and not isinstance(raw_candidate_sha256, str):
        raise RegistryIntegrityError("sealed anchor candidate hash is invalid")
    try:
        return SealedRegistryAnchor(
            schema_version=cast(int, payload["schema_version"]),
            line_count=cast(int, payload["line_count"]),
            head_hash=cast(str, payload["head_hash"]),
            candidate_sha256=cast(str | None, raw_candidate_sha256),
            outcome=outcome,
            rejection_sha256=rejection_sha256,
        )
    except (TypeError, ValueError) as exc:
        raise RegistryIntegrityError(str(exc)) from exc


def _daily_returns_to_json(series: DailyReturnSeries) -> dict[str, object]:
    return {
        "dates": [day.isoformat() for day in series.dates],
        "returns": [float(value) for value in series.returns],
    }


def _daily_returns_from_json(value: object) -> DailyReturnSeries:
    payload = _mapping(value, name="daily_returns")
    _exact_keys(payload, _DAILY_RETURN_KEYS, name="daily_returns")
    raw_dates = payload["dates"]
    raw_returns = payload["returns"]
    if not isinstance(raw_dates, list) or not isinstance(raw_returns, list):
        raise RegistryIntegrityError("daily return dates and returns must be JSON arrays")
    if any(not isinstance(item, str) for item in raw_dates):
        raise RegistryIntegrityError("daily return dates must be ISO date strings")
    try:
        invalid_return = any(
            isinstance(item, bool) or not isinstance(item, (int, float)) or not math.isfinite(item)
            for item in raw_returns
        )
    except OverflowError as exc:
        raise RegistryIntegrityError("daily return values must be finite numbers") from exc
    if invalid_return:
        raise RegistryIntegrityError("daily return values must be finite numbers")
    try:
        dates = tuple(date.fromisoformat(cast(str, item)) for item in raw_dates)
        returns = tuple(float(cast(int | float, item)) for item in raw_returns)
        return DailyReturnSeries(dates, returns)
    except (OverflowError, TypeError, ValueError) as exc:
        raise RegistryIntegrityError("daily return evidence is invalid") from exc


def _trial_payload(
    trial_id: int,
    status: TrialStatus,
    fingerprints: TrialFingerprints,
    daily_returns: DailyReturnSeries | None,
    failure_class: FailureClass | None,
) -> dict[str, object]:
    return {
        "trial_id": trial_id,
        "status": status.value,
        "fingerprints": fingerprints.as_dict(),
        "daily_returns": (_daily_returns_to_json(daily_returns) if daily_returns is not None else None),
        "failure_class": failure_class.value if failure_class is not None else None,
    }


def _candidate_sha256(trial: TrialRecord) -> str:
    return _sha256(
        {
            "candidate_schema_version": SCHEMA_VERSION,
            "selected_trial_id": trial.trial_id,
            "selected_trial_record_hash": trial.record_hash,
            "fingerprints": trial.fingerprints.as_dict(),
        }
    )


def _rejection_sha256(trials: tuple[TrialRecord, ...]) -> str:
    if not trials:
        raise ValueError("cannot fingerprint an empty rejected trial set")
    return _sha256(
        {
            "outcome_schema_version": REJECTION_RECORD_SCHEMA_VERSION,
            "outcome": RegistryOutcome.REJECT_ALL.value,
            "trial_count": len(trials),
            "trial_head_hash": trials[-1].record_hash,
            "trial_record_hashes": [trial.record_hash for trial in trials],
        }
    )


def _fsync_directory(path: Path) -> None:
    """Persist a newly created directory entry on platforms that support it."""

    if os.name == "nt":
        return
    descriptor: int | None = None
    try:
        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        descriptor = os.open(path, flags)
        os.fsync(descriptor)
    except OSError:
        # Directory fsync is unavailable on some otherwise POSIX filesystems.
        return
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _acquire_descriptor_lock(descriptor: int) -> None:
    if os.name == "nt":
        msvcrt = cast(_MsvcrtModule, importlib.import_module("msvcrt"))
        os.lseek(descriptor, 0, os.SEEK_SET)
        msvcrt.locking(descriptor, msvcrt.LK_LOCK, 1)
        return
    fcntl = importlib.import_module("fcntl")
    fcntl.flock(descriptor, fcntl.LOCK_EX)


def _release_descriptor_lock(descriptor: int) -> None:
    if os.name == "nt":
        msvcrt = cast(_MsvcrtModule, importlib.import_module("msvcrt"))
        os.lseek(descriptor, 0, os.SEEK_SET)
        msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
        return
    fcntl = importlib.import_module("fcntl")
    fcntl.flock(descriptor, fcntl.LOCK_UN)


class TrialRegistry:
    """Verified JSONL registry with serialized cooperating writers."""

    def __init__(self, path: Path) -> None:
        if not isinstance(path, Path):
            raise TypeError("registry path must be a pathlib.Path")
        if not path.name:
            raise ValueError("registry path must identify a file")
        self.path = path
        self.anchor_path = path.with_name(f"{path.name}.anchor.json")
        self.lock_path = path.with_name(f"{path.name}.lock")

    def read(self) -> RegistrySnapshot:
        """Read and verify canonical serialization, sequence and the full hash chain."""

        with self._exclusive_lock():
            return self._read_unlocked()

    def export_anchor(self) -> SealedRegistryAnchor:
        """Return the verified local seal for external signed anchoring."""

        snapshot = self.read()
        if snapshot.sealed_anchor is None:
            raise RegistryIntegrityError("registry has no finalized sealed anchor to export")
        return snapshot.sealed_anchor

    def _read_unlocked(self) -> RegistrySnapshot:
        anchor = self._read_anchor_unlocked()

        try:
            raw = self.path.read_bytes()
        except FileNotFoundError:
            if anchor is not None:
                raise RegistryIntegrityError("sealed registry file is missing") from None
            return RegistrySnapshot((), None, GENESIS_HASH, 0)
        except OSError as exc:
            raise RegistryIntegrityError("registry cannot be read") from exc
        if not raw:
            if anchor is not None:
                raise RegistryIntegrityError("sealed registry cannot be empty")
            return RegistrySnapshot((), None, GENESIS_HASH, 0)
        if not raw.endswith(b"\n"):
            raise RegistryIntegrityError("registry is truncated or missing its final newline")

        raw_lines = raw.split(b"\n")[:-1]
        if not raw_lines or any(not line for line in raw_lines):
            raise RegistryIntegrityError("registry contains an empty JSONL record")
        trials: list[TrialRecord] = []
        selection: SelectionRecord | None = None
        rejection: RejectionRecord | None = None
        previous_hash = GENESIS_HASH
        seen_hashes: set[str] = set()
        for expected_line_sequence, raw_line in enumerate(raw_lines, start=1):
            try:
                decoded = raw_line.decode("utf-8")
                parsed = json.loads(decoded)
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise RegistryIntegrityError("registry contains invalid canonical JSON") from exc
            envelope = _mapping(parsed, name="registry record")
            _exact_keys(envelope, _ENVELOPE_KEYS, name="registry record")
            try:
                canonical_envelope = _canonical_json(envelope)
            except ValueError as exc:
                raise RegistryIntegrityError("registry contains non-finite JSON values") from exc
            if canonical_envelope != raw_line:
                raise RegistryIntegrityError("registry record is not canonically serialized")
            schema_version = envelope["schema_version"]
            if (
                isinstance(schema_version, bool)
                or not isinstance(schema_version, int)
                or schema_version not in {SCHEMA_VERSION, REJECTION_RECORD_SCHEMA_VERSION}
            ):
                raise RegistryIntegrityError("registry schema version is unsupported")
            line_sequence = envelope["line_sequence"]
            if (
                isinstance(line_sequence, bool)
                or not isinstance(line_sequence, int)
                or line_sequence != expected_line_sequence
            ):
                raise RegistryIntegrityError("registry line sequence is not contiguous")
            persisted_previous_hash = envelope["previous_hash"]
            record_hash = envelope["record_hash"]
            if not _is_sha256(persisted_previous_hash) or not _is_sha256(record_hash):
                raise RegistryIntegrityError("registry chain hashes must be full lowercase SHA-256")
            if persisted_previous_hash != previous_hash:
                raise RegistryIntegrityError("registry previous hash does not match the chain head")
            if record_hash in seen_hashes:
                raise RegistryIntegrityError("registry contains a duplicate record hash")
            body = {key: value for key, value in envelope.items() if key != "record_hash"}
            try:
                computed_record_hash = _sha256(body)
            except ValueError as exc:
                raise RegistryIntegrityError("registry record cannot be hashed canonically") from exc
            if computed_record_hash != record_hash:
                raise RegistryIntegrityError("registry record hash verification failed")

            raw_record_type = envelope["record_type"]
            if not isinstance(raw_record_type, str):
                raise RegistryIntegrityError("registry record type is invalid")
            try:
                record_type = _RecordType(raw_record_type)
            except (TypeError, ValueError) as exc:
                raise RegistryIntegrityError("registry record type is invalid") from exc
            payload = _mapping(envelope["payload"], name="record payload")
            if (
                record_type is _RecordType.REJECTION and schema_version != REJECTION_RECORD_SCHEMA_VERSION
            ) or (record_type is not _RecordType.REJECTION and schema_version != SCHEMA_VERSION):
                raise RegistryIntegrityError("registry record type does not match its schema version")
            if selection is not None or rejection is not None:
                raise RegistryIntegrityError("no records may follow a terminal outcome")
            if record_type is _RecordType.TRIAL:
                _exact_keys(payload, _TRIAL_PAYLOAD_KEYS, name="trial payload")
                expected_trial_id = len(trials) + 1
                trial_id = payload["trial_id"]
                if (
                    isinstance(trial_id, bool)
                    or not isinstance(trial_id, int)
                    or trial_id != expected_trial_id
                ):
                    raise RegistryIntegrityError("trial_id sequence is not contiguous")
                raw_status = payload["status"]
                if not isinstance(raw_status, str):
                    raise RegistryIntegrityError("trial status must be SUCCESS or FAILURE")
                try:
                    status = TrialStatus(raw_status)
                except (TypeError, ValueError) as exc:
                    raise RegistryIntegrityError("trial status must be SUCCESS or FAILURE") from exc
                fingerprints = _fingerprints_from_json(payload["fingerprints"])
                if status is TrialStatus.SUCCESS:
                    if payload["daily_returns"] is None or payload["failure_class"] is not None:
                        raise RegistryIntegrityError(
                            "successful trials require returns and forbid a failure class"
                        )
                    daily_returns = _daily_returns_from_json(payload["daily_returns"])
                    failure_class = None
                else:
                    if payload["daily_returns"] is not None or not isinstance(payload["failure_class"], str):
                        raise RegistryIntegrityError("failed trials require only a sanitized failure class")
                    try:
                        failure_class = FailureClass(payload["failure_class"])
                    except ValueError as exc:
                        raise RegistryIntegrityError("failure class is not sanitized") from exc
                    daily_returns = None
                trials.append(
                    TrialRecord(
                        line_sequence=line_sequence,
                        trial_id=trial_id,
                        status=status,
                        fingerprints=fingerprints,
                        daily_returns=daily_returns,
                        failure_class=failure_class,
                        previous_hash=cast(str, persisted_previous_hash),
                        record_hash=cast(str, record_hash),
                    )
                )
            elif record_type is _RecordType.SELECTION:
                _exact_keys(payload, _SELECTION_PAYLOAD_KEYS, name="selection payload")
                selected_trial_id = payload["selected_trial_id"]
                selected_trial_record_hash = payload["selected_trial_record_hash"]
                candidate_sha256 = payload["candidate_sha256"]
                if (
                    isinstance(selected_trial_id, bool)
                    or not isinstance(selected_trial_id, int)
                    or selected_trial_id < 1
                    or not _is_sha256(selected_trial_record_hash)
                    or not _is_sha256(candidate_sha256)
                ):
                    raise RegistryIntegrityError("selection references are invalid")
                if selected_trial_id > len(trials):
                    raise RegistryIntegrityError("selection references a missing trial")
                selected_trial = trials[selected_trial_id - 1]
                if selected_trial.status is not TrialStatus.SUCCESS:
                    raise RegistryIntegrityError("selection references a failed trial")
                if selected_trial.record_hash != selected_trial_record_hash:
                    raise RegistryIntegrityError("selection trial hash does not match")
                if _candidate_sha256(selected_trial) != candidate_sha256:
                    raise RegistryIntegrityError("selection candidate fingerprint does not match")
                selection = SelectionRecord(
                    line_sequence=line_sequence,
                    selected_trial_id=selected_trial_id,
                    selected_trial_record_hash=cast(str, selected_trial_record_hash),
                    candidate_sha256=cast(str, candidate_sha256),
                    previous_hash=cast(str, persisted_previous_hash),
                    record_hash=cast(str, record_hash),
                )
            else:
                _exact_keys(payload, _REJECTION_PAYLOAD_KEYS, name="rejection payload")
                raw_outcome = payload["outcome"]
                trial_count = payload["trial_count"]
                trial_head_hash = payload["trial_head_hash"]
                rejection_sha256 = payload["rejection_sha256"]
                if (
                    raw_outcome != RegistryOutcome.REJECT_ALL.value
                    or isinstance(trial_count, bool)
                    or not isinstance(trial_count, int)
                    or trial_count < 1
                    or not _is_sha256(trial_head_hash)
                    or not _is_sha256(rejection_sha256)
                ):
                    raise RegistryIntegrityError("rejection outcome evidence is invalid")
                if trial_count != len(trials):
                    raise RegistryIntegrityError("rejection trial count does not match the registry")
                if trial_head_hash != persisted_previous_hash:
                    raise RegistryIntegrityError("rejection trial head does not match the chain")
                expected_rejection_sha256 = _rejection_sha256(tuple(trials))
                if rejection_sha256 != expected_rejection_sha256:
                    raise RegistryIntegrityError("rejection fingerprint does not match the trial set")
                rejection = RejectionRecord(
                    line_sequence=line_sequence,
                    trial_count=trial_count,
                    trial_head_hash=cast(str, trial_head_hash),
                    rejection_sha256=cast(str, rejection_sha256),
                    previous_hash=cast(str, persisted_previous_hash),
                    record_hash=cast(str, record_hash),
                )
            seen_hashes.add(cast(str, record_hash))
            previous_hash = cast(str, record_hash)
        terminal_record = selection if selection is not None else rejection
        if terminal_record is None and anchor is not None:
            raise RegistryIntegrityError("sealed anchor does not match an open registry")
        if selection is not None and anchor is None:
            raise RegistryIntegrityError("final selection is missing its sealed anchor")
        if rejection is not None and anchor is None:
            raise RegistryIntegrityError("final rejection is missing its sealed anchor")
        if anchor is not None and (
            anchor.line_count != len(raw_lines)
            or anchor.head_hash != previous_hash
            or terminal_record is None
        ):
            raise RegistryIntegrityError("sealed anchor does not match the registry head")
        if (
            selection is not None
            and anchor is not None
            and (
                anchor.outcome is not RegistryOutcome.SELECTION
                or anchor.candidate_sha256 != selection.candidate_sha256
                or anchor.rejection_sha256 is not None
            )
        ):
            raise RegistryIntegrityError("sealed anchor does not match the final selection")
        if (
            rejection is not None
            and anchor is not None
            and (
                anchor.outcome is not RegistryOutcome.REJECT_ALL
                or anchor.candidate_sha256 is not None
                or anchor.rejection_sha256 != rejection.rejection_sha256
            )
        ):
            raise RegistryIntegrityError("sealed anchor does not match the final rejection")
        return RegistrySnapshot(
            trials=tuple(trials),
            selection=selection,
            head_hash=previous_hash,
            byte_length=len(raw),
            sealed_anchor=anchor,
            rejection=rejection,
        )

    def append_success(
        self,
        fingerprints: TrialFingerprints,
        daily_returns: DailyReturnSeries,
    ) -> TrialRecord:
        """Append one successful attempt including its complete daily return series."""

        if not isinstance(fingerprints, TrialFingerprints):
            raise TypeError("fingerprints must be TrialFingerprints")
        if not isinstance(daily_returns, DailyReturnSeries):
            raise TypeError("daily_returns must be a DailyReturnSeries")
        with self._exclusive_lock():
            snapshot = self._read_unlocked()
            self._assert_open(snapshot)
            trial_id = len(snapshot.trials) + 1
            record_hash = self._append_record(
                snapshot,
                _RecordType.TRIAL,
                _trial_payload(
                    trial_id,
                    TrialStatus.SUCCESS,
                    fingerprints,
                    daily_returns,
                    None,
                ),
            )
            verified = self._read_unlocked()
            appended = verified.trials[-1]
            if appended.record_hash != record_hash or appended.trial_id != trial_id:
                raise RegistryIntegrityError("appended successful trial could not be verified")
            return appended

    def append_failure(
        self,
        fingerprints: TrialFingerprints,
        failure_class: FailureClass,
    ) -> TrialRecord:
        """Append one failed attempt without persisting exception text or secrets."""

        if not isinstance(fingerprints, TrialFingerprints):
            raise TypeError("fingerprints must be TrialFingerprints")
        if not isinstance(failure_class, FailureClass):
            raise TypeError("failure_class must be a sanitized FailureClass")
        with self._exclusive_lock():
            snapshot = self._read_unlocked()
            self._assert_open(snapshot)
            trial_id = len(snapshot.trials) + 1
            record_hash = self._append_record(
                snapshot,
                _RecordType.TRIAL,
                _trial_payload(
                    trial_id,
                    TrialStatus.FAILURE,
                    fingerprints,
                    None,
                    failure_class,
                ),
            )
            verified = self._read_unlocked()
            appended = verified.trials[-1]
            if appended.record_hash != record_hash or appended.trial_id != trial_id:
                raise RegistryIntegrityError("appended failed trial could not be verified")
            return appended

    def finalize_selection(self, selected_trial_id: int) -> SelectionRecord:
        """Append the immutable final candidate selection and freeze the registry."""

        if (
            isinstance(selected_trial_id, bool)
            or not isinstance(selected_trial_id, int)
            or selected_trial_id < 1
        ):
            raise ValueError("selected_trial_id must be a positive integer")
        with self._exclusive_lock():
            snapshot = self._read_unlocked()
            self._assert_open(snapshot)
            if selected_trial_id > len(snapshot.trials):
                raise ValueError("selection references a missing trial")
            selected = snapshot.trials[selected_trial_id - 1]
            if selected.status is not TrialStatus.SUCCESS:
                raise ValueError("selection must reference a successful trial")
            candidate_sha256 = _candidate_sha256(selected)
            record_hash = self._append_record(
                snapshot,
                _RecordType.SELECTION,
                {
                    "selected_trial_id": selected.trial_id,
                    "selected_trial_record_hash": selected.record_hash,
                    "candidate_sha256": candidate_sha256,
                },
            )
            anchor = SealedRegistryAnchor(
                schema_version=SCHEMA_VERSION,
                line_count=len(snapshot.trials) + 1,
                head_hash=record_hash,
                candidate_sha256=candidate_sha256,
            )
            self._write_anchor(anchor)
            verified = self._read_unlocked()
            appended = verified.selection
            if (
                appended is None
                or appended.record_hash != record_hash
                or appended.candidate_sha256 != candidate_sha256
                or verified.sealed_anchor != anchor
            ):
                raise RegistryIntegrityError("appended final selection could not be verified")
            return appended

    def finalize_rejection(self) -> RejectionRecord:
        """Append a sealed REJECT_ALL outcome without selecting a trial."""

        with self._exclusive_lock():
            snapshot = self._read_unlocked()
            self._assert_open(snapshot)
            if not snapshot.trials:
                raise ValueError("cannot reject all without at least one recorded trial")
            rejection_sha256 = _rejection_sha256(snapshot.trials)
            record_hash = self._append_record(
                snapshot,
                _RecordType.REJECTION,
                {
                    "outcome": RegistryOutcome.REJECT_ALL.value,
                    "trial_count": len(snapshot.trials),
                    "trial_head_hash": snapshot.head_hash,
                    "rejection_sha256": rejection_sha256,
                },
                schema_version=REJECTION_RECORD_SCHEMA_VERSION,
            )
            anchor = SealedRegistryAnchor(
                schema_version=ANCHOR_SCHEMA_VERSION,
                line_count=len(snapshot.trials) + 1,
                head_hash=record_hash,
                candidate_sha256=None,
                outcome=RegistryOutcome.REJECT_ALL,
                rejection_sha256=rejection_sha256,
            )
            self._write_anchor(anchor)
            verified = self._read_unlocked()
            appended = verified.rejection
            if (
                appended is None
                or appended.record_hash != record_hash
                or appended.rejection_sha256 != rejection_sha256
                or verified.sealed_anchor != anchor
                or verified.selection is not None
            ):
                raise RegistryIntegrityError("appended final rejection could not be verified")
            return appended

    @staticmethod
    def _assert_open(snapshot: RegistrySnapshot) -> None:
        if snapshot.frozen:
            raise RegistryFrozenError("registry is frozen by its terminal outcome record")

    @contextmanager
    def _exclusive_lock(self) -> Iterator[None]:
        """Serialize cooperating readers and writers across processes."""

        if not self.path.parent.is_dir():
            raise FileNotFoundError(f"registry parent directory does not exist: {self.path.parent}")
        flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_BINARY", 0)
        descriptor = os.open(self.lock_path, flags, 0o600)
        locked = False
        try:
            if os.fstat(descriptor).st_size == 0:
                os.write(descriptor, b"\0")
            _acquire_descriptor_lock(descriptor)
            locked = True
            yield
        finally:
            if locked:
                _release_descriptor_lock(descriptor)
            os.close(descriptor)

    def _read_anchor_unlocked(self) -> SealedRegistryAnchor | None:
        try:
            raw = self.anchor_path.read_bytes()
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise RegistryIntegrityError("sealed anchor cannot be read") from exc
        if not raw or not raw.endswith(b"\n") or raw.count(b"\n") != 1:
            raise RegistryIntegrityError("sealed anchor is truncated or malformed")
        raw_record = raw[:-1]
        try:
            parsed = json.loads(raw_record.decode("utf-8"))
            canonical = _canonical_json(parsed)
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
            raise RegistryIntegrityError("sealed anchor is not canonical JSON") from exc
        if canonical != raw_record:
            raise RegistryIntegrityError("sealed anchor is not canonically serialized")
        return _anchor_from_json(parsed)

    def _write_anchor(self, anchor: SealedRegistryAnchor) -> None:
        encoded = _canonical_json(anchor.as_dict()) + b"\n"
        flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_BINARY", 0)
        try:
            descriptor = os.open(self.anchor_path, flags, 0o600)
        except FileExistsError as exc:
            raise RegistryIntegrityError("sealed anchor already exists and cannot be replaced") from exc
        try:
            remaining = memoryview(encoded)
            while remaining:
                written = os.write(descriptor, remaining)
                if written <= 0:
                    raise OSError("sealed anchor write made no progress")
                remaining = remaining[written:]
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        _fsync_directory(self.anchor_path.parent)

    def _append_record(
        self,
        snapshot: RegistrySnapshot,
        record_type: _RecordType,
        payload: dict[str, object],
        *,
        schema_version: int = SCHEMA_VERSION,
    ) -> str:
        body: dict[str, object] = {
            "schema_version": schema_version,
            "line_sequence": len(snapshot.trials) + (1 if snapshot.terminal_record else 0) + 1,
            "record_type": record_type.value,
            "previous_hash": snapshot.head_hash,
            "payload": payload,
        }
        record_hash = _sha256(body)
        envelope = {**body, "record_hash": record_hash}
        encoded = _canonical_json(envelope) + b"\n"
        self._write_append(encoded, expected_size=snapshot.byte_length)
        return record_hash

    def _write_append(self, encoded: bytes, *, expected_size: int) -> None:
        if not self.path.parent.is_dir():
            raise FileNotFoundError(f"registry parent directory does not exist: {self.path.parent}")
        created = not self.path.exists()
        flags = os.O_APPEND | os.O_CREAT | os.O_WRONLY | getattr(os, "O_BINARY", 0)
        descriptor = os.open(self.path, flags, 0o600)
        try:
            if os.fstat(descriptor).st_size != expected_size:
                raise RegistryConcurrentModificationError(
                    "registry changed after verification; concurrent writers are unsupported"
                )
            remaining = memoryview(encoded)
            while remaining:
                written = os.write(descriptor, remaining)
                if written <= 0:
                    raise OSError("registry append made no progress")
                remaining = remaining[written:]
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        if created:
            _fsync_directory(self.path.parent)
