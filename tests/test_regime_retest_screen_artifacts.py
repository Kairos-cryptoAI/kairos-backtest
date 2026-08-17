from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]
REPORTS = ROOT / "reports" / "regime-retest-screen"


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _scenario(trial: dict[str, object], name: str) -> dict[str, object]:
    metrics = trial["metrics"]
    assert isinstance(metrics, list)
    return next(metric for metric in metrics if metric["scenario_name"] == name)


def test_committed_regime_retest_artifacts_are_internally_linked() -> None:
    plan_path = REPORTS / "plan.json"
    attempt_path = REPORTS / "attempt.json"
    summary_path = REPORTS / "summary.json"
    quality_path = REPORTS / "data-quality.json"
    report_path = REPORTS / "REPORT.md"

    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    attempt = json.loads(attempt_path.read_text(encoding="utf-8"))
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    quality = json.loads(quality_path.read_text(encoding="utf-8"))

    assert report_path.is_file()
    assert "Decision: **REJECT_ALL**" in report_path.read_text(encoding="utf-8")
    assert summary["classification"] == "development_diagnostics_only"
    assert summary["ranking"] == {
        "ranked_eligible_trial_ids": [],
        "selected_trial_id": "REJECT_ALL",
    }
    assert summary["permissions"] == {
        "live_allowed": False,
        "promotion_eligible": False,
        "shadow_allowed": False,
    }
    assert plan["permissions"] == summary["permissions"]
    assert summary["plan_sha256"] == plan["plan_sha256"] == attempt["plan_sha256"]

    assert _sha256(plan_path) == ("2eaf31ce1d3e2f3432c52fb0a5141b746308e7285bb1ac489ae403a6b71e58a4")
    assert _sha256(attempt_path) == ("b21238a3aca5abb6f72a6daade9d8527921340f15454421e55512f5a9927e397")
    assert _sha256(summary_path) == ("4906b8fdd5c5e34f1919991b8a2af733336c026ee3947d867432b235c7c25787")
    assert _sha256(quality_path) == ("89a360588ee63fa76ab23fba9182819b069b4a54365dd4264ac127cdffbb932b")

    assert attempt["status"] == "consumed"
    assert attempt["rerun_allowed"] is False
    assert attempt["crash_or_failure_releases_attempt"] is False
    assert attempt["plan_file"] == summary["plan_file"]
    assert summary["attempt"] == {
        "attempt_sha256": attempt["attempt_sha256"],
        "bytes": 983,
        "file_sha256": _sha256(attempt_path),
        "status": "consumed",
    }
    assert [row["lineage_trial_number"] for row in attempt["trial_lineage"]] == [7, 8, 9]

    assert plan["environment"]["source"]["git"] == {
        "dirty": False,
        "head_sha": "deba2568f3fbfde8c7dda75f36e74ac31d36cd29",
        "tree_sha": "37494ec536c1ee084a9107a369de44d61de60473",
    }
    assert summary["environment"] == plan["environment"]

    assert quality["overall_assessment"] == "ready_to_support_rejection_with_caveats"
    assert quality["archive_inventory"]["verified_sha256"] == 35
    assert quality["archive_inventory"]["verified_zip_crc"] == 35
    assert quality["screen_slice"]["rows_total"] == 1_533_600
    assert quality["screen_slice"]["missing_minutes"] == 0
    assert quality["screen_slice"]["duplicate_minutes"] == 0
    assert quality["screen_slice"]["invalid_rows"] == 0
    assert quality["screen_slice"]["zero_volume_rows"] == 0


def test_screen_rejects_all_three_trials_with_exact_frozen_metrics() -> None:
    summary = json.loads((REPORTS / "summary.json").read_text(encoding="utf-8"))
    trials = {trial["variant"]: trial for trial in summary["trials"]}

    assert list(trials) == [
        "structural_reclaim",
        "flow_reacceleration",
        "absorption_reclaim",
    ]
    assert all(not trial["eligible"] for trial in trials.values())

    structural = _scenario(trials["structural_reclaim"], "baseline")["combined"]
    assert structural["trades"] == 1
    assert structural["net_return"] == pytest.approx(-0.00015492170125352978, abs=1e-18)
    assert structural["reference_gross_pnl_usd"] == pytest.approx(-8.00041868451135, abs=1e-12)
    assert structural["expectancy_usd_per_trade"] == pytest.approx(-15.492170125352303, abs=1e-12)
    assert structural["profit_factor"] == 0.0

    assert _scenario(trials["structural_reclaim"], "stress")["combined"]["trades"] == 0
    for variant in ("flow_reacceleration", "absorption_reclaim"):
        for scenario_name in ("baseline", "stress"):
            combined = _scenario(trials[variant], scenario_name)["combined"]
            assert combined["trades"] == 0
            assert combined["net_return"] == 0.0


def test_generation_diagnostics_pin_the_signal_funnel() -> None:
    summary = json.loads((REPORTS / "summary.json").read_text(encoding="utf-8"))
    diagnostics = {trial["variant"]: trial["generation_diagnostics"] for trial in summary["trials"]}

    structural = diagnostics["structural_reclaim"]["aggregate_counters"]["total"]
    assert structural == {
        "armed_setups": 184,
        "boundary_failures": 59,
        "emitted_intents": 12,
        "expansion_rejects": 1931,
        "expiries": 37,
        "flow_mismatches": 0,
        "overextensions": 76,
        "pending_setups": 0,
        "regime_rejects": 39626,
        "risk_geometry_rejects": 0,
        "state_resets": 0,
        "structural_breakout_candidates": 41741,
        "structural_reclaims": 12,
    }
    assert diagnostics["flow_reacceleration"]["aggregate_counters"]["total"]["emitted_intents"] == 2
    assert diagnostics["absorption_reclaim"]["aggregate_counters"]["total"]["emitted_intents"] == 0


def test_full_replay_is_local_but_cryptographically_bound() -> None:
    summary = json.loads((REPORTS / "summary.json").read_text(encoding="utf-8"))
    full_result = summary["full_result"]
    result_path = REPORTS / "result.json"

    assert full_result == {
        "bytes": 289_890_537,
        "schema_version": 2,
        "sha256": "2d863d5bae54ec6e9fa5e6dae76711efd17f8a999d55c27787d12dff04cd5be1",
    }
    assert "reports/regime-retest-screen/result.json" in (ROOT / ".gitignore").read_text(encoding="utf-8")
    if result_path.exists():
        assert result_path.stat().st_size == full_result["bytes"]
        assert _sha256(result_path) == full_result["sha256"]
