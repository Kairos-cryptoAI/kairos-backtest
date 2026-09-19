from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from kairos_backtest import quarter_hour_features
from kairos_backtest.quarter_hour_features import (
    PLAN_FILENAME,
    QuarterHourFeatureIntegrityError,
    QuarterHourFeatureLedger,
    _logical_sha256,
    collect_features,
    load_plan,
    source_sha256,
)
from kairos_backtest.quarter_hour_v5_compatibility import (
    V5_COMPATIBLE_RUNTIME_SOURCE_SHA256,
    V5_FROZEN_PLAN_SHA256,
    V5_LEDGER_FILENAME,
    QuarterHourV5CompatibilityError,
)
from scripts.quarter_hour_recovery_preflight import (
    V5_FROZEN_FEATURE_SOURCE_SHA256,
    QuarterHourRecoveryPreflightError,
    create_verified_recovery_clone,
)


def _create_fixture(path: Path, *, feature_source_sha256: str) -> None:
    root = Path(__file__).resolve().parents[1]
    plan = load_plan(root / PLAN_FILENAME)
    with QuarterHourFeatureLedger(
        path,
        plan_sha256=_logical_sha256(plan),
        feature_source_sha256=feature_source_sha256,
    ):
        pass


def _create_v5_fixture(path: Path) -> None:
    _create_fixture(path, feature_source_sha256=V5_FROZEN_FEATURE_SOURCE_SHA256)


def test_read_only_ledger_open_never_creates_wal_state(tmp_path: Path) -> None:
    ledger_path = tmp_path / "v5.sqlite3"
    _create_fixture(ledger_path, feature_source_sha256=source_sha256())
    root = Path(__file__).resolve().parents[1]
    plan = load_plan(root / PLAN_FILENAME)
    wal_path = ledger_path.with_name(ledger_path.name + "-wal")
    assert not wal_path.exists()

    with QuarterHourFeatureLedger.open_read_only(
        ledger_path,
        plan_sha256=_logical_sha256(plan),
        feature_source_sha256=source_sha256(),
    ) as ledger:
        assert ledger.verify(require_complete=False, deep=True) == "0" * 64

    assert not wal_path.exists()


def test_preflight_snapshot_includes_committed_wal_and_verifies_hash_chain(tmp_path: Path) -> None:
    ledger_path = tmp_path / "v5.sqlite3"
    clone_path = tmp_path / "backup" / "v5-recovery.sqlite3"
    receipt_path = tmp_path / "backup" / "v5-recovery.receipt.json"
    _create_v5_fixture(ledger_path)
    assert source_sha256() != V5_FROZEN_FEATURE_SOURCE_SHA256
    wal_path = ledger_path.with_name(ledger_path.name + "-wal")
    with sqlite3.connect(ledger_path) as source_connection:
        source_connection.execute("PRAGMA journal_mode=WAL")
        source_connection.execute("PRAGMA wal_autocheckpoint=0")
        source_connection.execute("CREATE TABLE wal_probe(value TEXT NOT NULL)")
        source_connection.execute("INSERT INTO wal_probe(value) VALUES ('committed-wal-frame')")
        source_connection.commit()
        assert wal_path.is_file()

        payload = create_verified_recovery_clone(
            ledger=ledger_path,
            clone=clone_path,
            receipt=receipt_path,
            plan_path=Path(__file__).resolve().parents[1] / PLAN_FILENAME,
        )

    with sqlite3.connect(clone_path) as clone_connection:
        assert clone_connection.execute("SELECT value FROM wal_probe").fetchone() == ("committed-wal-frame",)
    assert payload["clone"]["batch_chain_sha256"] == "0" * 64
    assert payload["clone"]["completed_batches"] == 0
    assert payload["clone"]["includes_committed_wal_frames"] is True
    assert payload["source"]["wal_present"] is True
    assert payload["source"]["feature_source_sha256"] == V5_FROZEN_FEATURE_SOURCE_SHA256
    assert clone_path.is_file()
    assert receipt_path.is_file()


