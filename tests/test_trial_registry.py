import hashlib
import json
import os
import threading
from collections.abc import Callable
from dataclasses import replace
from datetime import date, timedelta
from pathlib import Path

import pytest

import kairos_backtest.trial_registry as registry_module
from kairos_backtest.robustness import DailyReturnSeries
from kairos_backtest.trial_registry import (
    FailureClass,
    RegistryFrozenError,
    RegistryIntegrityError,
    RegistryOutcome,
    RejectionRecord,
    TrialFingerprints,
    TrialRegistry,
    TrialStatus,
)


def digest(label: str) -> str:
    return hashlib.sha256(label.encode()).hexdigest()


def fingerprints(label: str = "base") -> TrialFingerprints:
    return TrialFingerprints(
        protocol_sha256=digest(f"{label}:protocol"),
        config_sha256=digest(f"{label}:config"),
        code_sha256=digest(f"{label}:code"),
        data_sha256=digest(f"{label}:data"),
        dependency_sha256=digest(f"{label}:dependency"),
        container_sha256=digest(f"{label}:container"),
    )


def returns() -> DailyReturnSeries:
    start = date(2025, 1, 1)
    values = (0.01, -0.005, 0.003, 0.007)
    return DailyReturnSeries(
        tuple(start + timedelta(days=index) for index in range(len(values))),
        values,
    )


def canonical(value: object) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode()


def rewrite_record(
    path: Path,
    index: int,
    mutate: Callable[[dict[str, object]], None],
) -> None:
    lines = path.read_bytes().splitlines()
    record = json.loads(lines[index])
    assert isinstance(record, dict)
    mutate(record)
    body = {key: value for key, value in record.items() if key != "record_hash"}
    record["record_hash"] = hashlib.sha256(canonical(body)).hexdigest()
    lines[index] = canonical(record)
    path.write_bytes(b"\n".join(lines) + b"\n")


def test_append_read_hash_chain_retains_failures_and_final_selection(tmp_path: Path):
    path = tmp_path / "trials.jsonl"
    registry = TrialRegistry(path)

    success = registry.append_success(fingerprints(), returns())
    failure = registry.append_failure(fingerprints("failed"), FailureClass.NUMERICAL)
    before_selection = path.read_bytes()
    selection = registry.finalize_selection(success.trial_id)
    after_selection = path.read_bytes()
    snapshot = registry.read()

    assert success.trial_id == 1
    assert failure.trial_id == 2
    assert failure.status is TrialStatus.FAILURE
    assert failure.failure_class is FailureClass.NUMERICAL
    assert failure.daily_returns is None
    assert snapshot.trials[0].daily_returns == returns()
    assert snapshot.selection == selection
    assert snapshot.rejection is None
    assert snapshot.terminal_record == selection
    assert snapshot.outcome is RegistryOutcome.SELECTION
    assert snapshot.head_hash == selection.record_hash
    assert snapshot.frozen is True
    assert snapshot.sealed_anchor == registry.export_anchor()
    assert snapshot.sealed_anchor is not None
    assert snapshot.sealed_anchor.line_count == 3
    assert snapshot.sealed_anchor.head_hash == selection.record_hash
    assert snapshot.sealed_anchor.candidate_sha256 == selection.candidate_sha256
    assert snapshot.sealed_anchor.outcome is RegistryOutcome.SELECTION
    assert snapshot.sealed_anchor.rejection_sha256 is None
    assert snapshot.sealed_anchor.terminal_sha256 == selection.candidate_sha256
    assert registry.anchor_path.is_file()
    assert after_selection.startswith(before_selection)
    assert b"NUMERICAL" in after_selection
    with pytest.raises(RegistryFrozenError, match="frozen"):
        registry.append_failure(fingerprints("late"), FailureClass.UNKNOWN)
    with pytest.raises(RegistryFrozenError, match="terminal outcome"):
        registry.finalize_rejection()


