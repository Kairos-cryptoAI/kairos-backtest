"""Append-only, read-only forward evidence for the frozen trial-15 candidate.

The observer accepts strict final Binance bars, regenerates the exact shared
strategy only on its registered decision clock and writes an auditable SQLite
hash chain.  It never calls an exchange, an LLM, or the Kairos order route, and
it deliberately withholds performance during the blind observation period.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import sys
import time
from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

from kairos_core.contracts import ClosedBarEventV1, StrategyIntentV1, canonical_json_bytes
from kairos_strategy.provenance import installed_source_tree_sha256
from kairos_strategy.registry import StrategyStatus, get_strategy
from kairos_strategy.runtime import candle_to_closed_bar, generate_runtime_strategy_intents
from kairos_strategy.runtime_requirements import get_runtime_requirements
from kairos_strategy.sleeves import RegimeAlignedRightTailConfig, RightTailTrendConfig

from .data import ArchiveFieldProfile, BinanceArchiveLoader, DatasetManifest
from .managed_evaluation import ManagedEvaluationPolicy
from .quarter_hour_screen import _execution_scenarios
from .right_tail_screen import (
    MAXIMUM_DRAWDOWN,
    MAXIMUM_ONE_SYMBOL_TRADE_SHARE,
    MINIMUM_ACTIVE_SYMBOLS,
    MINIMUM_DIRECTION_TRADES,
    MINIMUM_POSITIVE_EXPECTANCY_SYMBOLS,
    MINIMUM_PROFIT_FACTOR,
    MINIMUM_STRESS_TRADE_RETENTION,
    MINIMUM_TRADES_PER_SYMBOL,
    _costs,
)

PLAN_SCHEMA_VERSION = "kairos.regime-aligned-forward-plan.v2"
LEDGER_SCHEMA_VERSION = "kairos.regime-aligned-forward-ledger.v1"
PLAN_FILENAME = "reports/regime-aligned-forward/plan.json"
STRATEGY_ID = "regime_aligned_right_tail_v1"
BASE_STRATEGY_ID = "right_tail_trend_v1"
STRATEGY_ENGINE_COMMIT = "fb7d406c6e1a3060f481b91668ff3bc23a1b4b0d"
STRATEGY_SOURCE_TREE_SHA256 = "f795c8f01d58666c5f215e5c98eabf7368e25d1ac83ff3bc7ae27332e0966b84"
STRATEGY_CONFIG_SHA256 = "ae4ff7d7c54353a544be045903b64d5c0be9d6ca8d22ec6158e4942a36a59efe"
TRIAL15_PLAN_SHA256 = "aae0730019cb5f78099b0b3e89afbe21fe1d4bb9ef8f247c74e53f349fc31730"
TRIAL15_RESULT_SHA256 = "bc31c3134b296a80a234ed2d87a3851a5e6f409666f87ffe4fb8646a5367fd53"
SUPERSEDED_PLAN_SHA256 = "15cc52c1356cce349c623dd4753c1ca6b91de386041b132b016949add43f2528"
SYMBOLS = ("BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT")
OBSERVATION_WINDOW_BARS = 40 * 24 * 60
MINIMUM_FORWARD_DAYS = 365
MINIMUM_FORWARD_TRADES = 500
MINIMUM_BASE_TRADE_RETENTION = 0.50
WARMUP_START_MS = int(datetime(2026, 7, 23, tzinfo=UTC).timestamp() * 1_000)
BLIND_START_MS = int(datetime(2026, 9, 1, tzinfo=UTC).timestamp() * 1_000)
MINIMUM_END_MS = int(datetime(2027, 9, 1, tzinfo=UTC).timestamp() * 1_000)
_ONE_MINUTE_MS = 60_000
_ZERO_SHA256 = "0" * 64


class ForwardIntegrityError(RuntimeError):
    """A permanent gap, conflict, reorder or ledger-integrity failure."""


class IngestDisposition(StrEnum):
    INSERTED = "inserted"
    DUPLICATE = "duplicate"


@dataclass(frozen=True, slots=True)
class IngestSummary:
    inserted_bars: int = 0
    duplicate_bars: int = 0
    emitted_intents: int = 0


@dataclass(frozen=True, slots=True)
class ArchiveIngestSummary:
    start: str
    end_exclusive: str
    inserted_bars: int
    duplicate_bars: int
    emitted_intents: int
    manifests: tuple[dict[str, object], ...]


@dataclass(frozen=True, slots=True)
class BackupSummary:
    backup_path: str
    backup_sha256: str
    campaign_id: str
    evidence_sha256: str


@dataclass(frozen=True, slots=True)
class RecoveryDrillSummary:
    backup_path: str
    backup_sha256: str
    campaign_id: str
    evidence_sha256: str
    primary_unchanged: bool
    recovered_path: str
    recovered_sha256: str


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _create_empty_exclusive(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    os.close(descriptor)


def _remove_new_sqlite_files(path: Path) -> None:
    for candidate in (path, Path(f"{path}-wal"), Path(f"{path}-shm"), Path(f"{path}-journal")):
        candidate.unlink(missing_ok=True)


def _canonical_document(value: Mapping[str, Any]) -> bytes:
    return canonical_json_bytes(dict(value))


def _iso8601(timestamp_ms: int) -> str:
    return datetime.fromtimestamp(timestamp_ms / 1_000, UTC).isoformat().replace("+00:00", "Z")


def _date_ms(value: date) -> int:
    return int(datetime.combine(value, datetime.min.time(), UTC).timestamp() * 1_000)


def expected_plan() -> dict[str, object]:
    """Return the immutable, executable preregistration document."""

    config = RegimeAlignedRightTailConfig()
    base_config = RightTailTrendConfig()
    scenarios = _execution_scenarios()
    return {
        "candidate": {
            "base_config": asdict(base_config),
            "base_config_sha256": base_config.fingerprint,
            "base_strategy_id": BASE_STRATEGY_ID,
            "config": asdict(config),
            "config_sha256": config.fingerprint,
            "revision": "1",
            "runtime_window_bars": OBSERVATION_WINDOW_BARS,
            "source_tree_sha256": STRATEGY_SOURCE_TREE_SHA256,
            "strategy_engine_commit": STRATEGY_ENGINE_COMMIT,
            "strategy_id": STRATEGY_ID,
            "status": StrategyStatus.FORWARD_FROZEN.value,
        },
        "data": {
            "blind_start_inclusive": _iso8601(BLIND_START_MS),
            "closed_bar_contract": "closed-bar.v1",
            "discarded_fields": ["taker_buy_base_volume", "taker_buy_quote_volume"],
            "field_profile": "price_volume",
            "minimum_end_exclusive": _iso8601(MINIMUM_END_MS),
            "source": "BINANCE_UM",
            "timeframe": "1m",
            "universe": list(SYMBOLS),
            "warmup_is_feature_only": True,
            "warmup_start_inclusive": _iso8601(WARMUP_START_MS),
        },
        "decision_rule": {
            "base_comparison_required": True,
            "candidate_stress_drawdown_must_not_exceed_base": True,
            "candidate_stress_profit_factor_must_strictly_exceed_base": True,
            "maximum_drawdown": MAXIMUM_DRAWDOWN,
            "maximum_one_symbol_trade_share": MAXIMUM_ONE_SYMBOL_TRADE_SHARE,
            "minimum_active_symbols": MINIMUM_ACTIVE_SYMBOLS,
            "minimum_base_trade_retention": MINIMUM_BASE_TRADE_RETENTION,
            "minimum_direction_trades": MINIMUM_DIRECTION_TRADES,
            "minimum_expectancy_usd_per_trade": 0.0,
            "minimum_forward_days": MINIMUM_FORWARD_DAYS,
            "minimum_forward_trades": MINIMUM_FORWARD_TRADES,
            "minimum_hac_sharpe": 0.0,
            "minimum_positive_expectancy_symbols": MINIMUM_POSITIVE_EXPECTANCY_SYMBOLS,
            "minimum_profit_factor_strictly_greater_than": MINIMUM_PROFIT_FACTOR,
            "minimum_stress_trade_retention": MINIMUM_STRESS_TRADE_RETENTION,
            "minimum_total_return": 0.0,
            "minimum_trades_per_symbol": MINIMUM_TRADES_PER_SYMBOL,
            "parameters_or_universe_may_change": False,
            "pass_outcome": "ALPHA_CANDIDATE_REQUIRES_SEPARATE_PAPER_APPROVAL",  # nosec B105
        },
        "lineage": {
            "classification": "independent_forward_after_post_hoc_reused_data_screen",
            "supersedes_prestart_plan_sha256": SUPERSEDED_PLAN_SHA256,
            "trial15_plan_sha256": TRIAL15_PLAN_SHA256,
            "trial15_result_sha256": TRIAL15_RESULT_SHA256,
        },
        "permissions": {
            "alpha_ready": False,
            "live_allowed": False,
            "paper_allowed": False,
            "promotion_eligible": False,
        },
        "protocol": {
            "bar_ledger": "append_only_per_symbol_sha256_chain",
            "early_performance_access": False,
            "evedex_calls": False,
            "exchange_mutations": False,
            "final_evaluation": "one_shot_after_both_duration_and_trade_count_gates",
            "llm_calls": False,
            "normalization": "deterministic_price_volume_contract_before_hashing",
            "restart_policy": "verify_full_chain_then_resume",
        },
        "scenarios": {
            name: {
                "costs": asdict(_costs(execution)),
                "execution": asdict(execution),
                "policy": asdict(
                    ManagedEvaluationPolicy(
                        application_exit_latency_ms=execution.latency_ms,
                        terminal_liquidation_grace_ms=60 * 60 * 1_000,
                    )
                ),
            }
            for name, execution in scenarios.items()
        },
        "schema_version": PLAN_SCHEMA_VERSION,
    }


def plan_sha256(plan: Mapping[str, Any]) -> str:
    return _sha256_bytes(_canonical_document(plan))


def write_plan(path: Path) -> str:
    payload = expected_plan()
    encoded = _canonical_document(payload) + b"\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(encoded)
    return plan_sha256(payload)


def load_plan(path: Path) -> dict[str, object]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict) or _canonical_document(raw) != _canonical_document(expected_plan()):
        raise ValueError("committed forward plan differs from the executable preregistration")
    return raw


def validate_frozen_runtime() -> None:
    definition = get_strategy(STRATEGY_ID)
    if definition.status is not StrategyStatus.FORWARD_FROZEN:
        raise RuntimeError("forward observer requires an exact FORWARD_FROZEN strategy")
    if RegimeAlignedRightTailConfig().fingerprint != STRATEGY_CONFIG_SHA256:
        raise RuntimeError("forward strategy configuration no longer matches its plan")
    source_sha256 = installed_source_tree_sha256(definition.source_files)
    if source_sha256 != STRATEGY_SOURCE_TREE_SHA256:
        raise RuntimeError("forward strategy source tree no longer matches its plan")
    requirements = get_runtime_requirements(STRATEGY_ID)
    if OBSERVATION_WINDOW_BARS < requirements.minimum_window_bars:
        raise RuntimeError("forward window is smaller than the strategy runtime requirement")


def _record_sha256(previous_sha256: str, payload: bytes) -> str:
    return _sha256_bytes(b"kairos-forward-bar-v1\0" + previous_sha256.encode("ascii") + b"\0" + payload)


def _price_volume_bar(bar: ClosedBarEventV1) -> ClosedBarEventV1:
    """Discard envelope/taker fields outside the preregistered field profile."""

    return ClosedBarEventV1(
        source="forward-observer.price-volume",
        symbol=bar.symbol,
        open_time_ms=bar.open_time_ms,
        close_time_ms=bar.close_time_ms,
        open=bar.open,
        high=bar.high,
        low=bar.low,
        close=bar.close,
        base_volume=bar.base_volume,
        quote_volume=bar.quote_volume,
        taker_buy_base_volume=0.0,
        taker_buy_quote_volume=0.0,
    )


def _is_decision_bar(bar: ClosedBarEventV1) -> bool:
    requirements = get_runtime_requirements(STRATEGY_ID)
    closed_minute = (bar.close_time_ms + 1) // _ONE_MINUTE_MS
    return closed_minute % requirements.decision_interval_bars == requirements.decision_phase_bars


class ForwardLedger:
    """Durable single-writer ledger for one preregistered forward campaign."""

    def __init__(self, path: Path, plan: Mapping[str, Any]) -> None:
        validate_frozen_runtime()
        self.path = path
        self.plan = dict(plan)
        self.plan_sha256 = plan_sha256(plan)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(path, timeout=30, isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute("PRAGMA synchronous=FULL")
        self.connection.execute("PRAGMA foreign_keys=ON")
        self.connection.execute("PRAGMA busy_timeout=30000")
        self._initialize()

    def __enter__(self) -> ForwardLedger:
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.close()

    def close(self) -> None:
        self.connection.close()

    def _initialize(self) -> None:
        self.connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS campaign_metadata (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS symbol_state (
                symbol TEXT PRIMARY KEY,
                last_open_time_ms INTEGER,
                last_record_sha256 TEXT,
                bar_count INTEGER NOT NULL DEFAULT 0,
                intent_count INTEGER NOT NULL DEFAULT 0,
                blocked_reason TEXT
            );
            CREATE TABLE IF NOT EXISTS bars (
                symbol TEXT NOT NULL,
                open_time_ms INTEGER NOT NULL,
                close_time_ms INTEGER NOT NULL,
                role TEXT NOT NULL CHECK (role IN ('warmup', 'blind')),
                bar_sha256 TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                previous_record_sha256 TEXT NOT NULL,
                record_sha256 TEXT NOT NULL UNIQUE,
                PRIMARY KEY (symbol, open_time_ms),
                FOREIGN KEY (symbol) REFERENCES symbol_state(symbol)
            );
            CREATE TABLE IF NOT EXISTS intents (
                intent_id TEXT PRIMARY KEY,
                symbol TEXT NOT NULL,
                decision_ts_ms INTEGER NOT NULL,
                decision_bar_sha256 TEXT NOT NULL,
                payload_sha256 TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                FOREIGN KEY (symbol) REFERENCES symbol_state(symbol)
            );
            CREATE INDEX IF NOT EXISTS bars_symbol_time ON bars(symbol, open_time_ms);
            CREATE INDEX IF NOT EXISTS intents_symbol_time ON intents(symbol, decision_ts_ms);
            """
        )
        metadata = dict(self.connection.execute("SELECT key, value FROM campaign_metadata"))
        expected = {
            "ledger_schema_version": LEDGER_SCHEMA_VERSION,
            "plan_json": _canonical_document(self.plan).decode("ascii"),
            "plan_sha256": self.plan_sha256,
        }
        if metadata:
            if metadata != expected:
                raise ForwardIntegrityError("ledger metadata does not match the preregistered campaign")
        else:
            self.connection.execute("BEGIN IMMEDIATE")
            try:
                self.connection.executemany(
                    "INSERT INTO campaign_metadata(key, value) VALUES (?, ?)", expected.items()
                )
                self.connection.executemany(
                    "INSERT INTO symbol_state(symbol) VALUES (?)", ((symbol,) for symbol in SYMBOLS)
                )
                self.connection.execute("COMMIT")
            except Exception:
                self.connection.execute("ROLLBACK")
                raise

    def _block(self, symbol: str, reason: str) -> None:
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            self.connection.execute(
                "UPDATE symbol_state SET blocked_reason=COALESCE(blocked_reason, ?) WHERE symbol=?",
                (reason, symbol),
            )
            self.connection.execute("COMMIT")
        except Exception:
            self.connection.execute("ROLLBACK")
            raise

    def _history(self, symbol: str) -> tuple[ClosedBarEventV1, ...]:
        rows = self.connection.execute(
            """SELECT payload_json FROM bars
                 WHERE symbol=?
                 ORDER BY open_time_ms DESC
                 LIMIT ?""",
            (symbol, OBSERVATION_WINDOW_BARS),
        ).fetchall()
        return tuple(ClosedBarEventV1.model_validate_json(row["payload_json"]) for row in reversed(rows))

    def _persist_intents(self, bar: ClosedBarEventV1) -> int:
        if bar.open_time_ms < BLIND_START_MS or not _is_decision_bar(bar):
            return 0
        history = self._history(bar.symbol)
        if len(history) < OBSERVATION_WINDOW_BARS:
            return 0
        candidates = generate_runtime_strategy_intents(STRATEGY_ID, history)
        current = tuple(intent for intent in candidates if intent.decision_ts_ms == bar.close_time_ms)
        if len(current) > 1:
            raise ForwardIntegrityError(
                "frozen strategy emitted multiple intents for one symbol and decision"
            )
        emitted = 0
        for intent in current:
            if intent.intent_id is None:
                raise ForwardIntegrityError("strategy emitted an intent without a canonical identity")
            payload = canonical_json_bytes(intent.model_dump(mode="json"))
            payload_sha = _sha256_bytes(payload)
            existing = self.connection.execute(
                "SELECT payload_sha256 FROM intents WHERE intent_id=?", (intent.intent_id,)
            ).fetchone()
            if existing is not None:
                if existing["payload_sha256"] != payload_sha:
                    raise ForwardIntegrityError("intent identity collision in forward ledger")
                continue
            self.connection.execute(
                """INSERT INTO intents(
                       intent_id, symbol, decision_ts_ms, decision_bar_sha256,
                       payload_sha256, payload_json
                   ) VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    intent.intent_id,
                    intent.symbol,
                    intent.decision_ts_ms,
                    bar.bar_sha256,
                    payload_sha,
                    payload.decode("ascii"),
                ),
            )
            emitted += 1
        if emitted:
            self.connection.execute(
                "UPDATE symbol_state SET intent_count=intent_count+? WHERE symbol=?",
                (emitted, bar.symbol),
            )
        return emitted

    @staticmethod
    def _validated_bar(bar: ClosedBarEventV1, as_of_ms: int | None) -> ClosedBarEventV1:
        if not isinstance(bar, ClosedBarEventV1):
            raise TypeError("forward ledger accepts only ClosedBarEventV1")
        bar = _price_volume_bar(bar)
        if bar.symbol not in SYMBOLS:
            raise ValueError(f"bar symbol is outside the frozen universe: {bar.symbol}")
        if bar.open_time_ms < WARMUP_START_MS:
            raise ValueError("bar predates the preregistered feature-only warmup")
        observed_ms = int(time.time() * 1_000) if as_of_ms is None else as_of_ms
        if isinstance(observed_ms, bool) or not isinstance(observed_ms, int) or observed_ms < 0:
            raise ValueError("as_of_ms must be a non-negative integer")
        if bar.close_time_ms > observed_ms:
            raise ValueError("forward ledger refuses a bar that is not closed as of ingestion")
        return bar

    def _ingest_in_transaction(self, bar: ClosedBarEventV1) -> tuple[IngestDisposition, int]:
        canonical_payload = canonical_json_bytes(bar.model_dump(mode="json"))
        payload_json = canonical_payload.decode("ascii")
        state = self.connection.execute("SELECT * FROM symbol_state WHERE symbol=?", (bar.symbol,)).fetchone()
        if state is None:
            raise ForwardIntegrityError("forward symbol state is missing")
        if state["blocked_reason"] is not None:
            raise ForwardIntegrityError(
                f"{bar.symbol} is blocked after an integrity violation: {state['blocked_reason']}"
            )
        existing = self.connection.execute(
            "SELECT bar_sha256, payload_json FROM bars WHERE symbol=? AND open_time_ms=?",
            (bar.symbol, bar.open_time_ms),
        ).fetchone()
        if existing is not None:
            if existing["bar_sha256"] == bar.bar_sha256 and existing["payload_json"] == payload_json:
                return IngestDisposition.DUPLICATE, 0
            raise ForwardIntegrityError(f"conflicting closed bar at {bar.open_time_ms}")

        last_open = state["last_open_time_ms"]
        if last_open is None:
            if bar.open_time_ms != WARMUP_START_MS:
                raise ForwardIntegrityError(f"first bar must start at the warmup boundary {WARMUP_START_MS}")
            previous_sha = _ZERO_SHA256
        else:
            expected_open = int(last_open) + _ONE_MINUTE_MS
            if bar.open_time_ms != expected_open:
                raise ForwardIntegrityError(
                    f"closed-bar gap or reorder: expected {expected_open}, received {bar.open_time_ms}"
                )
            previous_sha = str(state["last_record_sha256"])
        record_sha = _record_sha256(previous_sha, canonical_payload)
        role = "warmup" if bar.open_time_ms < BLIND_START_MS else "blind"
        self.connection.execute(
            """INSERT INTO bars(
                   symbol, open_time_ms, close_time_ms, role, bar_sha256,
                   payload_json, previous_record_sha256, record_sha256
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                bar.symbol,
                bar.open_time_ms,
                bar.close_time_ms,
                role,
                bar.bar_sha256,
                payload_json,
                previous_sha,
                record_sha,
            ),
        )
        self.connection.execute(
            """UPDATE symbol_state
                  SET last_open_time_ms=?, last_record_sha256=?, bar_count=bar_count+1
                WHERE symbol=?""",
            (bar.open_time_ms, record_sha, bar.symbol),
        )
        emitted = self._persist_intents(bar)
        return IngestDisposition.INSERTED, emitted

    def ingest_bar(
        self,
        bar: ClosedBarEventV1,
        *,
        as_of_ms: int | None = None,
    ) -> tuple[IngestDisposition, int]:
        """Append one final bar or accept an exact idempotent replay."""

        bar = self._validated_bar(bar, as_of_ms)
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            result = self._ingest_in_transaction(bar)
            self.connection.execute("COMMIT")
            return result
        except Exception as exc:
            self.connection.execute("ROLLBACK")
            if isinstance(exc, ForwardIntegrityError):
                self._block(bar.symbol, str(exc))
            raise

    def ingest(
        self,
        bars: Iterable[ClosedBarEventV1],
        *,
        as_of_ms: int | None = None,
    ) -> IngestSummary:
        inserted = duplicates = intents = 0
        for bar in bars:
            disposition, emitted = self.ingest_bar(bar, as_of_ms=as_of_ms)
            inserted += disposition is IngestDisposition.INSERTED
            duplicates += disposition is IngestDisposition.DUPLICATE
            intents += emitted
        return IngestSummary(inserted, duplicates, intents)

    def ingest_atomic(
        self,
        bars: Iterable[ClosedBarEventV1],
        *,
        as_of_ms: int | None = None,
    ) -> IngestSummary:
        """Commit a verified archive block with one durable transaction."""

        inserted = duplicates = intents = 0
        current_symbol: str | None = None
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            for raw_bar in bars:
                bar = self._validated_bar(raw_bar, as_of_ms)
                current_symbol = bar.symbol
                disposition, emitted = self._ingest_in_transaction(bar)
                inserted += disposition is IngestDisposition.INSERTED
                duplicates += disposition is IngestDisposition.DUPLICATE
                intents += emitted
            self.connection.execute("COMMIT")
            return IngestSummary(inserted, duplicates, intents)
        except Exception as exc:
            self.connection.execute("ROLLBACK")
            if isinstance(exc, ForwardIntegrityError) and current_symbol is not None:
                self._block(current_symbol, str(exc))
            raise

    def verify_integrity(self) -> None:
        """Verify all bar chains, contracts, counters and stored intent bytes."""

        for symbol in SYMBOLS:
            state = self.connection.execute("SELECT * FROM symbol_state WHERE symbol=?", (symbol,)).fetchone()
            if state is None:
                raise ForwardIntegrityError(f"missing symbol state for {symbol}")
            previous_sha = _ZERO_SHA256
            previous_open: int | None = None
            count = 0
            for row in self.connection.execute(
                "SELECT * FROM bars WHERE symbol=? ORDER BY open_time_ms", (symbol,)
            ):
                payload = row["payload_json"].encode("ascii")
                bar = ClosedBarEventV1.model_validate_json(payload)
                if bar.symbol != symbol or bar.open_time_ms != row["open_time_ms"]:
                    raise ForwardIntegrityError(f"stored bar columns disagree with payload for {symbol}")
                if previous_open is None:
                    if bar.open_time_ms != WARMUP_START_MS:
                        raise ForwardIntegrityError(f"{symbol} chain does not begin at warmup")
                elif bar.open_time_ms != previous_open + _ONE_MINUTE_MS:
                    raise ForwardIntegrityError(f"{symbol} chain contains a gap or reorder")
                expected_record = _record_sha256(previous_sha, payload)
                if (
                    row["bar_sha256"] != bar.bar_sha256
                    or row["previous_record_sha256"] != previous_sha
                    or row["record_sha256"] != expected_record
                ):
                    raise ForwardIntegrityError(f"{symbol} bar hash chain is invalid")
                previous_sha = expected_record
                previous_open = bar.open_time_ms
                count += 1
            if count != state["bar_count"]:
                raise ForwardIntegrityError(f"{symbol} bar counter is invalid")
            if count and (
                state["last_open_time_ms"] != previous_open or state["last_record_sha256"] != previous_sha
            ):
                raise ForwardIntegrityError(f"{symbol} terminal chain state is invalid")

            intent_count = 0
            for row in self.connection.execute(
                "SELECT * FROM intents WHERE symbol=? ORDER BY decision_ts_ms", (symbol,)
            ):
                payload = row["payload_json"].encode("ascii")
                intent = StrategyIntentV1.model_validate_json(payload)
                decision_open_ms = intent.decision_ts_ms - (_ONE_MINUTE_MS - 1)
                decision_bar = self.connection.execute(
                    "SELECT bar_sha256 FROM bars WHERE symbol=? AND open_time_ms=?",
                    (symbol, decision_open_ms),
                ).fetchone()
                input_hashes = intent.provenance.input_bar_sha256s
                first_input_open_ms = decision_open_ms - (len(input_hashes) - 1) * _ONE_MINUTE_MS
                first_input_bar = self.connection.execute(
                    "SELECT bar_sha256 FROM bars WHERE symbol=? AND open_time_ms=?",
                    (symbol, first_input_open_ms),
                ).fetchone()
                if (
                    intent.intent_id != row["intent_id"]
                    or intent.symbol != symbol
                    or intent.decision_ts_ms != row["decision_ts_ms"]
                    or _sha256_bytes(payload) != row["payload_sha256"]
                    or intent.decision_ts_ms < BLIND_START_MS
                    or decision_bar is None
                    or row["decision_bar_sha256"] != decision_bar["bar_sha256"]
                    or len(input_hashes) != OBSERVATION_WINDOW_BARS
                    or input_hashes[-1] != decision_bar["bar_sha256"]
                    or first_input_bar is None
                    or input_hashes[0] != first_input_bar["bar_sha256"]
                    or intent.provenance.strategy_code_sha256 != STRATEGY_SOURCE_TREE_SHA256
                    or intent.provenance.config_sha256 != STRATEGY_CONFIG_SHA256
                ):
                    raise ForwardIntegrityError(f"{symbol} stored intent is invalid")
                intent_count += 1
            if intent_count != state["intent_count"]:
                raise ForwardIntegrityError(f"{symbol} intent counter is invalid")

    def iter_bars(
        self,
        symbol: str,
        *,
        end_exclusive_ms: int | None = None,
    ) -> Iterable[ClosedBarEventV1]:
        """Yield verified stored bars in canonical order without exposing performance."""

        if symbol not in SYMBOLS:
            raise ValueError(f"symbol is outside the frozen universe: {symbol}")
        query = "SELECT payload_json FROM bars WHERE symbol=?"
        parameters: tuple[object, ...] = (symbol,)
        if end_exclusive_ms is not None:
            if (
                isinstance(end_exclusive_ms, bool)
                or not isinstance(end_exclusive_ms, int)
                or end_exclusive_ms < 0
                or end_exclusive_ms % _ONE_MINUTE_MS
            ):
                raise ValueError("end_exclusive_ms must be a non-negative minute boundary")
            query += " AND open_time_ms<?"
            parameters += (end_exclusive_ms,)
        query += " ORDER BY open_time_ms"
        for row in self.connection.execute(query, parameters):
            yield ClosedBarEventV1.model_validate_json(row["payload_json"])

    def intents_before(self, symbol: str, end_exclusive_ms: int) -> tuple[StrategyIntentV1, ...]:
        """Return the stored candidate inventory below one sealed watermark."""

        if symbol not in SYMBOLS:
            raise ValueError(f"symbol is outside the frozen universe: {symbol}")
        if (
            isinstance(end_exclusive_ms, bool)
            or not isinstance(end_exclusive_ms, int)
            or end_exclusive_ms < 0
            or end_exclusive_ms % _ONE_MINUTE_MS
        ):
            raise ValueError("end_exclusive_ms must be a non-negative minute boundary")
        rows = self.connection.execute(
            """SELECT payload_json FROM intents
                 WHERE symbol=? AND decision_ts_ms<?
                 ORDER BY decision_ts_ms, intent_id""",
            (symbol, end_exclusive_ms),
        ).fetchall()
        return tuple(StrategyIntentV1.model_validate_json(row["payload_json"]) for row in rows)

    def sealed_dataset_sha256(self, watermark_ms: int) -> str:
        """Bind the common bar prefix and candidate inventory used by final evaluation."""

        if (
            isinstance(watermark_ms, bool)
            or not isinstance(watermark_ms, int)
            or watermark_ms <= BLIND_START_MS
            or watermark_ms % _ONE_MINUTE_MS
        ):
            raise ValueError("watermark_ms must be a post-blind minute boundary")
        self.verify_integrity()
        digest = hashlib.sha256(b"kairos-forward-sealed-dataset-v1\0")
        digest.update(self.plan_sha256.encode("ascii"))
        digest.update(str(watermark_ms).encode("ascii"))
        terminal_open_ms = watermark_ms - _ONE_MINUTE_MS
        for symbol in SYMBOLS:
            terminal = self.connection.execute(
                """SELECT record_sha256 FROM bars
                     WHERE symbol=? AND open_time_ms=?""",
                (symbol, terminal_open_ms),
            ).fetchone()
            if terminal is None:
                raise ForwardIntegrityError(f"{symbol} does not reach the sealed watermark")
            bar_count = self.connection.execute(
                "SELECT COUNT(*) FROM bars WHERE symbol=? AND open_time_ms<?",
                (symbol, watermark_ms),
            ).fetchone()[0]
            intent_rows = self.connection.execute(
                """SELECT intent_id, payload_sha256 FROM intents
                     WHERE symbol=? AND decision_ts_ms<?
                     ORDER BY decision_ts_ms, intent_id""",
                (symbol, watermark_ms),
            ).fetchall()
            digest.update(
                _canonical_document(
                    {
                        "bar_count": bar_count,
                        "intent_count": len(intent_rows),
                        "symbol": symbol,
                        "terminal_record_sha256": terminal["record_sha256"],
                    }
                )
            )
            for row in intent_rows:
                digest.update(_canonical_document(dict(row)))
        return digest.hexdigest()

    def evidence_sha256(self) -> str:
        """Fingerprint verified campaign state without disclosing performance."""

        self.verify_integrity()
        digest = hashlib.sha256(b"kairos-forward-evidence-v1\0")
        digest.update(self.plan_sha256.encode("ascii"))
        for row in self.connection.execute(
            """SELECT symbol, last_open_time_ms, last_record_sha256,
                      bar_count, intent_count, blocked_reason
                 FROM symbol_state ORDER BY symbol"""
        ):
            digest.update(_canonical_document(dict(row)))
        for row in self.connection.execute(
            """SELECT intent_id, symbol, decision_ts_ms, decision_bar_sha256,
                      payload_sha256
                 FROM intents ORDER BY symbol, decision_ts_ms, intent_id"""
        ):
            digest.update(_canonical_document(dict(row)))
        return digest.hexdigest()

    @staticmethod
    def _require_distinct_paths(*paths: Path) -> None:
        resolved = [path.resolve() for path in paths]
        if len(set(resolved)) != len(resolved):
            raise ValueError("ledger, backup and recovery paths must be distinct")

    def _copy_to_new_database(self, destination: Path) -> None:
        self._require_distinct_paths(self.path, destination)
        _create_empty_exclusive(destination)
        try:
            target = sqlite3.connect(destination, timeout=30, isolation_level=None)
            try:
                self.connection.backup(target)
            finally:
                target.close()
        except Exception:
            _remove_new_sqlite_files(destination)
            raise

    def backup_to(self, destination: Path) -> BackupSummary:
        """Create and verify an exclusive online backup of the current ledger."""

        source_evidence = self.evidence_sha256()
        self._copy_to_new_database(destination)
        try:
            with ForwardLedger(destination, self.plan) as backup:
                backup_evidence = backup.evidence_sha256()
            if backup_evidence != source_evidence:
                raise ForwardIntegrityError("backup evidence differs from the primary ledger")
            return BackupSummary(
                backup_path=str(destination.resolve()),
                backup_sha256=_file_sha256(destination),
                campaign_id=self.plan_sha256,
                evidence_sha256=backup_evidence,
            )
        except Exception:
            _remove_new_sqlite_files(destination)
            raise

    def recovery_drill(self, backup_path: Path, recovered_path: Path) -> RecoveryDrillSummary:
        """Restore an existing backup to a new path and compare all sealed evidence."""

        self._require_distinct_paths(self.path, backup_path, recovered_path)
        if not backup_path.is_file():
            raise FileNotFoundError(f"backup does not exist or is not a file: {backup_path}")
        primary_before = self.evidence_sha256()
        with ForwardLedger(backup_path, self.plan) as backup:
            backup_evidence = backup.evidence_sha256()
            if backup_evidence != primary_before:
                raise ForwardIntegrityError("backup evidence differs from the primary ledger")
            backup._copy_to_new_database(recovered_path)
        try:
            with ForwardLedger(recovered_path, self.plan) as recovered:
                recovered_evidence = recovered.evidence_sha256()
            if recovered_evidence != primary_before:
                raise ForwardIntegrityError("recovered evidence differs from the primary ledger")
            primary_after = self.evidence_sha256()
            if primary_after != primary_before:
                raise ForwardIntegrityError("primary ledger changed during the recovery drill")
            return RecoveryDrillSummary(
                backup_path=str(backup_path.resolve()),
                backup_sha256=_file_sha256(backup_path),
                campaign_id=self.plan_sha256,
                evidence_sha256=recovered_evidence,
                primary_unchanged=True,
                recovered_path=str(recovered_path.resolve()),
                recovered_sha256=_file_sha256(recovered_path),
            )
        except Exception:
            _remove_new_sqlite_files(recovered_path)
            raise

    def status(self) -> dict[str, object]:
        states = self.connection.execute("SELECT * FROM symbol_state ORDER BY symbol").fetchall()
        last_closes = [
            int(state["last_open_time_ms"]) + _ONE_MINUTE_MS
            for state in states
            if state["last_open_time_ms"] is not None and state["blocked_reason"] is None
        ]
        complete_universe = len(last_closes) == len(SYMBOLS)
        watermark_ms = min(last_closes) if complete_universe else None
        observed_ms = 0 if watermark_ms is None else max(0, watermark_ms - BLIND_START_MS)
        complete_days = observed_ms // (24 * 60 * 60 * 1_000)
        return {
            "blind_performance_disclosed": False,
            "campaign_id": self.plan_sha256,
            "complete_blind_days": complete_days,
            "duration_gate_satisfied": watermark_ms is not None and watermark_ms >= MINIMUM_END_MS,
            "minimum_forward_days": MINIMUM_FORWARD_DAYS,
            "minimum_forward_trades": MINIMUM_FORWARD_TRADES,
            "permissions": {
                "alpha_ready": False,
                "live_allowed": False,
                "paper_allowed": False,
                "promotion_eligible": False,
            },
            "sealed_trade_count_evaluated": False,
            "symbols": [
                {
                    "bar_count": state["bar_count"],
                    "blocked_reason": state["blocked_reason"],
                    "intent_count": state["intent_count"],
                    "last_open_time_ms": state["last_open_time_ms"],
                    "symbol": state["symbol"],
                }
                for state in states
            ],
            "watermark_ms": watermark_ms,
        }


