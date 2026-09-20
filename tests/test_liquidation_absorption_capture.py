from __future__ import annotations

import gzip
import hashlib
import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest

from kairos_backtest.liquidation_absorption_capture import (
    CaptureLeaseActiveError,
    CaptureNotYetEligibleError,
    LiquidationAbsorptionCaptureError,
    LiquidationAbsorptionTape,
)
from kairos_backtest.liquidation_absorption_preflight import PLAN_FILENAME, PLAN_LOGICAL_SHA256

ROOT = Path(__file__).resolve().parents[1]
PLAN = ROOT / PLAN_FILENAME
AFTER_START_NS = int(datetime(2026, 9, 22, tzinfo=UTC).timestamp() * 1_000_000_000)


def _tape(tmp_path: Path, **overrides: object) -> LiquidationAbsorptionTape:
    values: dict[str, object] = {
        "output_directory": tmp_path / "capture",
        "plan_path": PLAN,
        "now_ns": lambda: AFTER_START_NS,
        "checkpoint_events": 1,
        "segment_max_events": 100,
    }
    values.update(overrides)
    return LiquidationAbsorptionTape(**values)


def _book(*, update: int, source_ms: int, kind: str = "snapshot") -> str:
    return json.dumps(
        {
            "data": {"a": [["101", "2"]], "b": [["100", "3"]], "cts": source_ms, "seq": update, "u": update},
            "topic": "orderbook.50.BTCUSDT",
            "ts": source_ms,
            "type": kind,
        },
        separators=(",", ":"),
        sort_keys=True,
    )


def _sealed_events(output: Path) -> list[dict[str, object]]:
    segment = next((output / "segments").glob("*.ndjson.gz"))
    with gzip.open(segment, "rt", encoding="utf-8") as stream:
        return [json.loads(line) for line in stream]


def _verify(output: Path) -> None:
    verifier = LiquidationAbsorptionTape(
        output_directory=output,
        plan_path=PLAN,
        now_ns=lambda: AFTER_START_NS,
    )
    verifier.open(for_capture=False)
    try:
        status = verifier.verify()
        assert status.plan_sha256 == PLAN_LOGICAL_SHA256
    finally:
        verifier.close(clean=False)


