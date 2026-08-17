from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class PerformanceMetrics:
    total_return: float
    max_drawdown: float
    sharpe: float
    trades: int
    win_rate: float
    profit_factor: float | None = None
    expectancy: float = 0.0
    annualized_return: float = 0.0
    annualized_volatility: float = 0.0
    sortino: float = 0.0
    calmar: float | None = None


@dataclass(frozen=True, slots=True)
class MetricStatistics:
    periods: int
    return_sum: float
    return_squares_sum: float
    downside_squares_sum: float
    peak_equity: float
    minimum_equity: float
    max_drawdown: float
    gross_profit: float
    gross_loss: float
    total_trade_pnl: float
    trades: int
    wins: int


def calculate_statistics(equity_curve: list[float], trade_pnls: list[float]) -> MetricStatistics:
    if not equity_curve or any(not math.isfinite(equity) or equity <= 0 for equity in equity_curve):
        raise ValueError("equity curve must contain only finite positive values")
    if any(not math.isfinite(pnl) for pnl in trade_pnls):
        raise ValueError("trade PnL values must be finite")
    returns = [
        current / previous - 1 for previous, current in zip(equity_curve, equity_curve[1:], strict=False)
    ]
    peak = equity_curve[0]
    max_drawdown = 0.0
    for equity in equity_curve:
        peak = max(peak, equity)
        max_drawdown = max(max_drawdown, (peak - equity) / peak)
    return MetricStatistics(
        periods=len(returns),
        return_sum=sum(returns),
        return_squares_sum=sum(value * value for value in returns),
        downside_squares_sum=sum(min(value, 0.0) ** 2 for value in returns),
        peak_equity=max(equity_curve),
        minimum_equity=min(equity_curve),
        max_drawdown=max_drawdown,
        gross_profit=sum(max(pnl, 0.0) for pnl in trade_pnls),
        gross_loss=-sum(min(pnl, 0.0) for pnl in trade_pnls),
        total_trade_pnl=sum(trade_pnls),
        trades=len(trade_pnls),
        wins=sum(pnl > 0 for pnl in trade_pnls),
    )


def metrics_from_statistics(
    statistics: MetricStatistics,
    *,
    initial_equity: float,
    final_equity: float,
    periods_per_year: int = 365 * 24 * 60,
) -> PerformanceMetrics:
    statistic_values = (
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
    if (
        not math.isfinite(initial_equity)
        or not math.isfinite(final_equity)
        or initial_equity <= 0
        or final_equity <= 0
        or isinstance(periods_per_year, bool)
        or not isinstance(periods_per_year, int)
        or periods_per_year <= 0
        or any(not math.isfinite(value) for value in statistic_values)
        or statistics.periods < 0
        or statistics.trades < 0
        or not 0 <= statistics.wins <= statistics.trades
    ):
        raise ValueError("valid equity and annualization period are required")
    count = statistics.periods
    mean_return = statistics.return_sum / count if count else 0.0
    variance = max(0.0, statistics.return_squares_sum / count - mean_return**2) if count else 0.0
    volatility = math.sqrt(variance)
    annualized_volatility = volatility * math.sqrt(periods_per_year)
    sharpe = mean_return / volatility * math.sqrt(periods_per_year) if volatility else 0.0
    downside_deviation = math.sqrt(statistics.downside_squares_sum / count) if count else 0.0
    sortino = mean_return / downside_deviation * math.sqrt(periods_per_year) if downside_deviation else 0.0
    growth = final_equity / initial_equity
    exponent = math.log(growth) * periods_per_year / count if growth > 0 and count else float("-inf")
    annualized_return = math.exp(min(exponent, 700.0)) - 1 if growth > 0 and count else -1.0
    profit_factor = statistics.gross_profit / statistics.gross_loss if statistics.gross_loss else None
    return PerformanceMetrics(
        total_return=growth - 1,
        max_drawdown=statistics.max_drawdown,
        sharpe=sharpe,
        trades=statistics.trades,
        win_rate=statistics.wins / statistics.trades if statistics.trades else 0.0,
        profit_factor=profit_factor,
        expectancy=(statistics.total_trade_pnl / statistics.trades if statistics.trades else 0.0),
        annualized_return=annualized_return,
        annualized_volatility=annualized_volatility,
        sortino=sortino,
        calmar=annualized_return / statistics.max_drawdown if statistics.max_drawdown else None,
    )


def calculate_metrics(
    equity_curve: list[float],
    trade_pnls: list[float],
    *,
    periods_per_year: int = 365 * 24 * 60,
) -> PerformanceMetrics:
    statistics = calculate_statistics(equity_curve, trade_pnls)
    return metrics_from_statistics(
        statistics,
        initial_equity=equity_curve[0],
        final_equity=equity_curve[-1],
        periods_per_year=periods_per_year,
    )
