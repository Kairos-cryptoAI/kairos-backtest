from __future__ import annotations

import csv
import hashlib
import io
import json
import math
import time
import zipfile
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import urlopen

from kairos_quant.candles import Candle

from .validation import canonical_candles

ARCHIVE_ROOT = "https://data.binance.vision/data/futures/um/monthly/klines"
BINANCE_KLINE_COLUMNS = (
    "open_time",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "close_time",
    "quote_volume",
    "count",
    "taker_buy_volume",
    "taker_buy_quote_volume",
    "ignore",
)


@dataclass(frozen=True, slots=True)
class DatasetManifest:
    symbol: str
    interval: str
    requested_start: str
    requested_end: str
    actual_start_ms: int
    actual_end_ms: int
    rows: int
    sha256: str
    files: tuple[str, ...]
    gaps: int
    transport_verification: str
    checksum_status: str
    checksum_files_verified: int
    expected_files: int
    csv_schema: str


@dataclass(frozen=True, slots=True)
class SymbolArchiveAudit:
    symbol: str
    files: int
    checksum_files_verified: int
    rows: int
    gaps: int
    first_open_time_ms: int
    last_close_time_ms: int
    zip_bytes: int
    invalid_rows: int
    missing_minutes: int
    coverage_pct: float
    gap_samples: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ArchiveInventoryAudit:
    requested_start: str
    requested_end: str
    expected_files: int
    present_files: int
    checksum_files_verified: int
    rows: int
    gaps: int
    zip_bytes: int
    inventory_sha256: str
    csv_schema: str
    invalid_rows: int
    invalid_row_samples: tuple[str, ...]
    missing_minutes: int
    coverage_pct: float
    gap_samples: tuple[str, ...]
    symbols: tuple[SymbolArchiveAudit, ...]


def month_starts(start: date, end: date) -> list[date]:
    if start > end:
        raise ValueError("start date must not be after end date")
    current = date(start.year, start.month, 1)
    result = []
    while current < end:
        result.append(current)
        current = date(current.year + (current.month == 12), current.month % 12 + 1, 1)
    return result


def _parse_csv(
    payload: bytes,
    symbol: str,
    interval: str,
    *,
    domain_issues: list[tuple[int, str]] | None = None,
) -> list[Candle]:
    try:
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            corrupt_member = archive.testzip()
            if corrupt_member is not None:
                raise ValueError(f"Binance archive failed ZIP CRC for {corrupt_member}")
            csv_names = sorted(name for name in archive.namelist() if name.endswith(".csv"))
            if len(csv_names) != 1:
                raise ValueError("Binance archive must contain exactly one CSV file")
            name = csv_names[0]
            text = archive.read(name).decode("utf-8")
    except (zipfile.BadZipFile, UnicodeDecodeError) as exc:
        raise ValueError("Binance archive is corrupt or not a valid UTF-8 ZIP") from exc

    candles: list[Candle] = []
    try:
        rows = csv.reader(io.StringIO(text, newline=""), strict=True)
        for line_number, row in enumerate(rows, start=1):
            if line_number == 1 and tuple(row) == BINANCE_KLINE_COLUMNS:
                continue
            if row and row[0].strip().lower() == "open_time":
                raise ValueError(f"unexpected Binance CSV header at line {line_number}")
            if len(row) != len(BINANCE_KLINE_COLUMNS):
                raise ValueError(
                    f"malformed Binance CSV row at line {line_number}: "
                    f"expected {len(BINANCE_KLINE_COLUMNS)} columns, received {len(row)}"
                )
            if any(field != field.strip() or not field for field in row):
                raise ValueError(f"malformed Binance CSV row at line {line_number}: blank/whitespace field")
            if not row[0].isascii() or not row[0].isdigit():
                raise ValueError(f"malformed Binance CSV row at line {line_number}: invalid open_time")
            if not row[6].isascii() or not row[6].isdigit():
                raise ValueError(f"malformed Binance CSV row at line {line_number}: invalid close_time")
            if not row[8].isascii() or not row[8].isdigit():
                raise ValueError(f"malformed Binance CSV row at line {line_number}: invalid trade count")
            numeric = tuple(float(row[index]) for index in (1, 2, 3, 4, 5, 7, 9, 10, 11))
            if not all(math.isfinite(value) for value in numeric):
                raise ValueError(f"malformed Binance CSV row at line {line_number}: non-finite value")
            domain_reason = _row_domain_issue(numeric, int(row[8]))
            if domain_reason is not None:
                if domain_issues is not None:
                    domain_issues.append((line_number, domain_reason))
                    continue
                raise ValueError(f"malformed Binance CSV row at line {line_number}: {domain_reason}")
            try:
                candles.append(
                    Candle(
                        symbol=symbol,
                        timeframe=interval,
                        open_time_ms=int(row[0]),
                        close_time_ms=int(row[6]),
                        open=numeric[0],
                        high=numeric[1],
                        low=numeric[2],
                        close=numeric[3],
                        volume=numeric[4],
                        quote_volume=numeric[5],
                        taker_buy_volume=numeric[6],
                    )
                )
            except ValueError as exc:
                if domain_issues is not None:
                    domain_issues.append((line_number, str(exc)))
                    continue
                raise ValueError(f"malformed Binance CSV row at line {line_number}: {exc}") from exc
    except (csv.Error, OverflowError) as exc:
        raise ValueError("malformed Binance CSV encoding") from exc
    except ValueError as exc:
        if "line" in str(exc):
            raise
        raise ValueError(f"malformed Binance CSV row at line {getattr(rows, 'line_num', 0)}") from exc
    if not candles:
        raise ValueError("Binance CSV contains no data rows")
    return candles


