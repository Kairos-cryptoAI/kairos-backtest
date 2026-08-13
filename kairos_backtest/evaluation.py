from __future__ import annotations

from bisect import bisect_right
from dataclasses import asdict, dataclass

from kairos_core.enums import OrderSide, Side
from kairos_quant.candles import Candle

from .execution import ExecutionConfig, FillSimulator, TradeLedger
from .metrics import PerformanceMetrics, calculate_metrics
from .strategy import StrategySignal
from .validation import canonical_candles


@dataclass(frozen=True, slots=True)
class EvaluationResult:
    metrics: PerformanceMetrics
    final_equity: float
    return_pct: float
    trades: int
    fees_usd: float
    turnover_usd: float
    exposure_pct: float
    benchmark_return_pct: float

    def to_dict(self) -> dict[str, object]:
        return {**asdict(self), "metrics": asdict(self.metrics)}


def aggregate_results(segments: list[EvaluationResult], *, initial_equity: float) -> EvaluationResult:
    """Compound bounded-memory segment results into one horizon result."""
    if not segments:
        raise ValueError("at least one segment is required")
    growth = 1.0
    benchmark_growth = 1.0
    for segment in segments:
        growth *= 1 + segment.return_pct / 100
        benchmark_growth *= 1 + segment.benchmark_return_pct / 100
    trades = sum(segment.trades for segment in segments)
    weighted_win_rate = (
        sum(segment.metrics.win_rate * segment.trades for segment in segments) / trades if trades else 0.0
    )
    metrics = PerformanceMetrics(
        total_return=growth - 1,
        max_drawdown=max(segment.metrics.max_drawdown for segment in segments),
        sharpe=sum(segment.metrics.sharpe for segment in segments) / len(segments),
        trades=trades,
        win_rate=weighted_win_rate,
    )
    return EvaluationResult(
        metrics=metrics,
        final_equity=initial_equity * growth,
        return_pct=(growth - 1) * 100,
        trades=trades,
        fees_usd=sum(segment.fees_usd for segment in segments),
        turnover_usd=sum(segment.turnover_usd for segment in segments),
        exposure_pct=sum(segment.exposure_pct for segment in segments) / len(segments),
        benchmark_return_pct=(benchmark_growth - 1) * 100,
    )


def evaluate(
    candles: list[Candle],
    signals: list[StrategySignal],
    *,
    initial_equity: float,
    execution: ExecutionConfig,
    allocation: float = 1.0,
    seed: int = 0,
) -> EvaluationResult:
    ordered = canonical_candles(candles)
    if not ordered or initial_equity <= 0 or not 0 < allocation <= 1:
        raise ValueError("valid candles, equity and allocation are required")
    ordered_signals = sorted(signals, key=lambda signal: signal.timestamp_ms)
    if any(
        current.timestamp_ms == previous.timestamp_ms
        for previous, current in zip(ordered_signals, ordered_signals[1:], strict=False)
    ):
        raise ValueError("signals cannot share a timestamp")
    if ordered_signals and (
        ordered_signals[0].timestamp_ms < ordered[0].close_time_ms
        or ordered_signals[-1].timestamp_ms > ordered[-1].close_time_ms
    ):
        raise ValueError("signals must stay inside the closed-candle data boundary")

    times = [c.open_time_ms for c in ordered]
    simulator = FillSimulator(execution, seed=seed)
    ledger = TradeLedger()
    cash, fees, turnover = initial_equity, 0.0, 0.0
    equity_curve = [initial_equity]
    exposed = 0
    for signal in ordered_signals:
        index = bisect_right(times, signal.timestamp_ms + execution.latency_ms)
        if index >= len(ordered):
            break
        candle = ordered[index]
        equity = cash + ledger.position * candle.open
        target_notional = equity * allocation * signal.confidence
        target = (
            target_notional
            / candle.open
            * (1 if signal.side == Side.LONG else -1 if signal.side == Side.SHORT else 0)
        )
        delta = target - ledger.position
        if abs(delta * candle.open) > 1.0:
            side = OrderSide.BUY if delta > 0 else OrderSide.SELL
            fill = simulator.fill(
                candle,
                side,
                abs(delta),
                timestamp_ms=candle.open_time_ms,
            )
            signed = fill.filled_quantity if side is OrderSide.BUY else -fill.filled_quantity
            cash -= signed * fill.price + fill.fee_usd
            fees += fill.fee_usd
            turnover += fill.filled_quantity * fill.price
            ledger.apply(fill)
        exposed += ledger.position != 0
        equity_curve.append(cash + ledger.position * candle.close)

    if ledger.position:
        candle = ordered[-1]
        side = OrderSide.SELL if ledger.position > 0 else OrderSide.BUY
        fill = simulator.fill(
            candle,
            side,
            abs(ledger.position),
            timestamp_ms=candle.close_time_ms,
            reference_price=candle.close,
        )
        signed = fill.filled_quantity if side is OrderSide.BUY else -fill.filled_quantity
        cash -= signed * fill.price + fill.fee_usd
        fees += fill.fee_usd
        turnover += fill.filled_quantity * fill.price
        ledger.apply(fill)

    final = cash + ledger.position * ordered[-1].close
    equity_curve.append(final)
    benchmark = (ordered[-1].close / ordered[0].open - 1) * 100
    return EvaluationResult(
        calculate_metrics(equity_curve, ledger.closed_trade_pnls),
        final,
        (final / initial_equity - 1) * 100,
        len(ledger.closed_trade_pnls),
        fees,
        turnover,
        exposed / max(1, len(ordered_signals)) * 100,
        benchmark,
    )
