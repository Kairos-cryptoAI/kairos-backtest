from __future__ import annotations

import hashlib
import io
import json
import zipfile
from dataclasses import replace
from datetime import UTC, date, datetime
from pathlib import Path

import numpy as np
import pytest
from kairos_core.enums import Side
from kairos_strategy.candles import Candle

from kairos_backtest.data import ArchiveFieldProfile, BinanceArchiveLoader, audit_cached_archives
from kairos_backtest.evaluation import evaluate
from kairos_backtest.execution import ExecutionConfig
from kairos_backtest.provenance import runtime_manifest, source_fingerprint
from kairos_backtest.readiness import promotion_data_quality_reasons
from kairos_backtest.seeding import derive_seed
from kairos_backtest.strategy import StrategyConfig, StrategySignal, _rsi_series, generate_signals
from kairos_backtest.validation import canonical_candles
from kairos_backtest.validation_campaign import (
    FROZEN_QUANT_SHA,
    FROZEN_QUANT_URL,
    RUNTIME_QUANT_SHA,
    _assert_frozen_dependency,
    _assert_runtime_dependency,
    _cache_snapshot,
    _validate_installed_quant_direct_url,
)


def candles(count: int, *, start_ms: int = 0) -> list[Candle]:
    return [
        Candle(
            symbol="BTCUSDT",
            timeframe="1m",
            open_time_ms=start_ms + index * 60_000,
            close_time_ms=start_ms + (index + 1) * 60_000 - 1,
            open=100 + index / 100,
            high=101 + index / 100,
            low=99 + index / 100,
            close=100.5 + index / 100,
            volume=100,
            quote_volume=10_000,
            taker_buy_volume=55,
        )
        for index in range(count)
    ]


def test_derived_seed_is_stable_and_namespaced():
    first = derive_seed(42, "BTCUSDT", "baseline", date(2025, 1, 1))
    assert first == derive_seed(42, "BTCUSDT", "baseline", date(2025, 1, 1))
    assert first != derive_seed(42, "ETHUSDT", "baseline", date(2025, 1, 1))
    assert first != derive_seed(43, "BTCUSDT", "baseline", date(2025, 1, 1))


def test_flat_price_series_has_neutral_rsi():
    result = _rsi_series(np.full(30, 100.0))

    assert np.all(result == 50.0)


def test_source_fingerprint_is_path_order_independent(tmp_path):
    first = tmp_path / "a.py"
    second = tmp_path / "nested" / "b.py"
    second.parent.mkdir()
    second.write_text("B = 2\n", encoding="utf-8")
    first.write_text("A = 1\n", encoding="utf-8")

    digest = source_fingerprint(tmp_path)

    assert digest == source_fingerprint(tmp_path)
    second.write_text("B = 3\n", encoding="utf-8")
    assert digest != source_fingerprint(tmp_path)


def test_source_fingerprint_is_independent_of_platform_line_endings(tmp_path):
    unix = tmp_path / "unix"
    windows = tmp_path / "windows"
    unix.mkdir()
    windows.mkdir()
    (unix / "module.py").write_bytes(b"first = 1\nsecond = 2\n")
    (windows / "module.py").write_bytes(b"first = 1\r\nsecond = 2\r\n")

    assert source_fingerprint(unix) == source_fingerprint(windows)


def test_runtime_manifest_captures_numeric_environment():
    manifest = runtime_manifest()

    assert manifest["python"]
    assert manifest["implementation"] == "CPython"
    packages = manifest["packages"]
    assert isinstance(packages, dict)
    assert set(packages) == {
        "kairos-backtest",
        "kairos-core",
        "kairos-quant-scouts",
        "kairos-strategy-engine",
        "numpy",
    }


def test_runtime_dependency_matches_the_installed_distribution_and_lock():
    project_root = Path(__file__).resolve().parent.parent

    provenance = _assert_runtime_dependency(project_root)

    installed = provenance["installed_direct_url"]
    assert isinstance(installed, dict)
    assert installed["url"] == FROZEN_QUANT_URL
    assert installed["commit_id"] == RUNTIME_QUANT_SHA
    assert installed["requested_revision"] == RUNTIME_QUANT_SHA


def test_archived_legacy_dependency_is_not_silently_rebased_to_runtime():
    project_root = Path(__file__).resolve().parent.parent

    assert FROZEN_QUANT_SHA != RUNTIME_QUANT_SHA
    with pytest.raises(RuntimeError, match="expected commit"):
        _assert_frozen_dependency(project_root)


