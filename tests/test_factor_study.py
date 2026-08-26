from __future__ import annotations

import json
import math
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from kairos_backtest.factor_data import (
    FactorDataset,
    FundingObservation,
    LeverageObservation,
    PremiumObservation,
)
from kairos_backtest.factor_study import (
    HOUR_MS,
    JoinedFactorHour,
    _atomic_write,
    _decisions,
    _diagnostics,
    _sha256,
    expected_plan,
    join_symbol,
    load_preregistered_plan,
)
from kairos_backtest.market_anatomy import HourBar


def _hour_ms(day: date = date(2024, 7, 1)) -> int:
    return int(datetime.combine(day, datetime.min.time(), UTC).timestamp() * 1_000)


def _price_hour(index: int, *, start_ms: int, growth: float = 0.002) -> HourBar:
    open_price = 100.0 * math.exp(growth * index)
    close_price = open_price * math.exp(growth)
    return HourBar(
        symbol="BTCUSDT",
        open_time_ms=start_ms + index * HOUR_MS,
        close_time_ms=start_ms + (index + 1) * HOUR_MS - 1,
        open=open_price,
        high=max(open_price, close_price) * 1.001,
        low=min(open_price, close_price) * 0.999,
        close=close_price,
        volume=1_000.0,
        quote_volume=100_000.0,
        taker_buy_volume=600.0,
        taker_buy_quote_volume=60_000.0,
    )


def _joined(
    *,
    count: int = 120,
    growth: float = 0.002,
    funding: float = 0.0002,
    premium: float = 0.0008,
    oi_growth: float = -0.003,
) -> tuple[JoinedFactorHour, ...]:
    start_ms = _hour_ms()
    return tuple(
        JoinedFactorHour(
            symbol="BTCUSDT",
            open_time_ms=start_ms + index * HOUR_MS,
            close_time_ms=start_ms + (index + 1) * HOUR_MS - 1,
            close=100.0 * math.exp(growth * (index + 1)),
            premium_close=premium,
            funding_rate=funding,
            funding_age_hours=1.0,
            open_interest_value=1_000_000.0 * math.exp(oi_growth * index),
        )
        for index in range(count)
    )


def _factor_dataset(
    funding: tuple[FundingObservation, ...],
    premium: tuple[PremiumObservation, ...],
    leverage: tuple[LeverageObservation, ...],
) -> FactorDataset:
    return FactorDataset(
        funding={"BTCUSDT": funding},
        premium={"BTCUSDT": premium},
        leverage={"BTCUSDT": leverage},
        audits=(),
        inventory_sha256="a" * 64,
    )


def _window(mean: float = 20.0, hit: float = 0.52) -> dict[str, object]:
    distribution = {"24h": {"observations": 2_000, "mean_bps": mean, "positive_rate": hit}}
    return {
        "aggregate": {
            "funding_contrarian": distribution,
            "premium_contrarian": distribution,
            "deleveraging_trend": distribution,
            "crowding_veto_trend": {
                "kept": distribution,
                "crowded": {"24h": {"observations": 500, "mean_bps": -1.0, "positive_rate": 0.45}},
                "retained_fraction": 0.8,
                "improvement_24h_bps": 6.0,
            },
        }
    }


def test_committed_plan_matches_executable_definition():
    root = Path(__file__).resolve().parents[1]
    plan = load_preregistered_plan(root / "reports" / "derivatives-state" / "plan.json")

    assert plan == expected_plan()
    assert _sha256(plan) == "4072f504942ffeb993fccaea7e26fad2d5e2459b33611b36c7f22ac5d00bb309"
    assert not any(plan["permissions"].values())  # type: ignore[union-attr]


def test_committed_result_and_summary_preserve_rejected_decision():
    root = Path(__file__).resolve().parents[1]
    report_root = root / "reports" / "derivatives-state"
    result = json.loads((report_root / "result.json").read_text(encoding="utf-8"))
    summary = json.loads((report_root / "summary.json").read_text(encoding="utf-8"))

    expected_result_hash = _sha256({key: value for key, value in result.items() if key != "result_sha256"})
    assert result["result_sha256"] == expected_result_hash
    assert summary["result_sha256"] == expected_result_hash
    assert summary["plan_sha256"] == result["plan_sha256"]
    assert summary["decision"] == result["decision"] == "NO_PROTOTYPE_PASSED_DESCRIPTIVE_GATES"
    assert not any(result["permissions"].values())
    assert all(
        item["decision"] == "INSUFFICIENT_DESCRIPTIVE_EDGE" for item in result["prototype_decisions"].values()
    )


