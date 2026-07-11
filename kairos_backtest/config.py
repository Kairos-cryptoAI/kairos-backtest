from __future__ import annotations

from dataclasses import asdict, dataclass

SYMBOLS = ("BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT")
TIMEFRAMES = ("1m", "3m", "5m", "15m", "30m", "1h", "2h", "4h")


@dataclass(frozen=True)
class CostScenario:
    name: str
    fee_bps: float
    spread_bps: float
    slippage_bps: float
    latency_ms: int

    def as_dict(self) -> dict[str, str | float | int]:
        return asdict(self)


BASELINE = CostScenario("baseline", 4.0, 2.0, 2.0, 250)
STRESS = CostScenario("stress", 4.0, 4.0, 4.0, 500)
