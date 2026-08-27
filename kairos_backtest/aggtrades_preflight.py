"""Resumable, performance-blind qualification of official ``aggTrades`` data."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import sqlite3
import subprocess  # nosec B404
import tempfile
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict
from datetime import UTC, date, datetime
from enum import Enum
from pathlib import Path
from typing import cast

from . import aggtrades as aggtrades_module
from .aggtrades import AggTradeArchiveManifest, BinanceAggTradeArchiveLoader, completed_days
from .scenarios import SYMBOLS

SCHEMA_VERSION = "kairos.aggtrades-data-preflight.v1"
PLAN_FILENAME = "reports/aggtrades-preflight/plan.json"
RESULT_FILENAME = "reports/aggtrades-preflight/result.json"
CANARY_START = date(2026, 8, 25)
CANARY_END_EXCLUSIVE = date(2026, 8, 26)
_FORBIDDEN_RESULT_KEYS = {
    "alpha_ready",
    "expectancy",
    "live_allowed",
    "paper_allowed",
    "pnl",
    "profit_factor",
    "return",
    "signal",
    "trade_count",
    "win_rate",
}


class AggTradePreflightIntegrityError(RuntimeError):
    """The preregistered plan, progress ledger, or receipt is inconsistent."""


def _json_value(value: object) -> object:
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("preflight datetimes must be timezone-aware")
        return value.isoformat()
    if type(value) is date:
        return value.isoformat()
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
            raise ValueError("preflight JSON cannot contain non-finite values")
        return 0.0 if value == 0 else value
    raise TypeError(f"unsupported preflight JSON type: {type(value).__name__}")


def _json_bytes(value: object) -> bytes:
    return (
        json.dumps(_json_value(value), allow_nan=False, ensure_ascii=True, indent=2, sort_keys=True) + "\n"
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


def expected_plan() -> dict[str, object]:
    return {
        "classification": "PERFORMANCE_BLIND_SOURCE_QUALIFICATION",
        "data": {
            "dataset": "aggTrades",
            "end_exclusive": CANARY_END_EXCLUSIVE.isoformat(),
            "market": "Binance USD-M futures",
            "official_checksums_required": True,
            "start": CANARY_START.isoformat(),
            "symbols": list(SYMBOLS),
            "transport": "official daily ZIP plus adjacent SHA-256 sidecar",
        },
        "measurement_contract": {
            "boundary_minutes": [0, 15, 30, 45],
            "buyer_taker_rule": "not is_buyer_maker",
            "opening_reference": "latest transaction price at or before T",
            "peak_window": "(T,T+10s]",
            "source_paper": "https://arxiv.org/abs/2607.09426v2",
        },
        "permissions": {
            "alpha_ready": False,
            "live_allowed": False,
            "paper_allowed": False,
            "promotion_eligible": False,
        },
        "prohibitions": [
            "directional metric",
            "fitted model",
            "PnL",
            "strategy generator",
            "trade simulation",
        ],
        "schema_version": SCHEMA_VERSION,
    }


def load_plan(path: Path) -> dict[str, object]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or _json_value(payload) != _json_value(expected_plan()):
        raise AggTradePreflightIntegrityError(
            "committed aggTrades preflight plan differs from executable plan"
        )
    return payload


def _expected_sequence() -> tuple[tuple[str, str], ...]:
    return tuple(
        (symbol, day.isoformat())
        for day in completed_days(CANARY_START, CANARY_END_EXCLUSIVE)
        for symbol in SYMBOLS
    )


def _atomic_write(path: Path, payload: bytes) -> None:
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
        raise RuntimeError("aggTrades preflight refuses to open data from a dirty Git worktree")
    return _git(project_root, "rev-parse", "HEAD")


class AggTradePreflightLedger:
    """Append-only manifest chain bound to one exact data-only plan."""

    def __init__(self, path: Path, plan_sha256: str) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(path)
        self._connection.execute("PRAGMA journal_mode=WAL")
        self._connection.execute("PRAGMA synchronous=FULL")
        self._connection.execute(
            "CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
        )
        self._connection.execute(
            """
            CREATE TABLE IF NOT EXISTS archive_manifest (
                sequence INTEGER PRIMARY KEY,
                symbol TEXT NOT NULL,
                day TEXT NOT NULL,
                manifest_json TEXT NOT NULL,
                manifest_sha256 TEXT NOT NULL,
                previous_chain_sha256 TEXT NOT NULL,
                chain_sha256 TEXT NOT NULL,
                UNIQUE(symbol, day)
            )
            """
        )
        expected_metadata = {
            "plan_sha256": plan_sha256,
            "schema_version": SCHEMA_VERSION,
        }
        for key, value in expected_metadata.items():
            existing = self._connection.execute("SELECT value FROM metadata WHERE key = ?", (key,)).fetchone()
            if existing is None:
                self._connection.execute("INSERT INTO metadata(key, value) VALUES (?, ?)", (key, value))
            elif existing[0] != value:
                raise AggTradePreflightIntegrityError(f"preflight ledger {key} mismatch")
        self._connection.commit()

    def __enter__(self) -> AggTradePreflightLedger:
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.close()

    def close(self) -> None:
        self._connection.close()

    def _rows(self) -> list[tuple[object, ...]]:
        return self._connection.execute(
            """
            SELECT sequence, symbol, day, manifest_json, manifest_sha256,
                   previous_chain_sha256, chain_sha256
            FROM archive_manifest ORDER BY sequence
            """
        ).fetchall()

    def manifests(self) -> tuple[dict[str, object], ...]:
        return tuple(cast(dict[str, object], json.loads(cast(str, row[3]))) for row in self._rows())

    def append(self, sequence: int, manifest: AggTradeArchiveManifest) -> bool:
        expected = _expected_sequence()
        if sequence < 0 or sequence >= len(expected):
            raise AggTradePreflightIntegrityError("preflight sequence lies outside the plan")
        if (manifest.symbol, manifest.day) != expected[sequence]:
            raise AggTradePreflightIntegrityError("archive manifest does not match its plan sequence")
        payload = asdict(manifest)
        manifest_json = _json_bytes(payload).decode("ascii")
        manifest_sha256 = _logical_sha256(payload)
        existing = self._connection.execute(
            "SELECT manifest_json FROM archive_manifest WHERE sequence = ?", (sequence,)
        ).fetchone()
        if existing is not None:
            if existing[0] != manifest_json:
                raise AggTradePreflightIntegrityError("recorded archive manifest conflicts with replay")
            return False
        count = cast(int, self._connection.execute("SELECT COUNT(*) FROM archive_manifest").fetchone()[0])
        if sequence != count:
            raise AggTradePreflightIntegrityError("preflight manifests must append in exact plan order")
        previous = "0" * 64
        if sequence:
            row = self._connection.execute(
                "SELECT chain_sha256 FROM archive_manifest WHERE sequence = ?", (sequence - 1,)
            ).fetchone()
            if row is None:
                raise AggTradePreflightIntegrityError("preflight manifest chain is incomplete")
            previous = cast(str, row[0])
        chain_sha256 = hashlib.sha256(f"{previous}:{manifest_sha256}".encode("ascii")).hexdigest()
        with self._connection:
            self._connection.execute(
                """
                INSERT INTO archive_manifest(
                    sequence, symbol, day, manifest_json, manifest_sha256,
                    previous_chain_sha256, chain_sha256
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    sequence,
                    manifest.symbol,
                    manifest.day,
                    manifest_json,
                    manifest_sha256,
                    previous,
                    chain_sha256,
                ),
            )
        return True

    def verify(self, *, require_complete: bool) -> str:
        rows = self._rows()
        expected = _expected_sequence()
        if require_complete and len(rows) != len(expected):
            raise AggTradePreflightIntegrityError("preflight ledger is incomplete")
        previous = "0" * 64
        for sequence, row in enumerate(rows):
            stored_sequence, symbol, day, manifest_json, manifest_sha256, stored_previous, chain = row
            if stored_sequence != sequence or (symbol, day) != expected[sequence]:
                raise AggTradePreflightIntegrityError("preflight ledger order differs from the plan")
            payload = json.loads(cast(str, manifest_json))
            if _json_bytes(payload).decode("ascii") != manifest_json:
                raise AggTradePreflightIntegrityError("preflight manifest is not canonical JSON")
            actual_manifest_sha256 = _logical_sha256(payload)
            if manifest_sha256 != actual_manifest_sha256 or stored_previous != previous:
                raise AggTradePreflightIntegrityError("preflight manifest hash chain is invalid")
            actual_chain = hashlib.sha256(f"{previous}:{manifest_sha256}".encode("ascii")).hexdigest()
            if chain != actual_chain:
                raise AggTradePreflightIntegrityError("preflight manifest hash chain is invalid")
            previous = cast(str, chain)
        return previous


