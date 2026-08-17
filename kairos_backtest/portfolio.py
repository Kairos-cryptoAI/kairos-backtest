"""Synchronized portfolio evidence derived from trades and daily position state."""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta

from kairos_core.enums import Side

from .barrier_engine import ManagedPositionState
from .robustness import DailyReturnSeries
from .strategy_models import TradeRecord


def _trade_day(timestamp_ms: int) -> date:
    return datetime.fromtimestamp(timestamp_ms / 1_000, UTC).date()


def _position_equity_delta(state: ManagedPositionState, mark_price: float) -> float:
    direction = 1.0 if state.intent.side is Side.LONG else -1.0
    partial_realized = direction * (state.exit_notional_usd - state.exit_filled_quantity * state.entry_price)
    unrealized = direction * state.remaining_quantity * (mark_price - state.entry_price)
    return (
        partial_realized
        + unrealized
        - state.entry_fee_usd
        - state.exit_fee_usd
        - state.accumulated_carry_cost_usd
    )


@dataclass(frozen=True, slots=True)
class DailyCellSnapshot:
    """One complete UTC close with enough state to reproduce marked equity."""

    day: date
    closing_equity_usd: float
    cumulative_realized_pnl_usd: float
    mark_price: float | None = None
    open_position: ManagedPositionState | None = None

    def __post_init__(self) -> None:
        if type(self.day) is not date:
            raise TypeError("snapshot day must be a date value")
        values = (self.closing_equity_usd, self.cumulative_realized_pnl_usd)
        if any(isinstance(value, bool) or not math.isfinite(value) for value in values):
            raise ValueError("snapshot equity and realized PnL must be finite")
        if self.closing_equity_usd <= 0:
            raise ValueError("snapshot equity must be positive")
        if self.open_position is None:
            if self.mark_price is not None:
                raise ValueError("a flat snapshot cannot contain a mark price")
            return
        if not isinstance(self.open_position, ManagedPositionState):
            raise TypeError("open_position must be a ManagedPositionState")
        if self.open_position.closed or self.open_position.remaining_quantity <= 0:
            raise ValueError("daily open position state must be live with positive quantity")
        if self.mark_price is None or not math.isfinite(self.mark_price) or self.mark_price <= 0:
            raise ValueError("an open position requires a finite positive close mark")


