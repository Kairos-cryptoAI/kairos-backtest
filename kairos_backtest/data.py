from __future__ import annotations

import hashlib
import io
import json
import time
import zipfile
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import urlopen

from kairos_quant.candles import Candle

ARCHIVE_ROOT = "https://data.binance.vision/data/futures/um/monthly/klines"


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


def month_starts(start: date, end: date) -> list[date]:
    current = date(start.year, start.month, 1)
    result = []
    while current < end:
        result.append(current)
        current = date(current.year + (current.month == 12), current.month % 12 + 1, 1)
    return result


def _parse_csv(payload: bytes, symbol: str, interval: str) -> list[Candle]:
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        name = next(name for name in archive.namelist() if name.endswith(".csv"))
        lines = archive.read(name).decode().splitlines()
    candles = []
    for line in lines:
        row = line.split(",")
        if not row or not row[0].isdigit():
            continue
        candles.append(
            Candle(
                symbol=symbol,
                timeframe=interval,
                open_time_ms=int(row[0]),
                close_time_ms=int(row[6]),
                open=float(row[1]),
                high=float(row[2]),
                low=float(row[3]),
                close=float(row[4]),
                volume=float(row[5]),
                quote_volume=float(row[7]),
                taker_buy_volume=float(row[9]),
            )
        )
    return candles


class BinanceArchiveLoader:
    def __init__(self, cache_dir: Path, *, retries: int = 3) -> None:
        self.cache_dir = cache_dir
        self.retries = retries

    def _download(self, url: str, target: Path) -> bytes | None:
        if target.exists():
            return target.read_bytes()
        for attempt in range(self.retries):
            try:
                with urlopen(url, timeout=60) as response:  # noqa: S310 - fixed Binance host
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

    def load(
        self, symbol: str, start: date, end: date, interval: str = "1m"
    ) -> tuple[list[Candle], DatasetManifest]:
        symbol = symbol.upper()
        candles: list[Candle] = []
        files: list[str] = []
        for month in month_starts(start, end):
            filename = f"{symbol}-{interval}-{month:%Y-%m}.zip"
            url = f"{ARCHIVE_ROOT}/{symbol}/{interval}/{filename}"
            target = self.cache_dir / symbol / interval / filename
            payload = self._download(url, target)
            if payload is None:
                continue
            files.append(filename)
            candles.extend(_parse_csv(payload, symbol, interval))
        start_ms = int(datetime.combine(start, datetime.min.time(), UTC).timestamp() * 1000)
        end_ms = int(datetime.combine(end, datetime.min.time(), UTC).timestamp() * 1000)
        candles = sorted(
            {c.open_time_ms: c for c in candles if start_ms <= c.open_time_ms < end_ms}.values(),
            key=lambda c: c.open_time_ms,
        )
        if not candles:
            raise ValueError(f"no Binance archive data for {symbol} {start}..{end}")
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
        )
        return candles, manifest
