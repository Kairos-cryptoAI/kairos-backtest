from __future__ import annotations

import sqlite3
from decimal import Decimal
from pathlib import Path

import pytest

from kairos_backtest.quarter_hour_execution_ledger import (
    GENESIS_SHA256,
    EntryTickLedger,
    EntryTickLedgerIntegrityError,
    source_sha256,
)
from kairos_backtest.quarter_hour_execution_overlay import (
    PLAN_SHA256,
    EntryTickRequest,
    EntryTickResolution,
    EntryTickStatus,
)

PARENT_SHA256 = "1" * 64
SOURCE_SHA256 = "2" * 64


def test_entry_tick_ledger_source_fingerprint_is_deterministic() -> None:
    first = source_sha256()
    assert source_sha256() == first
    assert len(first) == 64
    assert set(first) <= set("0123456789abcdef")


def _found(request_id: str, submission_ms: int, trade_id: int) -> EntryTickResolution:
    return EntryTickResolution(
        request=EntryTickRequest(request_id, submission_ms),
        status=EntryTickStatus.FOUND,
        aggregate_trade_id=trade_id,
        transact_time_ms=submission_ms + 10,
        price=Decimal("100.5000"),
    )


def _ledger(path: Path) -> EntryTickLedger:
    return EntryTickLedger(
        path,
        plan_sha256=PLAN_SHA256,
        parent_result_sha256=PARENT_SHA256,
        extractor_source_sha256=SOURCE_SHA256,
    )


def test_entry_tick_ledger_append_reopen_and_idempotent_replay(tmp_path: Path) -> None:
    path = tmp_path / "ticks.sqlite3"
    first = _found("first", 1_000, 10)
    second = EntryTickResolution(
        request=EntryTickRequest("second", 2_000),
        status=EntryTickStatus.TIMEOUT,
    )

    with _ledger(path) as ledger:
        first_chain = ledger.append(0, first)
        assert ledger.append(0, first) == first_chain
        final_chain = ledger.append(1, second)
        assert ledger.completed_records() == 2
        assert ledger.verify() == final_chain

    with _ledger(path) as reopened:
        assert reopened.completed_records() == 2
        assert reopened.verify() == final_chain
        assert final_chain != GENESIS_SHA256


def test_entry_tick_ledger_rejects_conflict_and_noncontiguous_append(tmp_path: Path) -> None:
    path = tmp_path / "ticks.sqlite3"
    with _ledger(path) as ledger:
        ledger.append(0, _found("first", 1_000, 10))
        with pytest.raises(EntryTickLedgerIntegrityError, match="idempotency conflict"):
            ledger.append(0, _found("first", 1_000, 11))
        with pytest.raises(EntryTickLedgerIntegrityError, match="not contiguous"):
            ledger.append(2, _found("third", 3_000, 30))


@pytest.mark.parametrize(
    "column",
    [
        "record_json",
        "record_sha256",
        "previous_chain_sha256",
        "chain_sha256",
    ],
)
def test_entry_tick_ledger_detects_record_tampering(tmp_path: Path, column: str) -> None:
    path = tmp_path / "ticks.sqlite3"
    with _ledger(path) as ledger:
        ledger.append(0, _found("first", 1_000, 10))

    with sqlite3.connect(path) as connection:
        connection.execute(f"UPDATE entry_tick_record SET {column} = ? WHERE sequence = 0", ("f" * 64,))
        connection.commit()

    with _ledger(path) as ledger:
        with pytest.raises(EntryTickLedgerIntegrityError):
            ledger.verify()


def test_entry_tick_ledger_detects_metadata_mismatch(tmp_path: Path) -> None:
    path = tmp_path / "ticks.sqlite3"
    with _ledger(path):
        pass

    with pytest.raises(EntryTickLedgerIntegrityError, match="metadata mismatch"):
        EntryTickLedger(
            path,
            plan_sha256=PLAN_SHA256,
            parent_result_sha256="3" * 64,
            extractor_source_sha256=SOURCE_SHA256,
        )
