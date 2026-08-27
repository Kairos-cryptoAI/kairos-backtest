"""Checksum-verified Binance daily-archive ingestion for the forward ledger.

This module is deliberately outside the frozen evaluator source lock.  It can
only submit strict, closed bars to :class:`ForwardLedger`; the ledger owns
normalization, continuity, conflict handling, intent generation and hashing.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import tempfile
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from kairos_strategy.candles import Candle
from kairos_strategy.runtime import candle_to_closed_bar

from .data import ArchiveFieldProfile, _parse_csv
from .forward_observation import (
    PLAN_FILENAME,
    SYMBOLS,
    ForwardIntegrityError,
    ForwardLedger,
    _date_ms,
    load_plan,
)

DAILY_ARCHIVE_ROOT = "https://data.binance.vision/data/futures/um/daily/klines"
_ONE_MINUTE_MS = 60_000
_ROWS_PER_DAY = 24 * 60
_DAY_MS = _ROWS_PER_DAY * _ONE_MINUTE_MS
_PUBLICATION_GRACE_DAYS = 2


@dataclass(frozen=True, slots=True)
class DailyArchiveManifest:
    symbol: str
    day: str
    filename: str
    archive_sha256: str
    normalized_rows_sha256: str
    rows: int
    quarantined_optional_rows: int


@dataclass(frozen=True, slots=True)
class DailyCollectionSummary:
    start: str
    end_exclusive: str
    inserted_bars: int
    duplicate_bars: int
    emitted_intents: int
    manifests: tuple[DailyArchiveManifest, ...]


@dataclass(frozen=True, slots=True)
class DailySyncSummary:
    start: str
    latest_published_end_exclusive: str
    stopped_at_unpublished_day: str | None
    inserted_bars: int
    duplicate_bars: int
    emitted_intents: int
    manifests: tuple[DailyArchiveManifest, ...]


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _atomic_cache_write(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _days(start: date, end: date) -> tuple[date, ...]:
    if start >= end:
        raise ValueError("daily archive start must precede its exclusive end")
    return tuple(start + timedelta(days=offset) for offset in range((end - start).days))


class BinanceDailyArchiveLoader:
    """Download immutable public daily archives and verify official SHA-256 files."""

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

    def _download(self, url: str, target: Path) -> bytes:
        if not url.startswith(f"{DAILY_ARCHIVE_ROOT}/"):
            raise ValueError("daily archive URL must use the fixed Binance data host")
        if target.is_file():
            return target.read_bytes()
        request = Request(url, headers={"User-Agent": "kairos-forward-observer/1"})
        for attempt in range(self.retries):
            try:
                response = self._opener(request, timeout=60)
                with response:  # type: ignore[attr-defined]
                    payload = response.read()  # type: ignore[attr-defined]
                if not payload:
                    raise ValueError(f"empty Binance daily archive response: {target.name}")
                _atomic_cache_write(target, payload)
                return payload
            except HTTPError as exc:
                if exc.code == 404:
                    raise FileNotFoundError(f"Binance daily archive is not published: {target.name}") from exc
                if attempt + 1 == self.retries:
                    raise
            except URLError:
                if attempt + 1 == self.retries:
                    raise
            time.sleep(2**attempt)
        raise RuntimeError("unreachable daily archive retry state")

    @staticmethod
    def _verify_official_checksum(payload: bytes, checksum: bytes, filename: str) -> str:
        try:
            fields = checksum.decode("ascii").strip().split()
        except UnicodeDecodeError as exc:
            raise ValueError(f"non-ASCII Binance checksum for {filename}") from exc
        if len(fields) != 2 or len(fields[0]) != 64:
            raise ValueError(f"malformed Binance checksum for {filename}")
        expected = fields[0].lower()
        if any(character not in "0123456789abcdef" for character in expected):
            raise ValueError(f"malformed Binance checksum for {filename}")
        if fields[1].removeprefix("*") != filename:
            raise ValueError(f"Binance checksum references the wrong file for {filename}")
        actual = _sha256(payload)
        if actual != expected:
            raise ValueError(f"Binance daily SHA-256 mismatch for {filename}")
        return actual

    def load(self, symbol: str, day: date) -> tuple[list[Candle], DailyArchiveManifest]:
        symbol = symbol.upper()
        if symbol not in SYMBOLS:
            raise ValueError(f"symbol is outside the frozen universe: {symbol}")
        today = datetime.now(UTC).date()
        if day >= today:
            raise ValueError("daily archive ingestion requires a completed UTC day")
        filename = f"{symbol}-1m-{day.isoformat()}.zip"
        base_url = f"{DAILY_ARCHIVE_ROOT}/{symbol}/1m/{filename}"
        target = self.cache_dir / symbol / "1m" / filename
        payload = self._download(base_url, target)
        checksum = self._download(f"{base_url}.CHECKSUM", target.with_name(f"{filename}.CHECKSUM"))
        archive_sha256 = self._verify_official_checksum(payload, checksum, filename)
        quarantined: list[tuple[int, str]] = []
        rows = _parse_csv(
            payload,
            symbol,
            "1m",
            field_profile=ArchiveFieldProfile.PRICE_VOLUME,
            quarantined_issues=quarantined,
        )
        start_ms = _date_ms(day)
        end_ms = start_ms + 24 * 60 * _ONE_MINUTE_MS
        if (
            len(rows) != _ROWS_PER_DAY
            or rows[0].open_time_ms != start_ms
            or rows[-1].close_time_ms != end_ms - 1
            or any(
                row.open_time_ms != start_ms + index * _ONE_MINUTE_MS
                or row.close_time_ms != row.open_time_ms + _ONE_MINUTE_MS - 1
                for index, row in enumerate(rows)
            )
        ):
            raise ForwardIntegrityError(f"daily archive lacks exact UTC-minute coverage: {filename}")
        normalized = json.dumps(
            [asdict(row) for row in rows],
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        ).encode("ascii")
        return rows, DailyArchiveManifest(
            symbol=symbol,
            day=day.isoformat(),
            filename=filename,
            archive_sha256=archive_sha256,
            normalized_rows_sha256=_sha256(normalized),
            rows=len(rows),
            quarantined_optional_rows=len(quarantined),
        )


def collect_daily_archives(
    ledger: ForwardLedger,
    cache_dir: Path,
    start: date,
    end: date,
    *,
    loader_factory: Callable[[Path], BinanceDailyArchiveLoader] = BinanceDailyArchiveLoader,
) -> DailyCollectionSummary:
    """Verify every requested day first, then advance each symbol atomically."""

    days = _days(start, end)
    if end > datetime.now(UTC).date():
        raise ValueError("daily archive end cannot exceed the current UTC date")
    loader = loader_factory(cache_dir)
    staged: dict[str, list[Candle]] = {symbol: [] for symbol in SYMBOLS}
    manifests: list[DailyArchiveManifest] = []
    for symbol in SYMBOLS:
        for day in days:
            rows, manifest = loader.load(symbol, day)
            staged[symbol].extend(rows)
            manifests.append(manifest)
    inserted = duplicates = intents = 0
    for symbol in SYMBOLS:
        summary = ledger.ingest_atomic(
            (candle_to_closed_bar(row) for row in staged[symbol]),
            as_of_ms=_date_ms(end) - 1,
        )
        inserted += summary.inserted_bars
        duplicates += summary.duplicate_bars
        intents += summary.emitted_intents
    ledger.verify_integrity()
    return DailyCollectionSummary(
        start=start.isoformat(),
        end_exclusive=end.isoformat(),
        inserted_bars=inserted,
        duplicate_bars=duplicates,
        emitted_intents=intents,
        manifests=tuple(manifests),
    )


def _common_watermark_date(ledger: ForwardLedger) -> date:
    status = ledger.status()
    watermark_ms = status.get("watermark_ms")
    if isinstance(watermark_ms, bool) or not isinstance(watermark_ms, int):
        raise ForwardIntegrityError("forward ledger has no complete-universe watermark")
    if watermark_ms % _DAY_MS:
        raise ForwardIntegrityError("daily sync requires a common UTC-midnight watermark")
    symbols = status.get("symbols")
    if not isinstance(symbols, list) or len(symbols) != len(SYMBOLS):
        raise ForwardIntegrityError("forward ledger status has an incomplete universe")
    if any(not isinstance(item, dict) or item.get("blocked_reason") is not None for item in symbols):
        raise ForwardIntegrityError("daily sync refuses a blocked symbol")
    return datetime.fromtimestamp(watermark_ms / 1_000, UTC).date()


def sync_latest_daily_archives(
    ledger: ForwardLedger,
    cache_dir: Path,
    *,
    today: date | None = None,
    loader_factory: Callable[[Path], BinanceDailyArchiveLoader] = BinanceDailyArchiveLoader,
) -> DailySyncSummary:
    """Resume at the common watermark and stop only at normal publication lag."""

    effective_today = datetime.now(UTC).date() if today is None else today
    if not isinstance(effective_today, date):
        raise TypeError("today must be a date")
    start = _common_watermark_date(ledger)
    if start > effective_today:
        raise ForwardIntegrityError("forward watermark lies in the future")
    loader = loader_factory(cache_dir)
    inserted = duplicates = intents = 0
    manifests: list[DailyArchiveManifest] = []
    latest_end = start
    stopped_at: date | None = None
    for day in _days(start, effective_today) if start < effective_today else ():
        staged: dict[str, list[Candle]] = {}
        day_manifests: list[DailyArchiveManifest] = []
        try:
            for symbol in SYMBOLS:
                rows, manifest = loader.load(symbol, day)
                staged[symbol] = rows
                day_manifests.append(manifest)
        except FileNotFoundError:
            publication_age_days = (effective_today - day).days
            if publication_age_days >= _PUBLICATION_GRACE_DAYS:
                raise ForwardIntegrityError(
                    f"unpublished daily archive exceeds publication grace: {day.isoformat()}"
                ) from None
            stopped_at = day
            break
        day_end = day + timedelta(days=1)
        for symbol in SYMBOLS:
            summary = ledger.ingest_atomic(
                (candle_to_closed_bar(row) for row in staged[symbol]),
                as_of_ms=_date_ms(day_end) - 1,
            )
            inserted += summary.inserted_bars
            duplicates += summary.duplicate_bars
            intents += summary.emitted_intents
        ledger.verify_integrity()
        manifests.extend(day_manifests)
        latest_end = day_end
    return DailySyncSummary(
        start=start.isoformat(),
        latest_published_end_exclusive=latest_end.isoformat(),
        stopped_at_unpublished_day=None if stopped_at is None else stopped_at.isoformat(),
        inserted_bars=inserted,
        duplicate_bars=duplicates,
        emitted_intents=intents,
        manifests=tuple(manifests),
    )


def _print_json(value: object) -> None:
    print(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True))


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=Path, default=Path(PLAN_FILENAME))
    parser.add_argument("--ledger", type=Path, required=True)
    parser.add_argument("--cache-dir", type=Path, required=True)
    parser.add_argument("--sync-latest", action="store_true")
    parser.add_argument("--start", type=date.fromisoformat)
    parser.add_argument("--end-exclusive", type=date.fromisoformat)
    arguments = parser.parse_args(argv)
    if arguments.sync_latest and (arguments.start is not None or arguments.end_exclusive is not None):
        parser.error("--sync-latest cannot be combined with explicit dates")
    if not arguments.sync_latest and (arguments.start is None or arguments.end_exclusive is None):
        parser.error("explicit collection requires --start and --end-exclusive")
    plan = load_plan(arguments.plan)
    with ForwardLedger(arguments.ledger, plan) as ledger:
        if arguments.sync_latest:
            sync = sync_latest_daily_archives(ledger, arguments.cache_dir)
            _print_json({"daily_archive_sync": asdict(sync), "status": ledger.status()})
        else:
            summary = collect_daily_archives(
                ledger,
                arguments.cache_dir,
                arguments.start,
                arguments.end_exclusive,
            )
            _print_json({"daily_archive_ingest": asdict(summary), "status": ledger.status()})


if __name__ == "__main__":  # pragma: no cover
    main()