def test_reject_all_is_a_sealed_terminal_outcome_without_a_winner(tmp_path: Path):
    path = tmp_path / "trials.jsonl"
    registry = TrialRegistry(path)
    registry.append_success(fingerprints(), returns())
    registry.append_failure(fingerprints("failed"), FailureClass.STRATEGY)
    trial_prefix = path.read_bytes()

    rejection = registry.finalize_rejection()
    snapshot = TrialRegistry(path).read()
    exported = registry.export_anchor()

    assert isinstance(rejection, RejectionRecord)
    assert rejection.outcome is RegistryOutcome.REJECT_ALL
    assert rejection.trial_count == 2
    assert rejection.trial_head_hash == snapshot.trials[-1].record_hash
    expected_rejection_sha256 = hashlib.sha256(
        canonical(
            {
                "outcome_schema_version": 2,
                "outcome": "REJECT_ALL",
                "trial_count": len(snapshot.trials),
                "trial_head_hash": snapshot.trials[-1].record_hash,
                "trial_record_hashes": [trial.record_hash for trial in snapshot.trials],
            }
        )
    ).hexdigest()
    assert rejection.rejection_sha256 == expected_rejection_sha256
    assert snapshot.selection is None
    assert snapshot.rejection == rejection
    assert snapshot.terminal_record == rejection
    assert snapshot.outcome is RegistryOutcome.REJECT_ALL
    assert snapshot.frozen is True
    assert snapshot.head_hash == rejection.record_hash
    assert snapshot.sealed_anchor == exported
    assert exported.outcome is RegistryOutcome.REJECT_ALL
    assert exported.candidate_sha256 is None
    assert exported.rejection_sha256 == rejection.rejection_sha256
    assert exported.terminal_sha256 == rejection.rejection_sha256
    assert exported.line_count == 3
    assert path.read_bytes().startswith(trial_prefix)

    terminal_envelope = json.loads(path.read_bytes().splitlines()[-1])
    assert terminal_envelope["schema_version"] == 2
    assert terminal_envelope["record_type"] == "REJECTION"
    assert terminal_envelope["payload"]["outcome"] == "REJECT_ALL"
    assert "selected_trial_id" not in terminal_envelope["payload"]
    assert "candidate_sha256" not in terminal_envelope["payload"]

    frozen_operations = (
        lambda: registry.append_success(fingerprints("late-success"), returns()),
        lambda: registry.append_failure(fingerprints("late-failure"), FailureClass.DATA),
        lambda: registry.finalize_selection(1),
        registry.finalize_rejection,
    )
    for operation in frozen_operations:
        with pytest.raises(RegistryFrozenError, match="terminal outcome"):
            operation()


def test_reject_all_requires_at_least_one_recorded_trial(tmp_path: Path):
    registry = TrialRegistry(tmp_path / "trials.jsonl")

    with pytest.raises(ValueError, match="at least one recorded trial"):
        registry.finalize_rejection()

    assert registry.read().frozen is False
    assert not registry.anchor_path.exists()


