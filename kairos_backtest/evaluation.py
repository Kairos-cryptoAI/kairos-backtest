from __future__ import annotations

import math
from bisect import bisect_left
from dataclasses import asdict, dataclass

from kairos_core.enums import OrderSide, Side
from kairos_strategy.candles import Candle

from .execution import ExecutionConfig, FillSimulator, TradeLedger
from .metrics import (
    MetricStatistics,
    PerformanceMetrics,
    calculate_metrics,
    calculate_statistics,
    metrics_from_statistics,
)
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
    funding_usd: float = 0.0
    implementation_shortfall_usd: float = 0.0
    funding_source: str = "unavailable"
    funding_evidence: str = "unavailable"
    funding_coverage_pct: float = 0.0
    funding_observations_expected: int = 0
    funding_observations_observed: int = 0
    market_periods: int = 0
    exposed_periods: int = 0
    fill_count: int = 0
    fill_attempt_count: int = 0
    partial_fill_count: int = 0
    requested_quantity_total: float = 0.0
    filled_quantity_total: float = 0.0
    fill_ratio_pct: float = 100.0
    first_fill_timestamp_ms: int | None = None
    last_fill_timestamp_ms: int | None = None
    terminal_liquidation_complete: bool = True
    terminal_residual_quantity: float = 0.0
    terminal_residual_notional_usd: float = 0.0
    statistics: MetricStatistics | None = None

    def to_dict(self) -> dict[str, object]:
        payload = asdict(self)
        payload.pop("statistics")
        return {**payload, "metrics": asdict(self.metrics)}


