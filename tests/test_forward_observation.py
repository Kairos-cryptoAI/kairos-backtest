from __future__ import annotations

import json
import sqlite3
from dataclasses import replace
from pathlib import Path

import pytest
from kairos_core.contracts import (
    ClosedBarEventV1,
    ExitPlanV1,
    StrategyIntentV1,
    StrategyProvenanceV1,
)
from kairos_core.enums import Side
from kairos_strategy.runtime_requirements import RuntimeRequirements

import kairos_backtest.forward_observation as forward
from kairos_backtest.data import ArchiveFieldProfile, DatasetManifest
from kairos_backtest.forward_observation import (
    ForwardIntegrityError,
    ForwardLedger,
    IngestDisposition,
    expected_plan,
    load_plan,
    plan_sha256,
)


def _bar(open_time_ms: int, *, symbol: str = "BTCUSDT", close: float = 100.0) -> ClosedBarEventV1:
    return ClosedBarEventV1(
        source="quant-scouts",
        symbol=symbol,
        open_time_ms=open_time_ms,
        close_time_ms=open_time_ms + 59_999,
        open=close,
        high=close + 1,
        low=close - 1,
        close=close,
        base_volume=10,
        quote_volume=1_000,
        taker_buy_base_volume=5,
        taker_buy_quote_volume=500,
    )


def _intent(history: tuple[ClosedBarEventV1, ...]) -> StrategyIntentV1:
    bar = history[-1]
    return StrategyIntentV1(
        source="strategy-engine",
        strategy_id=forward.STRATEGY_ID,
        strategy_revision="1",
        symbol=bar.symbol,
        side=Side.LONG,
        decision_ts_ms=bar.close_time_ms,
        entry_eligible_ts_ms=bar.close_time_ms + 1,
        entry_expires_ts_ms=bar.close_time_ms + 60 * 60 * 1_000,
        reference_price=100,
        signal_strength=0.5,
        gross_reward_bps=200,
        exit_plan=ExitPlanV1(stop_price=99, target_price=102, max_holding_ms=72 * 60 * 60 * 1_000),
        provenance=StrategyProvenanceV1(
            strategy_code_sha256=forward.STRATEGY_SOURCE_TREE_SHA256,
            config_sha256=forward.STRATEGY_CONFIG_SHA256,
            input_window_sha256="3" * 64,
            features_sha256="4" * 64,
            input_bar_sha256s=tuple(item.bar_sha256 for item in history),
        ),
    )


def test_plan_freezes_forward_boundary_lineage_and_permissions():
    plan = expected_plan()

    assert plan["data"]["blind_start_inclusive"] == "2026-09-01T00:00:00Z"
    assert plan["data"]["minimum_end_exclusive"] == "2027-09-01T00:00:00Z"
    assert plan["candidate"]["runtime_window_bars"] == 57_600
    assert plan["data"]["field_profile"] == "price_volume"
    assert plan["lineage"]["supersedes_prestart_plan_sha256"] == forward.SUPERSEDED_PLAN_SHA256
    assert plan["decision_rule"]["minimum_forward_trades"] == 500
    assert plan["protocol"]["early_performance_access"] is False
    assert not any(plan["permissions"].values())
    assert len(plan_sha256(plan)) == 64


def test_ledger_is_idempotent_and_status_discloses_no_performance(tmp_path: Path):
    path = tmp_path / "forward.sqlite3"
    bar = _bar(forward.WARMUP_START_MS)
    with ForwardLedger(path, expected_plan()) as ledger:
        assert ledger.ingest_bar(bar, as_of_ms=bar.close_time_ms) == (IngestDisposition.INSERTED, 0)
        assert ledger.ingest_bar(bar, as_of_ms=bar.close_time_ms) == (IngestDisposition.DUPLICATE, 0)
        ledger.verify_integrity()
        status = ledger.status()
        assert tuple(ledger.iter_bars("BTCUSDT")) == (forward._price_volume_bar(bar),)

    encoded = json.dumps(status, sort_keys=True)
    assert "pnl" not in encoded
    assert "profit_factor" not in encoded
    assert "net_return" not in encoded
    assert status["blind_performance_disclosed"] is False
    assert sum(item["bar_count"] for item in status["symbols"]) == 1


