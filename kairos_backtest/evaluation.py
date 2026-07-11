from __future__ import annotations

from bisect import bisect_right
from dataclasses import asdict, dataclass

from kairos_core.enums import Side
from kairos_quant.candles import Candle

from .execution import ExecutionConfig
from .metrics import PerformanceMetrics, calculate_metrics
from .strategy import StrategySignal


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
) -> EvaluationResult:
    if not candles or initial_equity <= 0 or not 0 < allocation <= 1:
        raise ValueError("valid candles, equity and allocation are required")
    times = [c.open_time_ms for c in candles]
    cash, position, fees, turnover = initial_equity, 0.0, 0.0, 0.0
    equity_curve = [initial_equity]
    trade_pnls: list[float] = []
    exposed, entry_equity = 0, initial_equity
    for signal in signals:
        index = bisect_right(times, signal.timestamp_ms + execution.latency_ms)
        if index >= len(candles):
            break
        candle = candles[index]
        equity = cash + position * candle.open
        target_notional = equity * allocation * signal.confidence
        target = (
            target_notional
            / candle.open
            * (1 if signal.side == Side.LONG else -1 if signal.side == Side.SHORT else 0)
        )
        delta = target - position
        if abs(delta * candle.open) > 1.0:
            direction = 1 if delta > 0 else -1
            costs_bps = execution.spread_bps / 2 + execution.slippage_bps
            price = candle.open * (1 + direction * costs_bps / 10_000)
            notional = abs(delta) * price
            fee = notional * execution.fee_bps / 10_000
            cash -= delta * price + fee
            fees += fee
            turnover += notional
            if position and (target == 0 or position * target <= 0):
                trade_pnls.append(equity - entry_equity)
                entry_equity = equity
            position = target
        exposed += position != 0
        equity_curve.append(cash + position * candle.close)
    final = cash + position * candles[-1].close
    equity_curve.append(final)
    if position:
        trade_pnls.append(final - entry_equity)
    benchmark = (candles[-1].close / candles[0].open - 1) * 100
    return EvaluationResult(
        calculate_metrics(equity_curve, trade_pnls),
        final,
        (final / initial_equity - 1) * 100,
        len(trade_pnls),
        fees,
        turnover,
        exposed / max(1, len(signals)) * 100,
        benchmark,
    )
