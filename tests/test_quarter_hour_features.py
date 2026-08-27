from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from kairos_backtest.aggtrades import (
    AggTrade,
    AggTradePeriodManifest,
    PhasePeakExtraction,
    PhasePeakWindow,
)
from kairos_backtest.quarter_hour_features import (
    PLAN_LOGICAL_SHA256,
    QuarterHourFeatureIntegrityError,
    QuarterHourFeatureLedger,
    _expected_windows,
    _logical_sha256,
    collect_features,
    expected_sequence,
    load_plan,
    months,
)


def _extraction(
    *,
    symbol: str,
    period: str,
    first_aggregate_id: int = 10,
    first_raw_id: int = 100,
    return_value: str = "0.001",
) -> PhasePeakExtraction:
    month = date.fromisoformat(f"{period}-01")
    start_ms = int(datetime(month.year, month.month, 1, tzinfo=UTC).timestamp() * 1_000)
    trade = AggTrade(
        aggregate_trade_id=first_aggregate_id,
        price=Decimal("100"),
        quantity=Decimal("1"),
        first_trade_id=first_raw_id,
        last_trade_id=first_raw_id,
        transact_time_ms=start_ms + 10_000,
        buyer_is_maker=False,
    )
    response = Decimal(return_value)
    window = PhasePeakWindow(
        phase_offset_minutes=0,
        start_ms=start_ms,
        end_ms=start_ms + 10_000,
        opening_reference_price=Decimal("100"),
        vwap=Decimal("100") * (Decimal(1) + response),
        total_quantity=Decimal("1"),
        buyer_taker_quantity=Decimal("1"),
        seller_taker_quantity=Decimal(0),
        trade_count=1,
        first_aggregate_trade_id=first_aggregate_id,
        last_aggregate_trade_id=first_aggregate_id,
        missing_aggregate_trade_ids=0,
        missing_raw_trade_ids=0,
    )
    manifest = AggTradePeriodManifest(
        symbol=symbol,
        period=period,
        filename=f"{symbol}-aggTrades-{period}.zip",
        archive_sha256="a" * 64,
        normalized_rows_sha256="b" * 64,
        rows=1,
        first_aggregate_trade_id=first_aggregate_id,
        last_aggregate_trade_id=first_aggregate_id,
        first_raw_trade_id=first_raw_id,
        last_raw_trade_id=first_raw_id,
        first_transact_time_ms=trade.transact_time_ms,
        last_transact_time_ms=trade.transact_time_ms,
        missing_aggregate_trade_ids=0,
        missing_raw_trade_ids=0,
    )
    expected = _expected_windows(period)
    return PhasePeakExtraction(
        manifest=manifest,
        windows=(window,),
        last_trade=trade,
        expected_windows=expected,
        empty_windows=expected - 1,
        missing_reference_windows=0,
    )


def test_committed_plan_matches_the_preregistered_logical_hash(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[1]
    plan_path = root / "reports" / "quarter-hour-lag-replication" / "plan.json"
    plan = load_plan(plan_path)

    assert _logical_sha256(plan) == PLAN_LOGICAL_SHA256
    assert plan["classification"] == "PERFORMANCE_BLIND_STATISTICAL_REPLICATION"

    mutated = dict(plan)
    mutated["classification"] = "ALPHA"
    path = tmp_path / "plan.json"
    path.write_text(json.dumps(mutated), encoding="utf-8")
    with pytest.raises(QuarterHourFeatureIntegrityError, match="preregistration"):
        load_plan(path)


def test_month_sequence_is_exact_and_exclusive() -> None:
    assert months(date(2026, 6, 1), date(2026, 8, 1)) == (
        date(2026, 6, 1),
        date(2026, 7, 1),
    )
    assert len(expected_sequence()) == 67 * 5
    assert expected_sequence()[0] == ("BTCUSDT", "2021-01")
    assert expected_sequence()[-1] == ("XRPUSDT", "2026-07")


def test_ledger_appends_replays_verifies_and_queries_canonical_features(tmp_path: Path) -> None:
    extraction = _extraction(symbol="BTCUSDT", period="2021-01")
    ledger_path = tmp_path / "features.sqlite3"
    with QuarterHourFeatureLedger(
        ledger_path,
        plan_sha256=PLAN_LOGICAL_SHA256,
        feature_source_sha256="c" * 64,
    ) as ledger:
        assert ledger.append(0, extraction) is True
        assert ledger.append(0, extraction) is False
        assert ledger.completed_batches() == 1
        assert len(ledger.verify(require_complete=False, deep=True)) == 64
        rows = ledger.phase_returns(
            symbol="BTCUSDT",
            phase_offset_minutes=0,
            start_ms=extraction.windows[0].start_ms,
            end_ms=extraction.windows[0].end_ms + 1,
            clean_only=True,
        )
        assert rows == ((extraction.windows[0].start_ms, 0.001),)

    with pytest.raises(QuarterHourFeatureIntegrityError, match="source_sha256 mismatch"):
        QuarterHourFeatureLedger(
            ledger_path,
            plan_sha256=PLAN_LOGICAL_SHA256,
            feature_source_sha256="d" * 64,
        )


def test_ledger_rejects_source_gaps_mutation_and_out_of_order_batches(tmp_path: Path) -> None:
    first = _extraction(symbol="BTCUSDT", period="2021-01")
    bad_window = replace(first.windows[0], missing_aggregate_trade_ids=1)
    mutated = replace(first, windows=(bad_window,))
    with QuarterHourFeatureLedger(
        tmp_path / "features.sqlite3",
        plan_sha256=PLAN_LOGICAL_SHA256,
        feature_source_sha256="c" * 64,
    ) as ledger:
        with pytest.raises(QuarterHourFeatureIntegrityError, match="target crosses"):
            ledger.append(0, mutated)
        second = _extraction(symbol="ETHUSDT", period="2021-01")
        with pytest.raises(QuarterHourFeatureIntegrityError, match="exact plan order"):
            ledger.append(1, second)


def test_ledger_detects_cross_period_aggregate_id_gap(tmp_path: Path) -> None:
    with QuarterHourFeatureLedger(
        tmp_path / "features.sqlite3",
        plan_sha256=PLAN_LOGICAL_SHA256,
        feature_source_sha256="c" * 64,
    ) as ledger:
        for sequence, symbol in enumerate(("BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT")):
            ledger.append(sequence, _extraction(symbol=symbol, period="2021-01"))
        february = _extraction(
            symbol="BTCUSDT",
            period="2021-02",
            first_aggregate_id=12,
            first_raw_id=101,
        )
        with pytest.raises(QuarterHourFeatureIntegrityError, match="cross-period gap"):
            ledger.append(5, february)


def test_collection_rejects_invalid_parallelism_before_opening_data(tmp_path: Path) -> None:
    arguments = {
        "project_root": tmp_path,
        "plan_path": tmp_path / "plan.json",
        "ledger_path": tmp_path / "features.sqlite3",
        "cache_dir": tmp_path / "cache",
        "require_clean": False,
    }
    with pytest.raises(ValueError, match="workers"):
        collect_features(**arguments, workers=0)
    with pytest.raises(ValueError, match="positive integer"):
        collect_features(**arguments, max_new_batches=0)