def test_frozen_dependency_rejects_missing_direct_url_metadata():
    with pytest.raises(RuntimeError, match="missing direct_url"):
        _validate_installed_quant_direct_url(None)


def test_frozen_dependency_rejects_a_stale_installed_commit():
    stale = json.dumps(
        {
            "url": FROZEN_QUANT_URL,
            "vcs_info": {
                "vcs": "git",
                "commit_id": "0" * 40,
                "requested_revision": FROZEN_QUANT_SHA,
            },
        }
    )

    with pytest.raises(RuntimeError, match="does not match"):
        _validate_installed_quant_direct_url(stale)


def test_cache_snapshot_detects_archive_or_checksum_changes(tmp_path):
    target = tmp_path / "BTCUSDT" / "1m" / "BTCUSDT-1m-2025-01.zip"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"archive")
    checksum = target.with_name(f"{target.name}.CHECKSUM")
    checksum.write_text("checksum-v1", encoding="ascii")

    first = _cache_snapshot(
        tmp_path,
        date(2025, 1, 1),
        date(2025, 2, 1),
        ("BTCUSDT",),
    )
    checksum.write_text("checksum-v2", encoding="ascii")

    assert first != _cache_snapshot(
        tmp_path,
        date(2025, 1, 1),
        date(2025, 2, 1),
        ("BTCUSDT",),
    )


def test_evaluation_uses_seed_for_jitter_and_canonical_signal_order():
    source = candles(300)
    signals = [
        StrategySignal(source[200].close_time_ms, Side.FLAT, 0.0, ("exit",)),
        StrategySignal(source[10].close_time_ms, Side.LONG, 1.0, ("entry",)),
    ]
    execution = ExecutionConfig(slippage_jitter_bps=3)

    first = evaluate(source, signals, initial_equity=10_000, execution=execution, seed=7)
    repeated = evaluate(source, list(reversed(signals)), initial_equity=10_000, execution=execution, seed=7)
    different_seed = evaluate(source, signals, initial_equity=10_000, execution=execution, seed=8)

    assert first == repeated
    assert first != different_seed


def test_strategy_prefix_is_unchanged_by_future_candles():
    prefix = candles(48_100)
    extended = candles(48_600)

    expected = generate_signals(prefix)
    actual = [
        signal for signal in generate_signals(extended) if signal.timestamp_ms <= prefix[-1].close_time_ms
    ]

    assert actual == expected


def test_strategy_state_changes_respect_confirmation_and_minimum_hold():
    config = StrategyConfig()
    signals = generate_signals(candles(60_000), config)

    assert signals
    assert all(
        current.timestamp_ms - previous.timestamp_ms >= config.minimum_hold_bars * 5 * 60_000
        for previous, current in zip(signals, signals[1:], strict=False)
    )


def test_candle_boundaries_reject_duplicates_overlap_and_mixed_symbols():
    source = candles(2)
    with pytest.raises(ValueError, match="duplicate"):
        canonical_candles([source[0], source[0]])

    overlap = Candle(
        symbol="BTCUSDT",
        timeframe="1m",
        open_time_ms=source[0].close_time_ms,
        close_time_ms=source[0].close_time_ms + 60_000,
        open=100,
        high=101,
        low=99,
        close=100,
        volume=1,
    )
    with pytest.raises(ValueError, match="overlap"):
        canonical_candles([source[0], overlap])

    other = Candle(
        symbol="ETHUSDT",
        timeframe="1m",
        open_time_ms=source[1].open_time_ms,
        close_time_ms=source[1].close_time_ms,
        open=100,
        high=101,
        low=99,
        close=100,
        volume=1,
    )
    with pytest.raises(ValueError, match="mix"):
        canonical_candles([source[0], other])


def _archive(rows: list[tuple[int, int]]) -> bytes:
    lines = []
    for opened, closed in rows:
        lines.append(f"{opened},100,101,99,100.5,10,{closed},1000,1,5,500,0")
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("BTCUSDT-1m-2025-01.csv", "\n".join(lines))
    return buffer.getvalue()