def _validate_archive_manifest(manifest: DatasetManifest, start: date, end: date) -> None:
    start_ms = _date_ms(start)
    end_ms = _date_ms(end)
    expected_rows = (end_ms - start_ms) // _ONE_MINUTE_MS
    if (
        manifest.requested_start != start.isoformat()
        or manifest.requested_end != end.isoformat()
        or manifest.actual_start_ms != start_ms
        or manifest.actual_end_ms != end_ms - 1
        or manifest.rows != expected_rows
        or manifest.gaps != 0
        or manifest.field_profile != ArchiveFieldProfile.PRICE_VOLUME.value
        or manifest.checksum_status != "official_sha256_verified"
        or manifest.checksum_files_verified != manifest.expected_files
        or manifest.quarantined_optional_rows != 0
    ):
        raise ForwardIntegrityError(f"archive manifest failed forward gates for {manifest.symbol}")


def ingest_monthly_archives(
    ledger: ForwardLedger,
    cache_dir: Path,
    start: date,
    end: date,
) -> ArchiveIngestSummary:
    """Ingest checksum-verified complete monthly archives already in local cache."""

    if start >= end:
        raise ValueError("archive start must precede its exclusive end")
    if _date_ms(start) < WARMUP_START_MS:
        raise ValueError("archive import cannot precede the registered warmup")
    end_ms = _date_ms(end)
    if end_ms > int(time.time() * 1_000):
        raise ValueError("archive import requires a completed UTC date range")
    loader = BinanceArchiveLoader(
        cache_dir,
        allow_download=False,
        field_profile=ArchiveFieldProfile.PRICE_VOLUME,
    )
    inserted = duplicates = intents = 0
    evidence: list[dict[str, object]] = []
    for symbol in SYMBOLS:
        candles, manifest = loader.load(symbol, start, end)
        _validate_archive_manifest(manifest, start, end)
        summary = ledger.ingest_atomic(
            (candle_to_closed_bar(candle) for candle in candles),
            as_of_ms=end_ms - 1,
        )
        inserted += summary.inserted_bars
        duplicates += summary.duplicate_bars
        intents += summary.emitted_intents
        evidence.append(
            {
                "checksum_files_verified": manifest.checksum_files_verified,
                "dataset_sha256": manifest.sha256,
                "files": list(manifest.files),
                "rows": manifest.rows,
                "symbol": manifest.symbol,
            }
        )
    ledger.verify_integrity()
    return ArchiveIngestSummary(
        start=start.isoformat(),
        end_exclusive=end.isoformat(),
        inserted_bars=inserted,
        duplicate_bars=duplicates,
        emitted_intents=intents,
        manifests=tuple(evidence),
    )


