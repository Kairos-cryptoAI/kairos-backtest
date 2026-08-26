from __future__ import annotations

import json
import math
from pathlib import Path

import pytest
from kairos_strategy.candles import Candle

from kairos_backtest.market_anatomy import (
    HOUR_MS,
    HourBar,
    StudyData,
    SymbolDataAudit,
    _atomic_write,
    _diagnostics,
    _prototype_decisions,
    _sha256,
    aggregate_complete_hours,
    analyze,
    expected_plan,
    load_preregistered_plan,
)


def _minutes(symbol: str = "BTCUSDT", *, count: int = 60) -> list[Candle]:
    result = []
    for index in range(count):
        price = 100.0 + index / 100
        result.append(
            Candle(
                symbol=symbol,
                timeframe="1m",
                open_time_ms=index * 60_000,
                close_time_ms=(index + 1) * 60_000 - 1,
                open=price,
                high=price + 0.1,
                low=price - 0.1,
                close=price + 0.01,
                volume=10.0,
                quote_volume=price * 10,
                taker_buy_volume=6.0,
                taker_buy_quote_volume=price * 6,
            )
        )
    return result


def _hours(
    symbol: str,
    *,
    count: int = 120,
    growth: float = 0.002,
    start_ms: int = 0,
) -> tuple[HourBar, ...]:
    result = []
    price = 100.0
    for index in range(count):
        next_price = price * math.exp(growth)
        result.append(
            HourBar(
                symbol=symbol,
                open_time_ms=start_ms + index * HOUR_MS,
                close_time_ms=start_ms + (index + 1) * HOUR_MS - 1,
                open=price,
                high=max(price, next_price) * 1.0001,
                low=min(price, next_price) * 0.9999,
                close=next_price,
                volume=1_000.0,
                quote_volume=100_000.0,
                taker_buy_volume=650.0 if growth > 0 else 350.0,
                taker_buy_quote_volume=65_000.0 if growth > 0 else 35_000.0,
            )
        )
        price = next_price
    return tuple(result)


def _aggregate_window(mean: float = 30.0) -> dict[str, object]:
    return {
        "aggregate_families": {
            "regime_trend": {
                "4h": {"observations": 2_000, "mean_bps": mean, "positive_rate": 0.55},
                "24h": {"observations": 2_000, "mean_bps": mean, "positive_rate": 0.55},
            },
            "breakout_24h": {
                "4h": {"observations": 2_000, "mean_bps": mean, "positive_rate": 0.55},
                "24h": {"observations": 2_000, "mean_bps": mean, "positive_rate": 0.55},
            },
            "range_shock_reversion": {
                "4h": {"observations": 2_000, "mean_bps": mean, "positive_rate": 0.55},
                "24h": {"observations": 2_000, "mean_bps": mean, "positive_rate": 0.55},
            },
            "flow_alignment": {
                "1h": {"observations": 2_000, "mean_bps": mean, "positive_rate": 0.55},
                "4h": {"observations": 2_000, "mean_bps": mean, "positive_rate": 0.55},
            },
        }
    }


def test_committed_plan_matches_executable_definition():
    root = Path(__file__).resolve().parents[1]
    plan = load_preregistered_plan(root / "reports" / "market-anatomy" / "plan.json")

    assert plan == json.loads((root / "reports" / "market-anatomy" / "plan.json").read_text(encoding="utf-8"))
    assert _sha256(plan) == "07b6d8f4a79eec0781719d5777db88b31a00310a095f58267e71fad014e9c149"
    assert plan["permissions"] == {
        "alpha_ready": False,
        "live_allowed": False,
        "paper_allowed": False,
        "promotion_eligible": False,
    }


def test_plan_mutation_and_result_overwrite_are_rejected(tmp_path):
    plan = expected_plan()
    plan["classification"] = "promotion"
    path = tmp_path / "plan.json"
    path.write_text(json.dumps(plan), encoding="utf-8")
    with pytest.raises(ValueError, match="differs"):
        load_preregistered_plan(path)

    result_path = tmp_path / "result.json"
    _atomic_write(result_path, {"decision": "first"})
    first = result_path.read_bytes()
    with pytest.raises(FileExistsError, match="overwrite"):
        _atomic_write(result_path, {"decision": "second"})
    assert result_path.read_bytes() == first