def _raw_archive(lines: list[str]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("BTCUSDT-1m-2025-01.csv", "\n".join(lines))
    return buffer.getvalue()


def test_cached_archive_respects_inclusive_start_and_exclusive_end(tmp_path):
    start, end = date(2025, 1, 2), date(2025, 1, 3)
    start_ms = int(datetime(2025, 1, 2, tzinfo=UTC).timestamp() * 1000)
    end_ms = int(datetime(2025, 1, 3, tzinfo=UTC).timestamp() * 1000)
    rows = [
        (start_ms - 60_000, start_ms - 1),
        (start_ms, start_ms + 59_999),
        (end_ms - 60_000, end_ms - 1),
        (end_ms, end_ms + 59_999),
    ]
    target = tmp_path / "BTCUSDT" / "1m" / "BTCUSDT-1m-2025-01.zip"
    target.parent.mkdir(parents=True)
    target.write_bytes(_archive(rows))

    loaded, manifest = BinanceArchiveLoader(tmp_path).load("BTCUSDT", start, end)

    assert [candle.open_time_ms for candle in loaded] == [start_ms, end_ms - 60_000]
    assert manifest.requested_start == "2025-01-02"
    assert manifest.requested_end == "2025-01-03"
    assert manifest.actual_start_ms == start_ms
    assert manifest.actual_end_ms == end_ms - 1
    assert manifest.rows == 2
    assert manifest.sha256
    assert manifest.transport_verification == "zip_crc_and_parsed_rows_sha256"
    assert manifest.checksum_status == "unavailable"
    assert manifest.checksum_files_verified == 0
    assert manifest.expected_files == 1
    assert manifest.csv_schema == "binance_futures_kline_v1_12_columns"


def test_official_checksum_sidecar_is_verified(tmp_path):
    start_ms = int(datetime(2025, 1, 1, tzinfo=UTC).timestamp() * 1000)
    payload = _archive([(start_ms, start_ms + 59_999)])
    target = tmp_path / "BTCUSDT" / "1m" / "BTCUSDT-1m-2025-01.zip"
    target.parent.mkdir(parents=True)
    target.write_bytes(payload)
    target.with_name(f"{target.name}.CHECKSUM").write_text(
        f"{hashlib.sha256(payload).hexdigest()}  {target.name}\n",
        encoding="ascii",
    )

    _, manifest = BinanceArchiveLoader(tmp_path).load("BTCUSDT", date(2025, 1, 1), date(2025, 2, 1))

    assert manifest.checksum_status == "official_sha256_verified"
    assert manifest.checksum_files_verified == 1


def test_checksum_mismatch_fails_closed(tmp_path):
    start_ms = int(datetime(2025, 1, 1, tzinfo=UTC).timestamp() * 1000)
    target = tmp_path / "BTCUSDT" / "1m" / "BTCUSDT-1m-2025-01.zip"
    target.parent.mkdir(parents=True)
    target.write_bytes(_archive([(start_ms, start_ms + 59_999)]))
    target.with_name(f"{target.name}.CHECKSUM").write_text(
        f"{'0' * 64}  {target.name}\n",
        encoding="ascii",
    )

    with pytest.raises(ValueError, match="SHA-256 mismatch"):
        BinanceArchiveLoader(tmp_path).load("BTCUSDT", date(2025, 1, 1), date(2025, 2, 1))


def test_corrupt_cached_archive_fails_closed(tmp_path):
    target = tmp_path / "BTCUSDT" / "1m" / "BTCUSDT-1m-2025-01.zip"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"not a zip")

    with pytest.raises(ValueError, match="corrupt"):
        BinanceArchiveLoader(tmp_path).load(
            "BTCUSDT",
            date(2025, 1, 1),
            date(2025, 2, 1),
        )


@pytest.mark.parametrize(
    "lines",
    [
        ["garbage"],
        ["open_time,open,high,low,close,volume,close_time,quote,trades,taker", "garbage"],
        ["1735689600000,100,101"],
        ["1735689600000,100,101,99,100,1,1735689659999,100,1,0.5,50,0,extra"],
        ["1735689600000,100,101,99,100,1,1735689659999,100,not-an-int,0.5,50,0"],
        ["1735689600000,100,101,99,100,1,1735689659999,100,1,0.5,50,1"],
    ],
)
def test_malformed_csv_row_fails_closed(tmp_path, lines):
    target = tmp_path / "BTCUSDT" / "1m" / "BTCUSDT-1m-2025-01.zip"
    target.parent.mkdir(parents=True)
    target.write_bytes(_raw_archive(lines))

    with pytest.raises(ValueError, match="line"):
        BinanceArchiveLoader(tmp_path).load("BTCUSDT", date(2025, 1, 1), date(2025, 2, 1))