def test_sealed_dataset_uses_only_the_common_prefix(monkeypatch, tmp_path: Path):
    start = forward.WARMUP_START_MS
    monkeypatch.setattr(forward, "BLIND_START_MS", start - 60_000)
    path = tmp_path / "forward.sqlite3"
    first_rows = tuple(_bar(start, symbol=symbol) for symbol in forward.SYMBOLS)
    with ForwardLedger(path, expected_plan()) as ledger:
        ledger.ingest(first_rows, as_of_ms=start + 59_999)
        watermark = start + 60_000
        first_seal = ledger.sealed_dataset_sha256(watermark)
        ledger.ingest_bar(
            _bar(start + 60_000),
            as_of_ms=start + 2 * 60_000 - 1,
        )
        assert ledger.sealed_dataset_sha256(watermark) == first_seal
        assert tuple(ledger.iter_bars("BTCUSDT", end_exclusive_ms=watermark)) == (
            forward._price_volume_bar(first_rows[0]),
        )


def test_gap_or_conflict_permanently_blocks_only_that_symbol(tmp_path: Path):
    path = tmp_path / "forward.sqlite3"
    first = _bar(forward.WARMUP_START_MS)
    with ForwardLedger(path, expected_plan()) as ledger:
        ledger.ingest_bar(first, as_of_ms=first.close_time_ms)
        gap = _bar(first.open_time_ms + 2 * 60_000)
        with pytest.raises(ForwardIntegrityError, match="gap or reorder"):
            ledger.ingest_bar(gap, as_of_ms=gap.close_time_ms)
        with pytest.raises(ForwardIntegrityError, match="is blocked"):
            ledger.ingest_bar(
                _bar(first.open_time_ms + 60_000),
                as_of_ms=first.close_time_ms + 60_000,
            )

        eth = _bar(forward.WARMUP_START_MS, symbol="ETHUSDT")
        assert ledger.ingest_bar(eth, as_of_ms=eth.close_time_ms)[0] is IngestDisposition.INSERTED


def test_conflicting_replay_is_not_treated_as_a_duplicate(tmp_path: Path):
    path = tmp_path / "forward.sqlite3"
    first = _bar(forward.WARMUP_START_MS)
    with ForwardLedger(path, expected_plan()) as ledger:
        ledger.ingest_bar(first, as_of_ms=first.close_time_ms)
        with pytest.raises(ForwardIntegrityError, match="conflicting"):
            ledger.ingest_bar(
                _bar(forward.WARMUP_START_MS, close=101),
                as_of_ms=first.close_time_ms,
            )


def test_non_strategy_taker_fields_and_transport_envelope_are_canonicalized(tmp_path: Path):
    path = tmp_path / "forward.sqlite3"
    first = _bar(forward.WARMUP_START_MS)
    replay = first.model_copy(
        update={
            "message_id": "transport-replay",
            "source": "another-collector",
            "bar_sha256": None,
            "taker_buy_base_volume": 4.0,
            "taker_buy_quote_volume": 400.0,
        }
    )
    replay = ClosedBarEventV1.model_validate(replay.model_dump(mode="json"))
    with ForwardLedger(path, expected_plan()) as ledger:
        assert ledger.ingest_bar(first, as_of_ms=first.close_time_ms)[0] is IngestDisposition.INSERTED
        assert ledger.ingest_bar(replay, as_of_ms=replay.close_time_ms)[0] is IngestDisposition.DUPLICATE
        payload = ledger.connection.execute("SELECT payload_json FROM bars").fetchone()[0]

    stored = ClosedBarEventV1.model_validate_json(payload)
    assert stored.source == "forward-observer.price-volume"
    assert stored.taker_buy_base_volume == 0
    assert stored.taker_buy_quote_volume == 0