def _assert_performance_blind(value: object) -> None:
    if isinstance(value, Mapping):
        forbidden = _FORBIDDEN_RESULT_KEYS.intersection(value)
        if forbidden:
            raise AggTradePreflightIntegrityError(
                f"performance-blind receipt contains forbidden keys: {sorted(forbidden)}"
            )
        for child in value.values():
            _assert_performance_blind(child)
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        for child in value:
            _assert_performance_blind(child)


def _receipt(
    *,
    plan_sha256: str,
    git_head_sha: str,
    chain_sha256: str,
    manifests: tuple[dict[str, object], ...],
) -> dict[str, object]:
    rows = sum(cast(int, manifest["rows"]) for manifest in manifests)
    missing_aggregate_ids = sum(cast(int, manifest["missing_aggregate_trade_ids"]) for manifest in manifests)
    missing_raw_ids = sum(cast(int, manifest["missing_raw_trade_ids"]) for manifest in manifests)
    payload: dict[str, object] = {
        "archives": list(manifests),
        "classification": "DATA_PREFLIGHT_PASSED",
        "completed_at": datetime.now(UTC).isoformat(),
        "environment": {
            "git_head_sha": git_head_sha,
            "loader_source_sha256": hashlib.sha256(
                Path(cast(str, aggtrades_module.__file__)).read_bytes()
            ).hexdigest(),
            "preflight_source_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        },
        "evidence": {
            "archive_count": len(manifests),
            "manifest_chain_sha256": chain_sha256,
            "missing_aggregate_trade_ids": missing_aggregate_ids,
            "missing_raw_trade_ids": missing_raw_ids,
            "rows": rows,
        },
        "permissions": {
            "alpha": False,
            "live": False,
            "paper": False,
            "promotion": False,
        },
        "plan_sha256": plan_sha256,
        "result_schema_version": SCHEMA_VERSION,
    }
    _assert_performance_blind(payload)
    payload["result_sha256"] = _logical_sha256(payload)
    return payload


def run_preflight(
    *,
    plan_path: Path,
    ledger_path: Path,
    cache_dir: Path,
    result_path: Path,
    project_root: Path,
    loader_factory: Callable[[Path], BinanceAggTradeArchiveLoader] = BinanceAggTradeArchiveLoader,
    require_clean: bool = True,
    git_head_sha: str | None = None,
) -> dict[str, object]:
    plan = load_plan(plan_path)
    plan_sha256 = _logical_sha256(plan)
    head = _assert_clean(project_root) if require_clean else (git_head_sha or "0" * 40)
    loader = loader_factory(cache_dir)
    with AggTradePreflightLedger(ledger_path, plan_sha256) as ledger:
        ledger.verify(require_complete=False)
        completed = len(ledger.manifests())
        for sequence, (symbol, day_text) in enumerate(_expected_sequence()):
            if sequence < completed:
                continue
            archive = loader.load(symbol, date.fromisoformat(day_text))
            ledger.append(sequence, archive.manifest)
        chain_sha256 = ledger.verify(require_complete=True)
        result = _receipt(
            plan_sha256=plan_sha256,
            git_head_sha=head,
            chain_sha256=chain_sha256,
            manifests=ledger.manifests(),
        )
    encoded = _json_bytes(result)
    if result_path.exists():
        if result_path.read_bytes() != encoded:
            raise FileExistsError(f"aggTrades preflight result already exists: {result_path}")
    else:
        _atomic_write(result_path, encoded)
    return result


def verify_preflight(*, plan_path: Path, ledger_path: Path, result_path: Path) -> dict[str, object]:
    plan = load_plan(plan_path)
    plan_sha256 = _logical_sha256(plan)
    payload = json.loads(result_path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or result_path.read_bytes() != _json_bytes(payload):
        raise AggTradePreflightIntegrityError("aggTrades preflight receipt is not canonical JSON")
    result_sha256 = payload.pop("result_sha256", None)
    if result_sha256 != _logical_sha256(payload):
        raise AggTradePreflightIntegrityError("aggTrades preflight receipt SHA-256 is invalid")
    payload["result_sha256"] = result_sha256
    if payload.get("plan_sha256") != plan_sha256:
        raise AggTradePreflightIntegrityError("aggTrades preflight receipt belongs to another plan")
    _assert_performance_blind(payload)
    with AggTradePreflightLedger(ledger_path, plan_sha256) as ledger:
        chain_sha256 = ledger.verify(require_complete=True)
        if cast(dict[str, object], payload["evidence"]).get("manifest_chain_sha256") != chain_sha256:
            raise AggTradePreflightIntegrityError("receipt and ledger manifest chains differ")
        if payload.get("archives") != list(ledger.manifests()):
            raise AggTradePreflightIntegrityError("receipt and ledger manifests differ")
    return payload


def _print_json(value: object) -> None:
    print(json.dumps(_json_value(value), ensure_ascii=True, separators=(",", ":"), sort_keys=True))


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("verify-plan", "run", "verify"))
    parser.add_argument("--plan", type=Path, default=Path(PLAN_FILENAME))
    parser.add_argument("--ledger", type=Path)
    parser.add_argument("--cache-dir", type=Path)
    parser.add_argument("--result", type=Path, default=Path(RESULT_FILENAME))
    arguments = parser.parse_args(argv)
    if arguments.command == "verify-plan":
        plan = load_plan(arguments.plan)
        _print_json({"plan_sha256": _logical_sha256(plan), "status": "valid"})
        return
    if arguments.ledger is None:
        parser.error("run and verify require --ledger")
    if arguments.command == "run":
        if arguments.cache_dir is None:
            parser.error("run requires --cache-dir")
        result = run_preflight(
            plan_path=arguments.plan,
            ledger_path=arguments.ledger,
            cache_dir=arguments.cache_dir,
            result_path=arguments.result,
            project_root=Path.cwd(),
        )
    else:
        result = verify_preflight(
            plan_path=arguments.plan,
            ledger_path=arguments.ledger,
            result_path=arguments.result,
        )
    _print_json(result)


if __name__ == "__main__":  # pragma: no cover
    main()