def _row_domain_issue(numeric: tuple[float, ...], trade_count: int) -> str | None:
    open_price, high, low, close, volume, quote, taker_volume, taker_quote, ignored = numeric
    if trade_count < 0 or min(volume, quote, taker_volume, taker_quote) < 0 or ignored != 0:
        return "invalid non-negative/count/ignore domain value"
    if taker_volume > volume:
        return "taker-buy volume exceeds total volume"
    tolerance = max(1e-8, abs(quote) * 1e-10)
    if quote < volume * low - tolerance or quote > volume * high + tolerance:
        return "quote volume is inconsistent with OHLC and base volume"
    taker_tolerance = max(1e-8, abs(taker_quote) * 1e-10)
    if (
        taker_quote < taker_volume * low - taker_tolerance
        or taker_quote > taker_volume * high + taker_tolerance
    ):
        return "taker-buy quote volume is inconsistent with OHLC and base volume"
    if high < max(open_price, close) or low > min(open_price, close):
        return "OHLC bounds are inconsistent"
    return None


class BinanceArchiveLoader:
    def __init__(
        self,
        cache_dir: Path,
        *,
        retries: int = 3,
        allow_download: bool = True,
    ) -> None:
        if retries < 1:
            raise ValueError("retries must be positive")
        self.cache_dir = cache_dir
        self.retries = retries
        self.allow_download = allow_download

    def _download(self, url: str, target: Path) -> bytes | None:
        if not url.startswith(f"{ARCHIVE_ROOT}/"):
            raise ValueError("archive URL must use the fixed Binance data host")
        if target.exists():
            return target.read_bytes()
        if not self.allow_download:
            return None
        for attempt in range(self.retries):
            try:
                with urlopen(url, timeout=60) as response:  # nosec B310
                    payload = response.read()
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(payload)
                return payload
            except HTTPError as exc:
                if exc.code == 404:
                    return None
                if attempt + 1 == self.retries:
                    raise
            except URLError:
                if attempt + 1 == self.retries:
                    raise
            time.sleep(2**attempt)
        return None

    @staticmethod
    def _verify_checksum(payload: bytes, target: Path) -> str:
        checksum_path = target.with_name(f"{target.name}.CHECKSUM")
        if not checksum_path.exists():
            return "unavailable"
        fields = checksum_path.read_text(encoding="ascii").strip().split()
        if len(fields) != 2 or len(fields[0]) != 64:
            raise ValueError(f"malformed Binance checksum sidecar: {checksum_path.name}")
        expected = fields[0].lower()
        if any(character not in "0123456789abcdef" for character in expected):
            raise ValueError(f"malformed Binance checksum sidecar: {checksum_path.name}")
        referenced_name = fields[1].removeprefix("*")
        if referenced_name != target.name:
            raise ValueError(f"checksum sidecar references the wrong file: {checksum_path.name}")
        actual = hashlib.sha256(payload).hexdigest()
        if actual != expected:
            raise ValueError(f"Binance SHA-256 mismatch for {target.name}")
        return "official_sha256_verified"

    def load(
        self, symbol: str, start: date, end: date, interval: str = "1m"
    ) -> tuple[list[Candle], DatasetManifest]:
        if start >= end:
            raise ValueError("dataset start must be before its exclusive end")
        if interval != "1m":
            raise ValueError("historical evaluation currently accepts only 1m archives")
        symbol = symbol.upper()
        candles: list[Candle] = []
        files: list[str] = []
        missing_files: list[str] = []
        checksum_statuses: list[str] = []
        for month in month_starts(start, end):
            filename = f"{symbol}-{interval}-{month:%Y-%m}.zip"
            url = f"{ARCHIVE_ROOT}/{symbol}/{interval}/{filename}"
            target = self.cache_dir / symbol / interval / filename
            payload = self._download(url, target)
            if payload is None:
                missing_files.append(filename)
                continue
            checksum_statuses.append(self._verify_checksum(payload, target))
            files.append(filename)
            candles.extend(_parse_csv(payload, symbol, interval))
        if missing_files:
            raise FileNotFoundError(
                f"missing required Binance archives for {symbol}: {', '.join(missing_files)}"
            )
        start_ms = int(datetime.combine(start, datetime.min.time(), UTC).timestamp() * 1000)
        end_ms = int(datetime.combine(end, datetime.min.time(), UTC).timestamp() * 1000)
        unique: dict[int, Candle] = {}
        for candle in candles:
            if not start_ms <= candle.open_time_ms < end_ms:
                continue
            existing = unique.get(candle.open_time_ms)
            if existing is not None:
                kind = "conflicting" if existing != candle else "duplicate"
                raise ValueError(f"{kind} archive rows at {candle.open_time_ms}")
            unique[candle.open_time_ms] = candle
        candles = canonical_candles(unique.values(), expected_timeframe="1m")
        if not candles:
            raise ValueError(f"no Binance archive data for {symbol} {start}..{end}")
        if any(
            candle.open_time_ms % 60_000 != 0 or candle.close_time_ms != candle.open_time_ms + 59_999
            for candle in candles
        ):
            raise ValueError("archive contains a malformed one-minute boundary")
        gaps = sum(
            b.open_time_ms - a.open_time_ms != 60_000 for a, b in zip(candles, candles[1:], strict=False)
        )
        digest = hashlib.sha256(
            json.dumps([asdict(c) for c in candles], separators=(",", ":")).encode()
        ).hexdigest()
        manifest = DatasetManifest(
            symbol,
            interval,
            start.isoformat(),
            end.isoformat(),
            candles[0].open_time_ms,
            candles[-1].close_time_ms,
            len(candles),
            digest,
            tuple(files),
            gaps,
            "zip_crc_and_parsed_rows_sha256",
            (
                "official_sha256_verified"
                if checksum_statuses
                and all(status == "official_sha256_verified" for status in checksum_statuses)
                else "partially_verified"
                if any(status == "official_sha256_verified" for status in checksum_statuses)
                else "unavailable"
            ),
            sum(status == "official_sha256_verified" for status in checksum_statuses),
            len(month_starts(start, end)),
            "binance_futures_kline_v1_12_columns",
        )
        return candles, manifest


