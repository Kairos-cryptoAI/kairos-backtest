"""Strict Binance USD-M aggregate-trade archives and causal peak windows.

The quarter-hour paper uses transaction-level data, not one-minute candle
proxies.  This module provides only a checksum-verified, performance-blind data
surface.  It does not generate a direction, fit a model, calculate PnL, or
authorize any trading mode.
"""

from __future__ import annotations

import csv
import hashlib
import io
import os
import tempfile
import time
import zipfile
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .scenarios import SYMBOLS

DAILY_AGGTRADES_ROOT = "https://data.binance.vision/data/futures/um/daily/aggTrades"
_DAY_MS = 86_400_000
_PEAK_WINDOW_MS = 10_000
_QUARTER_HOUR_MS = 15 * 60_000
_DOWNLOAD_CHUNK_BYTES = 1024 * 1024


class AggTradeIntegrityError(RuntimeError):
    """The official transport or normalized aggregate-trade stream is invalid."""


@dataclass(frozen=True, slots=True)
class AggTrade:
    aggregate_trade_id: int
    price: Decimal
    quantity: Decimal
    first_trade_id: int
    last_trade_id: int
    transact_time_ms: int
    buyer_is_maker: bool

    @property
    def buyer_taker_quantity(self) -> Decimal:
        return Decimal(0) if self.buyer_is_maker else self.quantity

    @property
    def seller_taker_quantity(self) -> Decimal:
        return self.quantity if self.buyer_is_maker else Decimal(0)


@dataclass(frozen=True, slots=True)
class AggTradeArchiveManifest:
    symbol: str
    day: str
    filename: str
    archive_sha256: str
    normalized_rows_sha256: str
    rows: int
    first_aggregate_trade_id: int
    last_aggregate_trade_id: int
    first_transact_time_ms: int
    last_transact_time_ms: int
    missing_aggregate_trade_ids: int
    missing_raw_trade_ids: int


@dataclass(frozen=True, slots=True)
class AggTradeArchive:
    path: Path
    member_name: str
    manifest: AggTradeArchiveManifest


@dataclass(frozen=True, slots=True)
class QuarterHourPeakWindow:
    start_ms: int
    end_ms: int
    opening_reference_price: Decimal
    vwap: Decimal
    total_quantity: Decimal
    buyer_taker_quantity: Decimal
    seller_taker_quantity: Decimal
    trade_count: int
    first_aggregate_trade_id: int
    last_aggregate_trade_id: int

    @property
    def open_to_vwap_return(self) -> Decimal:
        return self.vwap / self.opening_reference_price - Decimal(1)

    @property
    def order_imbalance(self) -> Decimal:
        return (self.buyer_taker_quantity - self.seller_taker_quantity) / self.total_quantity


def _date_ms(value: date) -> int:
    return int(datetime(value.year, value.month, value.day, tzinfo=UTC).timestamp() * 1_000)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(_DOWNLOAD_CHUNK_BYTES):
            digest.update(chunk)
    return digest.hexdigest()


def _canonical_decimal(value: Decimal) -> str:
    normalized = value.normalize()
    return "0" if normalized == 0 else format(normalized, "f")


def _parse_decimal(raw: str, *, field: str, row_number: int) -> Decimal:
    try:
        value = Decimal(raw)
    except InvalidOperation as exc:
        raise AggTradeIntegrityError(f"invalid {field} at aggregate-trade row {row_number}") from exc
    if not value.is_finite() or value <= 0:
        raise AggTradeIntegrityError(f"non-positive or non-finite {field} at row {row_number}")
    return value


def _parse_non_negative_int(raw: str, *, field: str, row_number: int) -> int:
    try:
        value = int(raw)
    except ValueError as exc:
        raise AggTradeIntegrityError(f"invalid {field} at aggregate-trade row {row_number}") from exc
    if value < 0:
        raise AggTradeIntegrityError(f"negative {field} at aggregate-trade row {row_number}")
    return value


def _is_header(row: list[str]) -> bool:
    if not row:
        return False
    try:
        int(row[0])
    except ValueError:
        normalized = tuple(value.strip().lower().replace("_", "") for value in row)
        valid = (
            (
                "aggtradeid",
                "price",
                "quantity",
                "firsttradeid",
                "lasttradeid",
                "transacttime",
                "isbuyermaker",
            ),
            (
                "aggtradeid",
                "price",
                "qty",
                "firsttradeid",
                "lasttradeid",
                "transacttime",
                "isbuyermaker",
            ),
        )
        if normalized not in valid:
            raise AggTradeIntegrityError("unexpected Binance aggregate-trade CSV header") from None
        return True
    return False


