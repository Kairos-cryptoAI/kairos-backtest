from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from pathlib import Path
from typing import cast

ROOT = Path(__file__).resolve().parents[1]
PLAN_PATH = ROOT / "reports" / "quarter-hour-execution-overlay" / "plan.json"
PARENT_PLAN_PATH = ROOT / "reports" / "quarter-hour-lag-replication-v2" / "plan.json"
PLAN_SHA256 = "637e9240545f7dfcd10a21989bff761ce55ef4eed9f51d7eea4e6415af0ff073"
PARENT_PLAN_SHA256 = "2c5d91f76dcf5fd2f8c5bcc1ccec1032fb56b967e131d6136fb9b437c86f425f"


def _load(path: Path) -> dict[str, object]:
    value = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return value


def _logical_sha256(payload: object) -> str:
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def test_execution_overlay_plan_is_immutable_and_binds_parent() -> None:
    plan = _load(PLAN_PATH)
    parent = _load(PARENT_PLAN_PATH)

    assert _logical_sha256(plan) == PLAN_SHA256
    assert _logical_sha256(parent) == PARENT_PLAN_SHA256
    assert plan["model"]["parent_plan_sha256"] == PARENT_PLAN_SHA256  # type: ignore[index]
    assert plan["parent_precondition"]["required_classification"] == (  # type: ignore[index]
        "STATISTICAL_COMPONENT_CONFIRMED"
    )


def test_execution_overlay_cannot_manufacture_or_mutate_trade_intents() -> None:
    plan = _load(PLAN_PATH)
    overlay = plan["execution_overlay"]
    strategy = plan["strategy"]

    assert overlay["new_directional_intents"] == "forbidden"  # type: ignore[index]
    assert overlay["new_trades"] == "forbidden"  # type: ignore[index]
    assert overlay["base_intent_mutation"] == "forbidden"  # type: ignore[index]
    assert overlay["exit_plan_mutation"] == "forbidden"  # type: ignore[index]
    assert overlay["forecast_phase_offset_minutes"] == 0  # type: ignore[index]
    assert overlay["adverse_forecast_action"] == (  # type: ignore[index]
        "submit at T plus 10000 ms plus scenario latency"
    )
    assert strategy["base_id"] == "regime_aligned_right_tail_v1"  # type: ignore[index]
    assert strategy["candidate_role"] == "execution_timing_challenger_only"  # type: ignore[index]


def test_execution_overlay_is_fail_closed_for_permissions_and_trials() -> None:
    plan = _load(PLAN_PATH)
    permissions = cast(Mapping[str, object], plan["permissions"])

    assert set(plan["protocol"]["allowed_results"]) == {  # type: ignore[index]
        "NOT_RUN_PARENT_COMPONENT_REJECTED",
        "REJECT_EXECUTION_OVERLAY",
        "FORWARD_FREEZE_EXECUTION_OVERLAY",
    }
    assert plan["protocol"]["single_attempt"] is True  # type: ignore[index]
    assert plan["protocol"]["base_forward_campaign_must_remain_unchanged"] is True  # type: ignore[index]
    assert all(value is False for value in permissions.values())