def aggregate_results(segments: list[EvaluationResult], *, initial_equity: float) -> EvaluationResult:
    """Compound bounded-memory segment results into one horizon result."""
    if not segments:
        raise ValueError("at least one segment is required")
    if not math.isfinite(initial_equity) or initial_equity <= 0:
        raise ValueError("initial equity must be finite and positive")
    if any(segment.statistics is None for segment in segments):
        raise ValueError("segment sufficient statistics are required for aggregation")
    for segment in segments:
        statistics = segment.statistics
        if statistics is None:  # guarded above; retained for type narrowing
            raise ValueError("segment sufficient statistics are required for aggregation")
        finite_values = (
            segment.final_equity,
            segment.return_pct,
            segment.fees_usd,
            segment.turnover_usd,
            segment.exposure_pct,
            segment.benchmark_return_pct,
            segment.funding_usd,
            segment.implementation_shortfall_usd,
            segment.funding_coverage_pct,
            segment.requested_quantity_total,
            segment.filled_quantity_total,
            segment.fill_ratio_pct,
            segment.terminal_residual_quantity,
            segment.terminal_residual_notional_usd,
            statistics.return_sum,
            statistics.return_squares_sum,
            statistics.downside_squares_sum,
            statistics.peak_equity,
            statistics.minimum_equity,
            statistics.max_drawdown,
            statistics.gross_profit,
            statistics.gross_loss,
            statistics.total_trade_pnl,
        )
        if any(not math.isfinite(value) for value in finite_values):
            raise ValueError("segments must contain only finite metrics")
        if (
            segment.final_equity <= 0
            or segment.return_pct <= -100
            or segment.benchmark_return_pct <= -100
            or statistics.peak_equity <= 0
            or statistics.minimum_equity <= 0
            or not 0 <= statistics.max_drawdown <= 1
        ):
            raise ValueError("segment growth and equity statistics must remain positive")
        if (
            isinstance(segment.trades, bool)
            or not isinstance(segment.trades, int)
            or segment.trades < 0
            or statistics.trades != segment.trades
            or statistics.periods < 0
            or not 0 <= statistics.wins <= statistics.trades
            or segment.market_periods < 0
            or not 0 <= segment.exposed_periods <= segment.market_periods
            or segment.fill_count < 0
            or segment.fill_attempt_count < 0
            or segment.partial_fill_count < 0
            or segment.fill_count > segment.fill_attempt_count
            or segment.partial_fill_count > segment.fill_attempt_count
            or segment.requested_quantity_total < 0
            or segment.filled_quantity_total < 0
            or segment.filled_quantity_total > segment.requested_quantity_total + 1e-12
            or not 0 <= segment.fill_ratio_pct <= 100
            or segment.funding_observations_expected < 0
            or not 0 <= segment.funding_observations_observed <= segment.funding_observations_expected
            or not 0 <= segment.exposure_pct <= 100
            or not 0 <= segment.funding_coverage_pct <= 100
            or not isinstance(segment.terminal_liquidation_complete, bool)
            or segment.terminal_residual_quantity < 0
            or segment.terminal_residual_notional_usd < 0
            or (
                segment.terminal_liquidation_complete
                and (
                    segment.terminal_residual_quantity > 1e-12
                    or segment.terminal_residual_notional_usd > 1e-9
                )
            )
        ):
            raise ValueError("segment counts and percentages are inconsistent")
        expected_fill_ratio = (
            segment.filled_quantity_total / segment.requested_quantity_total * 100
            if segment.requested_quantity_total
            else 100.0
        )
        if not math.isclose(segment.fill_ratio_pct, expected_fill_ratio, rel_tol=1e-9, abs_tol=1e-9):
            raise ValueError("segment fill ratio is inconsistent")
    growth = 1.0
    benchmark_growth = 1.0
    capital = initial_equity
    global_peak = initial_equity
    global_minimum = initial_equity
    max_drawdown = 0.0
    periods = 0
    return_sum = return_squares = downside_squares = 0.0
    gross_profit = gross_loss = total_trade_pnl = 0.0
    fees = turnover = funding = shortfall = 0.0
    exposed_periods = 0
    market_periods = 0
    funding_expected = funding_observed = 0
    fill_count = 0
    fill_attempt_count = partial_fill_count = 0
    requested_quantity_total = filled_quantity_total = 0.0
    terminal_residual_quantity = 0.0
    terminal_residual_notional = 0.0
    for segment in segments:
        statistics = segment.statistics
        if statistics is None:  # guarded before aggregation; retained for type narrowing
            raise ValueError("segment sufficient statistics are required for aggregation")
        scale = capital / initial_equity
        segment_peak = statistics.peak_equity * scale
        segment_minimum = statistics.minimum_equity * scale
        if segment_minimum < global_peak:
            max_drawdown = max(max_drawdown, (global_peak - segment_minimum) / global_peak)
        max_drawdown = max(max_drawdown, statistics.max_drawdown)
        global_peak = max(global_peak, segment_peak)
        global_minimum = min(global_minimum, segment_minimum)
        periods += statistics.periods
        return_sum += statistics.return_sum
        return_squares += statistics.return_squares_sum
        downside_squares += statistics.downside_squares_sum
        gross_profit += statistics.gross_profit * scale
        gross_loss += statistics.gross_loss * scale
        total_trade_pnl += statistics.total_trade_pnl * scale
        fees += segment.fees_usd * scale
        turnover += segment.turnover_usd * scale
        funding += segment.funding_usd * scale
        shortfall += segment.implementation_shortfall_usd * scale
        exposed_periods += segment.exposed_periods
        market_periods += segment.market_periods
        funding_expected += segment.funding_observations_expected
        funding_observed += segment.funding_observations_observed
        fill_count += segment.fill_count
        fill_attempt_count += segment.fill_attempt_count
        partial_fill_count += segment.partial_fill_count
        requested_quantity_total += segment.requested_quantity_total * scale
        filled_quantity_total += segment.filled_quantity_total * scale
        terminal_residual_quantity += segment.terminal_residual_quantity * scale
        terminal_residual_notional += segment.terminal_residual_notional_usd * scale
        growth_factor = 1 + segment.return_pct / 100
        benchmark_growth_factor = 1 + segment.benchmark_return_pct / 100
        if (
            not math.isfinite(growth_factor)
            or growth_factor <= 0
            or not math.isfinite(benchmark_growth_factor)
            or benchmark_growth_factor <= 0
        ):
            raise ValueError("segment returns imply invalid compounded growth")
        growth *= growth_factor
        benchmark_growth *= benchmark_growth_factor
        if not math.isfinite(growth) or not math.isfinite(benchmark_growth):
            raise ValueError("compounded growth overflowed")
        capital = initial_equity * growth
    trades = sum(segment.trades for segment in segments)
    wins = sum(segment.statistics.wins for segment in segments if segment.statistics is not None)
    combined_statistics = MetricStatistics(
        periods=periods,
        return_sum=return_sum,
        return_squares_sum=return_squares,
        downside_squares_sum=downside_squares,
        peak_equity=global_peak,
        minimum_equity=global_minimum,
        max_drawdown=max_drawdown,
        gross_profit=gross_profit,
        gross_loss=gross_loss,
        total_trade_pnl=total_trade_pnl,
        trades=trades,
        wins=wins,
    )
    metrics = metrics_from_statistics(
        combined_statistics,
        initial_equity=initial_equity,
        final_equity=capital,
    )
    return EvaluationResult(
        metrics=metrics,
        final_equity=initial_equity * growth,
        return_pct=(growth - 1) * 100,
        trades=trades,
        fees_usd=fees,
        turnover_usd=turnover,
        exposure_pct=exposed_periods / market_periods * 100 if market_periods else 0.0,
        benchmark_return_pct=(benchmark_growth - 1) * 100,
        funding_usd=funding,
        implementation_shortfall_usd=shortfall,
        funding_source=(
            segments[0].funding_source
            if all(segment.funding_source == segments[0].funding_source for segment in segments)
            else "mixed"
        ),
        funding_evidence=(
            segments[0].funding_evidence
            if all(segment.funding_evidence == segments[0].funding_evidence for segment in segments)
            else "mixed"
        ),
        funding_coverage_pct=(funding_observed / funding_expected * 100 if funding_expected else 0.0),
        funding_observations_expected=funding_expected,
        funding_observations_observed=funding_observed,
        market_periods=market_periods,
        exposed_periods=exposed_periods,
        fill_count=fill_count,
        fill_attempt_count=fill_attempt_count,
        partial_fill_count=partial_fill_count,
        requested_quantity_total=requested_quantity_total,
        filled_quantity_total=filled_quantity_total,
        fill_ratio_pct=(
            filled_quantity_total / requested_quantity_total * 100 if requested_quantity_total else 100.0
        ),
        first_fill_timestamp_ms=next(
            (
                segment.first_fill_timestamp_ms
                for segment in segments
                if segment.first_fill_timestamp_ms is not None
            ),
            None,
        ),
        last_fill_timestamp_ms=next(
            (
                segment.last_fill_timestamp_ms
                for segment in reversed(segments)
                if segment.last_fill_timestamp_ms is not None
            ),
            None,
        ),
        terminal_liquidation_complete=all(segment.terminal_liquidation_complete for segment in segments),
        terminal_residual_quantity=terminal_residual_quantity,
        terminal_residual_notional_usd=terminal_residual_notional,
        statistics=combined_statistics,
    )


