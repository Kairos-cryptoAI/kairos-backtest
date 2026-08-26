from __future__ import annotations

import argparse
import csv
import hashlib
import io
import math
import time
import zipfile
from collections.abc import Callable, Iterable, Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import TypeVar
from urllib.error import HTTPError, URLError
from urllib.request import urlopen

from .data import month_starts

ARCHIVE_HOST = "https://data.binance.vision"
DATA_START = date(2021, 7, 1)
LEVERAGE_START = date(2024, 7, 1)
DATA_END = date(2026, 8, 1)
SYMBOLS = ("BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT")

FUNDING_HEADER = ("calc_time", "funding_interval_hours", "last_funding_rate")
PREMIUM_HEADER = (
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
LEVERAGE_HEADER = (
    "create_time",
    "symbol",
    "sum_open_interest",
    "sum_open_interest_value",
    "count_toptrader_long_short_ratio",
    "sum_toptrader_long_short_ratio",
    "count_long_short_ratio",
    "sum_taker_long_short_vol_ratio",
)


class FactorKind(StrEnum):
    FUNDING = "fundingRate"
    PREMIUM = "premiumIndexKlines"
    LEVERAGE = "metrics"


@dataclass(frozen=True, slots=True)
class ArchiveTarget:
    kind: FactorKind
    symbol: str
    filename: str
    url: str
    relative_path: Path

    def __post_init__(self) -> None:
        if (
            self.symbol not in SYMBOLS
            or self.filename != Path(self.filename).name
            or not self.filename.endswith(".zip")
            or not self.url.startswith(f"{ARCHIVE_HOST}/data/futures/um/")
            or self.relative_path.is_absolute()
            or ".." in self.relative_path.parts
        ):
            raise ValueError("invalid fixed Binance factor archive target")


@dataclass(frozen=True, slots=True)
class DownloadEvidence:
    kind: str
    symbol: str
    filename: str
    bytes: int
    sha256: str
    cache_status: str


@dataclass(frozen=True, slots=True)
class FundingObservation:
    symbol: str
    timestamp_ms: int
    interval_hours: int
    rate: float


@dataclass(frozen=True, slots=True)
class PremiumObservation:
    symbol: str
    open_time_ms: int
    close_time_ms: int
    open: float
    high: float
    low: float
    close: float


@dataclass(frozen=True, slots=True)
class LeverageObservation:
    symbol: str
    timestamp_ms: int
    open_interest: float
    open_interest_value: float
    top_accounts_long_short_ratio: float | None
    top_positions_long_short_ratio: float | None
    global_accounts_long_short_ratio: float | None
    taker_long_short_volume_ratio: float | None


@dataclass(frozen=True, slots=True)
class FactorSymbolAudit:
    symbol: str
    funding_archives: int
    funding_rows: int
    premium_archives: int
    premium_rows: int
    leverage_archives: int
    leverage_rows: int
    leverage_zero_open_interest_rows: int
    leverage_incomplete_positioning_rows: int
    checksum_files_verified: int
    first_timestamp_ms: int
    last_timestamp_ms: int


@dataclass(frozen=True, slots=True)
class FactorDataset:
    funding: dict[str, tuple[FundingObservation, ...]]
    premium: dict[str, tuple[PremiumObservation, ...]]
    leverage: dict[str, tuple[LeverageObservation, ...]]
    audits: tuple[FactorSymbolAudit, ...]
    inventory_sha256: str


def _days(start: date, end: date) -> Iterable[date]:
    current = start
    while current < end:
        yield current
        current += timedelta(days=1)


def expected_targets() -> tuple[ArchiveTarget, ...]:
    targets: list[ArchiveTarget] = []
    for symbol in SYMBOLS:
        for month in month_starts(DATA_START, DATA_END):
            funding_name = f"{symbol}-fundingRate-{month:%Y-%m}.zip"
            funding_relative = Path(FactorKind.FUNDING.value) / symbol / funding_name
            targets.append(
                ArchiveTarget(
                    FactorKind.FUNDING,
                    symbol,
                    funding_name,
                    f"{ARCHIVE_HOST}/data/futures/um/monthly/fundingRate/{symbol}/{funding_name}",
                    funding_relative,
                )
            )
            premium_name = f"{symbol}-1h-{month:%Y-%m}.zip"
            premium_relative = Path(FactorKind.PREMIUM.value) / symbol / "1h" / premium_name
            targets.append(
                ArchiveTarget(
                    FactorKind.PREMIUM,
                    symbol,
                    premium_name,
                    (f"{ARCHIVE_HOST}/data/futures/um/monthly/premiumIndexKlines/{symbol}/1h/{premium_name}"),
                    premium_relative,
                )
            )
        for day in _days(LEVERAGE_START, DATA_END):
            leverage_name = f"{symbol}-metrics-{day:%Y-%m-%d}.zip"
            leverage_relative = Path(FactorKind.LEVERAGE.value) / symbol / leverage_name
            targets.append(
                ArchiveTarget(
                    FactorKind.LEVERAGE,
                    symbol,
                    leverage_name,
                    f"{ARCHIVE_HOST}/data/futures/um/daily/metrics/{symbol}/{leverage_name}",
                    leverage_relative,
                )
            )
    return tuple(sorted(targets, key=lambda item: item.relative_path.as_posix()))


def _checksum(payload: bytes, checksum_payload: bytes, filename: str) -> str:
    try:
        fields = checksum_payload.decode("ascii").strip().split()
    except UnicodeDecodeError as exc:
        raise ValueError(f"checksum for {filename} is not ASCII") from exc
    if (
        len(fields) != 2
        or len(fields[0]) != 64
        or any(character not in "0123456789abcdefABCDEF" for character in fields[0])
        or fields[1].removeprefix("*") != filename
    ):
        raise ValueError(f"malformed checksum for {filename}")
    actual = hashlib.sha256(payload).hexdigest()
    if actual != fields[0].lower():
        raise ValueError(f"official Binance SHA-256 mismatch for {filename}")
    return actual


def _fetch(url: str, *, retries: int) -> bytes:
    if not url.startswith(f"{ARCHIVE_HOST}/data/futures/um/"):
        raise ValueError("factor download URL must use the fixed Binance archive host")
    for attempt in range(retries):
        try:
            with urlopen(url, timeout=60) as response:  # nosec B310
                return response.read()
        except HTTPError as exc:
            if exc.code == 404:
                raise FileNotFoundError(f"missing official Binance archive: {url}") from exc
            if attempt + 1 == retries:
                raise
        except (ConnectionError, TimeoutError, URLError):
            if attempt + 1 == retries:
                raise
        time.sleep(2**attempt)
    raise RuntimeError("unreachable factor download retry state")


def _atomic_bytes(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(f"{path.suffix}.tmp")
    temporary.write_bytes(payload)
    temporary.replace(path)


def download_target(cache_dir: Path, target: ArchiveTarget, *, retries: int = 3) -> DownloadEvidence:
    if retries < 1:
        raise ValueError("retries must be positive")
    path = cache_dir / target.relative_path
    checksum_path = path.with_name(f"{path.name}.CHECKSUM")
    status = "verified_cache"
    if path.is_file() and checksum_path.is_file():
        payload = path.read_bytes()
        checksum_payload = checksum_path.read_bytes()
    else:
        status = "downloaded"
        payload = _fetch(target.url, retries=retries)
        checksum_payload = _fetch(f"{target.url}.CHECKSUM", retries=retries)
        digest = _checksum(payload, checksum_payload, target.filename)
        _atomic_bytes(path, payload)
        _atomic_bytes(checksum_path, checksum_payload)
        return DownloadEvidence(
            target.kind.value, target.symbol, target.filename, len(payload), digest, status
        )
    digest = _checksum(payload, checksum_payload, target.filename)
    return DownloadEvidence(target.kind.value, target.symbol, target.filename, len(payload), digest, status)


def download_factor_cache(
    cache_dir: Path,
    *,
    workers: int = 12,
    retries: int = 3,
) -> tuple[DownloadEvidence, ...]:
    if isinstance(workers, bool) or not isinstance(workers, int) or not 1 <= workers <= 32:
        raise ValueError("workers must be an integer from 1 through 32")
    targets = expected_targets()
    evidence: list[DownloadEvidence] = []
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="kairos-factor") as executor:
        futures = {
            executor.submit(download_target, cache_dir, target, retries=retries): target for target in targets
        }
        for future in as_completed(futures):
            evidence.append(future.result())
    return tuple(sorted(evidence, key=lambda item: (item.kind, item.symbol, item.filename)))


def _csv_payload(payload: bytes, filename: str) -> list[list[str]]:
    try:
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            corrupt = archive.testzip()
            if corrupt is not None:
                raise ValueError(f"ZIP CRC failed for {filename}: {corrupt}")
            members = [name for name in archive.namelist() if name.endswith(".csv")]
            if len(members) != 1:
                raise ValueError(f"{filename} must contain exactly one CSV")
            text = archive.read(members[0]).decode("utf-8")
    except (zipfile.BadZipFile, UnicodeDecodeError) as exc:
        raise ValueError(f"invalid factor ZIP: {filename}") from exc
    try:
        return list(csv.reader(io.StringIO(text, newline=""), strict=True))
    except csv.Error as exc:
        raise ValueError(f"malformed factor CSV: {filename}") from exc


def _finite(row: Sequence[str], indices: Sequence[int], filename: str) -> tuple[float, ...]:
    try:
        values = tuple(float(row[index]) for index in indices)
    except (ValueError, IndexError) as exc:
        raise ValueError(f"non-numeric factor value in {filename}") from exc
    if not all(math.isfinite(value) for value in values):
        raise ValueError(f"non-finite factor value in {filename}")
    return values


def _optional_finite(row: Sequence[str], index: int, filename: str) -> float | None:
    try:
        raw = row[index]
    except IndexError as exc:
        raise ValueError(f"missing optional factor column in {filename}") from exc
    if raw == "":
        return None
    value = _finite(row, (index,), filename)[0]
    if value < 0:
        raise ValueError(f"negative optional factor value in {filename}")
    return value


def parse_funding(payload: bytes, symbol: str, filename: str) -> tuple[FundingObservation, ...]:
    rows = _csv_payload(payload, filename)
    if not rows or tuple(rows[0]) != FUNDING_HEADER:
        raise ValueError(f"unexpected funding schema in {filename}")
    observations: list[FundingObservation] = []
    for row in rows[1:]:
        if len(row) != len(FUNDING_HEADER):
            raise ValueError(f"unexpected funding row width in {filename}")
        rate = _finite(row, (2,), filename)[0]
        try:
            timestamp, interval = int(row[0]), int(row[1])
        except ValueError as exc:
            raise ValueError(f"invalid funding integer in {filename}") from exc
        if timestamp < 0 or interval not in {1, 2, 4, 8}:
            raise ValueError(f"invalid funding domain in {filename}")
        observations.append(FundingObservation(symbol, timestamp, interval, rate))
    return _ordered_unique(observations, lambda item: item.timestamp_ms, filename)


def parse_premium(payload: bytes, symbol: str, filename: str) -> tuple[PremiumObservation, ...]:
    rows = _csv_payload(payload, filename)
    if not rows:
        raise ValueError(f"empty premium archive in {filename}")
    # Binance's older premium-index kline archives omit the header, while
    # recent archives include the same canonical 12-column kline header.
    data_rows = rows[1:] if tuple(rows[0]) == PREMIUM_HEADER else rows
    observations: list[PremiumObservation] = []
    for row in data_rows:
        if len(row) != len(PREMIUM_HEADER):
            raise ValueError(f"unexpected premium row width in {filename}")
        prices = _finite(row, (1, 2, 3, 4), filename)
        try:
            open_time, close_time = int(row[0]), int(row[6])
        except ValueError as exc:
            raise ValueError(f"invalid premium timestamp in {filename}") from exc
        if (
            open_time < 0
            or open_time % 3_600_000
            or close_time != open_time + 3_600_000 - 1
            or prices[1] < max(prices[0], prices[3])
            or prices[2] > min(prices[0], prices[3])
        ):
            raise ValueError(f"invalid premium domain in {filename}")
        observations.append(PremiumObservation(symbol, open_time, close_time, *prices))
    return _ordered_unique(observations, lambda item: item.open_time_ms, filename)


def parse_leverage(payload: bytes, symbol: str, filename: str) -> tuple[LeverageObservation, ...]:
    rows = _csv_payload(payload, filename)
    if not rows or tuple(rows[0]) != LEVERAGE_HEADER:
        raise ValueError(f"unexpected leverage schema in {filename}")
    observations: list[LeverageObservation] = []
    for row in rows[1:]:
        if len(row) != len(LEVERAGE_HEADER) or row[1] != symbol:
            raise ValueError(f"unexpected leverage row in {filename}")
        open_interest, open_interest_value = _finite(row, (2, 3), filename)
        ratios = tuple(_optional_finite(row, index, filename) for index in range(4, 8))
        try:
            timestamp = int(
                datetime.strptime(row[0], "%Y-%m-%d %H:%M:%S").replace(tzinfo=UTC).timestamp() * 1_000
            )
        except ValueError as exc:
            raise ValueError(f"invalid leverage timestamp in {filename}") from exc
        if timestamp % 300_000 or min(open_interest, open_interest_value) < 0:
            raise ValueError(f"invalid leverage domain in {filename}")
        observations.append(
            LeverageObservation(symbol, timestamp, open_interest, open_interest_value, *ratios)
        )
    return _ordered_unique(observations, lambda item: item.timestamp_ms, filename)


T = TypeVar("T")


def _ordered_unique(items: Sequence[T], timestamp: Callable[[T], int], filename: str) -> tuple[T, ...]:
    ordered = tuple(sorted(items, key=timestamp))
    stamps = [timestamp(item) for item in ordered]
    if len(stamps) != len(set(stamps)):
        raise ValueError(f"duplicate factor timestamp in {filename}")
    return ordered


def load_factor_cache(cache_dir: Path) -> FactorDataset:
    funding_grouped: dict[str, list[FundingObservation]] = {symbol: [] for symbol in SYMBOLS}
    premium_grouped: dict[str, list[PremiumObservation]] = {symbol: [] for symbol in SYMBOLS}
    leverage_grouped: dict[str, list[LeverageObservation]] = {symbol: [] for symbol in SYMBOLS}
    counts: dict[tuple[str, FactorKind], int] = {}
    verified: dict[str, int] = {symbol: 0 for symbol in SYMBOLS}
    inventory = hashlib.sha256()
    for target in expected_targets():
        path = cache_dir / target.relative_path
        checksum_path = path.with_name(f"{path.name}.CHECKSUM")
        if not path.is_file() or not checksum_path.is_file():
            raise FileNotFoundError(f"missing cached factor archive or checksum: {path}")
        payload = path.read_bytes()
        digest = _checksum(payload, checksum_path.read_bytes(), target.filename)
        inventory.update(target.relative_path.as_posix().encode("ascii"))
        inventory.update(b"\0")
        inventory.update(bytes.fromhex(digest))
        if target.kind is FactorKind.FUNDING:
            funding_grouped[target.symbol].extend(parse_funding(payload, target.symbol, target.filename))
        elif target.kind is FactorKind.PREMIUM:
            premium_grouped[target.symbol].extend(parse_premium(payload, target.symbol, target.filename))
        else:
            leverage_grouped[target.symbol].extend(parse_leverage(payload, target.symbol, target.filename))
        counts[(target.symbol, target.kind)] = counts.get((target.symbol, target.kind), 0) + 1
        verified[target.symbol] += 1
    typed_funding: dict[str, tuple[FundingObservation, ...]] = {}
    typed_premium: dict[str, tuple[PremiumObservation, ...]] = {}
    typed_leverage: dict[str, tuple[LeverageObservation, ...]] = {}
    audits: list[FactorSymbolAudit] = []
    for symbol in SYMBOLS:
        funding = _ordered_unique(funding_grouped[symbol], lambda item: item.timestamp_ms, symbol)
        premium = _ordered_unique(premium_grouped[symbol], lambda item: item.open_time_ms, symbol)
        leverage = _ordered_unique(leverage_grouped[symbol], lambda item: item.timestamp_ms, symbol)
        typed_funding[symbol] = funding
        typed_premium[symbol] = premium
        typed_leverage[symbol] = leverage
        timestamps = (
            [item.timestamp_ms for item in funding]
            + [item.open_time_ms for item in premium]
            + [item.timestamp_ms for item in leverage]
        )
        audits.append(
            FactorSymbolAudit(
                symbol,
                counts[(symbol, FactorKind.FUNDING)],
                len(funding),
                counts[(symbol, FactorKind.PREMIUM)],
                len(premium),
                counts[(symbol, FactorKind.LEVERAGE)],
                len(leverage),
                sum(item.open_interest <= 0 or item.open_interest_value <= 0 for item in leverage),
                sum(
                    any(
                        value is None
                        for value in (
                            item.top_accounts_long_short_ratio,
                            item.top_positions_long_short_ratio,
                            item.global_accounts_long_short_ratio,
                            item.taker_long_short_volume_ratio,
                        )
                    )
                    for item in leverage
                ),
                verified[symbol],
                min(timestamps),
                max(timestamps),
            )
        )
    return FactorDataset(
        typed_funding,
        typed_premium,
        typed_leverage,
        tuple(audits),
        inventory.hexdigest(),
    )


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Acquire and verify official Binance factor archives")
    parser.add_argument("--cache-dir", type=Path, default=Path("data/historical-factors"))
    parser.add_argument("--workers", type=int, default=12)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--download", action="store_true")
    mode.add_argument("--audit", action="store_true")
    arguments = parser.parse_args(list(argv) if argv is not None else None)
    root = Path(__file__).resolve().parents[1]
    cache_dir = root / arguments.cache_dir
    if arguments.download:
        evidence = download_factor_cache(cache_dir, workers=arguments.workers)
        downloaded = sum(item.cache_status == "downloaded" for item in evidence)
        print(f"verified={len(evidence)} downloaded={downloaded} cached={len(evidence) - downloaded}")
        return 0
    dataset = load_factor_cache(cache_dir)
    print(f"inventory_sha256={dataset.inventory_sha256} symbols={len(dataset.audits)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
