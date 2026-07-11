from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from dataclasses import dataclass

from kairos_core.enums import OrderSide, Side
from kairos_quant.candles import Candle
from kairos_quant.replay import replay_candles

from .clock import ReplayClock
from .execution import ExecutionConfig, FillSimulator, SimulatedFill
from .metrics import PerformanceMetrics, calculate_metrics


@dataclass(frozen=True, slots=True)
class RunManifest:
    seed: int
    candles_sha256: str
    candle_count: int
    initial_equity: float
    quantity: float
    execution: ExecutionConfig


@dataclass(frozen=True, slots=True)
class BacktestReport:
    manifest: RunManifest
    fills: tuple[SimulatedFill, ...]
    equity_curve: tuple[float, ...]
    metrics: PerformanceMetrics


def _fingerprint(candles: list[Candle]) -> str:
    rows = [
        (c.symbol, c.timeframe, c.open_time_ms, c.close_time_ms, c.open, c.high, c.low, c.close, c.volume)
        for c in candles
    ]
    return hashlib.sha256(json.dumps(rows, separators=(",", ":")).encode()).hexdigest()


def run_backtest(
    candles: Iterable[Candle],
    *,
    initial_equity: float = 10_000.0,
    quantity: float = 0.01,
    seed: int = 0,
    execution: ExecutionConfig | None = None,
) -> BacktestReport:
    ordered = list(candles)
    if initial_equity <= 0 or quantity <= 0:
        raise ValueError("initial_equity and quantity must be positive")
    replay = replay_candles(ordered)
    config = execution or ExecutionConfig()
    simulator = FillSimulator(config, seed=seed)
    clock = ReplayClock()
    cash = initial_equity
    position = 0.0
    entry_cost = 0.0
    fills: list[SimulatedFill] = []
    equity_curve = [cash]
    trade_pnls: list[float] = []

    for index, point in enumerate(replay.points[:-1]):
        candle = ordered[index + 1]
        clock.advance_to(candle.open_time_ms)
        target = quantity if point.bias == Side.LONG else -quantity if point.bias == Side.SHORT else 0.0
        delta = target - position
        if delta:
            side = OrderSide.BUY if delta > 0 else OrderSide.SELL
            fill = simulator.fill(candle, side, abs(delta))
            fills.append(fill)
            signed = fill.filled_quantity if side == OrderSide.BUY else -fill.filled_quantity
            previous_position = position
            cash -= signed * fill.price + fill.fee_usd
            position += signed
            if previous_position and (position == 0 or previous_position * position <= 0):
                trade_pnls.append(cash + position * fill.price - equity_curve[-1])
            entry_cost = position * fill.price
        equity_curve.append(cash + position * candle.close)

    if position and ordered:
        candle = ordered[-1]
        side = OrderSide.SELL if position > 0 else OrderSide.BUY
        fill = simulator.fill(candle, side, abs(position))
        fills.append(fill)
        signed = fill.filled_quantity if side == OrderSide.BUY else -fill.filled_quantity
        cash -= signed * fill.price + fill.fee_usd
        position += signed
        trade_pnls.append(cash - equity_curve[-1] + entry_cost * 0)
        equity_curve.append(cash + position * candle.close)

    manifest = RunManifest(
        seed=seed,
        candles_sha256=_fingerprint(ordered),
        candle_count=len(ordered),
        initial_equity=initial_equity,
        quantity=quantity,
        execution=config,
    )
    return BacktestReport(
        manifest=manifest,
        fills=tuple(fills),
        equity_curve=tuple(equity_curve),
        metrics=calculate_metrics(equity_curve, trade_pnls),
    )
