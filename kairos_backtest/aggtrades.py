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
from collections.abc import Callable, Iterable, Iterator, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .scenarios import SYMBOLS

DAILY_AGGTRADES_ROOT = "https://data.binance.vision/data/futures/um/daily/aggTrades"
MONTHLY_AGGTRADES_ROOT = "https://data.binance.vision/data/futures/um/monthly/aggTrades"
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
class MonthlyAggTradeTransport:
    """Checksum- and structure-verified archive awaiting its one-pass row scan."""

    symbol: str
    month: str
    start_ms: int
    end_ms: int
    path: Path
    member_name: str
    archive_sha256: str


@dataclass(frozen=True, slots=True)
class AggTradePeriodManifest:
    symbol: str
    period: str
    filename: str
    archive_sha256: str
    normalized_rows_sha256: str
    rows: int
    first_aggregate_trade_id: int
    last_aggregate_trade_id: int
    first_raw_trade_id: int
    last_raw_trade_id: int
    first_transact_time_ms: int
    last_transact_time_ms: int
    missing_aggregate_trade_ids: int
    missing_raw_trade_ids: int


@dataclass(frozen=True, slots=True)
class AggregateTradeGap:
    previous_aggregate_trade_id: int
    next_aggregate_trade_id: int
    missing_aggregate_trade_ids: int
    previous_transact_time_ms: int
    next_transact_time_ms: int
    in_period: bool

    def __post_init__(self) -> None:
        if (
            isinstance(self.previous_aggregate_trade_id, bool)
            or isinstance(self.next_aggregate_trade_id, bool)
            or self.previous_aggregate_trade_id < 0
            or self.next_aggregate_trade_id <= self.previous_aggregate_trade_id + 1
        ):
            raise ValueError("aggregate gap IDs are invalid")
        if (
            isinstance(self.missing_aggregate_trade_ids, bool)
            or self.missing_aggregate_trade_ids
            != self.next_aggregate_trade_id - self.previous_aggregate_trade_id - 1
        ):
            raise ValueError("aggregate gap count does not match its endpoints")
        if (
            isinstance(self.previous_transact_time_ms, bool)
            or isinstance(self.next_transact_time_ms, bool)
            or self.previous_transact_time_ms < 0
            or self.next_transact_time_ms < self.previous_transact_time_ms
        ):
            raise ValueError("aggregate gap timestamps are invalid")
        if type(self.in_period) is not bool:
            raise TypeError("aggregate gap period marker must be boolean")


@dataclass(frozen=True, slots=True)
class AggregateGapCorroboration:
    gap: AggregateTradeGap
    daily_manifests: tuple[AggTradeArchiveManifest, ...]

    def __post_init__(self) -> None:
        if not self.daily_manifests:
            raise ValueError("aggregate gap corroboration requires daily evidence")


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


@dataclass(frozen=True, slots=True)
class PhasePeakWindow:
    """A causal ten-second target at one fixed phase of the 15-minute grid."""

    phase_offset_minutes: int
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
    missing_aggregate_trade_ids: int
    missing_raw_trade_ids: int

    @property
    def open_to_vwap_return(self) -> Decimal:
        return self.vwap / self.opening_reference_price - Decimal(1)

    @property
    def order_imbalance(self) -> Decimal:
        return (self.buyer_taker_quantity - self.seller_taker_quantity) / self.total_quantity


@dataclass(frozen=True, slots=True)
class PhasePeakExtraction:
    """One-pass archive scan containing both row and feature evidence."""

    manifest: AggTradePeriodManifest
    windows: tuple[PhasePeakWindow, ...]
    last_trade: AggTrade
    expected_windows: int
    empty_windows: int
    missing_reference_windows: int
    aggregate_gaps: tuple[AggregateTradeGap, ...] = ()
    gap_corroborations: tuple[AggregateGapCorroboration, ...] = ()


def _date_ms(value: date) -> int:
    return int(datetime(value.year, value.month, value.day, tzinfo=UTC).timestamp() * 1_000)


