"""Bounded archive preparation around the unchanged, resumable feature CLI."""

from __future__ import annotations

import argparse
import json
import sqlite3
import subprocess
import sys
import time
import zipfile
from datetime import date
from pathlib import Path
from urllib.error import HTTPError, URLError

from kairos_backtest.aggtrades import AggTradeIntegrityError, BinanceMonthlyAggTradeArchiveLoader
from kairos_backtest.quarter_hour_features import expected_sequence


def next_group(ledger: Path) -> tuple[int, tuple[tuple[str, str], ...]]:
    """Read only; never create a ledger or advance a research cursor."""
    with sqlite3.connect(ledger.resolve().as_uri() + "?mode=ro", uri=True) as connection:
        rows = connection.execute(
            "SELECT sequence, symbol, period FROM archive_batch ORDER BY sequence"
        ).fetchall()
    expected = expected_sequence()
    if rows != [(i, symbol, period) for i, (symbol, period) in enumerate(expected[: len(rows)])]:
        raise ValueError("committed batches are not the frozen sequence prefix")
    remaining = expected[len(rows) :]
    group = tuple(item for item in remaining if item[1] == remaining[0][1]) if remaining else ()
    return len(rows), group


def prepare_archive(loader, symbol: str, period: str, *, sleep=time.sleep) -> None:
    """At most three transport attempts; checksum/CRC errors are never retried."""
    for attempt in range(1, 4):
        try:
            archive = loader.load(symbol, date.fromisoformat(period + "-01"))
            with zipfile.ZipFile(archive.path) as compressed:
                if compressed.testzip() is not None:
                    raise AggTradeIntegrityError("prefetch ZIP CRC mismatch")
            print(json.dumps({"prepared": archive.path.name, "sha256": archive.archive_sha256}), flush=True)
            return
        except (TimeoutError, URLError) as exc:
            if isinstance(exc, HTTPError) and 400 <= exc.code < 500 and exc.code != 429:
                raise
            if attempt == 3:
                raise
            print(
                json.dumps(
                    {
                        "transport_retry": attempt,
                        "symbol": symbol,
                        "period": period,
                        "error_type": type(exc).__name__,
                    }
                ),
                flush=True,
            )
            sleep(10 * attempt)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ledger", type=Path, required=True)
    parser.add_argument("--cache-dir", type=Path, required=True)
    parser.add_argument("--workers", type=int, choices=range(1, 5), default=1)
    args = parser.parse_args(argv)
    command = [
        sys.executable,
        "-u",
        "-m",
        "kairos_backtest.quarter_hour_features",
        "--ledger",
        str(args.ledger),
        "--cache-dir",
        str(args.cache_dir),
    ]
    # The frozen verifier validates evidence and fingerprints before any download.
    subprocess.run([*command, "--verify"], check=True)
    loader = BinanceMonthlyAggTradeArchiveLoader(args.cache_dir, retries=1)
    while True:
        before, group = next_group(args.ledger)
        if not group:
            return 0
        print(
            json.dumps(
                {
                    "phase": "prepare_month",
                    "committed_batches": before,
                    "period": group[0][1],
                    "archives": len(group),
                }
            ),
            flush=True,
        )
        for symbol, period in group:
            prepare_archive(loader, symbol, period)
        # This remains the sole ledger writer. It performs its original checks
        # and excludes its original gap windows, without monkeypatching imports.
        subprocess.run(
            [*command, "--workers", str(args.workers), "--max-new-batches", str(len(group))], check=True
        )
        after, _ = next_group(args.ledger)
        if after != before + len(group):
            raise RuntimeError("frozen collector did not commit the prepared prefix")


if __name__ == "__main__":
    raise SystemExit(main())