@dataclass(frozen=True, slots=True)
class CellEquityCurve:
    """One frozen ``sleeve × symbol`` cell on complete, verified UTC closes."""

    cell_id: str
    sleeve_id: str
    symbol: str
    snapshots: tuple[DailyCellSnapshot, ...]
    initial_equity_usd: float
    trades: tuple[TradeRecord, ...]

    def __post_init__(self) -> None:
        identifiers = (self.cell_id, self.sleeve_id, self.symbol)
        if any(not isinstance(value, str) or not value or value != value.strip() for value in identifiers):
            raise ValueError("cell identifiers must be non-empty normalized strings")
        if self.symbol != self.symbol.upper():
            raise ValueError("cell symbol must be uppercase")
        if not isinstance(self.snapshots, tuple) or len(self.snapshots) < 2:
            raise ValueError("cell requires an immutable tuple of at least two daily snapshots")
        if any(not isinstance(snapshot, DailyCellSnapshot) for snapshot in self.snapshots):
            raise TypeError("snapshots must contain DailyCellSnapshot values")
        if any(
            current.day - previous.day != timedelta(days=1)
            for previous, current in zip(self.snapshots, self.snapshots[1:], strict=False)
        ):
            raise ValueError("cell snapshot days must be strictly increasing and contiguous")
        if (
            isinstance(self.initial_equity_usd, bool)
            or not math.isfinite(self.initial_equity_usd)
            or self.initial_equity_usd <= 0
        ):
            raise ValueError("initial equity must be finite and positive")
        if not isinstance(self.trades, tuple) or any(
            not isinstance(trade, TradeRecord) for trade in self.trades
        ):
            raise TypeError("cell trades must be an immutable tuple of TradeRecord values")
        if any(
            trade.intent.sleeve_id != self.sleeve_id or trade.intent.symbol != self.symbol
            for trade in self.trades
        ):
            raise ValueError("every trade must belong to the declared sleeve and symbol")
        first_day, last_day = self.snapshots[0].day, self.snapshots[-1].day
        if any(
            not first_day
            <= _trade_day(trade.entry_timestamp_ms)
            <= _trade_day(trade.exit_timestamp_ms)
            <= last_day
            for trade in self.trades
        ):
            raise ValueError("trade timestamps must fall inside the cell snapshot horizon")
        if len({trade.intent.intent_id for trade in self.trades}) != len(self.trades):
            raise ValueError("one cell cannot close the same intent more than once")

        trades_by_intent = {trade.intent.intent_id: trade for trade in self.trades}
        previous_state: ManagedPositionState | None = None

        for snapshot in self.snapshots:
            realized = sum(
                trade.net_pnl_usd
                for trade in self.trades
                if _trade_day(trade.exit_timestamp_ms) <= snapshot.day
            )
            tolerance = max(1e-8, self.initial_equity_usd * 1e-10)
            if not math.isclose(
                snapshot.cumulative_realized_pnl_usd,
                realized,
                rel_tol=1e-10,
                abs_tol=tolerance,
            ):
                raise ValueError("snapshot realized PnL is inconsistent with closed trades")
            open_delta = 0.0
            state = snapshot.open_position
            if state is not None:
                self._validate_open_state(state, snapshot.day)
                closed_match = trades_by_intent.get(state.intent.intent_id)
                if closed_match is not None and _trade_day(closed_match.exit_timestamp_ms) <= snapshot.day:
                    raise ValueError("an open intent cannot also appear as a closed trade")
                mark = snapshot.mark_price
                if mark is None:
                    raise RuntimeError("validated open snapshot lost its mark price")
                open_delta = _position_equity_delta(state, mark)
            expected_equity = self.initial_equity_usd + realized + open_delta
            if not math.isclose(
                snapshot.closing_equity_usd,
                expected_equity,
                rel_tol=1e-10,
                abs_tol=tolerance,
            ):
                raise ValueError("snapshot equity is inconsistent with its trade and position ledger")
            if previous_state is not None and (
                state is None or state.intent.intent_id != previous_state.intent.intent_id
            ):
                closed = trades_by_intent.get(previous_state.intent.intent_id)
                if closed is None or _trade_day(closed.exit_timestamp_ms) != snapshot.day:
                    raise ValueError("a daily open position cannot disappear without a closed trade")
            if state is not None and (
                previous_state is None or state.intent.intent_id != previous_state.intent.intent_id
            ):
                if _trade_day(state.entry_timestamp_ms) != snapshot.day:
                    raise ValueError("a newly observed daily position must enter on that UTC day")
            if (
                state is not None
                and previous_state is not None
                and state.intent.intent_id == previous_state.intent.intent_id
            ):
                self._validate_state_progression(previous_state, state)
            previous_state = state
        if self.snapshots[-1].open_position is not None:
            raise ValueError("portfolio evidence requires every cell to end flat")

    def _validate_open_state(self, state: ManagedPositionState, snapshot_day: date) -> None:
        if state.intent.sleeve_id != self.sleeve_id or state.intent.symbol != self.symbol:
            raise ValueError("daily position must belong to the declared sleeve and symbol")
        numeric = (
            state.entry_price,
            state.initial_quantity,
            state.remaining_quantity,
            state.entry_fee_usd,
            state.accumulated_carry_cost_usd,
            state.exit_filled_quantity,
            state.exit_notional_usd,
            state.exit_fee_usd,
        )
        if any(not math.isfinite(value) or value < 0 for value in numeric):
            raise ValueError("daily position accounting must be finite and non-negative")
        if state.entry_price <= 0 or state.initial_quantity <= 0 or state.remaining_quantity <= 0:
            raise ValueError("daily position entry and quantities must be positive")
        tolerance = max(1e-12, state.initial_quantity * 1e-12)
        if not math.isclose(
            state.exit_filled_quantity + state.remaining_quantity,
            state.initial_quantity,
            rel_tol=1e-12,
            abs_tol=tolerance,
        ):
            raise ValueError("daily partial-fill quantities are inconsistent")
        if state.exit_filled_quantity == 0 and (state.exit_notional_usd or state.exit_fee_usd):
            raise ValueError("daily exit costs/notional require an actual partial fill")
        if state.exit_filled_quantity > 0 and (not state.exit_pending or state.exit_notional_usd <= 0):
            raise ValueError("daily partial fills require an exit-pending state and notional")
        if _trade_day(state.entry_timestamp_ms) > snapshot_day:
            raise ValueError("daily position entry cannot occur after its snapshot")

    @staticmethod
    def _validate_state_progression(
        previous: ManagedPositionState,
        current: ManagedPositionState,
    ) -> None:
        immutable = (
            "entry_timestamp_ms",
            "entry_price",
            "initial_quantity",
            "entry_fee_usd",
        )
        if any(getattr(previous, name) != getattr(current, name) for name in immutable):
            raise ValueError("daily position entry identity cannot change")
        if (
            current.remaining_quantity > previous.remaining_quantity
            or current.exit_filled_quantity < previous.exit_filled_quantity
            or current.exit_notional_usd < previous.exit_notional_usd
            or current.exit_fee_usd < previous.exit_fee_usd
            or current.accumulated_carry_cost_usd < previous.accumulated_carry_cost_usd
        ):
            raise ValueError("daily position accounting must progress monotonically")

    @property
    def dates(self) -> tuple[date, ...]:
        return tuple(snapshot.day for snapshot in self.snapshots)

    @property
    def closing_equity_usd(self) -> tuple[float, ...]:
        return tuple(snapshot.closing_equity_usd for snapshot in self.snapshots)

    @property
    def net_profit_usd(self) -> float:
        return self.closing_equity_usd[-1] - self.initial_equity_usd

    @property
    def daily_returns(self) -> DailyReturnSeries:
        previous = self.initial_equity_usd
        returns: list[float] = []
        for equity in self.closing_equity_usd:
            returns.append(equity / previous - 1)
            previous = equity
        return DailyReturnSeries(self.dates, tuple(returns))