def _next_month(value: date) -> date:
    if value.day != 1:
        raise ValueError("monthly aggregate-trade periods must start on day one")
    return date(value.year + (value.month == 12), value.month % 12 + 1, 1)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(_DOWNLOAD_CHUNK_BYTES):
            digest.update(chunk)
    return digest.hexdigest()


def _canonical_decimal(value: Decimal) -> str:
    normalized = value.normalize()
    return "0" if normalized == 0 else format(normalized, "f")


def _canonical_trade_line(trade: AggTrade) -> bytes:
    return (
        f"{trade.aggregate_trade_id},{_canonical_decimal(trade.price)},"
        f"{_canonical_decimal(trade.quantity)},{trade.first_trade_id},{trade.last_trade_id},"
        f"{trade.transact_time_ms},{str(trade.buyer_is_maker).lower()}\n"
    ).encode("ascii")


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
        root: str = DAILY_AGGTRADES_ROOT,
    ) -> None:
        if isinstance(retries, bool) or not isinstance(retries, int) or retries < 1:
            raise ValueError("retries must be a positive integer")
        self.cache_dir = cache_dir
        self.retries = retries
        self._opener = opener
        if root not in {DAILY_AGGTRADES_ROOT, MONTHLY_AGGTRADES_ROOT}:
            raise ValueError("aggregate-trade root must be an official fixed Binance data path")
        self._root = root

    def _download(self, url: str, target: Path) -> None:
        if not url.startswith(f"{self._root}/"):
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
    def _csv_member(path: Path, filename: str, *, verify_crc: bool = True) -> str:
        try:
            with zipfile.ZipFile(path) as archive:
                if verify_crc:
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
            digest.update(_canonical_trade_line(trade))
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


class BinanceMonthlyAggTradeArchiveLoader:
    """Verify monthly transport without parsing a multi-gigabyte CSV twice."""

    def __init__(
        self,
        cache_dir: Path,
        *,
        retries: int = 3,
        opener: Callable[..., object] = urlopen,
    ) -> None:
        self.cache_dir = cache_dir
        self._transport = BinanceAggTradeArchiveLoader(
            cache_dir,
            retries=retries,
            opener=opener,
            root=MONTHLY_AGGTRADES_ROOT,
        )

    def load(self, symbol: str, month: date) -> MonthlyAggTradeTransport:
        symbol = symbol.upper()
        if symbol not in SYMBOLS:
            raise ValueError(f"symbol is outside the Kairos universe: {symbol}")
        end = _next_month(month)
        if end > datetime.now(UTC).date():
            raise ValueError("monthly aggregate-trade archive requires a completed UTC month")
        period = month.strftime("%Y-%m")
        filename = f"{symbol}-aggTrades-{period}.zip"
        base_url = f"{MONTHLY_AGGTRADES_ROOT}/{symbol}/{filename}"
        target = self.cache_dir / symbol / "aggTrades" / "monthly" / filename
        checksum_path = target.with_name(f"{filename}.CHECKSUM")
        self._transport._download(f"{base_url}.CHECKSUM", checksum_path)
        self._transport._download(base_url, target)
        expected = BinanceAggTradeArchiveLoader._expected_checksum(checksum_path, filename)
        archive_sha256 = _sha256_file(target)
        if archive_sha256 != expected:
            raise AggTradeIntegrityError(f"Binance aggregate-trade SHA-256 mismatch for {filename}")
        return MonthlyAggTradeTransport(
            symbol=symbol,
            month=period,
            start_ms=_date_ms(month),
            end_ms=_date_ms(end),
            path=target,
            member_name=BinanceAggTradeArchiveLoader._csv_member(
                target,
                filename,
                verify_crc=False,
            ),
            archive_sha256=archive_sha256,
        )

    @staticmethod
    def iter_trades(archive: MonthlyAggTradeTransport) -> Iterator[AggTrade]:
        try:
            with zipfile.ZipFile(archive.path) as compressed, compressed.open(archive.member_name) as raw:
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
                        yield _parse_row(
                            row,
                            row_number=row_number,
                            start_ms=archive.start_ms,
                            end_ms=archive.end_ms,
                        )
        except zipfile.BadZipFile as exc:
            raise AggTradeIntegrityError(f"ZIP CRC failed while scanning {archive.path.name}") from exc


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


