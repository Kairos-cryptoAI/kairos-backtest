from __future__ import annotations

import hashlib
import io
import zipfile
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from kairos_backtest import aggtrades


class _Response:
    def __init__(self, payload: bytes) -> None:
        self._stream = io.BytesIO(payload)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        return None

    def read(self, size: int = -1) -> bytes:
        return self._stream.read(size)


def _archive(symbol: str, day: date, rows: list[str], *, member_name: str | None = None) -> bytes:
    filename = f"{symbol}-aggTrades-{day.isoformat()}"
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(member_name or f"{filename}.csv", "".join(rows))
    return buffer.getvalue()


def _loader(tmp_path: Path, symbol: str, day: date, payload: bytes) -> aggtrades.BinanceAggTradeArchiveLoader:
    filename = f"{symbol}-aggTrades-{day.isoformat()}.zip"
    checksum = f"{hashlib.sha256(payload).hexdigest()}  {filename}\n".encode("ascii")

    def opener(request, *, timeout):
        assert timeout == 60
        return _Response(checksum if request.full_url.endswith(".CHECKSUM") else payload)

    return aggtrades.BinanceAggTradeArchiveLoader(tmp_path, opener=opener)


def _monthly_archive(symbol: str, month: date, rows: list[str]) -> bytes:
    stem = f"{symbol}-aggTrades-{month:%Y-%m}"
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(f"{stem}.csv", "".join(rows))
    return buffer.getvalue()


def _monthly_loader(
    tmp_path: Path,
    symbol: str,
    month: date,
    payload: bytes,
) -> aggtrades.BinanceMonthlyAggTradeArchiveLoader:
    filename = f"{symbol}-aggTrades-{month:%Y-%m}.zip"
    checksum = f"{hashlib.sha256(payload).hexdigest()}  {filename}\n".encode("ascii")

    def opener(request, *, timeout):
        assert timeout == 60
        return _Response(checksum if request.full_url.endswith(".CHECKSUM") else payload)

    return aggtrades.BinanceMonthlyAggTradeArchiveLoader(tmp_path, opener=opener)


def test_loader_verifies_checksum_crc_schema_and_records_source_gaps(tmp_path: Path) -> None:
    symbol = "BTCUSDT"
    day = date(2026, 8, 1)
    start_ms = aggtrades._date_ms(day)
    rows = [
        "agg_trade_id,price,quantity,first_trade_id,last_trade_id,transact_time,is_buyer_maker\n",
        f"10,100.00,2.0,100,101,{start_ms + 1},false\n",
        f"12,101.00,3.0,103,104,{start_ms + 2},true\n",
    ]
    payload = _archive(symbol, day, rows)

    loader = _loader(tmp_path, symbol, day, payload)
    archive = loader.load(symbol, day)
    trades = list(loader.iter_trades(archive))

    assert len(trades) == 2
    assert trades[0].buyer_taker_quantity == Decimal("2.0")
    assert trades[1].seller_taker_quantity == Decimal("3.0")
    assert archive.manifest.archive_sha256 == hashlib.sha256(payload).hexdigest()
    assert archive.manifest.rows == 2
    assert archive.manifest.first_aggregate_trade_id == 10
    assert archive.manifest.last_aggregate_trade_id == 12
    assert archive.manifest.missing_aggregate_trade_ids == 1
    assert archive.manifest.missing_raw_trade_ids == 1
    assert len(archive.manifest.normalized_rows_sha256) == 64


def test_loader_rejects_official_checksum_mismatch(tmp_path: Path) -> None:
    symbol = "ETHUSDT"
    day = date(2026, 8, 1)
    start_ms = aggtrades._date_ms(day)
    payload = _archive(symbol, day, [f"1,100,1,1,1,{start_ms + 1},false\n"])
    filename = f"{symbol}-aggTrades-{day.isoformat()}.zip"
    checksum = f"{'0' * 64}  {filename}\n".encode("ascii")

    def opener(request, *, timeout):
        return _Response(checksum if request.full_url.endswith(".CHECKSUM") else payload)

    with pytest.raises(aggtrades.AggTradeIntegrityError, match="SHA-256 mismatch"):
        aggtrades.BinanceAggTradeArchiveLoader(tmp_path, opener=opener).load(symbol, day)


