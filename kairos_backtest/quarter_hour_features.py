"""Durable, performance-blind feature collection for quarter-hour research."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import sqlite3
import subprocess  # nosec B404
from collections.abc import Mapping, Sequence
from concurrent.futures import ProcessPoolExecutor
from dataclasses import asdict
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from pathlib import Path
from typing import cast

from . import aggtrades as aggtrades_module
from .aggtrades import (
    AggTrade,
    BinanceMonthlyAggTradeArchiveLoader,
    PhasePeakExtraction,
    PhasePeakWindow,
    extract_phase_peak_windows,
)
from .scenarios import SYMBOLS

SCHEMA_VERSION = "kairos.quarter-hour-feature-ledger.v1"
PLAN_FILENAME = "reports/quarter-hour-lag-replication/plan.json"
PLAN_LOGICAL_SHA256 = "f335b3eaf75ffd153b2f4d4341271280cb725de98b1a7584a8eaed076da9dc99"
DATA_START = date(2021, 1, 1)
DATA_END_EXCLUSIVE = date(2026, 8, 1)
PHASE_OFFSETS_MINUTES = (0, 2, 5, 7)
_ZERO_SHA256 = "0" * 64


class QuarterHourFeatureIntegrityError(RuntimeError):
    """The plan, archive sequence, or durable feature evidence is inconsistent."""


def _json_value(value: object) -> object:
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("feature datetimes must be timezone-aware")
        return value.isoformat()
    if type(value) is date:
        return value.isoformat()
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise ValueError("feature JSON cannot contain non-finite decimals")
        normalized = value.normalize()
        return "0" if normalized == 0 else format(normalized, "f")
    if isinstance(value, Mapping):
        if any(not isinstance(key, str) for key in value):
            raise TypeError("JSON mappings require string keys")
        return {key: _json_value(value[key]) for key in sorted(value)}
    if isinstance(value, (tuple, list)):
        return [_json_value(item) for item in value]
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("feature JSON cannot contain non-finite floats")
        return 0.0 if value == 0 else value
    raise TypeError(f"unsupported feature JSON type: {type(value).__name__}")


def _json_bytes(value: object) -> bytes:
    return (
        json.dumps(
            _json_value(value),
            allow_nan=False,
            ensure_ascii=True,
            indent=2,
            sort_keys=True,
        )
        + "\n"
    ).encode("ascii")


def _logical_sha256(value: object) -> str:
    encoded = json.dumps(
        _json_value(value),
        allow_nan=False,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("ascii")
    return hashlib.sha256(encoded).hexdigest()


def load_plan(path: Path) -> dict[str, object]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or _logical_sha256(payload) != PLAN_LOGICAL_SHA256:
        raise QuarterHourFeatureIntegrityError(
            "committed quarter-hour replication plan differs from the preregistration"
        )
    return payload


def _next_month(value: date) -> date:
    if value.day != 1:
        raise ValueError("month sequence values must start on day one")
    return date(value.year + (value.month == 12), value.month % 12 + 1, 1)


def months(start: date, end_exclusive: date) -> tuple[date, ...]:
    if start.day != 1 or end_exclusive.day != 1 or start >= end_exclusive:
        raise ValueError("month range must be non-empty and aligned to calendar months")
    values: list[date] = []
    current = start
    while current < end_exclusive:
        values.append(current)
        current = _next_month(current)
    return tuple(values)


def expected_sequence() -> tuple[tuple[str, str], ...]:
    return tuple(
        (symbol, month.strftime("%Y-%m"))
        for month in months(DATA_START, DATA_END_EXCLUSIVE)
        for symbol in SYMBOLS
    )


def _expected_windows(period: str) -> int:
    start = date.fromisoformat(f"{period}-01")
    days = (_next_month(start) - start).days
    return days * 24 * 4 * len(PHASE_OFFSETS_MINUTES)


def _git(project_root: Path, *arguments: str) -> str:
    completed = subprocess.run(  # nosec B603
        ("git", *arguments),
        cwd=project_root,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    return completed.stdout.strip()


def _assert_clean(project_root: Path) -> str:
    if _git(project_root, "status", "--porcelain=v1", "--untracked-files=all"):
        raise RuntimeError("quarter-hour feature collection refuses a dirty Git worktree")
    return _git(project_root, "rev-parse", "HEAD")


def source_sha256() -> str:
    digest = hashlib.sha256()
    paths = sorted((Path(cast(str, aggtrades_module.__file__)), Path(__file__)), key=str)
    for path in paths:
        encoded_name = path.name.encode("ascii")
        content = path.read_bytes()
        digest.update(len(encoded_name).to_bytes(4, "big"))
        digest.update(encoded_name)
        digest.update(len(content).to_bytes(8, "big"))
        digest.update(content)
    return digest.hexdigest()


def _trade_from_json(raw: str) -> AggTrade:
    payload = json.loads(raw)
    if not isinstance(payload, dict):
        raise QuarterHourFeatureIntegrityError("stored last trade is not a mapping")
    try:
        integer_fields = {
            name: payload[name]
            for name in (
                "aggregate_trade_id",
                "first_trade_id",
                "last_trade_id",
                "transact_time_ms",
            )
        }
        if any(
            isinstance(value, bool) or not isinstance(value, int) or value < 0
            for value in integer_fields.values()
        ):
            raise TypeError("stored trade integer fields are invalid")
        maker = payload["buyer_is_maker"]
        if not isinstance(maker, bool):
            raise TypeError("stored trade maker flag is invalid")
        trade = AggTrade(
            aggregate_trade_id=integer_fields["aggregate_trade_id"],
            price=Decimal(payload["price"]),
            quantity=Decimal(payload["quantity"]),
            first_trade_id=integer_fields["first_trade_id"],
            last_trade_id=integer_fields["last_trade_id"],
            transact_time_ms=integer_fields["transact_time_ms"],
            buyer_is_maker=maker,
        )
        if (
            not trade.price.is_finite()
            or trade.price <= 0
            or not trade.quantity.is_finite()
            or trade.quantity <= 0
            or trade.first_trade_id > trade.last_trade_id
        ):
            raise ValueError("stored trade values are invalid")
        return trade
    except (KeyError, TypeError, ValueError) as exc:
        raise QuarterHourFeatureIntegrityError("stored last trade is invalid") from exc


def _window_payload(symbol: str, period: str, window: PhasePeakWindow) -> dict[str, object]:
    return {
        "buyer_taker_quantity": window.buyer_taker_quantity,
        "end_ms": window.end_ms,
        "first_aggregate_trade_id": window.first_aggregate_trade_id,
        "last_aggregate_trade_id": window.last_aggregate_trade_id,
        "missing_aggregate_trade_ids": window.missing_aggregate_trade_ids,
        "missing_raw_trade_ids": window.missing_raw_trade_ids,
        "open_to_vwap_return": window.open_to_vwap_return,
        "opening_reference_price": window.opening_reference_price,
        "period": period,
        "phase_offset_minutes": window.phase_offset_minutes,
        "seller_taker_quantity": window.seller_taker_quantity,
        "start_ms": window.start_ms,
        "symbol": symbol,
        "total_quantity": window.total_quantity,
        "trade_count": window.trade_count,
        "vwap": window.vwap,
    }


class QuarterHourFeatureLedger:
    """Append-only archive batches and causal feature rows bound to one source."""

    def __init__(self, path: Path, *, plan_sha256: str, feature_source_sha256: str) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(path)
        self._connection.execute("PRAGMA journal_mode=WAL")
        self._connection.execute("PRAGMA synchronous=FULL")
        self._connection.execute("PRAGMA foreign_keys=ON")
        self._connection.execute(
            "CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
        )
        self._connection.execute(
            """
            CREATE TABLE IF NOT EXISTS archive_batch (
                sequence INTEGER PRIMARY KEY,
                symbol TEXT NOT NULL,
                period TEXT NOT NULL,
                batch_json TEXT NOT NULL,
                batch_sha256 TEXT NOT NULL,
                previous_chain_sha256 TEXT NOT NULL,
                chain_sha256 TEXT NOT NULL,
                last_trade_json TEXT NOT NULL,
                window_count INTEGER NOT NULL,
                windows_chain_sha256 TEXT NOT NULL,
                UNIQUE(symbol, period)
            )
            """
        )
        self._connection.execute(
            """
            CREATE TABLE IF NOT EXISTS peak_window (
                batch_sequence INTEGER NOT NULL,
                ordinal INTEGER NOT NULL,
                symbol TEXT NOT NULL,
                period TEXT NOT NULL,
                phase_offset_minutes INTEGER NOT NULL,
                start_ms INTEGER NOT NULL,
                return_text TEXT NOT NULL,
                missing_aggregate_trade_ids INTEGER NOT NULL,
                missing_raw_trade_ids INTEGER NOT NULL,
                feature_json TEXT NOT NULL,
                feature_sha256 TEXT NOT NULL,
                PRIMARY KEY(batch_sequence, ordinal),
                UNIQUE(symbol, phase_offset_minutes, start_ms),
                FOREIGN KEY(batch_sequence) REFERENCES archive_batch(sequence)
            )
            """
        )
        expected_metadata = {
            "feature_source_sha256": feature_source_sha256,
            "plan_sha256": plan_sha256,
            "schema_version": SCHEMA_VERSION,
        }
        for key, value in expected_metadata.items():
            existing = self._connection.execute("SELECT value FROM metadata WHERE key = ?", (key,)).fetchone()
            if existing is None:
                self._connection.execute("INSERT INTO metadata(key, value) VALUES (?, ?)", (key, value))
            elif existing[0] != value:
                raise QuarterHourFeatureIntegrityError(f"feature ledger {key} mismatch")
        self._connection.commit()

    def __enter__(self) -> QuarterHourFeatureLedger:
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.close()

    def close(self) -> None:
        self._connection.close()

    def completed_batches(self) -> int:
        return cast(
            int,
            self._connection.execute("SELECT COUNT(*) FROM archive_batch").fetchone()[0],
        )

    def last_trade(self, symbol: str, *, before_sequence: int | None = None) -> AggTrade | None:
        query = "SELECT last_trade_json FROM archive_batch WHERE symbol = ?"
        parameters: list[object] = [symbol]
        if before_sequence is not None:
            query += " AND sequence < ?"
            parameters.append(before_sequence)
        query += " ORDER BY sequence DESC LIMIT 1"
        row = self._connection.execute(query, parameters).fetchone()
        return None if row is None else _trade_from_json(cast(str, row[0]))

    def _validate_extraction(
        self,
        *,
        sequence: int,
        symbol: str,
        period: str,
        extraction: PhasePeakExtraction,
    ) -> tuple[int, str, list[tuple[object, ...]]]:
        manifest = extraction.manifest
        if manifest.symbol != symbol or manifest.period != period:
            raise QuarterHourFeatureIntegrityError("extraction manifest differs from plan sequence")
        if manifest.missing_aggregate_trade_ids != 0:
            raise QuarterHourFeatureIntegrityError("aggregate trade IDs contain an in-period gap")
        if (
            extraction.last_trade.aggregate_trade_id != manifest.last_aggregate_trade_id
            or extraction.last_trade.transact_time_ms != manifest.last_transact_time_ms
        ):
            raise QuarterHourFeatureIntegrityError("last trade differs from archive manifest")
        expected_windows = _expected_windows(period)
        if extraction.expected_windows != expected_windows:
            raise QuarterHourFeatureIntegrityError("phase-window count differs from calendar plan")
        if (
            len(extraction.windows) + extraction.empty_windows + extraction.missing_reference_windows
            != expected_windows
        ):
            raise QuarterHourFeatureIntegrityError("phase-window disposition count is incomplete")

        previous_symbol_trade = self.last_trade(symbol, before_sequence=sequence)
        cross_period_aggregate_gap = 0
        cross_period_raw_gap = 0
        if previous_symbol_trade is not None:
            cross_period_aggregate_gap = (
                manifest.first_aggregate_trade_id - previous_symbol_trade.aggregate_trade_id - 1
            )
            cross_period_raw_gap = manifest.first_raw_trade_id - previous_symbol_trade.last_trade_id - 1
            if manifest.first_transact_time_ms < previous_symbol_trade.transact_time_ms:
                raise QuarterHourFeatureIntegrityError("archive timestamps regress across periods")
            if cross_period_aggregate_gap != 0:
                raise QuarterHourFeatureIntegrityError("aggregate trade IDs contain a cross-period gap")

        windows_chain = _ZERO_SHA256
        rows: list[tuple[object, ...]] = []
        previous_key: tuple[int, int] | None = None
        for ordinal, window in enumerate(extraction.windows):
            key = (window.start_ms, window.phase_offset_minutes)
            if previous_key is not None and key <= previous_key:
                raise QuarterHourFeatureIntegrityError("feature windows are not strictly ordered")
            previous_key = key
            if window.phase_offset_minutes not in PHASE_OFFSETS_MINUTES:
                raise QuarterHourFeatureIntegrityError("feature window uses an unplanned phase")
            if (window.start_ms // 60_000) % 15 != window.phase_offset_minutes:
                raise QuarterHourFeatureIntegrityError("feature window is off its planned phase")
            if window.end_ms != window.start_ms + 10_000:
                raise QuarterHourFeatureIntegrityError("feature window has the wrong width")
            if window.missing_aggregate_trade_ids != 0:
                raise QuarterHourFeatureIntegrityError("feature target crosses an aggregate-ID gap")
            payload = _window_payload(symbol, period, window)
            feature_json = _json_bytes(payload).decode("ascii")
            feature_sha256 = _logical_sha256(payload)
            windows_chain = hashlib.sha256(f"{windows_chain}:{feature_sha256}".encode("ascii")).hexdigest()
            rows.append(
                (
                    sequence,
                    ordinal,
                    symbol,
                    period,
                    window.phase_offset_minutes,
                    window.start_ms,
                    cast(str, _json_value(window.open_to_vwap_return)),
                    window.missing_aggregate_trade_ids,
                    window.missing_raw_trade_ids,
                    feature_json,
                    feature_sha256,
                )
            )
        return cross_period_raw_gap, windows_chain, rows

    def append(self, sequence: int, extraction: PhasePeakExtraction) -> bool:
        expected = expected_sequence()
        if sequence < 0 or sequence >= len(expected):
            raise QuarterHourFeatureIntegrityError("feature sequence lies outside the plan")
        symbol, period = expected[sequence]
        cross_raw_gap, windows_chain, rows = self._validate_extraction(
            sequence=sequence,
            symbol=symbol,
            period=period,
            extraction=extraction,
        )
        batch_payload: dict[str, object] = {
            "cross_period_missing_raw_trade_ids": cross_raw_gap,
            "empty_windows": extraction.empty_windows,
            "expected_windows": extraction.expected_windows,
            "last_trade": asdict(extraction.last_trade),
            "manifest": asdict(extraction.manifest),
            "missing_reference_windows": extraction.missing_reference_windows,
            "symbol": symbol,
            "period": period,
            "window_count": len(rows),
            "windows_chain_sha256": windows_chain,
        }
        batch_json = _json_bytes(batch_payload).decode("ascii")
        batch_sha256 = _logical_sha256(batch_payload)
        last_trade_json = _json_bytes(asdict(extraction.last_trade)).decode("ascii")
        existing = self._connection.execute(
            "SELECT batch_json, last_trade_json FROM archive_batch WHERE sequence = ?",
            (sequence,),
        ).fetchone()
        if existing is not None:
            if existing != (batch_json, last_trade_json):
                raise QuarterHourFeatureIntegrityError("recorded feature batch conflicts with replay")
            return False
        if sequence != self.completed_batches():
            raise QuarterHourFeatureIntegrityError("feature batches must append in exact plan order")
        previous = _ZERO_SHA256
        if sequence:
            row = self._connection.execute(
                "SELECT chain_sha256 FROM archive_batch WHERE sequence = ?", (sequence - 1,)
            ).fetchone()
            if row is None:
                raise QuarterHourFeatureIntegrityError("feature batch chain is incomplete")
            previous = cast(str, row[0])
        chain = hashlib.sha256(f"{previous}:{batch_sha256}".encode("ascii")).hexdigest()
        with self._connection:
            self._connection.execute(
                """
                INSERT INTO archive_batch(
                    sequence, symbol, period, batch_json, batch_sha256,
                    previous_chain_sha256, chain_sha256, last_trade_json,
                    window_count, windows_chain_sha256
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    sequence,
                    symbol,
                    period,
                    batch_json,
                    batch_sha256,
                    previous,
                    chain,
                    last_trade_json,
                    len(rows),
                    windows_chain,
                ),
            )
            self._connection.executemany(
                """
                INSERT INTO peak_window(
                    batch_sequence, ordinal, symbol, period, phase_offset_minutes,
                    start_ms, return_text, missing_aggregate_trade_ids,
                    missing_raw_trade_ids, feature_json, feature_sha256
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                rows,
            )
        return True

    def verify(self, *, require_complete: bool, deep: bool = False) -> str:
        expected = expected_sequence()
        batches = self._connection.execute(
            """
            SELECT sequence, symbol, period, batch_json, batch_sha256,
                   previous_chain_sha256, chain_sha256, last_trade_json,
                   window_count, windows_chain_sha256
            FROM archive_batch ORDER BY sequence
            """
        ).fetchall()
        if require_complete and len(batches) != len(expected):
            raise QuarterHourFeatureIntegrityError("feature ledger is incomplete")
        previous = _ZERO_SHA256
        for ordinal, row in enumerate(batches):
            (
                sequence,
                symbol,
                period,
                batch_json,
                batch_sha256,
                stored_previous,
                chain,
                last_trade_json,
                window_count,
                stored_windows_chain,
            ) = row
            if sequence != ordinal or (symbol, period) != expected[ordinal]:
                raise QuarterHourFeatureIntegrityError("feature batch order differs from plan")
            payload = json.loads(cast(str, batch_json))
            if _json_bytes(payload).decode("ascii") != batch_json:
                raise QuarterHourFeatureIntegrityError("feature batch JSON is not canonical")
            if _logical_sha256(payload) != batch_sha256 or stored_previous != previous:
                raise QuarterHourFeatureIntegrityError("feature batch hash chain is invalid")
            if (
                payload.get("symbol") != symbol
                or payload.get("period") != period
                or payload.get("window_count") != window_count
                or payload.get("windows_chain_sha256") != stored_windows_chain
                or _json_bytes(payload.get("last_trade")).decode("ascii") != last_trade_json
            ):
                raise QuarterHourFeatureIntegrityError("feature batch columns differ from evidence")
            actual_chain = hashlib.sha256(f"{previous}:{batch_sha256}".encode("ascii")).hexdigest()
            if actual_chain != chain:
                raise QuarterHourFeatureIntegrityError("feature batch hash chain is invalid")
            if deep:
                feature_rows = self._connection.execute(
                    """
                    SELECT ordinal, symbol, period, phase_offset_minutes, start_ms,
                           return_text, missing_aggregate_trade_ids,
                           missing_raw_trade_ids, feature_json, feature_sha256
                    FROM peak_window
                    WHERE batch_sequence = ? ORDER BY ordinal
                    """,
                    (sequence,),
                ).fetchall()
                if len(feature_rows) != window_count:
                    raise QuarterHourFeatureIntegrityError("feature row count differs from batch")
                windows_chain = _ZERO_SHA256
                for feature_row in feature_rows:
                    (
                        feature_ordinal,
                        feature_symbol,
                        feature_period,
                        feature_phase,
                        feature_start_ms,
                        return_text,
                        missing_aggregate_ids,
                        missing_raw_ids,
                        feature_json,
                        feature_sha256,
                    ) = feature_row
                    if feature_ordinal < 0 or feature_ordinal >= window_count:
                        raise QuarterHourFeatureIntegrityError("feature ordinal is invalid")
                    feature = json.loads(cast(str, feature_json))
                    if _json_bytes(feature).decode("ascii") != feature_json:
                        raise QuarterHourFeatureIntegrityError("feature JSON is not canonical")
                    if _logical_sha256(feature) != feature_sha256:
                        raise QuarterHourFeatureIntegrityError("feature SHA-256 is invalid")
                    if (
                        feature.get("symbol") != feature_symbol
                        or feature.get("period") != feature_period
                        or feature.get("phase_offset_minutes") != feature_phase
                        or feature.get("start_ms") != feature_start_ms
                        or feature.get("open_to_vwap_return") != return_text
                        or feature.get("missing_aggregate_trade_ids") != missing_aggregate_ids
                        or feature.get("missing_raw_trade_ids") != missing_raw_ids
                    ):
                        raise QuarterHourFeatureIntegrityError(
                            "feature query columns differ from canonical evidence"
                        )
                    windows_chain = hashlib.sha256(
                        f"{windows_chain}:{feature_sha256}".encode("ascii")
                    ).hexdigest()
                if windows_chain != stored_windows_chain:
                    raise QuarterHourFeatureIntegrityError("feature window chain is invalid")
            previous = cast(str, chain)
        return previous

    def phase_returns(
        self,
        *,
        symbol: str,
        phase_offset_minutes: int,
        start_ms: int,
        end_ms: int,
        clean_only: bool,
    ) -> tuple[tuple[int, float], ...]:
        if symbol not in SYMBOLS or phase_offset_minutes not in PHASE_OFFSETS_MINUTES:
            raise ValueError("return query lies outside the preregistered universe or phases")
        if start_ms < 0 or end_ms <= start_ms:
            raise ValueError("return query range must be positive and non-empty")
        query = """
            SELECT start_ms, return_text FROM peak_window
            WHERE symbol = ? AND phase_offset_minutes = ?
              AND start_ms >= ? AND start_ms < ?
        """
        parameters: list[object] = [symbol, phase_offset_minutes, start_ms, end_ms]
        if clean_only:
            query += " AND missing_aggregate_trade_ids = 0 AND missing_raw_trade_ids = 0"
        query += " ORDER BY start_ms"
        rows = self._connection.execute(query, parameters).fetchall()
        result: list[tuple[int, float]] = []
        previous_ms: int | None = None
        for raw_ms, raw_return in rows:
            timestamp_ms = int(raw_ms)
            value = float(Decimal(cast(str, raw_return)))
            if not math.isfinite(value):
                raise QuarterHourFeatureIntegrityError("stored feature return is non-finite")
            if previous_ms is not None and timestamp_ms <= previous_ms:
                raise QuarterHourFeatureIntegrityError("stored feature returns are not ordered")
            previous_ms = timestamp_ms
            result.append((timestamp_ms, value))
        return tuple(result)


def collect_features(
    *,
    project_root: Path,
    plan_path: Path,
    ledger_path: Path,
    cache_dir: Path,
    max_new_batches: int | None = None,
    workers: int = 1,
    loader_factory=BinanceMonthlyAggTradeArchiveLoader,
    require_clean: bool = True,
) -> dict[str, object]:
    if isinstance(workers, bool) or not isinstance(workers, int) or not 1 <= workers <= 8:
        raise ValueError("feature workers must be an integer in [1, 8]")
    if max_new_batches is not None and (
        isinstance(max_new_batches, bool) or not isinstance(max_new_batches, int) or max_new_batches < 1
    ):
        raise ValueError("maximum new feature batches must be a positive integer")
    if workers > 1 and loader_factory is not BinanceMonthlyAggTradeArchiveLoader:
        raise ValueError("parallel feature collection requires the official monthly loader")
    plan = load_plan(plan_path)
    plan_sha = _logical_sha256(plan)
    head = _assert_clean(project_root) if require_clean else "0" * 40
    source_sha = source_sha256()
    appended = 0
    sequence_plan = expected_sequence()
    with QuarterHourFeatureLedger(
        ledger_path,
        plan_sha256=plan_sha,
        feature_source_sha256=source_sha,
    ) as ledger:
        ledger.verify(require_complete=False)
        completed_before = ledger.completed_batches()
        pending = list(enumerate(sequence_plan))[completed_before:]
        if max_new_batches is not None:
            pending = pending[:max_new_batches]
        grouped: list[list[tuple[int, tuple[str, str]]]] = []
        for item in pending:
            if not grouped or grouped[-1][0][1][1] != item[1][1]:
                grouped.append([])
            grouped[-1].append(item)
        if workers == 1:
            loader = loader_factory(cache_dir)
            for group in grouped:
                for sequence, (symbol, period) in group:
                    extraction = _extract_one(
                        cache_dir=cache_dir,
                        symbol=symbol,
                        period=period,
                        prior_trade=ledger.last_trade(symbol),
                        loader=loader,
                    )
                    ledger.append(sequence, extraction)
                    appended += 1
                    _print_progress(sequence, sequence_plan, extraction)
        else:
            with ProcessPoolExecutor(max_workers=workers) as executor:
                for group in grouped:
                    futures = {
                        sequence: executor.submit(
                            _extract_worker,
                            cache_dir,
                            symbol,
                            period,
                            ledger.last_trade(symbol),
                        )
                        for sequence, (symbol, period) in group
                    }
                    for sequence, _ in group:
                        extraction = futures[sequence].result()
                        ledger.append(sequence, extraction)
                        appended += 1
                        _print_progress(sequence, sequence_plan, extraction)
        chain = ledger.verify(require_complete=False)
        completed_after = ledger.completed_batches()
    return {
        "appended_batches": appended,
        "batch_chain_sha256": chain,
        "completed_batches": completed_after,
        "feature_source_sha256": source_sha,
        "git_head_sha": head,
        "plan_sha256": plan_sha,
        "remaining_batches": len(sequence_plan) - completed_after,
        "total_batches": len(sequence_plan),
    }


def _extract_one(
    *,
    cache_dir: Path,
    symbol: str,
    period: str,
    prior_trade: AggTrade | None,
    loader: BinanceMonthlyAggTradeArchiveLoader,
) -> PhasePeakExtraction:
    month = date.fromisoformat(f"{period}-01")
    transport = loader.load(symbol, month)
    return extract_phase_peak_windows(
        transport,
        loader.iter_trades(transport),
        phase_offsets_minutes=PHASE_OFFSETS_MINUTES,
        prior_trade=prior_trade,
    )


def _extract_worker(
    cache_dir: Path,
    symbol: str,
    period: str,
    prior_trade: AggTrade | None,
) -> PhasePeakExtraction:
    return _extract_one(
        cache_dir=cache_dir,
        symbol=symbol,
        period=period,
        prior_trade=prior_trade,
        loader=BinanceMonthlyAggTradeArchiveLoader(cache_dir),
    )


def _print_progress(
    sequence: int,
    sequence_plan: Sequence[tuple[str, str]],
    extraction: PhasePeakExtraction,
) -> None:
    symbol, period = sequence_plan[sequence]
    print(
        f"feature_batch={sequence + 1}/{len(sequence_plan)} "
        f"symbol={symbol} period={period} rows={extraction.manifest.rows} "
        f"windows={len(extraction.windows)}"
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Collect or verify preregistered quarter-hour features")
    parser.add_argument("--plan", type=Path, default=Path(PLAN_FILENAME))
    parser.add_argument("--ledger", type=Path, required=True)
    parser.add_argument("--cache-dir", type=Path, required=True)
    parser.add_argument("--max-new-batches", type=int)
    parser.add_argument("--workers", type=int, default=1)
    parser.add_argument("--verify", action="store_true")
    parser.add_argument("--deep", action="store_true")
    arguments = parser.parse_args(argv)
    plan = load_plan(arguments.plan)
    if arguments.verify:
        with QuarterHourFeatureLedger(
            arguments.ledger,
            plan_sha256=_logical_sha256(plan),
            feature_source_sha256=source_sha256(),
        ) as ledger:
            chain = ledger.verify(require_complete=False, deep=arguments.deep)
            payload = {
                "batch_chain_sha256": chain,
                "completed_batches": ledger.completed_batches(),
                "total_batches": len(expected_sequence()),
            }
    else:
        project_root = Path(__file__).resolve().parents[1]
        payload = collect_features(
            project_root=project_root,
            plan_path=arguments.plan,
            ledger_path=arguments.ledger,
            cache_dir=arguments.cache_dir,
            max_new_batches=arguments.max_new_batches,
            workers=arguments.workers,
        )
    print(json.dumps(_json_value(payload), separators=(",", ":"), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
