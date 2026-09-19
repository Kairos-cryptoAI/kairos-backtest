"""Create a verified, read-only V5 recovery snapshot before any collector resume.

The SQLite backup API takes a transactionally consistent copy of the source
database.  Unlike a filesystem copy, that snapshot includes committed frames
which still live in the source database's WAL.  The source is opened read-only
throughout; only a new, caller-selected clone and receipt can be created.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from kairos_backtest.quarter_hour_features import (
    PLAN_FILENAME,
    QuarterHourFeatureLedger,
    _logical_sha256,
    load_plan,
)
from kairos_backtest.quarter_hour_v5_compatibility import (
    V5_FROZEN_FEATURE_SOURCE_SHA256,
    V5_FROZEN_LEDGER_SCHEMA_VERSION,
    V5_FROZEN_PLAN_SHA256,
    V5_FROZEN_SOURCE_COMMIT,
)


class QuarterHourRecoveryPreflightError(RuntimeError):
    """A V5 recovery snapshot cannot be trusted for a later explicit resume."""


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _require_new(path: Path, *, label: str) -> None:
    if path.exists() or path.is_symlink():
        raise FileExistsError(f"{label} already exists; refusing to overwrite: {path}")


def _source_read_uri(path: Path) -> str:
    """Avoid a sidecar for a sealed source; retain WAL visibility when it exists."""
    wal_path = path.with_name(path.name + "-wal")
    suffix = "?mode=ro" if wal_path.is_file() else "?mode=ro&immutable=1"
    return path.resolve().as_uri() + suffix


def _read_source_snapshot_metadata(
    path: Path,
    *,
    expected_plan_sha256: str,
) -> dict[str, object]:
    if not path.is_file():
        raise FileNotFoundError(f"V5 feature ledger does not exist: {path}")
    wal_path = path.with_name(path.name + "-wal")
    shm_path = path.with_name(path.name + "-shm")
    uri = _source_read_uri(path)
    connection = sqlite3.connect(uri, uri=True)
    try:
        integrity_rows = tuple(row[0] for row in connection.execute("PRAGMA integrity_check"))
        if integrity_rows != ("ok",):
            raise QuarterHourRecoveryPreflightError("source SQLite integrity_check did not return ok")
        foreign_key_rows = tuple(connection.execute("PRAGMA foreign_key_check"))
        if foreign_key_rows:
            raise QuarterHourRecoveryPreflightError("source SQLite foreign_key_check found violations")
        journal_mode = connection.execute("PRAGMA journal_mode").fetchone()[0]
        page_count = int(connection.execute("PRAGMA page_count").fetchone()[0])
        metadata = dict(connection.execute("SELECT key, value FROM metadata"))
    finally:
        connection.close()
    expected_metadata = {
        "feature_source_sha256": V5_FROZEN_FEATURE_SOURCE_SHA256,
        "plan_sha256": expected_plan_sha256,
        "schema_version": V5_FROZEN_LEDGER_SCHEMA_VERSION,
    }
    for key, expected in expected_metadata.items():
        if metadata.get(key) != expected:
            raise QuarterHourRecoveryPreflightError(
                f"source ledger {key} does not match the immutable V5 source identity"
            )
    return {
        "feature_source_sha256": metadata["feature_source_sha256"],
        "frozen_source_commit": V5_FROZEN_SOURCE_COMMIT,
        "frozen_plan_sha256": V5_FROZEN_PLAN_SHA256,
        "journal_mode": str(journal_mode).lower(),
        "main_database_sha256": _sha256(path),
        "page_count": page_count,
        "shm_present": shm_path.is_file(),
        "wal_bytes_at_snapshot": wal_path.stat().st_size if wal_path.is_file() else 0,
        "wal_present": wal_path.is_file(),
    }


def _backup_sqlite_snapshot(source: Path, temporary_clone: Path) -> None:
    """Use SQLite's online backup API, never a sidecar-blind filesystem copy."""
    source_uri = _source_read_uri(source)
    source_connection = sqlite3.connect(source_uri, uri=True)
    clone_connection = sqlite3.connect(temporary_clone)
    try:
        source_connection.backup(clone_connection)
    finally:
        clone_connection.close()
        source_connection.close()
    with temporary_clone.open("r+b") as stream:
        os.fsync(stream.fileno())


def _publish_new(temporary: Path, destination: Path, *, label: str) -> None:
    """Publish only a previously absent path, with a bounded Windows handoff retry."""
    for attempt in range(5):
        _require_new(destination, label=label)
        try:
            temporary.rename(destination)
            return
        except PermissionError:
            if attempt == 4:
                raise
            time.sleep(0.1)