def test_loader_rejects_overlapping_raw_trade_ranges(tmp_path: Path) -> None:
    symbol = "SOLUSDT"
    day = date(2026, 8, 1)
    start_ms = aggtrades._date_ms(day)
    payload = _archive(
        symbol,
        day,
        [
            f"1,100,1,10,12,{start_ms + 1},false\n",
            f"2,101,1,12,13,{start_ms + 2},true\n",
        ],
    )

    with pytest.raises(aggtrades.AggTradeIntegrityError, match="non-overlapping"):
        _loader(tmp_path, symbol, day, payload).load(symbol, day)


def test_loader_rejects_unexpected_zip_member(tmp_path: Path) -> None:
    symbol = "BNBUSDT"
    day = date(2026, 8, 1)
    start_ms = aggtrades._date_ms(day)
    payload = _archive(
        symbol,
        day,
        [f"1,100,1,1,1,{start_ms + 1},false\n"],
        member_name="unexpected.csv",
    )

    with pytest.raises(aggtrades.AggTradeIntegrityError, match="must contain only"):
        _loader(tmp_path, symbol, day, payload).load(symbol, day)


def test_monthly_transport_and_phase_extraction_scan_rows_once(tmp_path: Path) -> None:
    symbol = "BTCUSDT"
    month = date(2026, 7, 1)
    start_ms = aggtrades._date_ms(month)
    end_ms = aggtrades._date_ms(date(2026, 8, 1))
    rows = [
        "agg_trade_id,price,quantity,first_trade_id,last_trade_id,transact_time,is_buyer_maker\n",
        f"10,100,1,100,100,{start_ms},false\n",
        f"11,110,2,101,101,{start_ms + 1},false\n",
        f"12,90,1,103,103,{start_ms + 10_000},true\n",
        f"13,50,10,104,104,{start_ms + 10_001},true\n",
        f"14,101,1,105,105,{end_ms - 1},false\n",
    ]
    payload = _monthly_archive(symbol, month, rows)
    loader = _monthly_loader(tmp_path, symbol, month, payload)

    transport = loader.load(symbol, month)
    extraction = aggtrades.extract_phase_peak_windows(
        transport,
        loader.iter_trades(transport),
        phase_offsets_minutes=(0,),
        prior_trade=_trade(9, start_ms - 1, "99", "1", buyer_is_maker=True),
    )

    assert transport.archive_sha256 == hashlib.sha256(payload).hexdigest()
    assert extraction.manifest.rows == 5
    assert extraction.manifest.missing_aggregate_trade_ids == 0
    assert extraction.manifest.missing_raw_trade_ids == 1
    assert extraction.manifest.last_transact_time_ms == end_ms - 1
    assert extraction.last_trade.aggregate_trade_id == 14
    assert extraction.expected_windows == 31 * 24 * 4
    assert extraction.empty_windows == extraction.expected_windows - 1
    assert extraction.missing_reference_windows == 0
    assert len(extraction.windows) == 1
    window = extraction.windows[0]
    assert window.opening_reference_price == Decimal("100")
    assert window.vwap == Decimal(310) / Decimal(3)
    assert window.trade_count == 2
    assert window.missing_aggregate_trade_ids == 0
    assert window.missing_raw_trade_ids == 1


def test_multi_phase_extraction_is_causal_sorted_and_cross_month_gap_aware() -> None:
    month = date(2026, 7, 1)
    start_ms = aggtrades._date_ms(month)
    end_ms = aggtrades._date_ms(date(2026, 8, 1))
    transport = aggtrades.MonthlyAggTradeTransport(
        symbol="ETHUSDT",
        month="2026-07",
        start_ms=start_ms,
        end_ms=end_ms,
        path=Path("ETHUSDT-aggTrades-2026-07.zip"),
        member_name="ETHUSDT-aggTrades-2026-07.csv",
        archive_sha256="a" * 64,
    )
    prior = aggtrades.AggTrade(
        aggregate_trade_id=1,
        price=Decimal("99"),
        quantity=Decimal("1"),
        first_trade_id=10,
        last_trade_id=10,
        transact_time_ms=start_ms - 1,
        buyer_is_maker=True,
    )
    trades = [
        aggtrades.AggTrade(2, Decimal("101"), Decimal("1"), 12, 12, start_ms + 1, False),
        aggtrades.AggTrade(
            3,
            Decimal("102"),
            Decimal("1"),
            13,
            13,
            start_ms + 120_000,
            True,
        ),
        aggtrades.AggTrade(
            4,
            Decimal("103"),
            Decimal("1"),
            14,
            14,
            start_ms + 120_001,
            False,
        ),
        aggtrades.AggTrade(
            5,
            Decimal("104"),
            Decimal("1"),
            15,
            15,
            end_ms - 1,
            False,
        ),
    ]

    extraction = aggtrades.extract_phase_peak_windows(
        transport,
        trades,
        phase_offsets_minutes=(0, 2),
        prior_trade=prior,
    )

    assert [window.phase_offset_minutes for window in extraction.windows] == [0, 2]
    assert [window.start_ms for window in extraction.windows] == [start_ms, start_ms + 120_000]
    assert extraction.windows[0].missing_raw_trade_ids == 1
    assert extraction.windows[1].opening_reference_price == Decimal("102")
    assert extraction.windows[1].open_to_vwap_return == Decimal("103") / Decimal("102") - 1