def audit_cached_archives(
    cache_dir: Path,
    symbols: tuple[str, ...],
    start: date,
    end: date,
    *,
    interval: str = "1m",
) -> ArchiveInventoryAudit:
    """Strictly validate a cache inventory without downloading or retaining a horizon."""
    if start >= end or start.day != 1 or end.day != 1:
        raise ValueError("archive audit requires ordered month-aligned boundaries")
    if not symbols or len(set(symbols)) != len(symbols):
        raise ValueError("archive audit requires unique symbols")
    loader = BinanceArchiveLoader(cache_dir, allow_download=False)
    months = month_starts(start, end)
    expected_minutes_per_symbol = int(
        (
            datetime.combine(end, datetime.min.time(), UTC)
            - datetime.combine(start, datetime.min.time(), UTC)
        ).total_seconds()
        // 60
    )
    inventory_digest = hashlib.sha256()
    symbol_audits: list[SymbolArchiveAudit] = []
    invalid_row_samples: list[str] = []
    for raw_symbol in symbols:
        symbol = raw_symbol.upper()
        rows = gaps = verified = zip_bytes = invalid_rows = missing_minutes = 0
        gap_samples: list[str] = []
        first_open: int | None = None
        last_open: int | None = None
        last_close: int | None = None
        for month in months:
            filename = f"{symbol}-{interval}-{month:%Y-%m}.zip"
            target = cache_dir / symbol / interval / filename
            if not target.is_file():
                raise FileNotFoundError(f"missing required cached archive: {target}")
            payload = target.read_bytes()
            status = loader._verify_checksum(payload, target)
            verified += status == "official_sha256_verified"
            zip_bytes += len(payload)
            inventory_digest.update(filename.encode("ascii"))
            inventory_digest.update(b"\0")
            inventory_digest.update(hashlib.sha256(payload).digest())
            issues: list[tuple[int, str]] = []
            parsed = _parse_csv(payload, symbol, interval, domain_issues=issues)
            invalid_rows += len(issues)
            for line_number, reason in issues:
                if len(invalid_row_samples) < 25:
                    invalid_row_samples.append(f"{filename}:line {line_number}:{reason}")
            ordered = canonical_candles(parsed, expected_timeframe=interval)
            if parsed != ordered:
                raise ValueError(f"archive rows are not chronological: {filename}")
            next_month = date(month.year + (month.month == 12), month.month % 12 + 1, 1)
            month_start_ms = int(datetime.combine(month, datetime.min.time(), UTC).timestamp() * 1000)
            month_end_ms = int(datetime.combine(next_month, datetime.min.time(), UTC).timestamp() * 1000)
            for candle in ordered:
                if candle.open_time_ms % 60_000 != 0 or candle.close_time_ms != candle.open_time_ms + 59_999:
                    raise ValueError(f"archive contains a malformed row boundary: {filename}")
                if last_open is not None:
                    difference = candle.open_time_ms - last_open
                    if difference <= 0:
                        raise ValueError(
                            f"cross-archive duplicate or reversed timestamp in {filename}: "
                            f"{candle.open_time_ms}"
                        )
                    if difference != 60_000:
                        gaps += 1
                        missing_minutes += max(0, difference // 60_000 - 1)
                        if len(gap_samples) < 10:
                            gap_samples.append(
                                f"{symbol}:"
                                f"{datetime.fromtimestamp(last_open / 1000, UTC).isoformat()}"
                                f"..{datetime.fromtimestamp(candle.open_time_ms / 1000, UTC).isoformat()}"
                            )
                if not month_start_ms <= candle.open_time_ms < month_end_ms:
                    raise ValueError(f"archive contains an out-of-month row: {filename}")
                first_open = candle.open_time_ms if first_open is None else first_open
                last_open = candle.open_time_ms
                last_close = candle.close_time_ms
            rows += len(ordered)
        if first_open is None or last_close is None:
            raise ValueError(f"no cached archive rows for {symbol}")
        missing_minutes = max(0, expected_minutes_per_symbol - rows - invalid_rows)
        symbol_audits.append(
            SymbolArchiveAudit(
                symbol=symbol,
                files=len(months),
                checksum_files_verified=verified,
                rows=rows,
                gaps=gaps,
                first_open_time_ms=first_open,
                last_close_time_ms=last_close,
                zip_bytes=zip_bytes,
                invalid_rows=invalid_rows,
                missing_minutes=missing_minutes,
                coverage_pct=rows / expected_minutes_per_symbol * 100,
                gap_samples=tuple(gap_samples),
            )
        )
    expected_files = len(months) * len(symbols)
    valid_rows = sum(item.rows for item in symbol_audits)
    return ArchiveInventoryAudit(
        requested_start=start.isoformat(),
        requested_end=end.isoformat(),
        expected_files=expected_files,
        present_files=expected_files,
        checksum_files_verified=sum(item.checksum_files_verified for item in symbol_audits),
        rows=valid_rows,
        gaps=sum(item.gaps for item in symbol_audits),
        zip_bytes=sum(item.zip_bytes for item in symbol_audits),
        inventory_sha256=inventory_digest.hexdigest(),
        csv_schema="binance_futures_kline_v1_12_columns",
        invalid_rows=sum(item.invalid_rows for item in symbol_audits),
        invalid_row_samples=tuple(invalid_row_samples),
        missing_minutes=sum(item.missing_minutes for item in symbol_audits),
        coverage_pct=valid_rows / (expected_minutes_per_symbol * len(symbols)) * 100,
        gap_samples=tuple(sample for item in symbol_audits for sample in item.gap_samples),
        symbols=tuple(symbol_audits),
    )