def _parse_row(row: list[str], *, row_number: int, start_ms: int, end_ms: int) -> AggTrade:
    if len(row) != 7:
        raise AggTradeIntegrityError(f"aggregate-trade row {row_number} must contain exactly seven fields")
    aggregate_id = _parse_non_negative_int(row[0], field="aggregate trade id", row_number=row_number)
    price = _parse_decimal(row[1], field="price", row_number=row_number)
    quantity = _parse_decimal(row[2], field="quantity", row_number=row_number)
    first_trade_id = _parse_non_negative_int(row[3], field="first trade id", row_number=row_number)
    last_trade_id = _parse_non_negative_int(row[4], field="last trade id", row_number=row_number)
    transact_time_ms = _parse_non_negative_int(row[5], field="timestamp", row_number=row_number)
    if first_trade_id > last_trade_id:
        raise AggTradeIntegrityError(f"reversed raw-trade range at aggregate-trade row {row_number}")
    if not start_ms <= transact_time_ms < end_ms:
        raise AggTradeIntegrityError(
            f"aggregate-trade timestamp lies outside its UTC day at row {row_number}"
        )
    maker = row[6].strip().lower()
    if maker not in {"true", "false"}:
        raise AggTradeIntegrityError(f"invalid maker flag at aggregate-trade row {row_number}")
    return AggTrade(
        aggregate_trade_id=aggregate_id,
        price=price,
        quantity=quantity,
        first_trade_id=first_trade_id,
        last_trade_id=last_trade_id,
        transact_time_ms=transact_time_ms,
        buyer_is_maker=maker == "true",
    )


