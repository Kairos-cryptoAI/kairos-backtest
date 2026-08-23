from __future__ import annotations

import math
import random
from bisect import bisect_left
from dataclasses import dataclass, field
from typing import Literal

from kairos_core.enums import OrderSide
from kairos_strategy.candles import Candle

FundingEvidence = Literal["unavailable", "assumed", "historical"]


@dataclass(frozen=True, slots=True, order=True)
class FundingRateObservation:
    """Signed venue rate known for one hourly settlement timestamp."""

    timestamp_ms: int
    rate_8h_bps: float

    def __post_init__(self) -> None:
        if (
            isinstance(self.timestamp_ms, bool)
            or not isinstance(self.timestamp_ms, int)
            or self.timestamp_ms < 0
        ):
            raise ValueError("funding timestamp must be a non-negative integer")
        if not math.isfinite(self.rate_8h_bps):
            raise ValueError("historical funding rate must be finite")


@dataclass(frozen=True, slots=True)
class FundingConfig:
    """Funding assumption for datasets without venue funding history.

    ``rate_8h_bps=None`` is an explicit unavailable state. A configured positive
    rate is charged adversely to either side and settled hourly at one eighth of
    the eight-hour rate, matching the EVEDEX settlement cadence conservatively.
    """

    rate_8h_bps: float | None = None
    source: str = "unavailable"
    evidence: FundingEvidence = "unavailable"
    historical_rates: tuple[FundingRateObservation, ...] = ()
    settlement_interval_ms: int = 60 * 60 * 1000

    def __post_init__(self) -> None:
        if self.settlement_interval_ms <= 0:
            raise ValueError("settlement_interval_ms must be positive")
        if self.evidence == "unavailable":
            if self.rate_8h_bps is not None or self.historical_rates or self.source != "unavailable":
                raise ValueError("unavailable funding cannot include rates or a source")
            return
        if not self.source.strip() or self.source == "unavailable":
            raise ValueError("configured funding requires an explicit source")
        if self.evidence == "assumed":
            if self.historical_rates:
                raise ValueError("assumed funding cannot include historical observations")
            if self.rate_8h_bps is None or not math.isfinite(self.rate_8h_bps) or self.rate_8h_bps < 0:
                raise ValueError("assumed rate_8h_bps must be finite and non-negative")
            return
        if self.evidence != "historical":
            raise ValueError(f"unsupported funding evidence {self.evidence!r}")
        if self.rate_8h_bps is not None or not self.historical_rates:
            raise ValueError("historical funding requires timestamped observations only")
        timestamps = tuple(observation.timestamp_ms for observation in self.historical_rates)
        if timestamps != tuple(sorted(set(timestamps))):
            raise ValueError("historical funding observations must be sorted and unique")
        if any(timestamp % self.settlement_interval_ms for timestamp in timestamps):
            raise ValueError("historical funding timestamps must align with settlement cadence")

    @property
    def available(self) -> bool:
        return self.evidence != "unavailable"

    def settlement_cost(self, notional: float, timestamp_ms: int) -> tuple[float, bool]:
        """Return signed cash cost and whether an observed rate covered the event."""
        if not math.isfinite(notional):
            raise ValueError("funding notional must be finite")
        if self.evidence == "unavailable":
            return 0.0, False
        if self.evidence == "assumed":
            rate = self.rate_8h_bps
            if rate is None:  # defensive guard for corrupted/deserialized instances
                raise RuntimeError("assumed funding is missing its validated rate")
            return abs(notional) * rate / 8 / 10_000, False
        index = bisect_left(
            self.historical_rates,
            timestamp_ms,
            key=lambda observation: observation.timestamp_ms,
        )
        if index >= len(self.historical_rates) or self.historical_rates[index].timestamp_ms != timestamp_ms:
            return 0.0, False
        rate = self.historical_rates[index].rate_8h_bps
        return notional * rate / 8 / 10_000, True

    def hourly_cost(self, notional: float) -> float:
        """Compatibility helper for adverse assumed rates without a timestamp."""
        if self.evidence == "historical":
            raise ValueError("historical funding requires a settlement timestamp")
        return self.settlement_cost(notional, 0)[0]


@dataclass(frozen=True, slots=True)
class ExecutionConfig:
    latency_ms: int = 100
    spread_bps: float = 2.0
    slippage_bps: float = 1.0
    # EVEDEX base taker fee is 0.045%; no cashback is assumed.
    fee_bps: float = 4.5
    max_volume_participation: float = 0.05
    slippage_jitter_bps: float = 0.0
    funding: FundingConfig = field(default_factory=FundingConfig)

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
    reference_price: float = 0.0
    implementation_shortfall_usd: float = 0.0


@dataclass(slots=True)
class TradeLedger:
    """Track average entry and closed-trade PnL across partial fills and flips."""

    position: float = 0.0
    average_entry_price: float = 0.0
    current_trade_pnl: float = 0.0
    closed_trade_pnls: list[float] = field(default_factory=list)

    def apply_carry_cost(self, cost_usd: float) -> None:
        if not math.isfinite(cost_usd):
            raise ValueError("carry cost must be finite")
        if self.position:
            self.current_trade_pnl -= cost_usd

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
        available_volume: float,
        timestamp_ms: int | None = None,
        reference_price: float | None = None,
    ) -> SimulatedFill:
        if not math.isfinite(quantity) or quantity <= 0:
            raise ValueError("quantity must be positive")
        filled_at = candle.open_time_ms + self.config.latency_ms if timestamp_ms is None else timestamp_ms
        if not candle.open_time_ms <= filled_at <= candle.close_time_ms:
            raise ValueError("fill timestamp must fall inside its execution candle")
        reference = candle.open if reference_price is None else reference_price
        if not math.isfinite(reference) or reference <= 0:
            raise ValueError("reference price must be finite and positive")
        if not math.isfinite(available_volume) or available_volume < 0:
            raise ValueError("causally available volume must be finite and non-negative")
        available = available_volume * self.config.max_volume_participation
        filled = min(quantity, available)
        jitter = self._random.uniform(-self.config.slippage_jitter_bps, self.config.slippage_jitter_bps)
        cost_bps = max(0.0, self.config.spread_bps / 2 + self.config.slippage_bps + jitter)
        direction = 1.0 if side == OrderSide.BUY else -1.0
        price = reference * (1 + direction * cost_bps / 10_000)
        fee = filled * price * self.config.fee_bps / 10_000
        shortfall = filled * (price - reference) * direction
        return SimulatedFill(
            timestamp_ms=filled_at,
            side=side,
            requested_quantity=quantity,
            filled_quantity=filled,
            price=price,
            fee_usd=fee,
            reference_price=reference,
            implementation_shortfall_usd=shortfall,
        )