def _phase_boundaries(
    start_ms: int,
    end_ms: int,
    phase_offsets_minutes: Sequence[int],
) -> tuple[tuple[int, int], ...]:
    if start_ms < 0 or end_ms <= start_ms:
        raise ValueError("phase-window range must be positive and non-empty")
    offsets = tuple(phase_offsets_minutes)
    if not offsets or len(set(offsets)) != len(offsets):
        raise ValueError("phase offsets must be non-empty and unique")
    if any(
        isinstance(offset, bool) or not isinstance(offset, int) or not 0 <= offset < 15 for offset in offsets
    ):
        raise ValueError("phase offsets must be integer minutes in [0, 15)")
    events: list[tuple[int, int]] = []
    for offset in offsets:
        shifted_start = start_ms - offset * 60_000
        for boundary in _quarter_hour_starts(shifted_start, end_ms - offset * 60_000):
            actual = boundary + offset * 60_000
            if start_ms <= actual < end_ms:
                events.append((actual, offset))
    return tuple(sorted(events))


def extract_phase_peak_windows(
    archive: MonthlyAggTradeTransport,
    trades: Iterable[AggTrade],
    *,
    phase_offsets_minutes: Sequence[int],
    prior_trade: AggTrade | None = None,
) -> PhasePeakExtraction:
    """Scan one monthly archive once and extract all preregistered phases.

    Each phase uses the latest trade at or before ``T`` as its reference and
    the quantity-weighted prices in ``(T,T+10s]`` as its target.  Source-ID
    gaps between that reference and the target trades are attached to the
    window, so sensitivity analysis can remove affected observations without
    inventing a value.
    """

    boundaries = _phase_boundaries(
        archive.start_ms,
        archive.end_ms,
        phase_offsets_minutes,
    )
    if prior_trade is not None and prior_trade.transact_time_ms >= archive.start_ms:
        raise AggTradeIntegrityError("prior aggregate trade must precede the archive")

    iterator = iter(trades)
    current = next(iterator, None)
    last = prior_trade
    first: AggTrade | None = None
    previous_in_archive: AggTrade | None = None
    rows = 0
    missing_aggregate_ids = 0
    missing_raw_ids = 0
    aggregate_gaps: list[AggregateTradeGap] = []
    digest = hashlib.sha256()
    windows: list[PhasePeakWindow] = []
    empty_windows = 0
    missing_reference_windows = 0

    def consume(trade: AggTrade) -> tuple[int, int]:
        nonlocal first, previous_in_archive, last, rows
        nonlocal missing_aggregate_ids, missing_raw_ids
        if not archive.start_ms <= trade.transact_time_ms < archive.end_ms:
            raise AggTradeIntegrityError("aggregate-trade timestamp lies outside its archive period")
        previous = last
        if previous is not None:
            if trade.aggregate_trade_id <= previous.aggregate_trade_id:
                raise AggTradeIntegrityError("aggregate trade ids must be strictly increasing")
            if trade.transact_time_ms < previous.transact_time_ms:
                raise AggTradeIntegrityError("aggregate-trade timestamps must be nondecreasing")
            if trade.first_trade_id <= previous.last_trade_id:
                raise AggTradeIntegrityError("raw-trade ranges must be strictly ordered and non-overlapping")
        aggregate_gap = 0
        raw_gap = 0
        if previous is not None:
            aggregate_gap = trade.aggregate_trade_id - previous.aggregate_trade_id - 1
            raw_gap = trade.first_trade_id - previous.last_trade_id - 1
            if aggregate_gap:
                aggregate_gaps.append(
                    AggregateTradeGap(
                        previous_aggregate_trade_id=previous.aggregate_trade_id,
                        next_aggregate_trade_id=trade.aggregate_trade_id,
                        missing_aggregate_trade_ids=aggregate_gap,
                        previous_transact_time_ms=previous.transact_time_ms,
                        next_transact_time_ms=trade.transact_time_ms,
                        in_period=previous_in_archive is not None,
                    )
                )
        if previous_in_archive is not None:
            missing_aggregate_ids += aggregate_gap
            missing_raw_ids += raw_gap
        digest.update(_canonical_trade_line(trade))
        first = trade if first is None else first
        previous_in_archive = trade
        last = trade
        rows += 1
        return aggregate_gap, raw_gap

    for boundary, phase_offset in boundaries:
        while current is not None and current.transact_time_ms <= boundary:
            consume(current)
            current = next(iterator, None)
        if last is None:
            missing_reference_windows += 1
            continue
        reference = last
        total_quantity = Decimal(0)
        total_notional = Decimal(0)
        buyer_quantity = Decimal(0)
        seller_quantity = Decimal(0)
        count = 0
        first_id: int | None = None
        last_id: int | None = None
        target_aggregate_gaps = 0
        target_raw_gaps = 0
        window_end = boundary + _PEAK_WINDOW_MS
        while current is not None and current.transact_time_ms <= window_end:
            aggregate_gap, raw_gap = consume(current)
            target_aggregate_gaps += aggregate_gap
            target_raw_gaps += raw_gap
            total_quantity += current.quantity
            total_notional += current.price * current.quantity
            buyer_quantity += current.buyer_taker_quantity
            seller_quantity += current.seller_taker_quantity
            first_id = current.aggregate_trade_id if first_id is None else first_id
            last_id = current.aggregate_trade_id
            count += 1
            current = next(iterator, None)
        if count == 0 or first_id is None or last_id is None:
            empty_windows += 1
            continue
        windows.append(
            PhasePeakWindow(
                phase_offset_minutes=phase_offset,
                start_ms=boundary,
                end_ms=window_end,
                opening_reference_price=reference.price,
                vwap=total_notional / total_quantity,
                total_quantity=total_quantity,
                buyer_taker_quantity=buyer_quantity,
                seller_taker_quantity=seller_quantity,
                trade_count=count,
                first_aggregate_trade_id=first_id,
                last_aggregate_trade_id=last_id,
                missing_aggregate_trade_ids=target_aggregate_gaps,
                missing_raw_trade_ids=target_raw_gaps,
            )
        )

    while current is not None:
        consume(current)
        current = next(iterator, None)
    if first is None or previous_in_archive is None or last is None:
        raise AggTradeIntegrityError(f"aggregate-trade archive contains no rows: {archive.path.name}")
    manifest = AggTradePeriodManifest(
        symbol=archive.symbol,
        period=archive.month,
        filename=archive.path.name,
        archive_sha256=archive.archive_sha256,
        normalized_rows_sha256=digest.hexdigest(),
        rows=rows,
        first_aggregate_trade_id=first.aggregate_trade_id,
        last_aggregate_trade_id=previous_in_archive.aggregate_trade_id,
        first_raw_trade_id=first.first_trade_id,
        last_raw_trade_id=previous_in_archive.last_trade_id,
        first_transact_time_ms=first.transact_time_ms,
        last_transact_time_ms=previous_in_archive.transact_time_ms,
        missing_aggregate_trade_ids=missing_aggregate_ids,
        missing_raw_trade_ids=missing_raw_ids,
    )
    return PhasePeakExtraction(
        manifest=manifest,
        windows=tuple(windows),
        last_trade=last,
        expected_windows=len(boundaries),
        empty_windows=empty_windows,
        missing_reference_windows=missing_reference_windows,
        aggregate_gaps=tuple(aggregate_gaps),
    )


