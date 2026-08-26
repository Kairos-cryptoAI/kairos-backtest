from __future__ import annotations

import json
from dataclasses import replace
from datetime import date
from pathlib import Path
from typing import cast

import pytest
from kairos_strategy.candles import Candle

from kairos_backtest.data import (
    ArchiveInventoryAudit,
    DatasetManifest,
    SymbolArchiveAudit,
    month_starts,
)
from kairos_backtest.quarter_hour_screen import (
    DATA_END,
    DATA_START,
    _atomic_write,
    _gate_failures,
    _sha256,
    _utc_ms,
    _validate_archive_audit,
    _validate_window_manifest,
    expected_plan,
    load_preregistered_plan,
)


def _metrics(*, trades: int = 120, total_return: float = 0.02) -> dict[str, object]:
    return {
        "active_symbols": 5,
        "hac_sharpe": 0.5,
        "maximum_drawdown": 0.02,
        "per_symbol": [{"symbol": symbol, "trades": 24} for symbol in ("BTC", "ETH", "SOL", "BNB", "XRP")],
        "profit_factor": 1.2,
        "total_return": total_return,
        "trades": trades,
    }


def test_committed_plan_exactly_matches_the_executable_plan():
    root = Path(__file__).resolve().parents[1]
    path = root / "reports" / "quarter-hour-screen" / "plan.json"
    loaded = load_preregistered_plan(path)

    assert loaded == json.loads(path.read_text(encoding="utf-8"))
    assert _sha256(loaded) == "80cb424675a57a34cf195858a4742dfa891d819b177a1b83f3edd9114515a916"
    assert expected_plan()["permissions"] == {
        "alpha_ready": False,
        "live_allowed": False,
        "paper_allowed": False,
        "promotion_eligible": False,
    }


def test_plan_mutation_is_rejected_before_any_data_loader_exists(tmp_path):
    mutated = expected_plan()
    mutated["classification"] = "promotion"
    path = tmp_path / "plan.json"
    path.write_text(json.dumps(mutated), encoding="utf-8")

    with pytest.raises(ValueError, match="differs"):
        load_preregistered_plan(path)


def test_gates_require_both_selection_and_robustness_under_both_scenarios():
    passing = {
        name: {scenario: _metrics() for scenario in ("baseline", "stress")}
        for name in ("research", "selection", "robustness")
    }
    assert _gate_failures(passing) == ()

    failing = {
        name: {scenario: _metrics() for scenario in ("baseline", "stress")}
        for name in ("research", "selection", "robustness")
    }
    failing["robustness"]["stress"] = _metrics(trades=0, total_return=-0.01)
    failures = _gate_failures(failing)
    assert "robustness.stress.trades_below_minimum" in failures
    assert "robustness.stress.total_return_not_positive" in failures


def test_atomic_result_writer_never_overwrites(tmp_path):
    path = tmp_path / "summary.json"
    _atomic_write(path, {"classification": "REJECT"})
    first = path.read_bytes()

    with pytest.raises(FileExistsError, match="refusing to overwrite"):
        _atomic_write(path, {"classification": "MUTATED"})
    assert path.read_bytes() == first


def test_archive_audit_records_old_gaps_but_window_manifest_still_requires_contiguity():
    symbols = ("BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT")
    files = len(month_starts(DATA_START, DATA_END))
    audits = tuple(
        SymbolArchiveAudit(
            symbol=symbol,
            files=files,
            checksum_files_verified=files,
            rows=1,
            gaps=2 if symbol == "SOLUSDT" else 0,
            first_open_time_ms=_utc_ms(DATA_START),
            last_close_time_ms=_utc_ms(DATA_END) - 1,
            zip_bytes=1,
            invalid_rows=1 if symbol == "XRPUSDT" else 0,
            missing_minutes=1 if symbol in {"SOLUSDT", "XRPUSDT"} else 0,
            coverage_pct=99.0,
            gap_samples=(),
        )
        for symbol in symbols
    )
    inventory = ArchiveInventoryAudit(
        requested_start=DATA_START.isoformat(),
        requested_end=DATA_END.isoformat(),
        expected_files=files * len(symbols),
        present_files=files * len(symbols),
        checksum_files_verified=files * len(symbols),
        rows=5,
        gaps=2,
        zip_bytes=5,
        inventory_sha256="0" * 64,
        csv_schema="binance_futures_kline_v1_12_columns",
        invalid_rows=1,
        invalid_row_samples=(),
        missing_minutes=2,
        coverage_pct=99.0,
        gap_samples=(),
        symbols=audits,
    )
    _validate_archive_audit(inventory)

    start, end = date(2025, 1, 1), date(2025, 1, 3)
    expected_rows = (end - start).days * 24 * 60
    manifest = DatasetManifest(
        symbol="BTCUSDT",
        interval="1m",
        requested_start=start.isoformat(),
        requested_end=end.isoformat(),
        actual_start_ms=_utc_ms(start),
        actual_end_ms=_utc_ms(end) - 1,
        rows=expected_rows,
        sha256="0" * 64,
        files=("BTCUSDT-1m-2025-01.zip",),
        gaps=0,
        transport_verification="zip_crc_and_parsed_rows_sha256",
        checksum_status="official_sha256_verified",
        checksum_files_verified=1,
        expected_files=1,
        csv_schema="binance_futures_kline_v1_12_columns",
    )
    sized_rows = cast("list[Candle]", [None] * expected_rows)
    _validate_window_manifest("BTCUSDT", sized_rows, manifest, start=start, end=end)
    with pytest.raises(ValueError, match="complete verified"):
        _validate_window_manifest(
            "BTCUSDT",
            sized_rows,
            replace(manifest, gaps=1),
            start=start,
            end=end,
        )
