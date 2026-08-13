from __future__ import annotations

import math
import random
from dataclasses import dataclass, field

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

    def __post_init__(self) -> None:
        values = (
            self.spread_bps,
            self.slippage_bps,
            self.fee_bps,
            self.max_volume_participation,
            self.slippage_jitter_bps,
        )
        if not all(math.isfinite(value) for value in values):
            raise ValueError("execution parameters must be finite")
        if self.latency_ms < 0:
            raise ValueError("latency_ms cannot be negative")
        if min(self.spread_bps, self.slippage_bps, self.fee_bps, self.slippage_jitter_bps) < 0:
            raise ValueError("execution costs and jitter cannot be negative")
        if not 0 < self.max_volume_participation <= 1:
            raise ValueError("max_volume_participation must be in (0, 1]")


@dataclass(frozen=True, slots=True)
class SimulatedFill:
    timestamp_ms: int
    side: OrderSide
    requested_quantity: float
    filled_quantity: float
    price: float
    fee_usd: float


@dataclass(slots=True)
class TradeLedger:
    """Track average entry and closed-trade PnL across partial fills and flips."""

    position: float = 0.0
    average_entry_price: float = 0.0
    current_trade_pnl: float = 0.0
    closed_trade_pnls: list[float] = field(default_factory=list)

    def apply(self, fill: SimulatedFill) -> None:
        signed_fill = fill.filled_quantity if fill.side is OrderSide.BUY else -fill.filled_quantity
        if signed_fill == 0:
            return
        previous = self.position
        if previous == 0 or previous * signed_fill > 0:
            new_position = previous + signed_fill
            self.average_entry_price = (
                abs(previous) * self.average_entry_price + abs(signed_fill) * fill.price
            ) / abs(new_position)
            self.position = new_position
            self.current_trade_pnl -= fill.fee_usd
            return

        closing_quantity = min(abs(previous), abs(signed_fill))
        closing_fee = fill.fee_usd * closing_quantity / abs(signed_fill)
        direction = 1.0 if previous > 0 else -1.0
        self.current_trade_pnl += (
            closing_quantity * (fill.price - self.average_entry_price) * direction - closing_fee
        )
        new_position = previous + signed_fill
        if new_position == 0:
            self.closed_trade_pnls.append(self.current_trade_pnl)
            self.position = 0.0
            self.average_entry_price = 0.0
            self.current_trade_pnl = 0.0
        elif previous * new_position > 0:
            self.position = new_position
        else:
            self.closed_trade_pnls.append(self.current_trade_pnl)
            self.position = new_position
            self.average_entry_price = fill.price
            self.current_trade_pnl = -(fill.fee_usd - closing_fee)


class FillSimulator:
    def __init__(self, config: ExecutionConfig, *, seed: int = 0) -> None:
        self.config = config
        self._random = random.Random(seed)  # nosec B311

    def fill(
        self,
        candle: Candle,
        side: OrderSide,
        quantity: float,
        *,
        timestamp_ms: int | None = None,
        reference_price: float | None = None,
    ) -> SimulatedFill:
        if not math.isfinite(quantity) or quantity <= 0:
            raise ValueError("quantity must be positive")
        filled_at = candle.open_time_ms + self.config.latency_ms if timestamp_ms is None else timestamp_ms
        if not candle.open_time_ms <= filled_at <= candle.close_time_ms:
            raise ValueError("fill timestamp must fall inside its execution candle")
        reference = candle.open if reference_price is None else reference_price
        if not math.isfinite(reference) or not candle.low <= reference <= candle.high:
            raise ValueError("reference price must be finite and inside the execution candle")
        available = candle.volume * self.config.max_volume_participation
        filled = min(quantity, available)
        jitter = self._random.uniform(-self.config.slippage_jitter_bps, self.config.slippage_jitter_bps)
        cost_bps = max(0.0, self.config.spread_bps / 2 + self.config.slippage_bps + jitter)
        direction = 1.0 if side == OrderSide.BUY else -1.0
        price = reference * (1 + direction * cost_bps / 10_000)
        price = min(max(price, candle.low), candle.high)
        fee = filled * price * self.config.fee_bps / 10_000
        return SimulatedFill(
            timestamp_ms=filled_at,
            side=side,
            requested_quantity=quantity,
            filled_quantity=filled,
            price=price,
            fee_usd=fee,
        )
