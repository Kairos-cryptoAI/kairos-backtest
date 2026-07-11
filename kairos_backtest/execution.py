from __future__ import annotations

import random
from dataclasses import dataclass

from kairos_core.enums import OrderSide
from kairos_quant.candles import Candle


@dataclass(frozen=True, slots=True)
class ExecutionConfig:
    latency_ms: int = 100
    spread_bps: float = 2.0
    slippage_bps: float = 1.0
    fee_bps: float = 4.0
    max_volume_participation: float = 0.05
    slippage_jitter_bps: float = 0.0


@dataclass(frozen=True, slots=True)
class SimulatedFill:
    timestamp_ms: int
    side: OrderSide
    requested_quantity: float
    filled_quantity: float
    price: float
    fee_usd: float


class FillSimulator:
    def __init__(self, config: ExecutionConfig, *, seed: int = 0) -> None:
        self.config = config
        self._random = random.Random(seed)

    def fill(self, candle: Candle, side: OrderSide, quantity: float) -> SimulatedFill:
        if quantity <= 0:
            raise ValueError("quantity must be positive")
        available = candle.volume * self.config.max_volume_participation
        filled = min(quantity, available)
        jitter = self._random.uniform(-self.config.slippage_jitter_bps, self.config.slippage_jitter_bps)
        cost_bps = self.config.spread_bps / 2 + self.config.slippage_bps + jitter
        direction = 1.0 if side == OrderSide.BUY else -1.0
        price = candle.open * (1 + direction * cost_bps / 10_000)
        price = min(max(price, candle.low), candle.high)
        fee = filled * price * self.config.fee_bps / 10_000
        return SimulatedFill(
            timestamp_ms=candle.open_time_ms + self.config.latency_ms,
            side=side,
            requested_quantity=quantity,
            filled_quantity=filled,
            price=price,
            fee_usd=fee,
        )