def _load_json_lines(stream: Iterable[str]) -> Iterable[ClosedBarEventV1]:
    for line_number, line in enumerate(stream, start=1):
        if not line.strip():
            continue
        payload = json.loads(line)
        if not isinstance(payload, dict):
            raise ValueError(f"JSONL line {line_number} is not an object")
        yield ClosedBarEventV1.model_validate(payload)


def _print_json(value: object) -> None:
    print(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True))


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=Path, default=Path(PLAN_FILENAME))
    subparsers = parser.add_subparsers(dest="command", required=True)
    write = subparsers.add_parser("write-plan")
    write.add_argument("--output", type=Path, default=Path(PLAN_FILENAME))
    for command in ("init", "status", "verify"):
        child = subparsers.add_parser(command)
        child.add_argument("--ledger", type=Path, required=True)
    backup = subparsers.add_parser("backup")
    backup.add_argument("--ledger", type=Path, required=True)
    backup.add_argument("--output", type=Path, required=True)
    recovery = subparsers.add_parser("recovery-drill")
    recovery.add_argument("--ledger", type=Path, required=True)
    recovery.add_argument("--backup", type=Path, required=True)
    recovery.add_argument("--recovered", type=Path, required=True)
    ingest = subparsers.add_parser("ingest")
    ingest.add_argument("--ledger", type=Path, required=True)
    ingest.add_argument("--input", type=Path)
    ingest.add_argument("--as-of-ms", type=int)
    archives = subparsers.add_parser("ingest-monthly-archives")
    archives.add_argument("--ledger", type=Path, required=True)
    archives.add_argument("--cache-dir", type=Path, required=True)
    archives.add_argument("--start", type=date.fromisoformat, required=True)
    archives.add_argument("--end-exclusive", type=date.fromisoformat, required=True)
    return parser


