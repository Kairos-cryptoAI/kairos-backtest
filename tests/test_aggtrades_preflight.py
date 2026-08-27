from __future__ import annotations

import json
import sqlite3
from dataclasses import replace
from datetime import date
from pathlib import Path

import pytest

from kairos_backtest import aggtrades_preflight as preflight
from kairos_backtest.aggtrades import AggTradeArchive, AggTradeArchiveManifest
from kairos_backtest.scenarios import SYMBOLS


def _plan(path: Path) -> Path:
    path.write_bytes(preflight._json_bytes(preflight.expected_plan()))
    return path


def _manifest(symbol: str, day: date, *, rows: int = 10) -> AggTradeArchiveManifest:
    return AggTradeArchiveManifest(
        symbol=symbol,
        day=day.isoformat(),
        filename=f"{symbol}-aggTrades-{day.isoformat()}.zip",
        archive_sha256=(symbol[0].lower() if symbol[0].isalpha() else "a") * 64,
        normalized_rows_sha256="b" * 64,
        rows=rows,
        first_aggregate_trade_id=1,
        last_aggregate_trade_id=rows,
        first_transact_time_ms=1_787_616_000_001,
        last_transact_time_ms=1_787_616_010_000,
        missing_aggregate_trade_ids=0,
        missing_raw_trade_ids=2,
    )


def test_expected_plan_round_trips_and_rejects_mutation(tmp_path: Path) -> None:
    path = _plan(tmp_path / "plan.json")
    assert preflight.load_plan(path) == preflight.expected_plan()

    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["data"]["start"] = "2026-08-24"
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(preflight.AggTradePreflightIntegrityError, match="differs"):
        preflight.load_plan(path)


def test_ledger_is_idempotent_and_rejects_conflicting_replay(tmp_path: Path) -> None:
    ledger_path = tmp_path / "preflight.sqlite3"
    manifest = _manifest(SYMBOLS[0], date(2026, 8, 25))
    with preflight.AggTradePreflightLedger(ledger_path, "a" * 64) as ledger:
        assert ledger.append(0, manifest)
        assert not ledger.append(0, manifest)
        with pytest.raises(preflight.AggTradePreflightIntegrityError, match="conflicts"):
            ledger.append(0, replace(manifest, rows=11))
        assert len(ledger.verify(require_complete=False)) == 64


def test_ledger_detects_chain_tampering(tmp_path: Path) -> None:
    ledger_path = tmp_path / "preflight.sqlite3"
    with preflight.AggTradePreflightLedger(ledger_path, "a" * 64) as ledger:
        ledger.append(0, _manifest(SYMBOLS[0], date(2026, 8, 25)))
    connection = sqlite3.connect(ledger_path)
    connection.execute("UPDATE archive_manifest SET chain_sha256 = ? WHERE sequence = 0", ("0" * 64,))
    connection.commit()
    connection.close()

    with preflight.AggTradePreflightLedger(ledger_path, "a" * 64) as ledger:
        with pytest.raises(preflight.AggTradePreflightIntegrityError, match="hash chain"):
            ledger.verify(require_complete=False)


def test_run_and_verify_write_only_performance_blind_transport_evidence(tmp_path: Path) -> None:
    plan_path = _plan(tmp_path / "plan.json")
    loaded: list[tuple[str, date]] = []

    class Loader:
        def __init__(self, cache_dir: Path) -> None:
            assert cache_dir == tmp_path / "cache"

        def load(self, symbol: str, day: date) -> AggTradeArchive:
            loaded.append((symbol, day))
            return AggTradeArchive(
                path=tmp_path / f"{symbol}.zip",
                member_name=f"{symbol}.csv",
                manifest=_manifest(symbol, day),
            )

    result_path = tmp_path / "result.json"
    ledger_path = tmp_path / "preflight.sqlite3"
    result = preflight.run_preflight(
        plan_path=plan_path,
        ledger_path=ledger_path,
        cache_dir=tmp_path / "cache",
        result_path=result_path,
        project_root=tmp_path,
        loader_factory=Loader,  # type: ignore[arg-type]
        require_clean=False,
        git_head_sha="c" * 40,
    )

    assert loaded == [(symbol, date(2026, 8, 25)) for symbol in SYMBOLS]
    assert result["classification"] == "DATA_PREFLIGHT_PASSED"
    assert result["evidence"]["archive_count"] == len(SYMBOLS)  # type: ignore[index]
    assert result["evidence"]["rows"] == 10 * len(SYMBOLS)  # type: ignore[index]
    serialized = result_path.read_text(encoding="utf-8").lower()
    for forbidden in ("profit_factor", '"pnl"', '"return"', '"signal"', '"trade_count"'):
        assert forbidden not in serialized
    verified = preflight.verify_preflight(
        plan_path=plan_path,
        ledger_path=ledger_path,
        result_path=result_path,
    )
    assert verified == result


def test_run_resumes_after_an_interrupted_prefix(tmp_path: Path) -> None:
    plan_path = _plan(tmp_path / "plan.json")
    plan_sha256 = preflight._logical_sha256(preflight.expected_plan())
    ledger_path = tmp_path / "preflight.sqlite3"
    with preflight.AggTradePreflightLedger(ledger_path, plan_sha256) as ledger:
        ledger.append(0, _manifest(SYMBOLS[0], date(2026, 8, 25)))
    loaded: list[str] = []

    class Loader:
        def __init__(self, cache_dir: Path) -> None:
            pass

        def load(self, symbol: str, day: date) -> AggTradeArchive:
            loaded.append(symbol)
            return AggTradeArchive(
                path=tmp_path / f"{symbol}.zip",
                member_name=f"{symbol}.csv",
                manifest=_manifest(symbol, day),
            )

    preflight.run_preflight(
        plan_path=plan_path,
        ledger_path=ledger_path,
        cache_dir=tmp_path / "cache",
        result_path=tmp_path / "result.json",
        project_root=tmp_path,
        loader_factory=Loader,  # type: ignore[arg-type]
        require_clean=False,
        git_head_sha="c" * 40,
    )

    assert loaded == list(SYMBOLS[1:])
