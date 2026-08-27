"""Durable hash-chained evidence for exact quarter-hour overlay entry ticks."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path
from typing import Self

from . import quarter_hour_execution_overlay as overlay
from .quarter_hour_execution_overlay import (
    EntryTickRequest,
    EntryTickResolution,
    EntryTickStatus,
)

SCHEMA_VERSION = "kairos.quarter-hour-execution-entry-ledger.v1"
GENESIS_SHA256 = "0" * 64


class EntryTickLedgerIntegrityError(RuntimeError):
    """Raised when durable entry-tick evidence differs from its hash chain."""


def _canonical_decimal(value: object) -> str:
    from decimal import Decimal

    if not isinstance(value, Decimal) or not value.is_finite() or value <= 0:
        raise ValueError("entry tick price must be a finite positive Decimal")
    rendered = format(value, "f")
    if "." in rendered:
        rendered = rendered.rstrip("0").rstrip(".")
    return rendered


def _resolution_payload(resolution: EntryTickResolution) -> dict[str, object]:
    request = resolution.request
    return {
        "request": {
            "request_id": request.request_id,
            "submission_timestamp_ms": request.submission_timestamp_ms,
        },
        "resolution": {
            "aggregate_trade_id": resolution.aggregate_trade_id,
            "price": None if resolution.price is None else _canonical_decimal(resolution.price),
            "status": resolution.status.value,
            "transact_time_ms": resolution.transact_time_ms,
        },
        "schema_version": SCHEMA_VERSION,
    }


def _json_bytes(payload: object) -> bytes:
    return json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode()


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _chain_sha256(previous: str, record: str) -> str:
    return _sha256(bytes.fromhex(previous) + bytes.fromhex(record))


def _validate_sha256(name: str, value: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(char not in "0123456789abcdef" for char in value)
    ):
        raise ValueError(f"{name} must be a lowercase SHA-256")
    return value


def source_sha256() -> str:
    """Bind the exact timing, extraction and persistence implementation bytes."""

    digest = hashlib.sha256()
    paths = sorted((Path(__file__), Path(overlay.__file__)), key=lambda item: item.name)
    for path in paths:
        name = path.name.encode("ascii")
        content = path.read_bytes()
        digest.update(len(name).to_bytes(4, "big"))
        digest.update(name)
        digest.update(len(content).to_bytes(8, "big"))
        digest.update(content)
    return digest.hexdigest()


def _decode_resolution(payload: object) -> EntryTickResolution:
    from decimal import Decimal

    if not isinstance(payload, dict) or payload.get("schema_version") != SCHEMA_VERSION:
        raise EntryTickLedgerIntegrityError("entry tick record schema mismatch")
    request_payload = payload.get("request")
    resolution_payload = payload.get("resolution")
    if not isinstance(request_payload, dict) or not isinstance(resolution_payload, dict):
        raise EntryTickLedgerIntegrityError("entry tick record payload is malformed")
    try:
        request = EntryTickRequest(
            request_id=request_payload["request_id"],
            submission_timestamp_ms=request_payload["submission_timestamp_ms"],
        )
        raw_price = resolution_payload["price"]
        return EntryTickResolution(
            request=request,
            status=EntryTickStatus(resolution_payload["status"]),
            aggregate_trade_id=resolution_payload["aggregate_trade_id"],
            transact_time_ms=resolution_payload["transact_time_ms"],
            price=None if raw_price is None else Decimal(raw_price),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise EntryTickLedgerIntegrityError("entry tick record payload is invalid") from exc


class EntryTickLedger:
    """Append-only SQLite ledger bound to one plan, parent result and source."""

    def __init__(
        self,
        path: Path,
        *,
        plan_sha256: str,
        parent_result_sha256: str,
        extractor_source_sha256: str,
    ) -> None:
        self.path = path
        self.plan_sha256 = _validate_sha256("plan_sha256", plan_sha256)
        self.parent_result_sha256 = _validate_sha256(
            "parent_result_sha256",
            parent_result_sha256,
        )
        self.extractor_source_sha256 = _validate_sha256(
            "extractor_source_sha256",
            extractor_source_sha256,
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(path)
        self._connection.execute("PRAGMA journal_mode=WAL")
        self._connection.execute("PRAGMA synchronous=FULL")
        self._connection.execute("PRAGMA foreign_keys=ON")
        try:
            self._initialize()
        except Exception:
            self._connection.close()
            raise

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def close(self) -> None:
        self._connection.close()

    def _initialize(self) -> None:
        self._connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS metadata (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS entry_tick_record (
                sequence INTEGER PRIMARY KEY,
                request_id TEXT NOT NULL UNIQUE,
                record_json TEXT NOT NULL,
                record_sha256 TEXT NOT NULL,
                previous_chain_sha256 TEXT NOT NULL,
                chain_sha256 TEXT NOT NULL
            );
            """
        )
        expected = {
            "extractor_source_sha256": self.extractor_source_sha256,
            "parent_result_sha256": self.parent_result_sha256,
            "plan_sha256": self.plan_sha256,
            "schema_version": SCHEMA_VERSION,
        }
        actual = dict(self._connection.execute("SELECT key, value FROM metadata"))
        if actual and actual != expected:
            raise EntryTickLedgerIntegrityError("entry tick ledger metadata mismatch")
        if not actual:
            with self._connection:
                self._connection.executemany(
                    "INSERT INTO metadata(key, value) VALUES (?, ?)",
                    tuple(sorted(expected.items())),
                )

    def completed_records(self) -> int:
        return int(self._connection.execute("SELECT COUNT(*) FROM entry_tick_record").fetchone()[0])

    def append(self, sequence: int, resolution: EntryTickResolution) -> str:
        """Append one record, or accept an exact idempotent replay."""

        if isinstance(sequence, bool) or not isinstance(sequence, int) or sequence < 0:
            raise ValueError("entry tick sequence must be a non-negative integer")
        if not isinstance(resolution, EntryTickResolution):
            raise TypeError("entry tick ledger accepts EntryTickResolution values")
        payload = _resolution_payload(resolution)
        record_json = _json_bytes(payload).decode("ascii")
        record_sha = _sha256(record_json.encode("ascii"))

        self.verify()
        self._connection.execute("BEGIN IMMEDIATE")
        try:
            existing = self._connection.execute(
                "SELECT sequence, record_json, chain_sha256 FROM entry_tick_record WHERE request_id = ?",
                (resolution.request.request_id,),
            ).fetchone()
            if existing is not None:
                if int(existing[0]) != sequence or str(existing[1]) != record_json:
                    raise EntryTickLedgerIntegrityError("entry tick idempotency conflict")
                self._connection.rollback()
                return str(existing[2])

            count = self.completed_records()
            if sequence != count:
                raise EntryTickLedgerIntegrityError("entry tick append sequence is not contiguous")
            previous_row = self._connection.execute(
                "SELECT chain_sha256 FROM entry_tick_record ORDER BY sequence DESC LIMIT 1"
            ).fetchone()
            previous = GENESIS_SHA256 if previous_row is None else str(previous_row[0])
            chain = _chain_sha256(previous, record_sha)
            self._connection.execute(
                """
                INSERT INTO entry_tick_record(
                    sequence, request_id, record_json, record_sha256,
                    previous_chain_sha256, chain_sha256
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    sequence,
                    resolution.request.request_id,
                    record_json,
                    record_sha,
                    previous,
                    chain,
                ),
            )
            self._connection.commit()
            return chain
        except Exception:
            if self._connection.in_transaction:
                self._connection.rollback()
            raise

    def verify(self) -> str:
        """Recompute every record and chain link from stored canonical bytes."""

        metadata = dict(self._connection.execute("SELECT key, value FROM metadata"))
        expected = {
            "extractor_source_sha256": self.extractor_source_sha256,
            "parent_result_sha256": self.parent_result_sha256,
            "plan_sha256": self.plan_sha256,
            "schema_version": SCHEMA_VERSION,
        }
        if metadata != expected:
            raise EntryTickLedgerIntegrityError("entry tick ledger metadata was mutated")

        previous = GENESIS_SHA256
        count = 0
        for row in self._connection.execute(
            """
            SELECT sequence, request_id, record_json, record_sha256,
                   previous_chain_sha256, chain_sha256
            FROM entry_tick_record ORDER BY sequence
            """
        ):
            sequence, request_id, record_json, record_sha, stored_previous, stored_chain = row
            if int(sequence) != count:
                raise EntryTickLedgerIntegrityError("entry tick ledger sequence gap")
            encoded = str(record_json).encode("ascii")
            if _sha256(encoded) != record_sha:
                raise EntryTickLedgerIntegrityError("entry tick record hash mismatch")
            resolution = _decode_resolution(json.loads(encoded))
            if (
                resolution.request.request_id != request_id
                or _json_bytes(_resolution_payload(resolution)) != encoded
            ):
                raise EntryTickLedgerIntegrityError("entry tick canonical payload mismatch")
            if stored_previous != previous:
                raise EntryTickLedgerIntegrityError("entry tick previous-chain link mismatch")
            chain = _chain_sha256(previous, str(record_sha))
            if stored_chain != chain:
                raise EntryTickLedgerIntegrityError("entry tick chain hash mismatch")
            previous = chain
            count += 1
        return previous


__all__ = [
    "EntryTickLedger",
    "EntryTickLedgerIntegrityError",
    "GENESIS_SHA256",
    "SCHEMA_VERSION",
    "source_sha256",
]