def _verify_clone(
    clone: Path,
    *,
    expected_plan_sha256: str,
    expected_feature_source_sha256: str,
) -> dict[str, object]:
    with QuarterHourFeatureLedger.open_read_only(
        clone,
        plan_sha256=expected_plan_sha256,
        feature_source_sha256=expected_feature_source_sha256,
    ) as ledger:
        chain = ledger.verify(require_complete=False, deep=True)
        completed_batches = ledger.completed_batches()
    clone_uri = clone.resolve().as_uri() + "?mode=ro"
    connection = sqlite3.connect(clone_uri, uri=True)
    try:
        integrity_rows = tuple(row[0] for row in connection.execute("PRAGMA integrity_check"))
        if integrity_rows != ("ok",):
            raise QuarterHourRecoveryPreflightError("recovery clone SQLite integrity_check did not return ok")
        foreign_key_rows = tuple(connection.execute("PRAGMA foreign_key_check"))
        if foreign_key_rows:
            raise QuarterHourRecoveryPreflightError(
                "recovery clone SQLite foreign_key_check found violations"
            )
        journal_mode = connection.execute("PRAGMA journal_mode").fetchone()[0]
    finally:
        connection.close()
    return {
        "batch_chain_sha256": chain,
        "completed_batches": completed_batches,
        "includes_committed_wal_frames": True,
        "journal_mode": str(journal_mode).lower(),
        "sha256": _sha256(clone),
    }


def create_verified_recovery_clone(
    *,
    ledger: Path,
    clone: Path,
    receipt: Path,
    plan_path: Path,
) -> dict[str, object]:
    """Make a no-overwrite clone, validate it deeply, then publish a receipt.

    The final clone and receipt appear only after the hash-chain verification
    succeeds.  A failed temporary artifact is deliberately retained under its
    distinct ``.partial-...`` name for forensic inspection.
    """
    ledger = ledger.resolve()
    clone = clone.resolve()
    receipt = receipt.resolve()
    plan_path = plan_path.resolve()
    if clone == ledger:
        raise QuarterHourRecoveryPreflightError("recovery clone must not be the source ledger")
    _require_new(clone, label="recovery clone")
    _require_new(receipt, label="recovery receipt")
    if not plan_path.is_file():
        raise FileNotFoundError(f"quarter-hour plan does not exist: {plan_path}")
    clone.parent.mkdir(parents=True, exist_ok=True)
    receipt.parent.mkdir(parents=True, exist_ok=True)
    plan = load_plan(plan_path)
    expected_plan_sha256 = _logical_sha256(plan)
    if expected_plan_sha256 != V5_FROZEN_PLAN_SHA256:
        raise QuarterHourRecoveryPreflightError(
            "committed V2 plan does not match the immutable V5 recovery identity"
        )
    source = _read_source_snapshot_metadata(
        ledger,
        expected_plan_sha256=expected_plan_sha256,
    )
    temporary_clone = clone.with_name(f"{clone.name}.partial-{uuid.uuid4().hex}")
    temporary_receipt = receipt.with_name(f"{receipt.name}.partial-{uuid.uuid4().hex}")
    try:
        _backup_sqlite_snapshot(ledger, temporary_clone)
        clone_evidence = _verify_clone(
            temporary_clone,
            expected_plan_sha256=expected_plan_sha256,
            expected_feature_source_sha256=str(source["feature_source_sha256"]),
        )
        _publish_new(temporary_clone, clone, label="recovery clone")
        payload: dict[str, Any] = {
            "classification": "V5_RECOVERY_PREFLIGHT",
            "clone": {
                **clone_evidence,
                "path": str(clone),
            },
            "created_at_utc": datetime.now(UTC).isoformat(),
            "permissions": {
                "alpha_ready": False,
                "collector_resume_performed": False,
                "live_allowed": False,
                "paper_allowed": False,
            },
            "schema_version": "kairos.quarter-hour-v5-recovery-preflight.v1",
            "source": {
                **source,
                "path": str(ledger),
            },
        }
        temporary_receipt.write_text(
            json.dumps(payload, allow_nan=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        _publish_new(temporary_receipt, receipt, label="recovery receipt")
        return payload
    except BaseException:
        # Do not overwrite or remove a partial artifact: its name proves it was
        # never accepted as a recovery clone and retains forensic evidence.
        raise


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ledger", type=Path, required=True)
    parser.add_argument("--clone", type=Path, required=True)
    parser.add_argument("--receipt", type=Path, required=True)
    parser.add_argument("--plan", type=Path, default=Path(PLAN_FILENAME))
    arguments = parser.parse_args(argv)
    payload = create_verified_recovery_clone(
        ledger=arguments.ledger,
        clone=arguments.clone,
        receipt=arguments.receipt,
        plan_path=arguments.plan,
    )
    clone = cast(dict[str, object], payload["clone"])
    print(
        json.dumps(
            {
                "batch_chain_sha256": clone["batch_chain_sha256"],
                "clone": clone["path"],
                "completed_batches": clone["completed_batches"],
                "receipt": str(arguments.receipt.resolve()),
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