def main(argv: list[str] | None = None) -> None:
    args = _parser().parse_args(argv)
    if args.command == "write-plan":
        _print_json({"plan_sha256": write_plan(args.output), "written": str(args.output)})
        return
    plan = load_plan(args.plan)
    with ForwardLedger(args.ledger, plan) as ledger:
        if args.command == "init":
            _print_json(ledger.status())
        elif args.command == "status":
            _print_json(ledger.status())
        elif args.command == "verify":
            ledger.verify_integrity()
            _print_json(
                {
                    "evidence_sha256": ledger.evidence_sha256(),
                    "integrity": "valid",
                    **ledger.status(),
                }
            )
        elif args.command == "backup":
            _print_json({"backup": asdict(ledger.backup_to(args.output))})
        elif args.command == "recovery-drill":
            _print_json({"recovery_drill": asdict(ledger.recovery_drill(args.backup, args.recovered))})
        elif args.command == "ingest":
            if args.input is None:
                summary = ledger.ingest(_load_json_lines(sys.stdin), as_of_ms=args.as_of_ms)
            else:
                with args.input.open(encoding="utf-8") as stream:
                    summary = ledger.ingest(_load_json_lines(stream), as_of_ms=args.as_of_ms)
            _print_json({"ingest": asdict(summary), "status": ledger.status()})
        elif args.command == "ingest-monthly-archives":
            archive_summary = ingest_monthly_archives(
                ledger,
                args.cache_dir,
                args.start,
                args.end_exclusive,
            )
            _print_json({"archive_ingest": asdict(archive_summary), "status": ledger.status()})
        else:  # pragma: no cover - argparse owns the command domain
            raise RuntimeError(f"unsupported command: {args.command}")


if __name__ == "__main__":  # pragma: no cover
    main()
