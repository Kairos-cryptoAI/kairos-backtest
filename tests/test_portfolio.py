from dataclasses import replace
from datetime import UTC, date, datetime, timedelta

import pytest
from kairos_core.enums import Side

from kairos_backtest.barrier_engine import ManagedPosition
from kairos_backtest.portfolio import CellEquityCurve, DailyCellSnapshot, synchronize_cells
from kairos_backtest.strategy_models import ExitPlan, ExitReason, SleeveIntent, TradeRecord

_START_MS = int(datetime(2026, 1, 1, tzinfo=UTC).timestamp() * 1_000)


def trade(sleeve: str, symbol: str, pnl: float, *, day_index: int) -> TradeRecord:
    entry = 100.0
    timestamp = _START_MS + day_index * 24 * 60 * 60 * 1_000
    intent = SleeveIntent(
        sleeve_id=sleeve,
        symbol=symbol,
        side=Side.LONG,
        decision_ts_ms=timestamp,
        entry_eligible_ts_ms=timestamp + 1,
        entry_expires_ts_ms=timestamp + 10,
        reference_price=entry,
        signal_strength=0.5,
        gross_reward_bps=2_000,
        exit_plan=ExitPlan(stop_price=90, target_price=120, max_holding_ms=60_000),
    )
    return TradeRecord(
        intent=intent,
        entry_timestamp_ms=timestamp + 1,
        exit_timestamp_ms=timestamp + 2,
        entry_price=entry,
        exit_price=entry + pnl,
        quantity=1,
        exit_reason=ExitReason.TIMEOUT,
    )


def curve(
    cell_id: str,
    sleeve: str,
    symbol: str,
    profits: tuple[float, ...],
) -> CellEquityCurve:
    start = date(2026, 1, 1)
    trades = tuple(trade(sleeve, symbol, profit, day_index=index) for index, profit in enumerate(profits))
    running = 0.0
    snapshots: list[DailyCellSnapshot] = []
    for index in range(2):
        if index < len(profits):
            running += profits[index]
        snapshots.append(DailyCellSnapshot(start + timedelta(days=index), 10_000 + running, running))
    return CellEquityCurve(
        cell_id=cell_id,
        sleeve_id=sleeve,
        symbol=symbol,
        snapshots=tuple(snapshots),
        initial_equity_usd=10_000,
        trades=trades,
    )


def test_synchronized_portfolio_sums_dollars_before_calculating_returns():
    winner = curve("trend-btc", "trend", "BTCUSDT", (100, 100))
    loser = curve("range-eth", "range", "ETHUSDT", (-50, -50))

    portfolio = synchronize_cells((winner, loser))

    assert portfolio.initial_equity_usd == 20_000
    assert portfolio.closing_equity_usd == (20_050, 20_100)
    assert portfolio.total_return == pytest.approx(0.005)
    assert portfolio.daily_returns.returns[0] == pytest.approx(0.0025)
    assert portfolio.trades == 4
    assert portfolio.active_sleeves == 2
    assert portfolio.active_symbols == 2
    assert portfolio.maximum_sleeve_profit_contribution == 1
    assert portfolio.maximum_symbol_profit_contribution == 1


def test_joint_drawdown_is_not_an_average_of_opposing_cell_paths():
    first = curve("a", "trend", "BTCUSDT", (10, -10))
    second = curve("b", "range", "ETHUSDT", (-10, 10))

    portfolio = synchronize_cells((first, second))

    assert portfolio.closing_equity_usd == (20_000, 20_000)
    assert portfolio.maximum_drawdown == 0