def test_unclosed_or_pre_warmup_bar_is_rejected_without_poisoning_state(tmp_path: Path):
    path = tmp_path / "forward.sqlite3"
    first = _bar(forward.WARMUP_START_MS)
    with ForwardLedger(path, expected_plan()) as ledger:
        with pytest.raises(ValueError, match="predates"):
            ledger.ingest_bar(_bar(forward.WARMUP_START_MS - 60_000))
        with pytest.raises(ValueError, match="not closed"):
            ledger.ingest_bar(first, as_of_ms=first.close_time_ms - 1)
        assert ledger.ingest_bar(first, as_of_ms=first.close_time_ms)[0] is IngestDisposition.INSERTED


def test_full_chain_verification_detects_database_mutation(tmp_path: Path):
    path = tmp_path / "forward.sqlite3"
    first = _bar(forward.WARMUP_START_MS)
    with ForwardLedger(path, expected_plan()) as ledger:
        ledger.ingest_bar(first, as_of_ms=first.close_time_ms)
        ledger.connection.execute(
            "UPDATE bars SET record_sha256=? WHERE symbol=? AND open_time_ms=?",
            ("f" * 64, first.symbol, first.open_time_ms),
        )
        with pytest.raises(ForwardIntegrityError, match="hash chain"):
            ledger.verify_integrity()


def test_backup_and_recovery_drill_are_exclusive_and_preserve_primary(tmp_path: Path):
    primary_path = tmp_path / "forward.sqlite3"
    backup_path = tmp_path / "backups" / "forward.backup.sqlite3"
    recovered_path = tmp_path / "recovery" / "forward.recovered.sqlite3"
    first = _bar(forward.WARMUP_START_MS)
    second = _bar(forward.WARMUP_START_MS + 60_000)

    with ForwardLedger(primary_path, expected_plan()) as ledger:
        ledger.ingest_atomic((first, second), as_of_ms=second.close_time_ms)
        primary_evidence = ledger.evidence_sha256()
        backup = ledger.backup_to(backup_path)
        assert backup.evidence_sha256 == primary_evidence
        assert len(backup.backup_sha256) == 64
        with pytest.raises(FileExistsError):
            ledger.backup_to(backup_path)

        drill = ledger.recovery_drill(backup_path, recovered_path)
        assert drill.evidence_sha256 == primary_evidence
        assert drill.primary_unchanged is True
        assert ledger.evidence_sha256() == primary_evidence

    with ForwardLedger(recovered_path, expected_plan()) as recovered:
        assert recovered.evidence_sha256() == primary_evidence


def test_recovery_drill_rejects_a_tampered_backup(tmp_path: Path):
    primary_path = tmp_path / "forward.sqlite3"
    backup_path = tmp_path / "forward.backup.sqlite3"
    recovered_path = tmp_path / "forward.recovered.sqlite3"
    first = _bar(forward.WARMUP_START_MS)
    with ForwardLedger(primary_path, expected_plan()) as ledger:
        ledger.ingest_bar(first, as_of_ms=first.close_time_ms)
        ledger.backup_to(backup_path)
        with sqlite3.connect(backup_path) as connection:
            connection.execute("UPDATE bars SET record_sha256=?", ("f" * 64,))
        with pytest.raises(ForwardIntegrityError, match="hash chain"):
            ledger.recovery_drill(backup_path, recovered_path)
        assert not recovered_path.exists()


def test_recovery_drill_does_not_create_a_missing_backup(tmp_path: Path):
    primary_path = tmp_path / "forward.sqlite3"
    missing_backup = tmp_path / "missing.sqlite3"
    recovered_path = tmp_path / "forward.recovered.sqlite3"
    with ForwardLedger(primary_path, expected_plan()) as ledger:
        with pytest.raises(FileNotFoundError, match="backup does not exist"):
            ledger.recovery_drill(missing_backup, recovered_path)
    assert not missing_backup.exists()
    assert not recovered_path.exists()