def test_capture_binds_the_committed_plan_and_preserves_exact_raw_text(tmp_path: Path) -> None:
    tape = _tape(tmp_path)
    tape.open()
    raw = _book(update=1, source_ms=AFTER_START_NS // 1_000_000)
    captured = tape.record_text(raw_text=raw, received_at_ns=AFTER_START_NS)
    status = tape.close(clean=True)

    assert captured.barrier_reason is None
    assert status is not None and status.sealed_segments == 1
    events = _sealed_events(tmp_path / "capture")
    source = next(event for event in events if event["event_type"] == "FRAME")
    assert source["raw_text"] == raw
    assert source["raw_text_sha256"] == hashlib.sha256(raw.encode("utf-8")).hexdigest()
    assert source["symbol"] == "BTCUSDT"
    _verify(tmp_path / "capture")


def test_source_update_gap_records_raw_frame_then_explicit_barrier(tmp_path: Path) -> None:
    tape = _tape(tmp_path)
    tape.open()
    tape.record_text(
        raw_text=_book(update=10, source_ms=AFTER_START_NS // 1_000_000), received_at_ns=AFTER_START_NS
    )
    captured = tape.record_text(
        raw_text=_book(update=12, source_ms=AFTER_START_NS // 1_000_000 + 10, kind="delta"),
        received_at_ns=AFTER_START_NS + 10_000_000,
    )
    tape.close(clean=True)

    assert captured.barrier_reason == "ORDERBOOK_UPDATE_GAP"
    events = _sealed_events(tmp_path / "capture")
    assert [event["reason"] for event in events if event["event_type"] == "BARRIER"] == [
        "CAPTURE_STARTED",
        "ORDERBOOK_UPDATE_GAP",
        "CAPTURE_STOPPED",
    ]
    _verify(tmp_path / "capture")


def test_public_trade_and_liquidation_array_payloads_remain_raw_admitted_events(tmp_path: Path) -> None:
    tape = _tape(tmp_path)
    tape.open()
    source_ms = AFTER_START_NS // 1_000_000
    public_trade = json.dumps(
        {
            "data": [{"T": source_ms, "p": "100", "s": "BTCUSDT", "v": "1"}],
            "topic": "publicTrade.BTCUSDT",
            "ts": source_ms,
        },
        separators=(",", ":"),
        sort_keys=True,
    )
    liquidation = json.dumps(
        {
            "data": [{"S": "Buy", "T": source_ms, "s": "BTCUSDT", "v": "1"}],
            "topic": "allLiquidation.BTCUSDT",
            "ts": source_ms,
        },
        separators=(",", ":"),
        sort_keys=True,
    )

    assert tape.record_text(raw_text=public_trade, received_at_ns=AFTER_START_NS).barrier_reason is None
    assert tape.record_text(raw_text=liquidation, received_at_ns=AFTER_START_NS).barrier_reason is None
    tape.close(clean=True)
    _verify(tmp_path / "capture")


def test_unclean_open_segment_is_quarantined_and_new_chain_restarts_at_barrier(tmp_path: Path) -> None:
    tape = _tape(tmp_path)
    tape.open()
    tape.record_text(
        raw_text=_book(update=1, source_ms=AFTER_START_NS // 1_000_000), received_at_ns=AFTER_START_NS
    )
    tape.close(clean=False)

    resumed = _tape(tmp_path)
    resumed.open()
    resumed.record_text(
        raw_text=_book(update=1, source_ms=AFTER_START_NS // 1_000_000 + 10), received_at_ns=AFTER_START_NS
    )
    status = resumed.close(clean=True)

    assert status is not None and status.quarantined_segments == 1
    _verify(tmp_path / "capture")


def test_capture_rejects_start_before_preregistered_boundary(tmp_path: Path) -> None:
    tape = _tape(tmp_path, now_ns=lambda: 0)

    with pytest.raises(CaptureNotYetEligibleError, match="preregistered UTC boundary"):
        tape.open()


def test_capture_refuses_a_second_recent_writer_lease(tmp_path: Path) -> None:
    first = _tape(tmp_path)
    first.open()
    second = _tape(tmp_path)

    with pytest.raises(CaptureLeaseActiveError, match="already active"):
        second.open()

    first.close(clean=False)
    second.open()
    second.close(clean=True)


def test_verifier_rejects_a_mutated_sealed_compressed_segment(tmp_path: Path) -> None:
    tape = _tape(tmp_path)
    tape.open()
    tape.record_text(
        raw_text=_book(update=1, source_ms=AFTER_START_NS // 1_000_000), received_at_ns=AFTER_START_NS
    )
    tape.close(clean=True)
    segment = next((tmp_path / "capture" / "segments").glob("*.ndjson.gz"))
    segment.write_bytes(segment.read_bytes() + b"mutation")

    verifier = _tape(tmp_path)
    verifier.open(for_capture=False)
    try:
        with pytest.raises(LiquidationAbsorptionCaptureError, match="compressed SHA-256"):
            verifier.verify()
    finally:
        verifier.close(clean=False)


def test_verifier_does_not_create_a_segment_or_mutate_accepted_tape(tmp_path: Path) -> None:
    tape = _tape(tmp_path)
    tape.open()
    tape.record_text(
        raw_text=_book(update=1, source_ms=AFTER_START_NS // 1_000_000), received_at_ns=AFTER_START_NS
    )
    sealed = tape.close(clean=True)

    output = tmp_path / "capture"
    before_segment_bytes = {
        path.name: path.read_bytes() for path in sorted((output / "segments").glob("*.ndjson.gz"))
    }
    with sqlite3.connect(output / "capture.sqlite3") as connection:
        before_rows = connection.execute("SELECT * FROM segments ORDER BY segment_index").fetchall()

    verifier = _tape(tmp_path)
    verifier.open(for_capture=False)
    try:
        verified = verifier.verify()
    finally:
        verifier.close(clean=False)

    with sqlite3.connect(output / "capture.sqlite3") as connection:
        after_rows = connection.execute("SELECT * FROM segments ORDER BY segment_index").fetchall()
    after_segment_bytes = {
        path.name: path.read_bytes() for path in sorted((output / "segments").glob("*.ndjson.gz"))
    }

    assert sealed is not None and verified == sealed
    assert after_rows == before_rows
    assert after_segment_bytes == before_segment_bytes
