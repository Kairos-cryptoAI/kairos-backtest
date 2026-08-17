from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Literal

SYMBOLS = ("BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT")
TIMEFRAMES = ("1m", "3m", "5m", "15m", "30m", "1h", "2h", "4h")


@dataclass(frozen=True)
class CostScenario:
    name: str
    fee_bps: float
    spread_bps: float
    slippage_bps: float
    latency_ms: int
    funding_rate_8h_bps: float | None
    funding_source: str
    funding_evidence: Literal["unavailable", "assumed", "historical"]

    def as_dict(self) -> dict[str, str | float | int]:
        return asdict(self)


BASELINE = CostScenario("baseline", 4.5, 2.0, 2.0, 250, None, "unavailable", "unavailable")
STRESS = CostScenario("stress", 4.5, 4.0, 4.0, 500, 5.0, "assumed_adverse_stress", "assumed")
