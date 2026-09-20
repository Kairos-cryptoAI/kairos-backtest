"""Performance-blind, public Bybit event-time tape capture.

The capture process is deliberately downstream of the immutable
``liquidation_absorption_preflight`` plan and upstream of every research
result.  It records source provenance only: no signal, price outcome, model,
trade, order, account, EVEDEX, or paid-provider path exists here.

An unclean process stop never resumes a segment.  The whole open segment is
quarantined and a new segment begins with an explicit continuity barrier.  That
choice intentionally prefers losing a short source interval to presenting an
unknown gap as continuous evidence.
"""

from __future__ import annotations

import argparse
import asyncio
import gzip
import hashlib
import json
import os
import sqlite3
import time
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final, Literal, cast

import aiohttp

from .liquidation_absorption_preflight import (
    PLAN_FILENAME,
    PLAN_LOGICAL_SHA256,
    _logical_sha256,
    load_plan,
)
from .scenarios import SYMBOLS

CAPTURE_SCHEMA_VERSION: Final = "kairos.liquidation-absorption-capture.v1"
EVENT_SCHEMA_VERSION: Final = "kairos.liquidation-absorption-event.v1"
BYBIT_LINEAR_WEBSOCKET_URL: Final = "wss://stream.bybit.com/v5/public/linear"
SEGMENT_MAX_EVENTS: Final = 100_000
CHECKPOINT_EVENTS: Final = 256
MAXIMUM_SOURCE_FUTURE_SKEW_MS: Final = 2_000
DEFAULT_RECONNECT_DELAY_SECONDS: Final = 3.0
MAXIMUM_RECONNECT_DELAY_SECONDS: Final = 30.0
LEASE_STALE_AFTER_NS: Final = 5 * 60 * 1_000_000_000
EVENT_TYPES = frozenset({"BARRIER", "CONTROL", "FRAME"})


class LiquidationAbsorptionCaptureError(RuntimeError):
    """The public tape cannot safely accept or verify source evidence."""


class CaptureNotYetEligibleError(LiquidationAbsorptionCaptureError):
    """The preregistered wall-clock start time has not arrived."""


class CaptureLeaseActiveError(LiquidationAbsorptionCaptureError):
    """A recent recorder lease makes a second writer unsafe."""


@dataclass(frozen=True, slots=True)
class CaptureStatus:
    """Non-secret state suitable for a supervisor status receipt."""

    output_directory: Path
    plan_sha256: str
    sealed_segments: int
    quarantined_segments: int
    open_segment_index: int
    open_segment_events: int
    last_sealed_sequence: int
    last_sealed_chain_sha256: str


@dataclass(frozen=True, slots=True)
class CapturedText:
    """Classification of one raw inbound WebSocket text message."""

    event_type: Literal["CONTROL", "FRAME"]
    topic: str | None
    symbol: str | None
    exchange_at_ms: int | None
    channel_sequence: int | None
    barrier_reason: str | None


def _canonical_bytes(value: object) -> bytes:
    return json.dumps(
        value, allow_nan=False, ensure_ascii=True, separators=(",", ":"), sort_keys=True
    ).encode("ascii")


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _utc_now_ns() -> int:
    return time.time_ns()


def _not_before_ns(plan: Mapping[str, object]) -> int:
    data = cast(Mapping[str, object], plan["data"])
    raw = data["capture_start_not_before_utc"]
    if not isinstance(raw, str):
        raise LiquidationAbsorptionCaptureError("plan capture start is invalid")
    parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise LiquidationAbsorptionCaptureError("plan capture start must be UTC-aware")
    return int(parsed.astimezone(UTC).timestamp() * 1_000_000_000)


def _safe_relative_segment_path(index: int) -> str:
    if index <= 0:
        raise ValueError("segment index must be positive")
    return f"segments/{index:012d}.ndjson.gz"