@dataclass(frozen=True, slots=True)
class PortfolioEvidence:
    dates: tuple[date, ...]
    closing_equity_usd: tuple[float, ...]
    initial_equity_usd: float
    daily_returns: DailyReturnSeries
    total_return: float
    maximum_drawdown: float
    trades: int
    active_sleeves: int
    active_symbols: int
    maximum_sleeve_profit_contribution: float
    maximum_symbol_profit_contribution: float


def _maximum_positive_contribution(values: dict[str, float]) -> float:
    positive = tuple(max(0.0, value) for value in values.values())
    total = sum(positive)
    return max(positive, default=0.0) / total if total > 0 else 1.0


def synchronize_cells(cells: tuple[CellEquityCurve, ...]) -> PortfolioEvidence:
    """Sum synchronous dollar equity first, then calculate portfolio ratios."""

    if not isinstance(cells, tuple) or not cells:
        raise ValueError("portfolio requires a non-empty tuple of cell equity curves")
    if any(not isinstance(cell, CellEquityCurve) for cell in cells):
        raise TypeError("portfolio cells must be CellEquityCurve values")
    if len({cell.cell_id for cell in cells}) != len(cells):
        raise ValueError("portfolio cell identifiers must be unique")
    if len({(cell.sleeve_id, cell.symbol) for cell in cells}) != len(cells):
        raise ValueError("portfolio sleeve × symbol cells must be unique")
    dates = cells[0].dates
    if any(cell.dates != dates for cell in cells[1:]):
        raise ValueError("portfolio cells must share the same complete UTC-day index")

    initial = sum(cell.initial_equity_usd for cell in cells)
    closing = tuple(sum(cell.closing_equity_usd[index] for cell in cells) for index in range(len(dates)))
    previous = initial
    returns: list[float] = []
    peak = initial
    maximum_drawdown = 0.0
    for equity in closing:
        if not math.isfinite(equity) or equity <= 0:
            raise ValueError("synchronized portfolio equity must remain finite and positive")
        returns.append(equity / previous - 1)
        previous = equity
        peak = max(peak, equity)
        maximum_drawdown = max(maximum_drawdown, (peak - equity) / peak)

    sleeve_profit: dict[str, float] = {}
    symbol_profit: dict[str, float] = {}
    for cell in cells:
        gross_positive = sum(max(0.0, trade.net_pnl_usd) for trade in cell.trades)
        sleeve_profit[cell.sleeve_id] = sleeve_profit.get(cell.sleeve_id, 0.0) + gross_positive
        symbol_profit[cell.symbol] = symbol_profit.get(cell.symbol, 0.0) + gross_positive
    return PortfolioEvidence(
        dates=dates,
        closing_equity_usd=closing,
        initial_equity_usd=initial,
        daily_returns=DailyReturnSeries(dates, tuple(returns)),
        total_return=closing[-1] / initial - 1,
        maximum_drawdown=maximum_drawdown,
        trades=sum(len(cell.trades) for cell in cells),
        active_sleeves=len({cell.sleeve_id for cell in cells if cell.trades}),
        active_symbols=len({cell.symbol for cell in cells if cell.trades}),
        maximum_sleeve_profit_contribution=_maximum_positive_contribution(sleeve_profit),
        maximum_symbol_profit_contribution=_maximum_positive_contribution(symbol_profit),
    )