class BinanceAggTradeArchiveLoader:
    """Cache and validate immutable official daily USD-M ``aggTrades`` archives."""

    def __init__(
        self,
        cache_dir: Path,
        *,
        retries: int = 3,
        opener: Callable[..., object] = urlopen,
    ) -> None:
        if isinstance(retries, bool) or not isinstance(retries, int) or retries < 1:
            raise ValueError("retries must be a positive integer")
        self.cache_dir = cache_dir
        self.retries = retries
        self._opener = opener

    def _download(self, url: str, target: Path) -> None:
        if not url.startswith(f"{DAILY_AGGTRADES_ROOT}/"):
            raise ValueError("aggregate-trade URL must use the fixed Binance data host")
        if target.is_file():
            return
        target.parent.mkdir(parents=True, exist_ok=True)
        request = Request(url, headers={"User-Agent": "kairos-microstructure-preflight/1"})
        for attempt in range(self.retries):
            descriptor = -1
            temporary: Path | None = None
            try:
                response = self._opener(request, timeout=60)
                descriptor, temporary_name = tempfile.mkstemp(dir=target.parent, prefix=f".{target.name}.")
                temporary = Path(temporary_name)
                written = 0
                with response, os.fdopen(descriptor, "wb") as stream:  # type: ignore[attr-defined]
                    descriptor = -1
                    while chunk := response.read(_DOWNLOAD_CHUNK_BYTES):  # type: ignore[attr-defined]
                        if not isinstance(chunk, bytes):
                            raise TypeError("Binance downloader returned a non-byte chunk")
                        stream.write(chunk)
                        written += len(chunk)
                    stream.flush()
                    os.fsync(stream.fileno())
                if written == 0:
                    raise AggTradeIntegrityError(f"empty Binance aggregate-trade response: {target.name}")
                os.replace(temporary, target)
                return
            except HTTPError as exc:
                if exc.code == 404:
                    raise FileNotFoundError(
                        f"Binance aggregate-trade archive is not published: {target.name}"
                    ) from exc
                if attempt + 1 == self.retries:
                    raise
            except URLError:
                if attempt + 1 == self.retries:
                    raise
            finally:
                if descriptor >= 0:
                    os.close(descriptor)
                if temporary is not None:
                    temporary.unlink(missing_ok=True)
            time.sleep(2**attempt)
        raise RuntimeError("unreachable aggregate-trade retry state")

    @staticmethod
    def _expected_checksum(checksum_path: Path, filename: str) -> str:
        try:
            fields = checksum_path.read_text(encoding="ascii").strip().split()
        except UnicodeDecodeError as exc:
            raise AggTradeIntegrityError(f"non-ASCII Binance checksum for {filename}") from exc
        if len(fields) != 2 or len(fields[0]) != 64:
            raise AggTradeIntegrityError(f"malformed Binance checksum for {filename}")
        expected = fields[0].lower()
        if any(character not in "0123456789abcdef" for character in expected):
            raise AggTradeIntegrityError(f"malformed Binance checksum for {filename}")
        if fields[1].removeprefix("*") != filename:
            raise AggTradeIntegrityError(f"Binance checksum references the wrong file for {filename}")
        return expected

    @staticmethod
    def _csv_member(path: Path, filename: str) -> str:
        try:
            with zipfile.ZipFile(path) as archive:
                corrupt_member = archive.testzip()
                if corrupt_member is not None:
                    raise AggTradeIntegrityError(f"ZIP CRC failed for {filename}: {corrupt_member}")
                members = [item.filename for item in archive.infolist() if not item.is_dir()]
        except zipfile.BadZipFile as exc:
            raise AggTradeIntegrityError(f"invalid Binance ZIP archive: {filename}") from exc
        expected_member = filename.removesuffix(".zip") + ".csv"
        if members != [expected_member]:
            raise AggTradeIntegrityError(f"aggregate-trade ZIP must contain only {expected_member}")
        return expected_member

    @staticmethod
    def _rows(path: Path, member_name: str, day: date) -> Iterator[AggTrade]:
        start_ms = _date_ms(day)
        end_ms = start_ms + _DAY_MS
        with zipfile.ZipFile(path) as archive, archive.open(member_name) as raw:
            with io.TextIOWrapper(raw, encoding="utf-8", newline="") as text:
                reader = csv.reader(text)
                first_data_seen = False
                for row_number, row in enumerate(reader, start=1):
                    if not row or all(not value.strip() for value in row):
                        raise AggTradeIntegrityError(f"blank aggregate-trade row at line {row_number}")
                    if not first_data_seen and _is_header(row):
                        first_data_seen = True
                        continue
                    first_data_seen = True
                    yield _parse_row(row, row_number=row_number, start_ms=start_ms, end_ms=end_ms)

    def load(self, symbol: str, day: date) -> AggTradeArchive:
        symbol = symbol.upper()
        if symbol not in SYMBOLS:
            raise ValueError(f"symbol is outside the Kairos universe: {symbol}")
        if day >= datetime.now(UTC).date():
            raise ValueError("aggregate-trade archive requires a completed UTC day")
        filename = f"{symbol}-aggTrades-{day.isoformat()}.zip"
        base_url = f"{DAILY_AGGTRADES_ROOT}/{symbol}/{filename}"
        target = self.cache_dir / symbol / "aggTrades" / filename
        checksum_path = target.with_name(f"{filename}.CHECKSUM")
        self._download(f"{base_url}.CHECKSUM", checksum_path)
        self._download(base_url, target)
        expected = self._expected_checksum(checksum_path, filename)
        archive_sha256 = _sha256_file(target)
        if archive_sha256 != expected:
            raise AggTradeIntegrityError(f"Binance aggregate-trade SHA-256 mismatch for {filename}")
        member_name = self._csv_member(target, filename)

        digest = hashlib.sha256()
        count = 0
        first: AggTrade | None = None
        previous: AggTrade | None = None
        missing_aggregate_ids = 0
        missing_raw_ids = 0
        for trade in self._rows(target, member_name, day):
            if previous is not None:
                if trade.aggregate_trade_id <= previous.aggregate_trade_id:
                    raise AggTradeIntegrityError("aggregate trade ids must be strictly increasing")
                if trade.transact_time_ms < previous.transact_time_ms:
                    raise AggTradeIntegrityError("aggregate-trade timestamps must be nondecreasing")
                if trade.first_trade_id <= previous.last_trade_id:
                    raise AggTradeIntegrityError(
                        "raw-trade ranges must be strictly ordered and non-overlapping"
                    )
                missing_aggregate_ids += trade.aggregate_trade_id - previous.aggregate_trade_id - 1
                missing_raw_ids += trade.first_trade_id - previous.last_trade_id - 1
            canonical = (
                f"{trade.aggregate_trade_id},{_canonical_decimal(trade.price)},"
                f"{_canonical_decimal(trade.quantity)},{trade.first_trade_id},{trade.last_trade_id},"
                f"{trade.transact_time_ms},{str(trade.buyer_is_maker).lower()}\n"
            )
            digest.update(canonical.encode("ascii"))
            first = trade if first is None else first
            previous = trade
            count += 1
        if first is None or previous is None:
            raise AggTradeIntegrityError(f"aggregate-trade archive contains no rows: {filename}")
        manifest = AggTradeArchiveManifest(
            symbol=symbol,
            day=day.isoformat(),
            filename=filename,
            archive_sha256=archive_sha256,
            normalized_rows_sha256=digest.hexdigest(),
            rows=count,
            first_aggregate_trade_id=first.aggregate_trade_id,
            last_aggregate_trade_id=previous.aggregate_trade_id,
            first_transact_time_ms=first.transact_time_ms,
            last_transact_time_ms=previous.transact_time_ms,
            missing_aggregate_trade_ids=missing_aggregate_ids,
            missing_raw_trade_ids=missing_raw_ids,
        )
        return AggTradeArchive(path=target, member_name=member_name, manifest=manifest)

    def iter_trades(self, archive: AggTradeArchive) -> Iterator[AggTrade]:
        day = date.fromisoformat(archive.manifest.day)
        yield from self._rows(archive.path, archive.member_name, day)


