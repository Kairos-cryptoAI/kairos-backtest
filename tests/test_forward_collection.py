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


class _SyncLedger:
    def __init__(self, watermark: date) -> None:
        self.watermark_ms = _date_ms(watermark)
        self.ingested: list[tuple[str, int]] = []
        self.verify_calls = 0

    def status(self) -> dict[str, object]:
        return {
            "watermark_ms": self.watermark_ms,
            "symbols": [
                {"symbol": symbol, "blocked_reason": None, "last_open_time_ms": self.watermark_ms - 60_000}
                for symbol in SYMBOLS
            ],
        }

    def ingest_atomic(self, bars, *, as_of_ms):
        materialized = list(bars)
        assert materialized
        self.ingested.append((materialized[0].symbol, as_of_ms))
        return IngestSummary(inserted_bars=len(materialized))

    def verify_integrity(self) -> None:
        self.verify_calls += 1


def _sync_manifest(symbol: str, day: date) -> collection.DailyArchiveManifest:
    return collection.DailyArchiveManifest(
        symbol=symbol,
        day=day.isoformat(),
        filename=f"{symbol}-{day.isoformat()}.zip",
        archive_sha256="a" * 64,
        normalized_rows_sha256="b" * 64,
        rows=1,
        quarantined_optional_rows=0,
    )


def test_sync_latest_resumes_from_common_watermark_and_advances_complete_days(tmp_path: Path) -> None:
    start = date(2026, 8, 24)
    today = date(2026, 8, 26)
    loaded: list[tuple[str, date]] = []

    class Loader:
        def __init__(self, cache_dir: Path) -> None:
            assert cache_dir == tmp_path

        def load(self, symbol: str, day: date):
            loaded.append((symbol, day))
            return [_candle(symbol, day, 0)], _sync_manifest(symbol, day)

    ledger = _SyncLedger(start)
    summary = collection.sync_latest_daily_archives(
        ledger,  # type: ignore[arg-type]
        tmp_path,
        today=today,
        loader_factory=Loader,  # type: ignore[arg-type]
    )

    assert loaded == [(symbol, day) for day in (date(2026, 8, 24), date(2026, 8, 25)) for symbol in SYMBOLS]
    assert summary.start == "2026-08-24"
    assert summary.latest_published_end_exclusive == "2026-08-26"
    assert summary.stopped_at_unpublished_day is None
    assert summary.inserted_bars == 2 * len(SYMBOLS)
    assert ledger.verify_calls == 2


def test_sync_latest_treats_yesterday_404_as_publication_lag_without_mutation(tmp_path: Path) -> None:
    yesterday = date(2026, 8, 26)

    class Loader:
        def __init__(self, cache_dir: Path) -> None:
            pass

        def load(self, symbol: str, day: date):
            if symbol == SYMBOLS[1]:
                raise FileNotFoundError("not published")
            return [_candle(symbol, day, 0)], _sync_manifest(symbol, day)

    ledger = _SyncLedger(yesterday)
    summary = collection.sync_latest_daily_archives(
        ledger,  # type: ignore[arg-type]
        tmp_path,
        today=date(2026, 8, 27),
        loader_factory=Loader,  # type: ignore[arg-type]
    )

    assert summary.latest_published_end_exclusive == yesterday.isoformat()
    assert summary.stopped_at_unpublished_day == yesterday.isoformat()
    assert summary.inserted_bars == 0
    assert ledger.ingested == []
    assert ledger.verify_calls == 0


def test_sync_latest_rejects_an_unpublished_day_outside_grace(tmp_path: Path) -> None:
    missing = date(2026, 8, 24)

    class Loader:
        def __init__(self, cache_dir: Path) -> None:
            pass

        def load(self, symbol: str, day: date):
            raise FileNotFoundError("not published")

    ledger = _SyncLedger(missing)
    with pytest.raises(collection.ForwardIntegrityError, match="exceeds publication grace"):
        collection.sync_latest_daily_archives(
            ledger,  # type: ignore[arg-type]
            tmp_path,
            today=date(2026, 8, 27),
            loader_factory=Loader,  # type: ignore[arg-type]
        )
    assert ledger.ingested == []


def test_sync_latest_requires_a_utc_midnight_common_watermark(tmp_path: Path) -> None:
    ledger = _SyncLedger(date(2026, 8, 26))
    ledger.watermark_ms += 60_000

    with pytest.raises(collection.ForwardIntegrityError, match="UTC-midnight"):
        collection.sync_latest_daily_archives(
            ledger,  # type: ignore[arg-type]
            tmp_path,
            today=date(2026, 8, 27),
        )