def _utc_day(timestamp_ms: int) -> date:
    return datetime.fromtimestamp(timestamp_ms // 1_000, UTC).date()


def _inclusive_days(start: date, end: date) -> tuple[date, ...]:
    if end < start:
        raise ValueError("daily corroboration range is reversed")
    return tuple(start + timedelta(days=offset) for offset in range((end - start).days + 1))


def _gap_key(gap: AggregateTradeGap) -> tuple[int, int, int, int, int]:
    return (
        gap.previous_aggregate_trade_id,
        gap.next_aggregate_trade_id,
        gap.missing_aggregate_trade_ids,
        gap.previous_transact_time_ms,
        gap.next_transact_time_ms,
    )


def corroborate_aggregate_gaps(
    extraction: PhasePeakExtraction,
    daily_loader: BinanceAggTradeArchiveLoader,
) -> PhasePeakExtraction:
    """Bind every monthly aggregate-ID gap to checksum-verified daily evidence."""

    if extraction.gap_corroborations:
        raise AggTradeIntegrityError("aggregate gaps have already been corroborated")
    if not extraction.aggregate_gaps:
        return extraction

    required_days = sorted(
        {
            day
            for gap in extraction.aggregate_gaps
            for day in _inclusive_days(
                _utc_day(gap.previous_transact_time_ms),
                _utc_day(gap.next_transact_time_ms),
            )
        }
    )
    archives = {day: daily_loader.load(extraction.manifest.symbol, day) for day in required_days}
    same_day_keys: dict[date, set[tuple[int, int, int, int, int]]] = {}
    for day in {
        _utc_day(gap.previous_transact_time_ms)
        for gap in extraction.aggregate_gaps
        if _utc_day(gap.previous_transact_time_ms) == _utc_day(gap.next_transact_time_ms)
    }:
        previous: AggTrade | None = None
        keys: set[tuple[int, int, int, int, int]] = set()
        for trade in daily_loader.iter_trades(archives[day]):
            if previous is not None and trade.aggregate_trade_id > previous.aggregate_trade_id + 1:
                keys.add(
                    (
                        previous.aggregate_trade_id,
                        trade.aggregate_trade_id,
                        trade.aggregate_trade_id - previous.aggregate_trade_id - 1,
                        previous.transact_time_ms,
                        trade.transact_time_ms,
                    )
                )
            previous = trade
        same_day_keys[day] = keys

    proofs: list[AggregateGapCorroboration] = []
    for gap in extraction.aggregate_gaps:
        start_day = _utc_day(gap.previous_transact_time_ms)
        end_day = _utc_day(gap.next_transact_time_ms)
        days = _inclusive_days(start_day, end_day)
        if start_day == end_day:
            corroborated = _gap_key(gap) in same_day_keys[start_day]
        else:
            first = archives[start_day].manifest
            last = archives[end_day].manifest
            corroborated = (
                len(days) == 2
                and first.last_aggregate_trade_id == gap.previous_aggregate_trade_id
                and first.last_transact_time_ms == gap.previous_transact_time_ms
                and last.first_aggregate_trade_id == gap.next_aggregate_trade_id
                and last.first_transact_time_ms == gap.next_transact_time_ms
            )
        if not corroborated:
            raise AggTradeIntegrityError(
                "monthly aggregate-ID gap is not reproduced by official daily archives: "
                f"{extraction.manifest.symbol} {extraction.manifest.period} "
                f"{gap.previous_aggregate_trade_id}->{gap.next_aggregate_trade_id}"
            )
        proofs.append(
            AggregateGapCorroboration(
                gap=gap,
                daily_manifests=tuple(archives[day].manifest for day in days),
            )
        )
    return replace(extraction, gap_corroborations=tuple(proofs))


def completed_days(start: date, end_exclusive: date) -> tuple[date, ...]:
    if start >= end_exclusive:
        raise ValueError("aggregate-trade start must precede its exclusive end")
    if end_exclusive > datetime.now(UTC).date():
        raise ValueError("aggregate-trade end cannot exceed the current UTC date")
    return tuple(start + timedelta(days=offset) for offset in range((end_exclusive - start).days))
