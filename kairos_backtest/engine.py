from __future__ import annotations

import hashlib
import json
import math
from bisect import bisect_left
from collections.abc import Iterable
from dataclasses import dataclass

from kairos_core.enums import OrderSide, Side
from kairos_quant.replay import replay_candles
from kairos_strategy.candles import Candle

from .clock import ReplayClock
from .execution import ExecutionConfig, FillSimulator, SimulatedFill, TradeLedger
from .metrics import PerformanceMetrics, calculate_metrics
from .provenance import RuntimeProvenance, runtime_provenance, source_fingerprint
from .validation import canonical_candles


@dataclass(frozen=True, slots=True)
class RunManifest:
    seed: int
    candles_sha256: str
    candle_count: int
    actual_start_ms: int
    actual_end_ms: int
    initial_equity: float
    quantity: float
    execution: ExecutionConfig
    source_sha256: str
    runtime: RuntimeProvenance


@dataclass(frozen=True, slots=True)
class BacktestReport:
    manifest: RunManifest
    fills: tuple[SimulatedFill, ...]
    equity_curve: tuple[float, ...]
    metrics: PerformanceMetrics


def _fingerprint(candles: list[Candle]) -> str:
    rows = [
        (
            c.symbol,
            c.timeframe,
            c.open_time_ms,
            c.close_time_ms,
            c.open,
            c.high,
            c.low,
            c.close,
            c.volume,
            c.quote_volume,
            c.taker_buy_volume,
        )
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
    ordered = canonical_candles(candles)
    if not ordered:
        raise ValueError("at least one candle is required")
    if (
        not math.isfinite(initial_equity)
        or not math.isfinite(quantity)
        or initial_equity <= 0
        or quantity <= 0
    ):
        raise ValueError("initial_equity and quantity must be positive")
    replay = replay_candles(ordered)
    open_times = [candle.open_time_ms for candle in ordered]
    config = execution or ExecutionConfig()
    simulator = FillSimulator(config, seed=seed)
    clock = ReplayClock()
    cash = initial_equity
    ledger = TradeLedger()
    fills: list[SimulatedFill] = []
    equity_curve = [cash]
    funding_interval = config.funding.settlement_interval_ms
    next_funding = (ordered[0].open_time_ms // funding_interval + 1) * funding_interval

    for point in replay.points:
        execution_index = bisect_left(open_times, point.timestamp_ms + config.latency_ms)
        if execution_index == 0:
            execution_index = 1
        if execution_index >= len(ordered):
            break
        candle = ordered[execution_index]
        clock.advance_to(candle.open_time_ms)
        while next_funding <= candle.open_time_ms:
            funding_cost, _ = config.funding.settlement_cost(
                ledger.position * candle.open,
                next_funding,
            )
            cash -= funding_cost
            ledger.apply_carry_cost(funding_cost)
            next_funding += funding_interval
        marked_open_equity = cash + ledger.position * candle.open
        if not math.isfinite(marked_open_equity) or marked_open_equity <= 0:
            raise ValueError("strategy equity became insolvent before target sizing")
        target = quantity if point.bias == Side.LONG else -quantity if point.bias == Side.SHORT else 0.0
        delta = target - ledger.position
        if delta:
            side = OrderSide.BUY if delta > 0 else OrderSide.SELL
            fill = simulator.fill(
                candle,
                side,
                abs(delta),
                available_volume=ordered[execution_index - 1].volume,
                timestamp_ms=candle.open_time_ms,
            )
            fills.append(fill)
            signed = fill.filled_quantity if side == OrderSide.BUY else -fill.filled_quantity
            cash -= signed * fill.price + fill.fee_usd
            ledger.apply(fill)
        while next_funding <= candle.close_time_ms:
            funding_cost, _ = config.funding.settlement_cost(
                ledger.position * candle.close,
                next_funding,
            )
            cash -= funding_cost
            ledger.apply_carry_cost(funding_cost)
            next_funding += funding_interval
        marked_close_equity = cash + ledger.position * candle.close
        if not math.isfinite(marked_close_equity) or marked_close_equity <= 0:
            raise ValueError("strategy equity became insolvent during backtest")
        equity_curve.append(marked_close_equity)

    if ledger.position and ordered:
        candle = ordered[-1]
        side = OrderSide.SELL if ledger.position > 0 else OrderSide.BUY
        fill = simulator.fill(
            candle,
            side,
            abs(ledger.position),
            available_volume=candle.volume,
            timestamp_ms=candle.close_time_ms,
            reference_price=candle.close,
        )
        fills.append(fill)
        signed = fill.filled_quantity if side == OrderSide.BUY else -fill.filled_quantity
        cash -= signed * fill.price + fill.fee_usd
        ledger.apply(fill)
        if abs(ledger.position) > max(1e-12, fill.requested_quantity * 1e-12):
            raise ValueError("terminal liquidation exceeded causal candle liquidity; backtest is incomplete")
        final_equity = cash + ledger.position * candle.close
        if not math.isfinite(final_equity) or final_equity <= 0:
            raise ValueError("strategy equity became insolvent after terminal liquidation")
        equity_curve[-1] = final_equity

    manifest = RunManifest(
        seed=seed,
        candles_sha256=_fingerprint(ordered),
        candle_count=len(ordered),
        actual_start_ms=ordered[0].open_time_ms,
        actual_end_ms=ordered[-1].close_time_ms,
        initial_equity=initial_equity,
        quantity=quantity,
        execution=config,
        source_sha256=source_fingerprint(),
        runtime=runtime_provenance(),
    )
    return BacktestReport(
        manifest=manifest,
        fills=tuple(fills),
        equity_curve=tuple(equity_curve),
        metrics=calculate_metrics(equity_curve, ledger.closed_trade_pnls),
    )
