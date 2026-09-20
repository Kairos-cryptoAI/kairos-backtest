"""Immutable, performance-blind preflight for event-time microstructure research.

This module intentionally fixes only the data-qualification boundary for the
``liquidation_absorption_v1`` research lineage.  It cannot create a signal,
fit a model, calculate a market outcome, simulate a trade, or contact a
provider.  Those operations require a later, separately preregistered study
after this live public-data tape is qualified.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
from collections.abc import Mapping
from pathlib import Path
from typing import cast

from .scenarios import SYMBOLS

SCHEMA_VERSION = "kairos.liquidation-absorption-data.v1"
CLASSIFICATION = "PERFORMANCE_BLIND_LIVE_MICROSTRUCTURE_DATA_QUALIFICATION"
PLAN_FILENAME = "reports/liquidation-absorption-v1/plan.json"
# This is the SHA-256 of the plan's compact, canonical JSON serialization.
# It is filled in before the plan is committed and prevents silent edits later.
PLAN_LOGICAL_SHA256 = "384a430e905b63ba58da026b6a41ad69fdad1c18aeba6a6a8fdcb27c72733ae0"

BOOK_CONTINUITY = (
    "persist every snapshot/delta sequence and record a barrier on reconnect, gap, sequence "
    "regression, malformed frame, or queue overflow"
)
EVENT_ELIGIBILITY = (
    "a later observation may use an event only when its complete pre/post window has raw trade, "
    "ticker, order-book, and liquidation provenance with no barrier"
)
LIQUIDATION_SEMANTICS = (
    "S=Buy is a liquidated long and therefore sell pressure; S=Sell is a liquidated short and "
    "therefore buy pressure; bankruptcy price is not execution price"
)
RAW_LINEAGE = (
    "persist original UTF-8 WebSocket text, SHA-256, exchange timestamps, local receipt timestamps, "
    "channel sequence, and a global append-only hash chain"
)
RPI_LIMITATION = "displayed order-book depth excludes RPI orders and is an incomplete liquidity proxy"
TIME_BASIS = (
    "all intervals use exchange timestamp and retain local receipt timestamp separately; future-dated "
    "or non-monotonic source state is rejected"
)
ECONOMIC_MECHANISM = (
    "after an observed one-sided liquidation burst, subsequent 30-180 second movement depends on "
    "forced notional relative to opposite-side depth replenishment and realized impact"
)
NOT_A_CLAIM = (
    "the study does not predict a cascade before it is observable and does not assume a displayed book "
    "is executable liquidity"
)
RESEARCH_QUESTION = (
    "does post-shock absorption versus continuation remain measurable after a fixed non-colocated "
    "information delay and later conservative execution costs"
)
DATA_BEFORE_MODEL = (
    "only a qualified immutable tape may be used by a later separately preregistered statistical study"
)
EXECUTION_VENUE_BOUNDARY = "EVEDEX basis, depth, latency, and fills remain a separate later qualification"


class LiquidationAbsorptionPreflightError(RuntimeError):
    """The committed no-performance data contract is inconsistent."""


def _json_value(value: object) -> object:
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
            raise ValueError("plan JSON cannot contain non-finite values")
        return 0.0 if value == 0 else value
    raise TypeError(f"unsupported plan JSON type: {type(value).__name__}")


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
    """Return the exact preregistered public-data contract.

    The source is intentionally a research-only feed.  It is not a claim that
    a Bybit observation is executable on EVEDEX or a permission to trade on
    either venue.
    """

    return {
        "classification": CLASSIFICATION,
        "data": {
            "capture_start_not_before_utc": "2026-09-21T00:00:00Z",
            "collection_duration_days": 90,
            "market": "Bybit linear perpetual public market data",
            "purpose": "research-only event-time tape; not execution data",
            "symbols": list(SYMBOLS),
            "transport": {
                "topics": [
                    "orderbook.50.{symbol}",
                    "publicTrade.{symbol}",
                    "tickers.{symbol}",
                    "allLiquidation.{symbol}",
                ],
                "websocket_url": "wss://stream.bybit.com/v5/public/linear",
            },
        },
        "data_quality": {
            "book_continuity": BOOK_CONTINUITY,
            "event_eligibility": EVENT_ELIGIBILITY,
            "liquidation_semantics": LIQUIDATION_SEMANTICS,
            "raw_lineage": RAW_LINEAGE,
            "rpi_limitation": RPI_LIMITATION,
            "time_basis": TIME_BASIS,
        },
        "hypothesis": {
            "economic_mechanism": ECONOMIC_MECHANISM,
            "not_a_claim": NOT_A_CLAIM,
            "research_question": RESEARCH_QUESTION,
        },
        "permissions": {
            "alpha_ready": False,
            "live_allowed": False,
            "paper_allowed": False,
            "promotion_eligible": False,
        },
        "prohibitions": [
            "account credentials",
            "EVEDEX connection",
            "LLM or paid feed call",
            "model fitting",
            "order submission",
            "PnL or simulated trade",
            "signal generation",
            "strategy intent",
        ],
        "protocol": {
            "data_before_model": DATA_BEFORE_MODEL,
            "execution_venue_boundary": EXECUTION_VENUE_BOUNDARY,
            "historical_result_examined": False,
            "result_may_not_change_this_plan": True,
            "source_documentation": [
                "https://bybit-exchange.github.io/docs/v5/websocket/public/all-liquidation",
                "https://bybit-exchange.github.io/docs/v5/websocket/public/orderbook",
                "https://bybit-exchange.github.io/docs/v5/websocket/public/ticker",
                "https://bybit-exchange.github.io/docs/v5/websocket/public/trade",
            ],
        },
        "schema_version": SCHEMA_VERSION,
    }


def load_plan(path: Path) -> dict[str, object]:
    """Read and fail closed unless the file is the exact committed plan."""

    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise LiquidationAbsorptionPreflightError("data-qualification plan cannot be read") from exc
    if not isinstance(payload, dict):
        raise LiquidationAbsorptionPreflightError("data-qualification plan must be a JSON object")
    if payload != expected_plan() or _logical_sha256(payload) != PLAN_LOGICAL_SHA256:
        raise LiquidationAbsorptionPreflightError(
            "committed liquidation-absorption plan differs from its preregistration"
        )
    return cast(dict[str, object], payload)


def main(argv: list[str] | None = None) -> int:
    """Verify the static contract without opening data or a network connection."""

    parser = argparse.ArgumentParser(
        description="Verify the performance-blind liquidation-absorption data plan"
    )
    parser.add_argument("--plan", type=Path, default=Path(PLAN_FILENAME))
    arguments = parser.parse_args(argv)
    plan = load_plan(arguments.plan)
    print(f"classification={plan['classification']}")
    print(f"plan_logical_sha256={PLAN_LOGICAL_SHA256}")
    print("network_access=false")
    print("trading_access=false")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