def test_monthly_transport_rejects_partial_or_future_month(tmp_path: Path) -> None:
    loader = aggtrades.BinanceMonthlyAggTradeArchiveLoader(tmp_path)
    with pytest.raises(ValueError, match="day one"):
        loader.load("BTCUSDT", date(2026, 7, 2))
    with pytest.raises(ValueError, match="completed UTC month"):
        loader.load("BTCUSDT", date(2026, 8, 1))


def _trade(
    aggregate_id: int,
    timestamp_ms: int,
    price: str,
    quantity: str,
    *,
    buyer_is_maker: bool,
) -> aggtrades.AggTrade:
    return aggtrades.AggTrade(
        aggregate_trade_id=aggregate_id,
        price=Decimal(price),
        quantity=Decimal(quantity),
        first_trade_id=aggregate_id,
        last_trade_id=aggregate_id,
        transact_time_ms=timestamp_ms,
        buyer_is_maker=buyer_is_maker,
    )


def test_peak_window_uses_causal_open_interval_and_closed_end() -> None:
    day_start = aggtrades._date_ms(date(2026, 8, 1))
    boundary = day_start + 15 * 60_000
    trades = [
        _trade(1, boundary, "100", "1", buyer_is_maker=False),
        _trade(2, boundary + 1, "110", "2", buyer_is_maker=False),
        _trade(3, boundary + 10_000, "90", "1", buyer_is_maker=True),
        _trade(4, boundary + 10_001, "50", "10", buyer_is_maker=True),
    ]

    windows = list(
        aggtrades.quarter_hour_peak_windows(
            trades,
            start_ms=boundary,
            end_ms=boundary + 15 * 60_000,
        )
    )

    assert len(windows) == 1
    window = windows[0]
    assert window.opening_reference_price == Decimal("100")
    assert window.trade_count == 2
    assert window.vwap == Decimal(310) / Decimal(3)
    assert window.order_imbalance == Decimal(1) / Decimal(3)
    assert window.first_aggregate_trade_id == 2
    assert window.last_aggregate_trade_id == 3


def test_peak_window_drops_empty_forward_windows() -> None:
    boundary = aggtrades._date_ms(date(2026, 8, 1))
    trades = [_trade(1, boundary, "100", "1", buyer_is_maker=False)]

    assert (
        list(
            aggtrades.quarter_hour_peak_windows(
                trades,
                start_ms=boundary,
                end_ms=boundary + 15 * 60_000,
            )
        )
        == []
    )


def test_peak_window_rejects_reordered_input() -> None:
    boundary = aggtrades._date_ms(date(2026, 8, 1))
    trades = [
        _trade(2, boundary + 2, "100", "1", buyer_is_maker=False),
        _trade(1, boundary + 1, "100", "1", buyer_is_maker=False),
    ]

    with pytest.raises(aggtrades.AggTradeIntegrityError, match="not ordered"):
        list(
            aggtrades.quarter_hour_peak_windows(
                trades,
                start_ms=boundary,
                end_ms=boundary + 15 * 60_000,
                prior_trade=_trade(0, boundary - 1, "99", "1", buyer_is_maker=True),
            )
        )


def test_completed_days_is_exclusive_and_rejects_invalid_ranges() -> None:
    assert aggtrades.completed_days(date(2026, 8, 1), date(2026, 8, 3)) == (
        date(2026, 8, 1),
        date(2026, 8, 2),
    )
    with pytest.raises(ValueError, match="must precede"):
        aggtrades.completed_days(date(2026, 8, 1), date(2026, 8, 1))
