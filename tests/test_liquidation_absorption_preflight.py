from __future__ import annotations

import json
from pathlib import Path

import pytest

from kairos_backtest import liquidation_absorption_preflight as preflight
from kairos_backtest.scenarios import SYMBOLS


def _write_plan(path: Path) -> Path:
    path.write_text(json.dumps(preflight.expected_plan(), indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def test_committed_plan_is_exact_and_performance_blind() -> None:
    root = Path(__file__).resolve().parents[1]
    plan = preflight.load_plan(root / preflight.PLAN_FILENAME)

    assert plan == preflight.expected_plan()
    assert plan["classification"] == preflight.CLASSIFICATION
    assert plan["permissions"] == {
        "alpha_ready": False,
        "live_allowed": False,
        "paper_allowed": False,
        "promotion_eligible": False,
    }
    assert plan["data"]["symbols"] == list(SYMBOLS)  # type: ignore[index]
    assert plan["protocol"]["historical_result_examined"] is False  # type: ignore[index]


def test_plan_mutation_fails_closed(tmp_path: Path) -> None:
    path = _write_plan(tmp_path / "plan.json")
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["data"]["collection_duration_days"] = 91
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(preflight.LiquidationAbsorptionPreflightError, match="differs"):
        preflight.load_plan(path)


def test_cli_only_verifies_static_contract(capsys: pytest.CaptureFixture[str]) -> None:
    root = Path(__file__).resolve().parents[1]

    assert preflight.main(["--plan", str(root / preflight.PLAN_FILENAME)]) == 0

    assert capsys.readouterr().out.splitlines() == [
        f"classification={preflight.CLASSIFICATION}",
        f"plan_logical_sha256={preflight.PLAN_LOGICAL_SHA256}",
        "network_access=false",
        "trading_access=false",
    ]