def test_preflight_never_publishes_a_clone_when_hash_chain_validation_fails(tmp_path: Path) -> None:
    ledger_path = tmp_path / "v5.sqlite3"
    clone_path = tmp_path / "backup" / "v5-recovery.sqlite3"
    receipt_path = tmp_path / "backup" / "v5-recovery.receipt.json"
    _create_v5_fixture(ledger_path)
    connection = sqlite3.connect(ledger_path)
    try:
        connection.execute(
            """
            INSERT INTO archive_batch(
                sequence, symbol, period, batch_json, batch_sha256,
                previous_chain_sha256, chain_sha256, last_trade_json,
                window_count, windows_chain_sha256
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (0, "BTCUSDT", "2021-01", "{}", "0" * 64, "0" * 64, "0" * 64, "{}", 0, "0" * 64),
        )
        connection.commit()
    finally:
        connection.close()

    with pytest.raises(QuarterHourFeatureIntegrityError, match="canonical"):
        create_verified_recovery_clone(
            ledger=ledger_path,
            clone=clone_path,
            receipt=receipt_path,
            plan_path=Path(__file__).resolve().parents[1] / PLAN_FILENAME,
        )

    assert not clone_path.exists()
    assert not receipt_path.exists()
    assert list((tmp_path / "backup").glob("v5-recovery.sqlite3.partial-*"))


def test_preflight_rejects_a_ledger_from_an_unknown_feature_source(tmp_path: Path) -> None:
    ledger_path = tmp_path / "v5.sqlite3"
    clone_path = tmp_path / "backup" / "v5-recovery.sqlite3"
    receipt_path = tmp_path / "backup" / "v5-recovery.receipt.json"
    _create_fixture(ledger_path, feature_source_sha256="f" * 64)

    with pytest.raises(QuarterHourRecoveryPreflightError, match="feature_source_sha256"):
        create_verified_recovery_clone(
            ledger=ledger_path,
            clone=clone_path,
            receipt=receipt_path,
            plan_path=Path(__file__).resolve().parents[1] / PLAN_FILENAME,
        )

    assert not clone_path.exists()
    assert not receipt_path.exists()


def test_preflight_rejects_a_ledger_with_a_different_plan_identity(tmp_path: Path) -> None:
    ledger_path = tmp_path / "v5.sqlite3"
    clone_path = tmp_path / "backup" / "v5-recovery.sqlite3"
    receipt_path = tmp_path / "backup" / "v5-recovery.receipt.json"
    _create_v5_fixture(ledger_path)
    connection = sqlite3.connect(ledger_path)
    try:
        connection.execute("UPDATE metadata SET value = 'tampered' WHERE key = 'plan_sha256'")
        connection.commit()
    finally:
        connection.close()

    with pytest.raises(QuarterHourRecoveryPreflightError, match="plan_sha256"):
        create_verified_recovery_clone(
            ledger=ledger_path,
            clone=clone_path,
            receipt=receipt_path,
            plan_path=Path(__file__).resolve().parents[1] / PLAN_FILENAME,
        )

    assert not clone_path.exists()
    assert not receipt_path.exists()


def test_preflight_does_not_mutate_a_sealed_v5_source_ledger(tmp_path: Path) -> None:
    ledger_path = tmp_path / "v5.sqlite3"
    clone_path = tmp_path / "backup" / "v5-recovery.sqlite3"
    receipt_path = tmp_path / "backup" / "v5-recovery.receipt.json"
    _create_v5_fixture(ledger_path)
    source_before = ledger_path.read_bytes()
    wal_path = ledger_path.with_name(ledger_path.name + "-wal")
    assert not wal_path.exists()

    create_verified_recovery_clone(
        ledger=ledger_path,
        clone=clone_path,
        receipt=receipt_path,
        plan_path=Path(__file__).resolve().parents[1] / PLAN_FILENAME,
    )

    assert ledger_path.read_bytes() == source_before
    assert not wal_path.exists()


def test_preflight_refuses_to_overwrite_an_explicit_clone_or_receipt(tmp_path: Path) -> None:
    ledger_path = tmp_path / "v5.sqlite3"
    clone_path = tmp_path / "backup" / "v5-recovery.sqlite3"
    receipt_path = tmp_path / "backup" / "v5-recovery.receipt.json"
    _create_v5_fixture(ledger_path)
    clone_path.parent.mkdir()
    clone_path.write_bytes(b"preserved")

    with pytest.raises(FileExistsError, match="recovery clone already exists"):
        create_verified_recovery_clone(
            ledger=ledger_path,
            clone=clone_path,
            receipt=receipt_path,
            plan_path=Path(__file__).resolve().parents[1] / PLAN_FILENAME,
        )

    assert clone_path.read_bytes() == b"preserved"
    assert not receipt_path.exists()


class _ReachedExtractionWithoutNetwork(RuntimeError):
    """Local sentinel proving the collector passed the V5 ledger-open barrier."""


def test_v5_runtime_source_digest_is_the_exact_reviewed_allowlist() -> None:
    assert source_sha256() == V5_COMPATIBLE_RUNTIME_SOURCE_SHA256


def _collect_v5_until_local_sentinel(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    ledger_path = tmp_path / V5_LEDGER_FILENAME
    _create_v5_fixture(ledger_path)
    reached_extraction = False

    def stop_after_ledger_open(**_: object) -> object:
        nonlocal reached_extraction
        reached_extraction = True
        raise _ReachedExtractionWithoutNetwork

    monkeypatch.setattr(quarter_hour_features, "_extract_one", stop_after_ledger_open)
    root = Path(__file__).resolve().parents[1]
    with pytest.raises(_ReachedExtractionWithoutNetwork):
        collect_features(
            project_root=tmp_path,
            plan_path=root / PLAN_FILENAME,
            ledger_path=ledger_path,
            cache_dir=tmp_path / "no-network-cache",
            loader_factory=lambda _: object(),
            max_new_batches=1,
            require_clean=False,
            v5_compatibility=True,
        )
    assert reached_extraction
    return ledger_path


def test_v5_collector_opens_frozen_ledger_only_with_reviewed_runtime_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ledger_path = _collect_v5_until_local_sentinel(tmp_path, monkeypatch)

    with sqlite3.connect(ledger_path) as connection:
        metadata = dict(connection.execute("SELECT key, value FROM metadata"))
        assert connection.execute("SELECT COUNT(*) FROM archive_batch").fetchone() == (0,)
    assert metadata["feature_source_sha256"] == V5_FROZEN_FEATURE_SOURCE_SHA256
    assert metadata["plan_sha256"] == V5_FROZEN_PLAN_SHA256


def test_default_collector_still_rejects_a_historical_v5_fingerprint(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ledger_path = tmp_path / V5_LEDGER_FILENAME
    _create_v5_fixture(ledger_path)
    monkeypatch.setattr(
        quarter_hour_features,
        "_extract_one",
        lambda **_: pytest.fail("default source check must reject before extraction"),
    )
    root = Path(__file__).resolve().parents[1]

    with pytest.raises(QuarterHourFeatureIntegrityError, match="feature_source_sha256 mismatch"):
        collect_features(
            project_root=tmp_path,
            plan_path=root / PLAN_FILENAME,
            ledger_path=ledger_path,
            cache_dir=tmp_path / "no-network-cache",
            loader_factory=lambda _: object(),
            max_new_batches=1,
            require_clean=False,
        )


def test_v5_compatibility_rejects_an_unreviewed_runtime_digest_before_ledger_open(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ledger_path = tmp_path / V5_LEDGER_FILENAME
    _create_v5_fixture(ledger_path)
    monkeypatch.setattr(quarter_hour_features, "source_sha256", lambda: "f" * 64)
    monkeypatch.setattr(
        quarter_hour_features,
        "_extract_one",
        lambda **_: pytest.fail("unreviewed runtime must reject before extraction"),
    )
    root = Path(__file__).resolve().parents[1]

    with pytest.raises(QuarterHourV5CompatibilityError, match="reviewed V5-compatible runtime digest"):
        collect_features(
            project_root=tmp_path,
            plan_path=root / PLAN_FILENAME,
            ledger_path=ledger_path,
            cache_dir=tmp_path / "no-network-cache",
            loader_factory=lambda _: object(),
            max_new_batches=1,
            require_clean=False,
            v5_compatibility=True,
        )


def test_v5_compatibility_rejects_a_tampered_stored_source_before_extraction(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ledger_path = tmp_path / V5_LEDGER_FILENAME
    _create_v5_fixture(ledger_path)
    with sqlite3.connect(ledger_path) as connection:
        connection.execute("UPDATE metadata SET value = 'tampered' WHERE key = 'feature_source_sha256'")
    monkeypatch.setattr(
        quarter_hour_features,
        "_extract_one",
        lambda **_: pytest.fail("tampered V5 ledger must reject before extraction"),
    )
    root = Path(__file__).resolve().parents[1]

    with pytest.raises(QuarterHourV5CompatibilityError, match="feature_source_sha256"):
        collect_features(
            project_root=tmp_path,
            plan_path=root / PLAN_FILENAME,
            ledger_path=ledger_path,
            cache_dir=tmp_path / "no-network-cache",
            loader_factory=lambda _: object(),
            max_new_batches=1,
            require_clean=False,
            v5_compatibility=True,
        )
