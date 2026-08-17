from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]
REPORTS = ROOT / "reports" / "orderflow-screen"


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _scenario(trial: dict[str, object], name: str) -> dict[str, object]:
    metrics = trial["metrics"]
    assert isinstance(metrics, list)
    return next(metric for metric in metrics if metric["scenario_name"] == name)


def test_committed_orderflow_screen_artifacts_are_internally_linked() -> None:
    plan_path = REPORTS / "plan.json"
    summary_path = REPORTS / "summary.json"
    quality_path = REPORTS / "data-quality.json"
    report_path = REPORTS / "REPORT.md"

    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    quality = json.loads(quality_path.read_text(encoding="utf-8"))

    assert report_path.is_file()
    assert "Decision: **REJECT_ALL**" in report_path.read_text(encoding="utf-8")
    assert summary["classification"] == "development_diagnostics_only"
    assert summary["ranking"]["selected_trial_id"] == "REJECT_ALL"
    assert summary["permissions"] == {
        "live_allowed": False,
        "promotion_eligible": False,
        "shadow_allowed": False,
    }
    assert plan["permissions"] == summary["permissions"]
    assert summary["plan_sha256"] == plan["plan_sha256"]

    assert _sha256(plan_path) == ("e25b52e2fedf359815c82feb6f4ae6be34dce734217dd25bd16b5b5cd512279d")
    assert _sha256(summary_path) == ("af00dcdbe8be68c1ee93d6eae3c25045035b5537f4e3bbfff17169318c485007")
    assert _sha256(quality_path) == ("723c68de7b8640e70d266fafd39d648fa2492b4d6336965f20c0c109b65dfb0d")

    assert plan["environment"]["git"] == {
        "dirty": False,
        "head_sha": "0bf8fd82c62d819c5fce6170f158717ca7d01d91",
        "tree_sha": "05ca3ca9e784bb14cc073ac52b9968cc7f878b5c",
    }
    assert summary["environment"]["source_sha256"] == (
        "1a0b3104465945f8fa7975fe6f4fd41bdb43d49aac39e1804a5bcc054d332aba"
    )
    assert summary["environment"] == plan["environment"]

    assert quality["classification"] == "development_only_reused_research_data"
    assert quality["archive_inventory"]["verified_sha256"] == 40
    assert quality["archive_inventory"]["verified_zip_crc"] == 40
    assert quality["screen_slice"]["rows_total"] == 1_576_800
    assert quality["screen_slice"]["missing_minutes"] == 0
    assert quality["screen_slice"]["duplicate_minutes"] == 0
    assert quality["screen_slice"]["invalid_rows"] == 0
    assert quality["screen_slice"]["canonical_raw_slice_sha256"] == (
        "61c543bbdcc3aceceefcfa820b9513c3feb2fc61af1ca7a94241a6d58e16fadf"
    )


def test_screen_result_is_rejected_with_exact_frozen_metrics() -> None:
    summary = json.loads((REPORTS / "summary.json").read_text(encoding="utf-8"))
    trials = {trial["variant"]: trial for trial in summary["trials"]}

    assert list(trials) == ["impulse", "persistence", "flip_release"]
    assert all(not trial["eligible"] for trial in trials.values())

    expected = {
        ("impulse", "baseline"): (124, -0.014559143572043687),
        ("impulse", "stress"): (92, -0.012051565639263906),
        ("persistence", "baseline"): (387, -0.02900473720922081),
        ("persistence", "stress"): (301, -0.032539554101570944),
        ("flip_release", "baseline"): (80, -0.0066870356535935205),
        ("flip_release", "stress"): (60, -0.005589567170428467),
    }
    for (variant, scenario_name), (trades, net_return) in expected.items():
        scenario = _scenario(trials[variant], scenario_name)
        combined = scenario["combined"]
        assert combined["trades"] == trades
        assert combined["net_return"] == pytest.approx(net_return, abs=1e-15)
        assert combined["profit_factor"] < 1.0
        assert combined["expectancy_usd_per_trade"] < 0.0


def test_full_replay_is_local_but_cryptographically_bound() -> None:
    summary = json.loads((REPORTS / "summary.json").read_text(encoding="utf-8"))
    full_result = summary["full_result"]
    result_path = REPORTS / "result.json"

    assert full_result == {
        "bytes": 7_813_861,
        "schema_version": 1,
        "sha256": "d8237f5ed8501077e2406e61f2fa9f000073d71ef09e8375510be258a14fdc69",
    }
    assert "reports/orderflow-screen/result.json" in (ROOT / ".gitignore").read_text(encoding="utf-8")
    if result_path.exists():
        assert result_path.stat().st_size == full_result["bytes"]
        assert _sha256(result_path) == full_result["sha256"]