def test_exact_official_header_is_accepted_and_schema_drift_is_rejected(tmp_path):
    header = (
        "open_time,open,high,low,close,volume,close_time,quote_volume,count,"
        "taker_buy_volume,taker_buy_quote_volume,ignore"
    )
    row = "1735689600000,100,101,99,100,1,1735689659999,100,1,0.5,50,0"
    target = tmp_path / "BTCUSDT" / "1m" / "BTCUSDT-1m-2025-01.zip"
    target.parent.mkdir(parents=True)
    target.write_bytes(_raw_archive([header, row]))

    loaded, _ = BinanceArchiveLoader(tmp_path).load("BTCUSDT", date(2025, 1, 1), date(2025, 2, 1))

    assert len(loaded) == 1
    target.write_bytes(_raw_archive([header.replace("count", "trades"), row]))
    with pytest.raises(ValueError, match="header"):
        BinanceArchiveLoader(tmp_path).load("BTCUSDT", date(2025, 1, 1), date(2025, 2, 1))


def test_duplicate_archive_rows_fail_closed(tmp_path):
    row = "1735689600000,100,101,99,100,1,1735689659999,100,1,0.5,50,0"
    target = tmp_path / "BTCUSDT" / "1m" / "BTCUSDT-1m-2025-01.zip"
    target.parent.mkdir(parents=True)
    target.write_bytes(_raw_archive([row, row]))

    with pytest.raises(ValueError, match="duplicate"):
        BinanceArchiveLoader(tmp_path).load("BTCUSDT", date(2025, 1, 1), date(2025, 2, 1))


def test_checksum_sidecar_must_reference_the_archive(tmp_path):
    payload = _raw_archive(["1735689600000,100,101,99,100,1,1735689659999,100,1,0.5,50,0"])
    target = tmp_path / "BTCUSDT" / "1m" / "BTCUSDT-1m-2025-01.zip"
    target.parent.mkdir(parents=True)
    target.write_bytes(payload)
    target.with_name(f"{target.name}.CHECKSUM").write_text(
        f"{hashlib.sha256(payload).hexdigest()}  wrong.zip\n",
        encoding="ascii",
    )

    with pytest.raises(ValueError, match="wrong file"):
        BinanceArchiveLoader(tmp_path).load("BTCUSDT", date(2025, 1, 1), date(2025, 2, 1))


def test_offline_archive_inventory_audit_is_strict_and_fingerprinted(tmp_path):
    payload = _raw_archive(["1735689600000,100,101,99,100,1,1735689659999,100,1,0.5,50,0"])
    target = tmp_path / "BTCUSDT" / "1m" / "BTCUSDT-1m-2025-01.zip"
    target.parent.mkdir(parents=True)
    target.write_bytes(payload)
    target.with_name(f"{target.name}.CHECKSUM").write_text(
        f"{hashlib.sha256(payload).hexdigest()}  {target.name}\n",
        encoding="ascii",
    )

    audit = audit_cached_archives(
        tmp_path,
        ("BTCUSDT",),
        date(2025, 1, 1),
        date(2025, 2, 1),
    )

    assert audit.expected_files == audit.present_files == audit.checksum_files_verified == 1
    assert audit.rows == 1
    assert audit.gaps == 0
    assert audit.invalid_rows == 0
    assert len(audit.inventory_sha256) == 64

    impossible = replace(
        audit,
        expected_files=-1,
        present_files=-1,
        checksum_files_verified=-1,
        rows=-1,
        zip_bytes=-1,
        coverage_pct=100.0,
    )
    assert "dataset_audit_invalid" in promotion_data_quality_reasons((impossible,))

    claimed_complete_symbol = replace(
        audit.symbols[0],
        last_close_time_ms=int(datetime(2025, 2, 1, tzinfo=UTC).timestamp() * 1000) - 1,
        missing_minutes=0,
        coverage_pct=100.0,
    )
    claimed_complete = replace(
        audit,
        missing_minutes=0,
        coverage_pct=100.0,
        symbols=(claimed_complete_symbol,),
    )
    assert "dataset_audit_invalid" in promotion_data_quality_reasons((claimed_complete,))