def test_archive_batch_commits_once_and_rolls_back_the_whole_bad_block(tmp_path: Path):
    good_path = tmp_path / "good.sqlite3"
    first = _bar(forward.WARMUP_START_MS)
    second = _bar(forward.WARMUP_START_MS + 60_000)
    with ForwardLedger(good_path, expected_plan()) as ledger:
        summary = ledger.ingest_atomic((first, second), as_of_ms=second.close_time_ms)
        assert summary.inserted_bars == 2
        ledger.verify_integrity()

    bad_path = tmp_path / "bad.sqlite3"
    gap = _bar(forward.WARMUP_START_MS + 2 * 60_000)
    with ForwardLedger(bad_path, expected_plan()) as ledger:
        with pytest.raises(ForwardIntegrityError, match="gap or reorder"):
            ledger.ingest_atomic((first, gap), as_of_ms=gap.close_time_ms)
        btc = next(item for item in ledger.status()["symbols"] if item["symbol"] == "BTCUSDT")
        assert btc["bar_count"] == 0
        assert "gap or reorder" in btc["blocked_reason"]


def test_reopen_refuses_a_different_campaign_plan(tmp_path: Path):
    path = tmp_path / "forward.sqlite3"
    plan = expected_plan()
    with ForwardLedger(path, plan):
        pass
    changed = dict(plan)
    changed["schema_version"] = "different"
    with pytest.raises(ForwardIntegrityError, match="metadata"):
        ForwardLedger(path, changed)


def test_decision_clock_is_the_close_of_the_0000_0059_utc_hour():
    assert forward._is_decision_bar(_bar(forward.BLIND_START_MS + 59 * 60_000))
    assert not forward._is_decision_bar(_bar(forward.BLIND_START_MS + 23 * 60 * 60_000 + 59 * 60_000))


def test_archive_manifest_requires_exact_complete_checksum_verified_price_volume():
    start = forward.date(2026, 7, 23)
    end = forward.date(2026, 7, 24)
    manifest = DatasetManifest(
        symbol="BTCUSDT",
        interval="1m",
        requested_start=start.isoformat(),
        requested_end=end.isoformat(),
        actual_start_ms=forward._date_ms(start),
        actual_end_ms=forward._date_ms(end) - 1,
        rows=1_440,
        sha256="1" * 64,
        files=("BTCUSDT-1m-2026-07.zip",),
        gaps=0,
        transport_verification="zip_crc_and_profiled_rows_sha256",
        checksum_status="official_sha256_verified",
        checksum_files_verified=1,
        expected_files=1,
        csv_schema="binance_futures_kline_v1_12_columns",
        field_profile=ArchiveFieldProfile.PRICE_VOLUME.value,
    )

    forward._validate_archive_manifest(manifest, start, end)
    with pytest.raises(ForwardIntegrityError, match="manifest"):
        forward._validate_archive_manifest(replace(manifest, gaps=1), start, end)


def test_intent_is_persisted_only_after_blind_boundary_and_full_window(monkeypatch, tmp_path: Path):
    start = forward.WARMUP_START_MS
    monkeypatch.setattr(forward, "BLIND_START_MS", start)
    monkeypatch.setattr(forward, "MINIMUM_END_MS", start + 365 * 24 * 60 * 60 * 1_000)
    monkeypatch.setattr(forward, "OBSERVATION_WINDOW_BARS", 2)
    monkeypatch.setattr(
        forward,
        "get_runtime_requirements",
        lambda strategy_id: RuntimeRequirements(
            minimum_window_bars=2,
            decision_interval_bars=2,
            decision_phase_bars=0,
        ),
    )
    calls: list[tuple[ClosedBarEventV1, ...]] = []

    def generator(strategy_id, history):
        calls.append(history)
        return (_intent(history),)

    monkeypatch.setattr(forward, "generate_runtime_strategy_intents", generator)
    first = _bar(start)
    second = _bar(start + 60_000)
    path = tmp_path / "forward.sqlite3"
    with ForwardLedger(path, expected_plan()) as ledger:
        assert ledger.ingest_bar(first, as_of_ms=first.close_time_ms)[1] == 0
        assert ledger.ingest_bar(second, as_of_ms=second.close_time_ms)[1] == 1
        ledger.verify_integrity()
        assert sum(item["intent_count"] for item in ledger.status()["symbols"]) == 1

    assert len(calls) == 1
    assert calls[0] == (forward._price_volume_bar(first), forward._price_volume_bar(second))


def test_committed_plan_matches_executable_plan():
    root = Path(__file__).resolve().parents[1]
    plan = load_plan(root / forward.PLAN_FILENAME)
    assert plan_sha256(plan) == plan_sha256(expected_plan())
