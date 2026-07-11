from __future__ import annotations

import math
from dataclasses import dataclass
from statistics import fmean, pstdev


@dataclass(frozen=True, slots=True)
class PerformanceMetrics:
    total_return: float
    max_drawdown: float
    sharpe: float
    trades: int
    win_rate: float


def calculate_metrics(equity_curve: list[float], trade_pnls: list[float]) -> PerformanceMetrics:
    if not equity_curve or equity_curve[0] <= 0:
        raise ValueError("positive equity curve is required")
    returns = [
        current / previous - 1
        for previous, current in zip(equity_curve, equity_curve[1:], strict=False)
        if previous > 0
    ]
    peak = equity_curve[0]
    max_drawdown = 0.0
    for equity in equity_curve:
        peak = max(peak, equity)
        max_drawdown = max(max_drawdown, (peak - equity) / peak)
    volatility = pstdev(returns) if len(returns) > 1 else 0.0
    sharpe = fmean(returns) / volatility * math.sqrt(365 * 24 * 60) if volatility else 0.0
    wins = sum(pnl > 0 for pnl in trade_pnls)
    return PerformanceMetrics(
        total_return=equity_curve[-1] / equity_curve[0] - 1,
        max_drawdown=max_drawdown,
        sharpe=sharpe,
        trades=len(trade_pnls),
        win_rate=wins / len(trade_pnls) if trade_pnls else 0.0,
    )
