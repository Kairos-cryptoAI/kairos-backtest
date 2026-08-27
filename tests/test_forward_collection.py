from __future__ import annotations

import hashlib
import io
import zipfile
from datetime import date
from pathlib import Path

import pytest
from kairos_strategy.candles import Candle

import kairos_backtest.forward_collection as collection
from kairos_backtest.forward_observation import SYMBOLS, IngestSummary, _date_ms


def _archive(symbol: str, day: date, *, rows: int = 1_440) -> bytes:
    start_ms = _date_ms(day)
    lines = []
    for index in range(rows):
        open_time = start_ms + index * 60_000
        lines.append(f"{open_time},100,101,99,100,10,{open_time + 59_999},1000,1,5,500,0\n")
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(f"{symbol}-1m-{day.isoformat()}.csv", "".join(lines))
    return buffer.getvalue()


class _Response:
    def __init__(self, payload: bytes) -> None:
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        return None

    def read(self) -> bytes:
        return self.payload


def test_daily_loader_requires_official_checksum_and_exact_utc_coverage(tmp_path: Path) -> None:
    day = date(2026, 8, 1)
    symbol = "BTCUSDT"
    filename = f"{symbol}-1m-{day.isoformat()}.zip"
    payload = _archive(symbol, day)
    checksum = f"{hashlib.sha256(payload).hexdigest()}  {filename}\n".encode("ascii")

    def opener(request, *, timeout):
        assert timeout == 60
        return _Response(checksum if request.full_url.endswith(".CHECKSUM") else payload)

    loader = collection.BinanceDailyArchiveLoader(tmp_path, opener=opener)
    rows, manifest = loader.load(symbol, day)

    assert len(rows) == 1_440
    assert rows[0].open_time_ms == _date_ms(day)
    assert rows[-1].close_time_ms == _date_ms(day) + 86_400_000 - 1
    assert manifest.archive_sha256 == hashlib.sha256(payload).hexdigest()
    assert manifest.rows == 1_440
    assert manifest.quarantined_optional_rows == 0
    assert (tmp_path / symbol / "1m" / filename).is_file()
    assert (tmp_path / symbol / "1m" / f"{filename}.CHECKSUM").is_file()


def test_daily_loader_rejects_checksum_mismatch(tmp_path: Path) -> None:
    day = date(2026, 8, 1)
    payload = _archive("BTCUSDT", day)

    def opener(request, *, timeout):
        bad = f"{'0' * 64}  BTCUSDT-1m-{day.isoformat()}.zip\n".encode("ascii")
        return _Response(bad if request.full_url.endswith(".CHECKSUM") else payload)

    with pytest.raises(ValueError, match="SHA-256 mismatch"):
        collection.BinanceDailyArchiveLoader(tmp_path, opener=opener).load("BTCUSDT", day)


def test_daily_loader_rejects_incomplete_day_even_with_valid_checksum(tmp_path: Path) -> None:
    day = date(2026, 8, 1)
    filename = f"BTCUSDT-1m-{day.isoformat()}.zip"
    payload = _archive("BTCUSDT", day, rows=1_439)
    checksum = f"{hashlib.sha256(payload).hexdigest()}  {filename}\n".encode("ascii")

    def opener(request, *, timeout):
        return _Response(checksum if request.full_url.endswith(".CHECKSUM") else payload)

    loader = collection.BinanceDailyArchiveLoader(tmp_path, opener=opener)
    with pytest.raises(collection.ForwardIntegrityError, match="exact UTC-minute coverage"):
        loader.load("BTCUSDT", day)


def _candle(symbol: str, day: date, index: int) -> Candle:
    open_time = _date_ms(day) + index * 60_000
    return Candle(
        symbol=symbol,
        timeframe="1m",
        open_time_ms=open_time,
        close_time_ms=open_time + 59_999,
        open=100.0,
        high=101.0,
        low=99.0,
        close=100.0,
        volume=10.0,
        quote_volume=1_000.0,
        taker_buy_volume=0.0,
        taker_buy_quote_volume=0.0,
    )


def test_collection_stages_entire_universe_before_advancing_any_symbol(tmp_path: Path) -> None:
    day = date(2026, 8, 1)
    loaded: list[str] = []

    class Loader:
        def __init__(self, cache_dir: Path) -> None:
            assert cache_dir == tmp_path

        def load(self, symbol: str, requested_day: date):
            assert requested_day == day
            loaded.append(symbol)
            manifest = collection.DailyArchiveManifest(
                symbol=symbol,
                day=day.isoformat(),
                filename=f"{symbol}.zip",
                archive_sha256="a" * 64,
                normalized_rows_sha256="b" * 64,
                rows=1,
                quarantined_optional_rows=0,
            )
            return [_candle(symbol, day, 0)], manifest

    class Ledger:
        def __init__(self) -> None:
            self.ingested: list[str] = []
            self.verified = False

        def ingest_atomic(self, bars, *, as_of_ms):
            assert loaded == list(SYMBOLS)
            materialized = list(bars)
            assert len(materialized) == 1
            self.ingested.append(materialized[0].symbol)
            return IngestSummary(inserted_bars=1)

        def verify_integrity(self) -> None:
            self.verified = True

    ledger = Ledger()
    summary = collection.collect_daily_archives(
        ledger,  # type: ignore[arg-type]
        tmp_path,
        day,
        date(2026, 8, 2),
        loader_factory=Loader,  # type: ignore[arg-type]
    )

    assert ledger.ingested == list(SYMBOLS)
    assert ledger.verified
    assert summary.inserted_bars == len(SYMBOLS)
    assert len(summary.manifests) == len(SYMBOLS)


def test_collection_download_failure_advances_no_symbol(tmp_path: Path) -> None:
    day = date(2026, 8, 1)

    class Loader:
        def __init__(self, cache_dir: Path) -> None:
            pass

        def load(self, symbol: str, requested_day: date):
            if symbol == SYMBOLS[-1]:
                raise FileNotFoundError("not published")
            return [_candle(symbol, day, 0)], object()

    class Ledger:
        def ingest_atomic(self, bars, *, as_of_ms):
            raise AssertionError("ledger must remain untouched until all downloads validate")

    with pytest.raises(FileNotFoundError, match="not published"):
        collection.collect_daily_archives(
            Ledger(),  # type: ignore[arg-type]
            tmp_path,
            day,
            date(2026, 8, 2),
            loader_factory=Loader,  # type: ignore[arg-type]
        )