def _quarter_hour_starts(start_ms: int, end_ms: int) -> Iterator[int]:
    if start_ms < 0 or end_ms <= start_ms:
        raise ValueError("peak-window range must be positive and non-empty")
    first = ((start_ms + _QUARTER_HOUR_MS - 1) // _QUARTER_HOUR_MS) * _QUARTER_HOUR_MS
    yield from range(first, end_ms, _QUARTER_HOUR_MS)


def quarter_hour_peak_windows(
    trades: Iterable[AggTrade],
    *,
    start_ms: int,
    end_ms: int,
    prior_trade: AggTrade | None = None,
) -> Iterator[QuarterHourPeakWindow]:
    """Yield the paper's causal ``(T, T+10s]`` windows at UTC quarter-hours.

    The opening reference is the latest transaction at or before ``T``.
    Trades exactly at ``T`` update that reference and are excluded from the
    forward VWAP; trades exactly at ``T+10s`` are included.  Empty forward
    windows are dropped exactly as in the source study.
    """

    iterator = iter(trades)
    current = next(iterator, None)
    last = prior_trade
    previous_key: tuple[int, int] | None = None
    for boundary in _quarter_hour_starts(start_ms, end_ms):
        while current is not None and current.transact_time_ms <= boundary:
            key = (current.transact_time_ms, current.aggregate_trade_id)
            if previous_key is not None and key < previous_key:
                raise AggTradeIntegrityError("peak-window input trades are not ordered")
            previous_key = key
            last = current
            current = next(iterator, None)
        if last is None:
            continue
        reference_price = last.price
        total_quantity = Decimal(0)
        total_notional = Decimal(0)
        buyer_quantity = Decimal(0)
        seller_quantity = Decimal(0)
        count = 0
        first_id: int | None = None
        last_id: int | None = None
        window_end = boundary + _PEAK_WINDOW_MS
        while current is not None and current.transact_time_ms <= window_end:
            key = (current.transact_time_ms, current.aggregate_trade_id)
            if previous_key is not None and key < previous_key:
                raise AggTradeIntegrityError("peak-window input trades are not ordered")
            previous_key = key
            total_quantity += current.quantity
            total_notional += current.price * current.quantity
            buyer_quantity += current.buyer_taker_quantity
            seller_quantity += current.seller_taker_quantity
            first_id = current.aggregate_trade_id if first_id is None else first_id
            last_id = current.aggregate_trade_id
            last = current
            count += 1
            current = next(iterator, None)
        if count == 0 or first_id is None or last_id is None:
            continue
        yield QuarterHourPeakWindow(
            start_ms=boundary,
            end_ms=window_end,
            opening_reference_price=reference_price,
            vwap=total_notional / total_quantity,
            total_quantity=total_quantity,
            buyer_taker_quantity=buyer_quantity,
            seller_taker_quantity=seller_quantity,
            trade_count=count,
            first_aggregate_trade_id=first_id,
            last_aggregate_trade_id=last_id,
        )


def completed_days(start: date, end_exclusive: date) -> tuple[date, ...]:
    if start >= end_exclusive:
        raise ValueError("aggregate-trade start must precede its exclusive end")
    if end_exclusive > datetime.now(UTC).date():
        raise ValueError("aggregate-trade end cannot exceed the current UTC date")
    return tuple(start + timedelta(days=offset) for offset in range((end_exclusive - start).days))