def test_inventory_records_but_loader_rejects_source_domain_anomalies(tmp_path):
    valid = "1735689600000,100,101,99,100,1,1735689659999,100,1,0.5,50,0"
    invalid = "1735689660000,100,101,99,100,1,1735689719999,100,1,2,100,0"
    payload = _raw_archive([valid, invalid])
    target = tmp_path / "BTCUSDT" / "1m" / "BTCUSDT-1m-2025-01.zip"
    target.parent.mkdir(parents=True)
    target.write_bytes(payload)
    target.with_name(f"{target.name}.CHECKSUM").write_text(
        f"{hashlib.sha256(payload).hexdigest()}  {target.name}\n",
        encoding="ascii",
    )

    with pytest.raises(ValueError, match="taker-buy volume"):
        BinanceArchiveLoader(tmp_path, allow_download=False).load(
            "BTCUSDT", date(2025, 1, 1), date(2025, 2, 1)
        )
    audit = audit_cached_archives(
        tmp_path,
        ("BTCUSDT",),
        date(2025, 1, 1),
        date(2025, 2, 1),
    )

    assert audit.rows == 1
    assert audit.invalid_rows == 1
    assert "taker-buy volume" in audit.invalid_row_samples[0]
    assert set(promotion_data_quality_reasons((audit,))) == {
        "dataset_audit_invalid",
        "dataset_invalid_rows",
        "dataset_gaps",
        "dataset_incomplete_coverage",
    }

    profiled, manifest = BinanceArchiveLoader(
        tmp_path,
        allow_download=False,
        field_profile=ArchiveFieldProfile.PRICE_VOLUME,
    ).load("BTCUSDT", date(2025, 1, 1), date(2025, 2, 1))

    assert len(profiled) == 2
    assert profiled[1].volume == 1
    assert profiled[1].quote_volume == 100
    assert profiled[1].taker_buy_volume == 0
    assert profiled[1].taker_buy_quote_volume == 0
    assert manifest.field_profile == "price_volume"
    assert manifest.transport_verification == "zip_crc_and_profiled_rows_sha256"
    assert manifest.quarantined_optional_rows == 1
    assert manifest.quarantined_optional_samples == (
        "BTCUSDT-1m-2025-01.zip:line 2:taker-buy volume exceeds total volume",
    )


def test_price_volume_profile_still_rejects_price_or_volume_domain_corruption(tmp_path):
    invalid = "1735689600000,100,101,99,100,1,1735689659999,200,1,0.5,50,0"
    payload = _raw_archive([invalid])
    target = tmp_path / "BTCUSDT" / "1m" / "BTCUSDT-1m-2025-01.zip"
    target.parent.mkdir(parents=True)
    target.write_bytes(payload)
    target.with_name(f"{target.name}.CHECKSUM").write_text(
        f"{hashlib.sha256(payload).hexdigest()}  {target.name}\n",
        encoding="ascii",
    )

    with pytest.raises(ValueError, match="quote volume is inconsistent"):
        BinanceArchiveLoader(
            tmp_path,
            allow_download=False,
            field_profile=ArchiveFieldProfile.PRICE_VOLUME,
        ).load("BTCUSDT", date(2025, 1, 1), date(2025, 2, 1))


def test_archive_audit_rejects_cross_archive_duplicate_timestamps(tmp_path):
    opened = int(datetime(2025, 1, 31, 23, 59, tzinfo=UTC).timestamp() * 1000)
    row = f"{opened},100,101,99,100,1,{opened + 59_999},100,1,0.5,50,0"
    for month in ("2025-01", "2025-02"):
        payload = _raw_archive([row])
        target = tmp_path / "BTCUSDT" / "1m" / f"BTCUSDT-1m-{month}.zip"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(payload)
        target.with_name(f"{target.name}.CHECKSUM").write_text(
            f"{hashlib.sha256(payload).hexdigest()}  {target.name}\n",
            encoding="ascii",
        )

    with pytest.raises(ValueError, match="cross-archive duplicate"):
        audit_cached_archives(
            tmp_path,
            ("BTCUSDT",),
            date(2025, 1, 1),
            date(2025, 3, 1),
        )


def test_archive_audit_rejects_cross_archive_reversed_timestamps(tmp_path):
    opened = int(datetime(2025, 1, 31, 23, 59, tzinfo=UTC).timestamp() * 1000)
    rows = {
        "2025-01": f"{opened},100,101,99,100,1,{opened + 59_999},100,1,0.5,50,0",
        "2025-02": (f"{opened - 60_000},100,101,99,100,1,{opened - 1},100,1,0.5,50,0"),
    }
    for month, row in rows.items():
        payload = _raw_archive([row])
        target = tmp_path / "BTCUSDT" / "1m" / f"BTCUSDT-1m-{month}.zip"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(payload)
        target.with_name(f"{target.name}.CHECKSUM").write_text(
            f"{hashlib.sha256(payload).hexdigest()}  {target.name}\n",
            encoding="ascii",
        )

    with pytest.raises(ValueError, match="duplicate or reversed"):
        audit_cached_archives(
            tmp_path,
            ("BTCUSDT",),
            date(2025, 1, 1),
            date(2025, 3, 1),
        )