def test_factor_join_is_causal_and_uses_last_observation_within_hour():
    start_ms = _hour_ms()
    prices = (_price_hour(0, start_ms=start_ms),)
    factors = _factor_dataset(
        (
            FundingObservation("BTCUSDT", start_ms + 30 * 60_000, 8, 0.0001),
            FundingObservation("BTCUSDT", start_ms + HOUR_MS, 8, 0.0009),
        ),
        (PremiumObservation("BTCUSDT", start_ms, start_ms + HOUR_MS - 1, -0.001, -0.0005, -0.0015, -0.0008),),
        (
            LeverageObservation("BTCUSDT", start_ms + 50 * 60_000, 100, 200, 1.1, 1.2, 1.0, 0.9),
            LeverageObservation("BTCUSDT", start_ms + 55 * 60_000, 110, 220, 1.2, 1.3, 1.1, 1.0),
            LeverageObservation("BTCUSDT", start_ms + 59 * 60_000, 0, 0, None, None, None, 1.0),
            LeverageObservation("BTCUSDT", start_ms + HOUR_MS, 999, 999, 9.0, 9.0, 9.0, 9.0),
        ),
    )

    joined = join_symbol(prices, factors, "BTCUSDT")

    assert len(joined) == 1
    assert joined[0].funding_rate == 0.0001
    assert joined[0].premium_close == -0.0008
    assert joined[0].open_interest_value == 220


def test_diagnostics_preserve_direction_and_never_bridge_gap():
    rising = _diagnostics(_joined())
    falling = _diagnostics(_joined(growth=-0.002))

    assert rising["funding_contrarian"]["24h"]["mean_bps"] < 0  # type: ignore[index]
    assert falling["funding_contrarian"]["24h"]["mean_bps"] > 0  # type: ignore[index]
    assert rising["deleveraging_trend"]["24h"]["mean_bps"] > 0  # type: ignore[index]

    complete = _joined()
    shifted = tuple(
        JoinedFactorHour(
            row.symbol,
            row.open_time_ms + HOUR_MS,
            row.close_time_ms + HOUR_MS,
            row.close,
            row.premium_close,
            row.funding_rate,
            row.funding_age_hours,
            row.open_interest_value,
        )
        for row in complete[60:]
    )
    with_gap = complete[:60] + shifted
    assert (
        _diagnostics(with_gap)["funding_contrarian"]["24h"]["observations"]  # type: ignore[index]
        < rising["funding_contrarian"]["24h"]["observations"]  # type: ignore[index]
    )


def test_descriptive_gates_require_both_windows_and_never_change_permissions():
    windows = {"selection": _window(), "robustness": _window()}
    passing = _decisions(windows)
    assert all("ELIGIBLE" in item["decision"] for item in passing.values())  # type: ignore[index]

    windows["robustness"] = _window(mean=-1.0, hit=0.49)
    failing = _decisions(windows)
    assert all(item["decision"] == "INSUFFICIENT_DESCRIPTIVE_EDGE" for item in failing.values())  # type: ignore[index]
    assert not any(expected_plan()["permissions"].values())  # type: ignore[union-attr]


def test_plan_mutation_and_result_overwrite_are_rejected(tmp_path):
    plan = expected_plan()
    plan["classification"] = "promotion"
    plan_path = tmp_path / "plan.json"
    plan_path.write_text(json.dumps(plan), encoding="utf-8")
    with pytest.raises(ValueError, match="differs"):
        load_preregistered_plan(plan_path)

    result_path = tmp_path / "result.json"
    _atomic_write(result_path, {"decision": "first"})
    first = result_path.read_bytes()
    with pytest.raises(FileExistsError, match="overwrite"):
        _atomic_write(result_path, {"decision": "second"})
    assert result_path.read_bytes() == first