def test_daily_open_position_mark_is_recomputed_from_managed_state():
    intent = SleeveIntent(
        "trend",
        "BTCUSDT",
        Side.LONG,
        _START_MS,
        _START_MS,
        _START_MS,
        100,
        0.5,
        2_000,
        ExitPlan(90, 120, 2 * 24 * 60 * 60 * 1_000),
    )
    managed = ManagedPosition(
        intent,
        entry_timestamp_ms=_START_MS,
        entry_price=100,
        quantity=2,
        entry_fee_usd=1,
    )
    closed = TradeRecord(
        intent,
        _START_MS,
        _START_MS + 24 * 60 * 60 * 1_000 + 1,
        100,
        100,
        2,
        ExitReason.TIMEOUT,
        entry_fee_usd=1,
    )
    snapshots = (
        DailyCellSnapshot(date(2026, 1, 1), 10_019, 0, 110, managed.state),
        DailyCellSnapshot(date(2026, 1, 2), 9_999, -1),
    )

    cell = CellEquityCurve("trend-btc", "trend", "BTCUSDT", snapshots, 10_000, (closed,))

    assert cell.closing_equity_usd[0] == 10_019


def test_daily_path_cannot_be_changed_without_matching_trade_or_position_ledger():
    valid = curve("trend-btc", "trend", "BTCUSDT", (100,))
    forged = replace(valid.snapshots[0], closing_equity_usd=20_000)

    with pytest.raises(ValueError, match="ledger"):
        CellEquityCurve(
            valid.cell_id,
            valid.sleeve_id,
            valid.symbol,
            (forged, valid.snapshots[1]),
            valid.initial_equity_usd,
            valid.trades,
        )


def test_cell_rejects_mutable_evidence_wrong_trade_identity_and_open_terminal():
    valid = curve("trend-btc", "trend", "BTCUSDT", (100,))
    with pytest.raises(ValueError, match="immutable"):
        CellEquityCurve(
            valid.cell_id,
            valid.sleeve_id,
            valid.symbol,
            list(valid.snapshots),  # type: ignore[arg-type]
            valid.initial_equity_usd,
            valid.trades,
        )
    with pytest.raises(ValueError, match="declared"):
        CellEquityCurve(
            "bad",
            "range",
            "BTCUSDT",
            valid.snapshots,
            valid.initial_equity_usd,
            valid.trades,
        )

    intent = valid.trades[0].intent
    managed = ManagedPosition(
        intent,
        entry_timestamp_ms=intent.entry_eligible_ts_ms,
        entry_price=100,
        quantity=1,
    )
    terminal_open = replace(
        valid.snapshots[-1],
        closing_equity_usd=valid.initial_equity_usd + valid.trades[0].net_pnl_usd,
        mark_price=100,
        open_position=managed.state,
    )
    with pytest.raises(ValueError, match="closed trade|end flat"):
        CellEquityCurve(
            valid.cell_id,
            valid.sleeve_id,
            valid.symbol,
            (valid.snapshots[0], terminal_open),
            valid.initial_equity_usd,
            valid.trades,
        )


def test_portfolio_rejects_unsynchronized_duplicate_or_repeated_strategy_cells():
    first = curve("a", "trend", "BTCUSDT", (100,))
    shifted = replace(
        curve("b", "range", "ETHUSDT", ()),
        snapshots=(
            DailyCellSnapshot(date(2026, 1, 2), 10_000, 0),
            DailyCellSnapshot(date(2026, 1, 3), 10_000, 0),
        ),
    )

    with pytest.raises(ValueError, match="same"):
        synchronize_cells((first, shifted))
    with pytest.raises(ValueError, match="unique"):
        synchronize_cells((first, first))
    duplicate_cell = replace(first, cell_id="different")
    with pytest.raises(ValueError, match="sleeve × symbol"):
        synchronize_cells((first, duplicate_cell))


def test_active_sleeves_count_unique_strategy_ids_across_symbols():
    first = curve("trend-btc", "trend", "BTCUSDT", (100,))
    second = curve("trend-eth", "trend", "ETHUSDT", (50,))

    portfolio = synchronize_cells((first, second))

    assert portfolio.active_sleeves == 1
    assert portfolio.active_symbols == 2
    assert portfolio.maximum_symbol_profit_contribution == pytest.approx(2 / 3)