def _parse_integer(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.isdecimal():
        return int(value)
    return None


def _topic_metadata(payload: Mapping[str, object]) -> CapturedText:
    topic = payload.get("topic")
    if topic is None:
        return CapturedText("CONTROL", None, None, None, None, None)
    if not isinstance(topic, str):
        return CapturedText("FRAME", None, None, None, None, "TOPIC_NOT_TEXT")
    parts = topic.split(".")
    if len(parts) == 3 and parts[:2] == ["orderbook", "50"]:
        symbol = parts[2]
    elif len(parts) == 2 and parts[0] in {"publicTrade", "tickers", "allLiquidation"}:
        symbol = parts[1]
    else:
        return CapturedText("FRAME", topic, None, None, None, "UNEXPECTED_TOPIC")
    if symbol not in SYMBOLS:
        return CapturedText("FRAME", topic, symbol, None, None, "UNEXPECTED_SYMBOL")
    data = payload.get("data")
    if topic.startswith(("orderbook.50.", "tickers.")) and not isinstance(data, Mapping):
        return CapturedText("FRAME", topic, symbol, None, None, "TOPIC_DATA_NOT_OBJECT")
    if topic.startswith(("publicTrade.", "allLiquidation.")) and not isinstance(data, (Mapping, list)):
        return CapturedText("FRAME", topic, symbol, None, None, "TOPIC_DATA_NOT_COLLECTION")
    exchange_at_ms = _parse_integer(payload.get("ts"))
    if exchange_at_ms is None and isinstance(data, Mapping):
        exchange_at_ms = _parse_integer(data.get("cts"))
    if exchange_at_ms is None and isinstance(data, Mapping):
        exchange_at_ms = _parse_integer(data.get("T"))
    channel_sequence = _parse_integer(data.get("seq")) if isinstance(data, Mapping) else None
    if exchange_at_ms is None:
        return CapturedText("FRAME", topic, symbol, None, channel_sequence, "SOURCE_TIMESTAMP_MISSING")
    return CapturedText("FRAME", topic, symbol, exchange_at_ms, channel_sequence, None)


class LiquidationAbsorptionTape:
    """Append-only segmented tape whose accepted segments are immutable gzip files."""

    def __init__(
        self,
        *,
        output_directory: Path,
        plan_path: Path,
        now_ns: Callable[[], int] = _utc_now_ns,
        segment_max_events: int = SEGMENT_MAX_EVENTS,
        checkpoint_events: int = CHECKPOINT_EVENTS,
    ) -> None:
        if segment_max_events < 1 or checkpoint_events < 1 or checkpoint_events > segment_max_events:
            raise ValueError("capture segment and checkpoint bounds are invalid")
        self.output_directory = output_directory.resolve()
        self.plan_path = plan_path.resolve()
        self._now_ns = now_ns
        self._segment_max_events = segment_max_events
        self._checkpoint_events = checkpoint_events
        self._connection: sqlite3.Connection | None = None
        self._plan_sha256 = ""
        self._segment_index = 0
        self._segment_initial_sequence = 1
        self._segment_initial_chain = "0" * 64
        self._segment_events = 0
        self._segment_buffer: list[bytes] = []
        self._next_sequence = 1
        self._previous_chain = "0" * 64
        self._source_state: dict[tuple[str, str], tuple[int, int | None]] = {}
        self._lease_id: str | None = None

    def __enter__(self) -> LiquidationAbsorptionTape:
        self.open()
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.close(clean=exc_type is None)

    @property
    def plan_sha256(self) -> str:
        if not self._plan_sha256:
            raise LiquidationAbsorptionCaptureError("capture tape is not open")
        return self._plan_sha256

    @property
    def status(self) -> CaptureStatus:
        connection = self._require_connection()
        sealed = int(connection.execute("SELECT COUNT(*) FROM segments WHERE state = 'SEALED'").fetchone()[0])
        quarantined = int(
            connection.execute("SELECT COUNT(*) FROM segments WHERE state = 'QUARANTINED'").fetchone()[0]
        )
        open_row = connection.execute(
            "SELECT segment_index FROM segments WHERE state = 'OPEN' ORDER BY segment_index"
        ).fetchone()
        if open_row is None:
            open_index = 0
            open_events = 0
        else:
            open_index = int(open_row[0])
            open_events = self._segment_events if open_index == self._segment_index else 0
        last_sequence = int(self._metadata("last_sealed_sequence"))
        last_chain = self._metadata("last_sealed_chain_sha256")
        return CaptureStatus(
            output_directory=self.output_directory,
            plan_sha256=self.plan_sha256,
            sealed_segments=sealed,
            quarantined_segments=quarantined,
            open_segment_index=open_index,
            open_segment_events=open_events,
            last_sealed_sequence=last_sequence,
            last_sealed_chain_sha256=last_chain,
        )

    def open(self, *, for_capture: bool = True) -> None:
        if self._connection is not None:
            raise LiquidationAbsorptionCaptureError("capture tape is already open")
        plan = load_plan(self.plan_path)
        plan_sha256 = _logical_sha256(plan)
        if plan_sha256 != PLAN_LOGICAL_SHA256:
            raise LiquidationAbsorptionCaptureError("capture plan logical SHA-256 is invalid")
        now_ns = self._checked_now_ns()
        if now_ns < _not_before_ns(plan):
            raise CaptureNotYetEligibleError("capture must not start before its preregistered UTC boundary")
        database_path = self.output_directory / "capture.sqlite3"
        if not for_capture and not database_path.is_file():
            raise LiquidationAbsorptionCaptureError("capture ledger does not exist")
        self.output_directory.mkdir(parents=True, exist_ok=True)
        (self.output_directory / "segments").mkdir(exist_ok=True)
        connection = sqlite3.connect(database_path)
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=FULL")
        self._connection = connection
        self._initialize(plan_sha256)
        if not for_capture:
            return
        try:
            self._acquire_lease(now_ns)
            self._recover_open_segment()
            self._begin_segment()
            reason = "CAPTURE_STARTED" if self._next_sequence == 1 else "PROCESS_RESTART"
            self.record_barrier(reason=reason, received_at_ns=now_ns)
        except BaseException:
            self._release_lease()
            connection.close()
            self._connection = None
            raise

    def close(self, *, clean: bool) -> CaptureStatus | None:
        connection = self._connection
        if connection is None:
            return None
        status: CaptureStatus | None = None
        try:
            if clean:
                self.record_barrier(reason="CAPTURE_STOPPED", received_at_ns=self._checked_now_ns())
                self._seal_open_segment()
                status = self.status
            else:
                if self._segment_index:
                    self._flush_open_member()
        finally:
            self._release_lease()
            connection.close()
            self._connection = None
        return status

    def record_barrier(self, *, reason: str, received_at_ns: int) -> None:
        if (
            not reason
            or reason != reason.strip()
            or not reason.isupper()
            or not reason.replace("_", "").isalnum()
        ):
            raise ValueError("capture barrier reason must be uppercase snake case")
        self._append(
            event_type="BARRIER",
            topic=None,
            symbol=None,
            exchange_at_ms=None,
            channel_sequence=None,
            raw_text=None,
            received_at_ns=received_at_ns,
            reason=reason,
        )
        self._source_state.clear()

    def record_text(self, *, raw_text: str, received_at_ns: int) -> CapturedText:
        if not isinstance(raw_text, str):
            raise TypeError("capture accepts original WebSocket text only")
        try:
            raw_text.encode("utf-8")
        except UnicodeEncodeError as exc:
            self.record_barrier(reason="RAW_TEXT_NOT_UTF8", received_at_ns=received_at_ns)
            raise LiquidationAbsorptionCaptureError("WebSocket text cannot be represented as UTF-8") from exc
        try:
            parsed = json.loads(raw_text)
        except json.JSONDecodeError:
            captured = CapturedText("FRAME", None, None, None, None, "MALFORMED_JSON")
        else:
            captured = (
                _topic_metadata(parsed)
                if isinstance(parsed, Mapping)
                else CapturedText("FRAME", None, None, None, None, "ROOT_NOT_OBJECT")
            )
        reason = captured.barrier_reason
        received_ms = received_at_ns // 1_000_000
        if reason is None and captured.exchange_at_ms is not None:
            if captured.exchange_at_ms > received_ms + MAXIMUM_SOURCE_FUTURE_SKEW_MS:
                reason = "SOURCE_CLOCK_FUTURE"
            elif captured.topic is not None and captured.symbol is not None:
                key = (captured.topic, captured.symbol)
                prior = self._source_state.get(key)
                if prior is not None and captured.exchange_at_ms < prior[0]:
                    reason = "SOURCE_TIME_REGRESSION"
                else:
                    update, snapshot = self._orderbook_update(raw_text, captured.topic)
                    if update is not None and prior is not None:
                        prior_update = prior[1]
                        if not snapshot and prior_update is not None and update <= prior_update:
                            reason = "ORDERBOOK_UPDATE_REGRESSION"
                        elif not snapshot and prior_update is not None and update != prior_update + 1:
                            reason = "ORDERBOOK_UPDATE_GAP"
                    self._source_state[key] = (captured.exchange_at_ms, update)
        self._append(
            event_type=captured.event_type,
            topic=captured.topic,
            symbol=captured.symbol,
            exchange_at_ms=captured.exchange_at_ms,
            channel_sequence=captured.channel_sequence,
            raw_text=raw_text,
            received_at_ns=received_at_ns,
            reason=reason,
        )
        if reason is not None:
            self.record_barrier(reason=reason, received_at_ns=received_at_ns)
        return CapturedText(
            captured.event_type,
            captured.topic,
            captured.symbol,
            captured.exchange_at_ms,
            captured.channel_sequence,
            reason,
        )

    def verify(self) -> CaptureStatus:
        connection = self._require_connection()
        if self._segment_buffer:
            raise LiquidationAbsorptionCaptureError("cannot verify while unflushed evidence is buffered")
        rows = connection.execute(
            """
            SELECT segment_index, relative_path, first_sequence, previous_chain_sha256,
                   last_sequence, chain_sha256, compressed_sha256, state
            FROM segments ORDER BY segment_index
            """
        ).fetchall()
        expected_sequence = 1
        previous_chain = "0" * 64
        for row in rows:
            (
                index,
                relative_path,
                first_sequence,
                stored_previous,
                last_sequence,
                stored_chain,
                compressed_sha256,
                state,
            ) = row
            if state == "QUARANTINED":
                continue
            if state != "SEALED":
                raise LiquidationAbsorptionCaptureError("an active capture segment is not immutable")
            if (
                index <= 0
                or first_sequence != expected_sequence
                or stored_previous != previous_chain
                or not isinstance(relative_path, str)
            ):
                raise LiquidationAbsorptionCaptureError("sealed capture segments are not contiguous")
            payload_path = self.output_directory / relative_path
            compressed = payload_path.read_bytes()
            if _sha256(compressed) != compressed_sha256:
                raise LiquidationAbsorptionCaptureError("sealed segment compressed SHA-256 differs")
            sequence, chain = self._verify_segment(payload_path, expected_sequence, previous_chain)
            if sequence != last_sequence or chain != stored_chain:
                raise LiquidationAbsorptionCaptureError("sealed segment metadata differs from event chain")
            expected_sequence = sequence + 1
            previous_chain = chain
        if expected_sequence - 1 != int(self._metadata("last_sealed_sequence")):
            raise LiquidationAbsorptionCaptureError("capture metadata sequence differs from segments")
        if previous_chain != self._metadata("last_sealed_chain_sha256"):
            raise LiquidationAbsorptionCaptureError("capture metadata chain differs from segments")
        return self.status

    def _initialize(self, plan_sha256: str) -> None:
        connection = self._require_connection()
        connection.execute("CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS segments (
                segment_index INTEGER PRIMARY KEY,
                relative_path TEXT NOT NULL UNIQUE,
                first_sequence INTEGER NOT NULL,
                previous_chain_sha256 TEXT NOT NULL,
                last_sequence INTEGER,
                chain_sha256 TEXT,
                compressed_sha256 TEXT,
                state TEXT NOT NULL CHECK(state IN ('OPEN', 'QUARANTINED', 'SEALED')),
                reason TEXT,
                created_at_ns INTEGER NOT NULL,
                sealed_at_ns INTEGER
            )
            """
        )
        immutable = {
            "capture_schema_version": CAPTURE_SCHEMA_VERSION,
            "capture_source_sha256": _sha256(Path(__file__).read_bytes()),
            "plan_sha256": plan_sha256,
        }
        for key, value in immutable.items():
            row = connection.execute("SELECT value FROM metadata WHERE key = ?", (key,)).fetchone()
            if row is None:
                connection.execute("INSERT INTO metadata(key, value) VALUES (?, ?)", (key, value))
            elif row[0] != value:
                raise LiquidationAbsorptionCaptureError(f"capture metadata {key} differs")
        for key, value in {
            "active_lease_heartbeat_ns": "0",
            "active_lease_id": "",
            "last_sealed_chain_sha256": "0" * 64,
            "last_sealed_sequence": "0",
        }.items():
            row = connection.execute("SELECT value FROM metadata WHERE key = ?", (key,)).fetchone()
            if row is None:
                connection.execute("INSERT INTO metadata(key, value) VALUES (?, ?)", (key, value))
        connection.commit()
        self._plan_sha256 = plan_sha256
        self._next_sequence = int(self._metadata("last_sealed_sequence")) + 1
        self._previous_chain = self._metadata("last_sealed_chain_sha256")

    def _acquire_lease(self, now_ns: int) -> None:
        connection = self._require_connection()
        connection.execute("BEGIN IMMEDIATE")
        try:
            active_id = self._metadata("active_lease_id")
            active_heartbeat_ns = int(self._metadata("active_lease_heartbeat_ns"))
            if active_id and now_ns - active_heartbeat_ns < LEASE_STALE_AFTER_NS:
                raise CaptureLeaseActiveError("a recent capture recorder lease is already active")
            self._lease_id = uuid.uuid4().hex
            connection.execute(
                "UPDATE metadata SET value = ? WHERE key = 'active_lease_id'", (self._lease_id,)
            )
            connection.execute(
                "UPDATE metadata SET value = ? WHERE key = 'active_lease_heartbeat_ns'", (str(now_ns),)
            )
            connection.commit()
        except BaseException:
            connection.rollback()
            self._lease_id = None
            raise

    def _touch_lease(self) -> None:
        if self._lease_id is None:
            return
        connection = self._require_connection()
        with connection:
            connection.execute(
                """
                UPDATE metadata SET value = ?
                WHERE key = 'active_lease_heartbeat_ns'
                  AND (SELECT value FROM metadata WHERE key = 'active_lease_id') = ?
                """,
                (str(self._checked_now_ns()), self._lease_id),
            )

    def _release_lease(self) -> None:
        if self._lease_id is None or self._connection is None:
            return
        with self._connection:
            self._connection.execute(
                """
                UPDATE metadata SET value = ''
                WHERE key = 'active_lease_id' AND value = ?
                """,
                (self._lease_id,),
            )
            self._connection.execute(
                """
                UPDATE metadata SET value = '0'
                WHERE key = 'active_lease_heartbeat_ns'
                  AND (SELECT value FROM metadata WHERE key = 'active_lease_id') = ''
                """
            )
        self._lease_id = None

    def _recover_open_segment(self) -> None:
        connection = self._require_connection()
        open_rows = connection.execute(
            """
            SELECT segment_index, first_sequence, previous_chain_sha256
            FROM segments WHERE state = 'OPEN' ORDER BY segment_index
            """
        ).fetchall()
        if len(open_rows) > 1:
            raise LiquidationAbsorptionCaptureError("multiple active capture segments are impossible")
        if not open_rows:
            return
        index, first_sequence, previous_chain = open_rows[0]
        connection.execute(
            "UPDATE segments SET state = 'QUARANTINED', reason = ? WHERE segment_index = ?",
            ("UNCLEAN_PROCESS_STOP", index),
        )
        connection.commit()
        self._next_sequence = int(first_sequence)
        self._previous_chain = cast(str, previous_chain)

    def _begin_segment(self) -> None:
        connection = self._require_connection()
        row = connection.execute("SELECT COALESCE(MAX(segment_index), 0) + 1 FROM segments").fetchone()
        self._segment_index = int(row[0])
        self._segment_initial_sequence = self._next_sequence
        self._segment_initial_chain = self._previous_chain
        self._segment_events = 0
        self._segment_buffer = []
        relative_path = _safe_relative_segment_path(self._segment_index)
        connection.execute(
            """
            INSERT INTO segments(
                segment_index, relative_path, first_sequence, previous_chain_sha256,
                state, created_at_ns
            ) VALUES (?, ?, ?, ?, 'OPEN', ?)
            """,
            (
                self._segment_index,
                relative_path,
                self._segment_initial_sequence,
                self._segment_initial_chain,
                self._checked_now_ns(),
            ),
        )
        connection.commit()

    def _append(
        self,
        *,
        event_type: str,
        topic: str | None,
        symbol: str | None,
        exchange_at_ms: int | None,
        channel_sequence: int | None,
        raw_text: str | None,
        received_at_ns: int,
        reason: str | None,
    ) -> None:
        if event_type not in EVENT_TYPES:
            raise ValueError("capture event type is unsupported")
        if isinstance(received_at_ns, bool) or not isinstance(received_at_ns, int) or received_at_ns < 0:
            raise ValueError("capture receipt timestamp must be a non-negative integer")
        if raw_text is None and event_type == "FRAME":
            raise LiquidationAbsorptionCaptureError("raw frame must preserve original text")
        if raw_text is not None:
            raw_sha256 = _sha256(raw_text.encode("utf-8"))
        else:
            raw_sha256 = None
        body: dict[str, object] = {
            "channel_sequence": channel_sequence,
            "event_schema_version": EVENT_SCHEMA_VERSION,
            "event_type": event_type,
            "exchange_at_ms": exchange_at_ms,
            "previous_chain_sha256": self._previous_chain,
            "raw_text": raw_text,
            "raw_text_sha256": raw_sha256,
            "reason": reason,
            "received_at_ns": received_at_ns,
            "sequence": self._next_sequence,
            "symbol": symbol,
            "topic": topic,
        }
        event_sha256 = _sha256(_canonical_bytes(body))
        chain_sha256 = _sha256(f"{self._previous_chain}:{event_sha256}".encode("ascii"))
        payload = {**body, "event_sha256": event_sha256, "chain_sha256": chain_sha256}
        self._segment_buffer.append(_canonical_bytes(payload) + b"\n")
        self._next_sequence += 1
        self._previous_chain = chain_sha256
        self._segment_events += 1
        if len(self._segment_buffer) >= self._checkpoint_events:
            self._flush_open_member()
        if self._segment_events >= self._segment_max_events:
            self._seal_open_segment()
            self._begin_segment()

    def _flush_open_member(self) -> None:
        if not self._segment_buffer:
            return
        relative_path = _safe_relative_segment_path(self._segment_index)
        path = self.output_directory / relative_path
        encoded = b"".join(self._segment_buffer)
        with path.open("ab") as raw_stream:
            with gzip.GzipFile(fileobj=raw_stream, mode="wb") as compressed_stream:
                compressed_stream.write(encoded)
            raw_stream.flush()
            os.fsync(raw_stream.fileno())
        self._segment_buffer.clear()
        self._touch_lease()

    def _seal_open_segment(self) -> None:
        connection = self._require_connection()
        self._flush_open_member()
        relative_path = _safe_relative_segment_path(self._segment_index)
        path = self.output_directory / relative_path
        if not path.is_file() or path.stat().st_size == 0:
            raise LiquidationAbsorptionCaptureError("cannot seal an empty capture segment")
        last_sequence, chain_sha256 = self._verify_segment(
            path, self._segment_initial_sequence, self._segment_initial_chain
        )
        if last_sequence != self._next_sequence - 1 or chain_sha256 != self._previous_chain:
            raise LiquidationAbsorptionCaptureError("sealed segment differs from its in-memory event chain")
        compressed_sha256 = _sha256(path.read_bytes())
        with connection:
            connection.execute(
                """
                UPDATE segments
                SET last_sequence = ?, chain_sha256 = ?, compressed_sha256 = ?, state = 'SEALED',
                    sealed_at_ns = ?
                WHERE segment_index = ? AND state = 'OPEN'
                """,
                (
                    self._next_sequence - 1,
                    chain_sha256,
                    compressed_sha256,
                    self._checked_now_ns(),
                    self._segment_index,
                ),
            )
            connection.execute(
                "UPDATE metadata SET value = ? WHERE key = 'last_sealed_sequence'",
                (str(self._next_sequence - 1),),
            )
            connection.execute(
                "UPDATE metadata SET value = ? WHERE key = 'last_sealed_chain_sha256'",
                (chain_sha256,),
            )

    def _verify_segment(self, path: Path, expected_sequence: int, previous_chain: str) -> tuple[int, str]:
        try:
            with gzip.open(path, "rb") as stream:
                for raw_line in stream:
                    if not raw_line.endswith(b"\n"):
                        raise LiquidationAbsorptionCaptureError("capture event line is incomplete")
                    try:
                        event = json.loads(raw_line)
                    except json.JSONDecodeError as exc:
                        raise LiquidationAbsorptionCaptureError("capture event is not JSON") from exc
                    if not isinstance(event, dict):
                        raise LiquidationAbsorptionCaptureError("capture event must be an object")
                    chain = event.pop("chain_sha256", None)
                    event_sha = event.pop("event_sha256", None)
                    if event.get("event_schema_version") != EVENT_SCHEMA_VERSION:
                        raise LiquidationAbsorptionCaptureError("capture event schema differs")
                    if event.get("event_type") not in EVENT_TYPES:
                        raise LiquidationAbsorptionCaptureError("capture event type differs")
                    if event.get("sequence") != expected_sequence:
                        raise LiquidationAbsorptionCaptureError("capture event sequence differs")
                    if event.get("previous_chain_sha256") != previous_chain:
                        raise LiquidationAbsorptionCaptureError("capture event predecessor differs")
                    if event_sha != _sha256(_canonical_bytes(event)):
                        raise LiquidationAbsorptionCaptureError("capture event SHA-256 differs")
                    actual_chain = _sha256(f"{previous_chain}:{event_sha}".encode("ascii"))
                    if chain != actual_chain:
                        raise LiquidationAbsorptionCaptureError("capture event chain SHA-256 differs")
                    raw_text = event.get("raw_text")
                    if raw_text is not None:
                        if not isinstance(raw_text, str) or event.get("raw_text_sha256") != _sha256(
                            raw_text.encode("utf-8")
                        ):
                            raise LiquidationAbsorptionCaptureError("capture raw text SHA-256 differs")
                    elif event.get("raw_text_sha256") is not None:
                        raise LiquidationAbsorptionCaptureError("capture non-text event has a raw SHA-256")
                    expected_sequence += 1
                    previous_chain = cast(str, chain)
        except (OSError, EOFError, gzip.BadGzipFile) as exc:
            raise LiquidationAbsorptionCaptureError("capture segment cannot be decoded") from exc
        return expected_sequence - 1, previous_chain

    def _metadata(self, key: str) -> str:
        row = (
            self._require_connection().execute("SELECT value FROM metadata WHERE key = ?", (key,)).fetchone()
        )
        if row is None or not isinstance(row[0], str):
            raise LiquidationAbsorptionCaptureError(f"capture metadata {key} is absent")
        return row[0]

    def _require_connection(self) -> sqlite3.Connection:
        if self._connection is None:
            raise LiquidationAbsorptionCaptureError("capture tape is not open")
        return self._connection

    def _checked_now_ns(self) -> int:
        value = self._now_ns()
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError("capture wall clock must return a non-negative integer")
        return value

    @staticmethod
    def _orderbook_update(raw_text: str, topic: str) -> tuple[int | None, bool]:
        if not topic.startswith("orderbook.50."):
            return None, False
        try:
            payload = json.loads(raw_text)
            data = payload.get("data") if isinstance(payload, Mapping) else None
        except json.JSONDecodeError:
            return None, False
        if not isinstance(data, Mapping):
            return None, False
        return _parse_integer(data.get("u")), payload.get("type") == "snapshot"


class BybitLiquidationAbsorptionCapture:
    """Reconnectable public-only WebSocket capture with explicit source barriers."""

    def __init__(
        self,
        *,
        tape: LiquidationAbsorptionTape,
        websocket_url: str = BYBIT_LINEAR_WEBSOCKET_URL,
        now_ns: Callable[[], int] = _utc_now_ns,
        sleep: Callable[[float], Any] = asyncio.sleep,
    ) -> None:
        if websocket_url != BYBIT_LINEAR_WEBSOCKET_URL:
            raise ValueError("capture requires the preregistered Bybit public linear WebSocket URL")
        self.tape = tape
        self.websocket_url = websocket_url
        self._now_ns = now_ns
        self._sleep = sleep

    @property
    def topics(self) -> tuple[str, ...]:
        return tuple(
            f"{prefix}{symbol}"
            for prefix in ("orderbook.50.", "publicTrade.", "tickers.", "allLiquidation.")
            for symbol in SYMBOLS
        )

    async def run_for(self, duration_seconds: int) -> CaptureStatus:
        if (
            isinstance(duration_seconds, bool)
            or not isinstance(duration_seconds, int)
            or duration_seconds < 1
        ):
            raise ValueError("capture duration must be a positive integer")
        self.tape.open()
        deadline_ns = self._checked_now_ns() + duration_seconds * 1_000_000_000
        delay = DEFAULT_RECONNECT_DELAY_SECONDS
        completed = False
        status: CaptureStatus | None = None
        try:
            timeout = aiohttp.ClientTimeout(total=None, sock_connect=20, sock_read=60)
            async with aiohttp.ClientSession(timeout=timeout, raise_for_status=True) as session:
                while self._checked_now_ns() < deadline_ns:
                    disconnected = False
                    try:
                        async with session.ws_connect(
                            self.websocket_url, heartbeat=20, autoping=True
                        ) as websocket:
                            await websocket.send_json({"op": "subscribe", "args": list(self.topics)})
                            delay = DEFAULT_RECONNECT_DELAY_SECONDS
                            while self._checked_now_ns() < deadline_ns:
                                message = await websocket.receive(timeout=65)
                                if message.type is aiohttp.WSMsgType.TEXT:
                                    captured = self.tape.record_text(
                                        raw_text=cast(str, message.data),
                                        received_at_ns=self._checked_now_ns(),
                                    )
                                    if captured.barrier_reason is not None:
                                        disconnected = True
                                        break
                                elif message.type in {aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.CLOSE}:
                                    disconnected = True
                                    break
                                elif message.type is aiohttp.WSMsgType.ERROR:
                                    disconnected = True
                                    break
                                else:
                                    self.tape.record_barrier(
                                        reason="NON_TEXT_WEBSOCKET_MESSAGE",
                                        received_at_ns=self._checked_now_ns(),
                                    )
                                    disconnected = True
                                    break
                    except (TimeoutError, aiohttp.ClientError):
                        disconnected = True
                    if self._checked_now_ns() >= deadline_ns:
                        break
                    if disconnected:
                        self.tape.record_barrier(
                            reason="WEBSOCKET_RECONNECT", received_at_ns=self._checked_now_ns()
                        )
                    await self._sleep(delay)
                    delay = min(delay * 2, MAXIMUM_RECONNECT_DELAY_SECONDS)
            completed = True
        finally:
            status = self.tape.close(clean=completed)
        if status is None:
            raise AssertionError("clean capture close did not return a status")
        return status

    def _checked_now_ns(self) -> int:
        value = self._now_ns()
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError("capture wall clock must return a non-negative integer")
        return value


def _status_json(status: CaptureStatus) -> str:
    return json.dumps(
        {
            "classification": "PERFORMANCE_BLIND_LIVE_MICROSTRUCTURE_DATA_CAPTURE",
            "last_sealed_chain_sha256": status.last_sealed_chain_sha256,
            "last_sealed_sequence": status.last_sealed_sequence,
            "open_segment_events": status.open_segment_events,
            "open_segment_index": status.open_segment_index,
            "plan_sha256": status.plan_sha256,
            "quarantined_segments": status.quarantined_segments,
            "sealed_segments": status.sealed_segments,
            "trading_access": False,
        },
        sort_keys=True,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-directory", type=Path, required=True)
    parser.add_argument("--plan", type=Path, default=Path(PLAN_FILENAME))
    parser.add_argument("--duration-seconds", type=int)
    parser.add_argument("--verify", action="store_true")
    arguments = parser.parse_args(argv)
    if arguments.verify == (arguments.duration_seconds is not None):
        parser.error("choose exactly one of --verify or --duration-seconds")
    tape = LiquidationAbsorptionTape(output_directory=arguments.output_directory, plan_path=arguments.plan)
    if arguments.verify:
        tape.open(for_capture=False)
        try:
            status = tape.verify()
        finally:
            tape.close(clean=False)
        print(_status_json(status))
        return 0
    status = asyncio.run(
        BybitLiquidationAbsorptionCapture(tape=tape).run_for(cast(int, arguments.duration_seconds))
    )
    print(_status_json(status))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
