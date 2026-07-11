from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime

from .execution import ExecutionConfig

SYMBOLS = ("BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT")


@dataclass(frozen=True, slots=True)
class Horizon:
    name: str
    start: date
    end: date


def default_horizons(today: date | None = None) -> tuple[Horizon, Horizon]:
    today = today or datetime.now(UTC).date()
    end = date(today.year, today.month, 1)
    return (
        Horizon("5y", date(end.year - 5, end.month, 1), end),
        Horizon("12m", date(end.year - 1, end.month, 1), end),
    )


BASELINE = ExecutionConfig(latency_ms=250, spread_bps=2, slippage_bps=2, fee_bps=4)
STRESS = ExecutionConfig(latency_ms=500, spread_bps=4, slippage_bps=4, fee_bps=4)
SCENARIOS = {"baseline": BASELINE, "stress": STRESS}