def test_append_fsyncs_each_record(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    registry = TrialRegistry(tmp_path / "trials.jsonl")
    actual_fsync = os.fsync
    calls: list[int] = []

    def recording_fsync(descriptor: int) -> None:
        calls.append(descriptor)
        actual_fsync(descriptor)

    monkeypatch.setattr(registry_module.os, "fsync", recording_fsync)

    registry.append_failure(fingerprints(), FailureClass.DATA)

    assert len(calls) >= 1
    if os.name != "nt":
        assert len(calls) >= 2  # registry contents plus its new directory entry

    calls.clear()
    registry.append_failure(fingerprints("second"), FailureClass.DATA)
    assert len(calls) == 1  # an append to an existing file needs only file fsync


def test_final_selection_anchor_detects_valid_prefix_rollback(tmp_path: Path):
    path = tmp_path / "trials.jsonl"
    registry = TrialRegistry(path)
    selected = registry.append_success(fingerprints(), returns())
    valid_prefix = path.read_bytes()
    registry.finalize_selection(selected.trial_id)

    path.write_bytes(valid_prefix)

    with pytest.raises(RegistryIntegrityError, match="sealed anchor"):
        registry.read()
    with pytest.raises(RegistryIntegrityError, match="sealed anchor"):
        registry.append_failure(fingerprints("late"), FailureClass.DATA)


def test_reject_all_anchor_detects_valid_prefix_rollback(tmp_path: Path):
    path = tmp_path / "trials.jsonl"
    registry = TrialRegistry(path)
    registry.append_failure(fingerprints(), FailureClass.STRATEGY)
    valid_open_prefix = path.read_bytes()
    registry.finalize_rejection()

    path.write_bytes(valid_open_prefix)

    with pytest.raises(RegistryIntegrityError, match="sealed anchor"):
        registry.read()
    with pytest.raises(RegistryIntegrityError, match="sealed anchor"):
        registry.finalize_selection(1)


def test_reject_all_requires_its_anchor(tmp_path: Path):
    path = tmp_path / "trials.jsonl"
    registry = TrialRegistry(path)
    registry.append_failure(fingerprints(), FailureClass.STRATEGY)
    registry.finalize_rejection()

    registry.anchor_path.unlink()

    with pytest.raises(RegistryIntegrityError, match="final rejection.*sealed anchor"):
        registry.read()


def test_reject_all_anchor_and_terminal_payload_tampering_fail_closed(tmp_path: Path):
    anchor_path = tmp_path / "anchor-tamper.jsonl"
    anchor_registry = TrialRegistry(anchor_path)
    anchor_registry.append_success(fingerprints(), returns())
    anchor_registry.finalize_rejection()
    tampered_anchor = json.loads(anchor_registry.anchor_path.read_bytes())
    tampered_anchor["rejection_sha256"] = digest("forged rejection")
    anchor_registry.anchor_path.write_bytes(canonical(tampered_anchor) + b"\n")

    with pytest.raises(RegistryIntegrityError, match="final rejection"):
        anchor_registry.read()

    record_path = tmp_path / "record-tamper.jsonl"
    record_registry = TrialRegistry(record_path)
    record_registry.append_success(fingerprints("record"), returns())
    record_registry.finalize_rejection()

    def invent_selected_trial(record: dict[str, object]) -> None:
        payload = record["payload"]
        assert isinstance(payload, dict)
        payload["outcome"] = "SELECTION"

    rewrite_record(record_path, 1, invent_selected_trial)
    with pytest.raises(RegistryIntegrityError, match="rejection outcome"):
        record_registry.read()


def test_final_selection_requires_its_anchor(tmp_path: Path):
    path = tmp_path / "trials.jsonl"
    registry = TrialRegistry(path)
    selected = registry.append_success(fingerprints(), returns())
    registry.finalize_selection(selected.trial_id)

    registry.anchor_path.unlink()
    with pytest.raises(RegistryIntegrityError, match="missing its sealed anchor"):
        registry.read()


def test_sealed_anchor_requires_canonical_serialization(tmp_path: Path):
    path = tmp_path / "trials.jsonl"
    registry = TrialRegistry(path)
    selected = registry.append_success(fingerprints(), returns())
    registry.finalize_selection(selected.trial_id)
    parsed = json.loads(registry.anchor_path.read_bytes())
    registry.anchor_path.write_text(json.dumps(parsed, indent=2) + "\n", encoding="utf-8")

    with pytest.raises(RegistryIntegrityError, match="sealed anchor"):
        registry.read()


def test_selection_anchor_retains_legacy_schema_and_remains_readable(tmp_path: Path):
    path = tmp_path / "trials.jsonl"
    registry = TrialRegistry(path)
    selected = registry.append_success(fingerprints(), returns())
    selection = registry.finalize_selection(selected.trial_id)
    current_anchor = json.loads(registry.anchor_path.read_bytes())
    assert set(current_anchor) == {
        "schema_version",
        "line_count",
        "head_hash",
        "candidate_sha256",
    }
    assert current_anchor["schema_version"] == 1
    legacy_anchor = {
        "schema_version": 1,
        "line_count": current_anchor["line_count"],
        "head_hash": current_anchor["head_hash"],
        "candidate_sha256": current_anchor["candidate_sha256"],
    }
    registry.anchor_path.write_bytes(canonical(legacy_anchor) + b"\n")

    snapshot = TrialRegistry(path).read()

    assert snapshot.selection == selection
    assert snapshot.outcome is RegistryOutcome.SELECTION
    assert snapshot.sealed_anchor is not None
    assert snapshot.sealed_anchor.schema_version == 1
    assert snapshot.sealed_anchor.outcome is RegistryOutcome.SELECTION
    assert snapshot.sealed_anchor.candidate_sha256 == selection.candidate_sha256


def test_os_lock_serializes_a_second_writer(tmp_path: Path):
    path = tmp_path / "trials.jsonl"
    first = TrialRegistry(path)
    second = TrialRegistry(path)
    attempted = threading.Event()
    finished = threading.Event()
    failures: list[BaseException] = []

    def append_from_second_writer() -> None:
        attempted.set()
        try:
            second.append_failure(fingerprints(), FailureClass.DATA)
        except BaseException as exc:  # pragma: no cover - asserted below if populated
            failures.append(exc)
        finally:
            finished.set()

    worker = threading.Thread(target=append_from_second_writer)
    with first._exclusive_lock():
        worker.start()
        assert attempted.wait(timeout=1)
        assert not finished.wait(timeout=0.1)
    assert finished.wait(timeout=3)
    worker.join(timeout=1)

    assert failures == []
    assert len(first.read().trials) == 1


def test_failure_class_is_sanitized_and_never_accepts_exception_text(tmp_path: Path):
    registry = TrialRegistry(tmp_path / "trials.jsonl")

    with pytest.raises(TypeError, match="sanitized"):
        registry.append_failure(fingerprints(), "secret: api-key")  # type: ignore[arg-type]

    registry.append_failure(fingerprints(), FailureClass.INFRASTRUCTURE)
    persisted = (tmp_path / "trials.jsonl").read_text(encoding="utf-8")
    assert "INFRASTRUCTURE" in persisted
    assert "secret" not in persisted


def test_tampered_content_and_wrong_hash_fail_closed(tmp_path: Path):
    content_path = tmp_path / "content.jsonl"
    TrialRegistry(content_path).append_success(fingerprints(), returns())
    content_path.write_bytes(content_path.read_bytes().replace(b"0.003", b"0.004"))
    with pytest.raises(RegistryIntegrityError, match="hash verification"):
        TrialRegistry(content_path).read()

    hash_path = tmp_path / "hash.jsonl"
    TrialRegistry(hash_path).append_failure(fingerprints(), FailureClass.DATA)
    record = json.loads(hash_path.read_bytes())
    record["record_hash"] = "f" * 64
    hash_path.write_bytes(canonical(record) + b"\n")
    with pytest.raises(RegistryIntegrityError, match="hash verification"):
        TrialRegistry(hash_path).read()


def test_truncated_duplicate_and_noncanonical_records_fail_closed(tmp_path: Path):
    truncated = tmp_path / "truncated.jsonl"
    TrialRegistry(truncated).append_failure(fingerprints(), FailureClass.DATA)
    truncated.write_bytes(truncated.read_bytes()[:-1])
    with pytest.raises(RegistryIntegrityError, match="truncated"):
        TrialRegistry(truncated).read()

    duplicate = tmp_path / "duplicate.jsonl"
    TrialRegistry(duplicate).append_failure(fingerprints(), FailureClass.DATA)
    first_line = duplicate.read_bytes()
    duplicate.write_bytes(first_line + first_line)
    with pytest.raises(RegistryIntegrityError, match="sequence|duplicate"):
        TrialRegistry(duplicate).read()

    noncanonical = tmp_path / "noncanonical.jsonl"
    TrialRegistry(noncanonical).append_failure(fingerprints(), FailureClass.DATA)
    parsed = json.loads(noncanonical.read_bytes())
    noncanonical.write_text(json.dumps(parsed, indent=2) + "\n", encoding="utf-8")
    with pytest.raises(RegistryIntegrityError, match="canonical"):
        TrialRegistry(noncanonical).read()


def test_wrong_line_and_trial_sequences_fail_even_with_recomputed_record_hash(tmp_path: Path):
    line_path = tmp_path / "line.jsonl"
    TrialRegistry(line_path).append_failure(fingerprints(), FailureClass.DATA)

    def wrong_line(record: dict[str, object]) -> None:
        record["line_sequence"] = 2

    rewrite_record(line_path, 0, wrong_line)
    with pytest.raises(RegistryIntegrityError, match="line sequence"):
        TrialRegistry(line_path).read()

    trial_path = tmp_path / "trial.jsonl"
    registry = TrialRegistry(trial_path)
    registry.append_failure(fingerprints(), FailureClass.DATA)
    registry.append_failure(fingerprints("second"), FailureClass.RESOURCE)

    def duplicate_trial(record: dict[str, object]) -> None:
        payload = record["payload"]
        assert isinstance(payload, dict)
        payload["trial_id"] = 1

    rewrite_record(trial_path, 1, duplicate_trial)
    with pytest.raises(RegistryIntegrityError, match="trial_id sequence"):
        TrialRegistry(trial_path).read()


def test_selection_rejects_missing_and_failed_trials(tmp_path: Path):
    registry = TrialRegistry(tmp_path / "trials.jsonl")
    registry.append_failure(fingerprints(), FailureClass.STRATEGY)

    with pytest.raises(ValueError, match="successful"):
        registry.finalize_selection(1)
    with pytest.raises(ValueError, match="missing"):
        registry.finalize_selection(2)


def test_changed_fingerprint_creates_a_distinct_frozen_candidate(tmp_path: Path):
    first_registry = TrialRegistry(tmp_path / "first.jsonl")
    first_trial = first_registry.append_success(fingerprints(), returns())
    first_selection = first_registry.finalize_selection(first_trial.trial_id)

    changed = replace(fingerprints(), config_sha256=digest("changed:config"))
    second_registry = TrialRegistry(tmp_path / "second.jsonl")
    second_trial = second_registry.append_success(changed, returns())
    second_selection = second_registry.finalize_selection(second_trial.trial_id)

    assert first_selection.candidate_sha256 != second_selection.candidate_sha256
    assert first_trial.record_hash != second_trial.record_hash


def test_fingerprints_require_full_lowercase_sha256():
    with pytest.raises(ValueError, match="full lowercase"):
        replace(fingerprints(), code_sha256="A" * 64)
