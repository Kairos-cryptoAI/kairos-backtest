from __future__ import annotations

import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).parents[1]
REPORTS = ROOT / "reports" / "development-screen"


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_committed_development_screen_artifacts_are_internally_linked():
    plan_path = REPORTS / "plan.json"
    summary_path = REPORTS / "summary.json"
    report_path = REPORTS / "REPORT.md"

    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    summary = json.loads(summary_path.read_text(encoding="utf-8"))

    assert report_path.is_file()
    assert summary["classification"] == "development_diagnostics_only"
    assert summary["decision"] == "REJECT_ALL"
    assert summary["permissions"] == {
        "promotion_eligible": False,
        "shadow_allowed": False,
        "live_allowed": False,
    }
    assert summary["artifacts"]["plan_file_sha256"] == _sha256(plan_path)
    assert summary["artifacts"]["plan_file_bytes"] == plan_path.stat().st_size
    assert summary["artifacts"]["plan_internal_sha256"] == plan["plan_sha256"]
    assert summary["environment"]["source_sha256"] == plan["environment"]["source_sha256"]
    assert summary["environment"]["environment_sha256"] == plan["environment"]["environment_sha256"]
    assert plan["permissions"] == summary["permissions"]
    assert [trial["variant"] for trial in summary["trials"]] == [
        "shallow",
        "medium",
        "deep",
    ]
    assert all(not trial["eligible"] for trial in summary["trials"])


def test_full_replay_evidence_is_deliberately_not_a_committed_fixture():
    summary = json.loads((REPORTS / "summary.json").read_text(encoding="utf-8"))

    assert summary["artifacts"]["full_result_committed"] is False
    assert summary["artifacts"]["full_result_file_bytes"] > 80_000_000
    assert len(summary["artifacts"]["full_result_file_sha256"]) == 64