def evaluate(
    candles: list[Candle],
    signals: list[StrategySignal],
    *,
    initial_equity: float,
    execution: ExecutionConfig,
    allocation: float = 1.0,
    seed: int = 0,
    initial_liquidity_volume: float | None = None,
    allow_incomplete_terminal: bool = False,
) -> EvaluationResult:
    ordered = canonical_candles(candles)
    if (
        not ordered
        or not math.isfinite(initial_equity)
        or initial_equity <= 0
        or not math.isfinite(allocation)
        or not 0 < allocation <= 1
    ):
        raise ValueError("valid candles, equity and allocation are required")
    if any(
        current.open_time_ms != previous.close_time_ms + 1
        for previous, current in zip(ordered, ordered[1:], strict=False)
    ):
        raise ValueError("evaluation candles must be contiguous")
    if initial_liquidity_volume is not None and (
        not math.isfinite(initial_liquidity_volume) or initial_liquidity_volume < 0
    ):
        raise ValueError("initial liquidity volume must be finite and non-negative")
    ordered_signals = sorted(signals, key=lambda signal: signal.timestamp_ms)
    if any(
        current.timestamp_ms == previous.timestamp_ms
        for previous, current in zip(ordered_signals, ordered_signals[1:], strict=False)
    ):
        raise ValueError("signals cannot share a timestamp")
    if ordered_signals and (
        ordered_signals[0].timestamp_ms < ordered[0].open_time_ms
        or ordered_signals[-1].timestamp_ms > ordered[-1].close_time_ms
    ):
        raise ValueError("signals must stay inside the closed-candle data boundary")

    open_times = [c.open_time_ms for c in ordered]
    simulator = FillSimulator(execution, seed=seed)
    ledger = TradeLedger()
    cash, fees, turnover, funding, shortfall = initial_equity, 0.0, 0.0, 0.0, 0.0
    funding_expected = funding_observed = 0
    equity_curve = [initial_equity]
    fill_timestamps: list[int] = []
    fill_attempt_count = partial_fill_count = 0
    requested_quantity_total = filled_quantity_total = 0.0
    exposed = 0
    terminal_liquidation_complete = True
    terminal_residual_quantity = 0.0
    terminal_residual_notional = 0.0
    scheduled: dict[int, list[tuple[StrategySignal, int]]] = {}
    for signal in ordered_signals:
        execution_timestamp = signal.timestamp_ms + execution.latency_ms
        index = bisect_left(open_times, execution_timestamp)
        if index == 0 and initial_liquidity_volume is None:
            index = 1
        if index >= len(ordered):
            continue
        scheduled.setdefault(index, []).append((signal, execution_timestamp))

    interval = execution.funding.settlement_interval_ms
    next_funding = (ordered[0].open_time_ms // interval + 1) * interval

    def settle_funding(through_timestamp_ms: int, mark_price: float) -> None:
        nonlocal cash, funding, funding_expected, funding_observed, next_funding
        while next_funding <= through_timestamp_ms:
            funding_cost, observed = execution.funding.settlement_cost(
                ledger.position * mark_price,
                next_funding,
            )
            cash -= funding_cost
            funding += funding_cost
            funding_expected += 1
            funding_observed += observed
            ledger.apply_carry_cost(funding_cost)
            next_funding += interval

    for index, candle in enumerate(ordered):
        # Hourly settlement at the candle boundary precedes a latency-delayed fill.
        settle_funding(candle.open_time_ms, candle.open)
        marked_open_equity = cash + ledger.position * candle.open
        if not math.isfinite(marked_open_equity) or marked_open_equity <= 0:
            raise ValueError("strategy equity became insolvent before target sizing")
        causal_volume = initial_liquidity_volume if index == 0 else ordered[index - 1].volume
        remaining_capacity = (
            causal_volume * execution.max_volume_participation if causal_volume is not None else 0.0
        )
        for signal, _execution_timestamp in scheduled.get(index, []):
            fill_timestamp = candle.open_time_ms
            equity = cash + ledger.position * candle.open
            if not math.isfinite(equity) or equity <= 0:
                raise ValueError("strategy equity became insolvent before target sizing")
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
                    available_volume=(remaining_capacity / execution.max_volume_participation),
                    timestamp_ms=fill_timestamp,
                    reference_price=candle.open,
                )
                fill_attempt_count += 1
                requested_quantity_total += fill.requested_quantity
                filled_quantity_total += fill.filled_quantity
                partial_fill_count += fill.filled_quantity < fill.requested_quantity - 1e-12
                signed = fill.filled_quantity if side is OrderSide.BUY else -fill.filled_quantity
                cash -= signed * fill.price + fill.fee_usd
                fees += fill.fee_usd
                turnover += fill.filled_quantity * fill.price
                shortfall += fill.implementation_shortfall_usd
                ledger.apply(fill)
                remaining_capacity = max(0.0, remaining_capacity - fill.filled_quantity)
                if fill.filled_quantity:
                    fill_timestamps.append(fill.timestamp_ms)
        settle_funding(candle.close_time_ms, candle.close)
        exposed += ledger.position != 0
        marked_close_equity = cash + ledger.position * candle.close
        if not math.isfinite(marked_close_equity) or marked_close_equity <= 0:
            raise ValueError("strategy equity became insolvent during evaluation")
        equity_curve.append(marked_close_equity)

    if ledger.position:
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
        fill_attempt_count += 1
        requested_quantity_total += fill.requested_quantity
        filled_quantity_total += fill.filled_quantity
        partial_fill_count += fill.filled_quantity < fill.requested_quantity - 1e-12
        signed = fill.filled_quantity if side is OrderSide.BUY else -fill.filled_quantity
        cash -= signed * fill.price + fill.fee_usd
        fees += fill.fee_usd
        turnover += fill.filled_quantity * fill.price
        shortfall += fill.implementation_shortfall_usd
        ledger.apply(fill)
        if fill.filled_quantity:
            fill_timestamps.append(fill.timestamp_ms)
        if abs(ledger.position) > max(1e-12, fill.requested_quantity * 1e-12):
            if not allow_incomplete_terminal:
                raise ValueError(
                    "terminal liquidation exceeded causal candle liquidity; evaluation is incomplete"
                )
            terminal_liquidation_complete = False
            terminal_residual_quantity = abs(ledger.position)
            terminal_residual_notional = terminal_residual_quantity * candle.close

    final = cash + ledger.position * ordered[-1].close
    if not math.isfinite(final) or final <= 0:
        raise ValueError("strategy equity became insolvent after terminal liquidation")
    equity_curve[-1] = final
    benchmark = (ordered[-1].close / ordered[0].open - 1) * 100
    statistics = calculate_statistics(equity_curve, ledger.closed_trade_pnls)
    return EvaluationResult(
        metrics=calculate_metrics(equity_curve, ledger.closed_trade_pnls),
        final_equity=final,
        return_pct=(final / initial_equity - 1) * 100,
        trades=len(ledger.closed_trade_pnls),
        fees_usd=fees,
        turnover_usd=turnover,
        exposure_pct=exposed / len(ordered) * 100,
        benchmark_return_pct=benchmark,
        funding_usd=funding,
        implementation_shortfall_usd=shortfall,
        funding_source=execution.funding.source,
        funding_evidence=execution.funding.evidence,
        funding_coverage_pct=(funding_observed / funding_expected * 100 if funding_expected else 0.0),
        funding_observations_expected=funding_expected,
        funding_observations_observed=funding_observed,
        market_periods=len(ordered),
        exposed_periods=exposed,
        fill_count=len(fill_timestamps),
        fill_attempt_count=fill_attempt_count,
        partial_fill_count=partial_fill_count,
        requested_quantity_total=requested_quantity_total,
        filled_quantity_total=filled_quantity_total,
        fill_ratio_pct=(
            filled_quantity_total / requested_quantity_total * 100 if requested_quantity_total else 100.0
        ),
        first_fill_timestamp_ms=fill_timestamps[0] if fill_timestamps else None,
        last_fill_timestamp_ms=fill_timestamps[-1] if fill_timestamps else None,
        terminal_liquidation_complete=terminal_liquidation_complete,
        terminal_residual_quantity=terminal_residual_quantity,
        terminal_residual_notional_usd=terminal_residual_notional,
        statistics=statistics,
    )