def test_hourly_aggregation_requires_every_closed_minute():
    complete, dropped = aggregate_complete_hours(_minutes())
    assert dropped == 0
    assert len(complete) == 1
    assert complete[0].open == 100.0
    assert complete[0].close == pytest.approx(100.6)
    assert complete[0].volume == 600.0

    incomplete, dropped = aggregate_complete_hours(_minutes(count=59))
    assert incomplete == ()
    assert dropped == 1


def test_directional_diagnostics_preserve_signed_outcomes():
    rising = _diagnostics(_hours("BTCUSDT", growth=0.002))
    falling = _diagnostics(_hours("BTCUSDT", growth=-0.002))

    for diagnostics in (rising, falling):
        assert diagnostics["regime_trend"]["1h"]["mean_bps"] > 0  # type: ignore[index]
        assert diagnostics["breakout_24h"]["4h"]["mean_bps"] > 0  # type: ignore[index]
        assert diagnostics["flow_alignment"]["1h"]["mean_bps"] > 0  # type: ignore[index]
        assert diagnostics["continuation"]["24h_to_4h"]["mean_bps"] > 0  # type: ignore[index]


def test_diagnostics_never_bridge_an_hourly_gap():
    bars = list(_hours("BTCUSDT", count=120))
    shifted = [
        HourBar(
            symbol=bar.symbol,
            open_time_ms=bar.open_time_ms + HOUR_MS,
            close_time_ms=bar.close_time_ms + HOUR_MS,
            open=bar.open,
            high=bar.high,
            low=bar.low,
            close=bar.close,
            volume=bar.volume,
            quote_volume=bar.quote_volume,
            taker_buy_volume=bar.taker_buy_volume,
            taker_buy_quote_volume=bar.taker_buy_quote_volume,
        )
        for bar in bars[60:]
    ]
    with_gap = tuple(bars[:60] + shifted)

    complete_count = _diagnostics(tuple(bars))["regime_trend"]["1h"]["observations"]  # type: ignore[index]
    gap_count = _diagnostics(with_gap)["regime_trend"]["1h"]["observations"]  # type: ignore[index]
    assert gap_count < complete_count


def test_prototype_gate_requires_both_later_reused_windows():
    windows = {
        "research": _aggregate_window(),
        "selection": _aggregate_window(),
        "robustness": _aggregate_window(),
    }
    passing = _prototype_decisions(windows)
    assert all(item["decision"] == "PROTOTYPE_ELIGIBLE" for item in passing.values())  # type: ignore[index]

    windows["robustness"] = _aggregate_window(mean=-1.0)
    failing = _prototype_decisions(windows)
    assert all(
        item["decision"] == "INSUFFICIENT_DESCRIPTIVE_EDGE"
        for item in failing.values()  # type: ignore[index]
    )
    assert all(item["failures"] for item in failing.values())  # type: ignore[index]


def test_analysis_is_descriptive_and_never_grants_trading_permissions(monkeypatch):
    import kairos_backtest.market_anatomy as module

    monkeypatch.setattr(
        module,
        "WINDOWS",
        (
            ("research", date_from_ms(0), date_from_ms(120 * HOUR_MS)),
            ("selection", date_from_ms(0), date_from_ms(120 * HOUR_MS)),
            ("robustness", date_from_ms(0), date_from_ms(120 * HOUR_MS)),
        ),
    )
    bars = {symbol: _hours(symbol) for symbol in module.SYMBOLS}
    audits = tuple(
        SymbolDataAudit(symbol, 1, 1, 7_200, 0, 120, 0, 0, 0, 119 * HOUR_MS) for symbol in module.SYMBOLS
    )
    result = analyze(StudyData(bars, audits, "a" * 64), expected_plan())

    assert result["classification"] == "descriptive_reused_data_only"
    assert not any(result["permissions"].values())  # type: ignore[union-attr]


def date_from_ms(milliseconds: int):
    from datetime import UTC, datetime

    return datetime.fromtimestamp(milliseconds / 1_000, UTC).date()
