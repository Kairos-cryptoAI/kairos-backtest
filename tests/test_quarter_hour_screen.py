from __future__ import annotations

import json
from pathlib import Path

import pytest

from kairos_backtest.quarter_hour_screen import (
    _atomic_write,
    _gate_failures,
    _sha256,
    expected_plan,
    load_preregistered_plan,
)


def _metrics(*, trades: int = 120, total_return: float = 0.02) -> dict[str, object]:
    return {
        "active_symbols": 5,
        "hac_sharpe": 0.5,
        "maximum_drawdown": 0.02,
        "per_symbol": [{"symbol": symbol, "trades": 24} for symbol in ("BTC", "ETH", "SOL", "BNB", "XRP")],
        "profit_factor": 1.2,
        "total_return": total_return,
        "trades": trades,
    }


def test_committed_plan_exactly_matches_the_executable_plan():
    root = Path(__file__).resolve().parents[1]
    path = root / "reports" / "quarter-hour-screen" / "plan.json"
    loaded = load_preregistered_plan(path)

    assert loaded == json.loads(path.read_text(encoding="utf-8"))
    assert _sha256(loaded) == "80cb424675a57a34cf195858a4742dfa891d819b177a1b83f3edd9114515a916"
    assert expected_plan()["permissions"] == {
        "alpha_ready": False,
        "live_allowed": False,
        "paper_allowed": False,
        "promotion_eligible": False,
    }


def test_plan_mutation_is_rejected_before_any_data_loader_exists(tmp_path):
    mutated = expected_plan()
    mutated["classification"] = "promotion"
    path = tmp_path / "plan.json"
    path.write_text(json.dumps(mutated), encoding="utf-8")

    with pytest.raises(ValueError, match="differs"):
        load_preregistered_plan(path)


def test_gates_require_both_selection_and_robustness_under_both_scenarios():
    passing = {
        name: {scenario: _metrics() for scenario in ("baseline", "stress")}
        for name in ("research", "selection", "robustness")
    }
    assert _gate_failures(passing) == ()

    failing = {
        name: {scenario: _metrics() for scenario in ("baseline", "stress")}
        for name in ("research", "selection", "robustness")
    }
    failing["robustness"]["stress"] = _metrics(trades=0, total_return=-0.01)
    failures = _gate_failures(failing)
    assert "robustness.stress.trades_below_minimum" in failures
    assert "robustness.stress.total_return_not_positive" in failures


def test_atomic_result_writer_never_overwrites(tmp_path):
    path = tmp_path / "summary.json"
    _atomic_write(path, {"classification": "REJECT"})
    first = path.read_bytes()

    with pytest.raises(FileExistsError, match="refusing to overwrite"):
        _atomic_write(path, {"classification": "MUTATED"})
    assert path.read_bytes() == first
