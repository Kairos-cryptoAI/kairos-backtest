from dataclasses import replace
from datetime import UTC, date, datetime, timedelta, timezone

import pytest

from kairos_backtest.research_protocol import (
    DEVELOPMENT_WINDOWS,
    DataRole,
    DataWindow,
    ResearchProtocol,
    ResearchPurpose,
)


def protocol(**changes: object) -> ResearchProtocol:
    values: dict[str, object] = {
        "protocol_name": "multi-strategy-v2-development",
        "universe": ("BTCUSDT", "ETHUSDT"),
        "windows": DEVELOPMENT_WINDOWS,
        "max_trials": 24,
        "maximum_holding_ms": 2 * 60 * 60 * 1000,
        "maximum_label_horizon_ms": 90 * 60 * 1000,
        "maximum_execution_latency_ms": 60_000,
        "warmup_ms": 35 * 24 * 60 * 60 * 1000,
    }
    values.update(changes)
    return ResearchProtocol(**values)  # type: ignore[arg-type]


def test_development_windows_are_reused_roles_not_a_holdout():
    candidate = protocol()

    assert [window.role for window in candidate.windows] == [
        DataRole.RESEARCH,
        DataRole.SELECTION,
        DataRole.ROBUSTNESS,
    ]
    assert candidate.assert_access("research", ResearchPurpose.FIT).start == date(2021, 7, 1)
    assert candidate.assert_access("selection", ResearchPurpose.SELECT).end == date(2025, 7, 1)
    assert candidate.assert_access("robustness", ResearchPurpose.DIAGNOSE).end == date(2026, 8, 1)
    with pytest.raises(PermissionError, match="cannot be used to promote"):
        candidate.assert_access("robustness", ResearchPurpose.PROMOTE)


def test_purge_covers_the_longest_outcome_horizon_plus_latency():
    candidate = protocol()
    assert candidate.purge_ms == 2 * 60 * 60 * 1000 + 60_000

    longer_label = replace(candidate, maximum_label_horizon_ms=3 * 60 * 60 * 1000)
    assert longer_label.purge_ms == 3 * 60 * 60 * 1000 + 60_000


def test_blind_window_can_be_preregistered_but_requires_freeze_and_explicit_unlock():
    blind = DataWindow("future-blind", date(2026, 9, 1), date(2027, 5, 1), DataRole.BLIND)
    preregistered = protocol(windows=DEVELOPMENT_WINDOWS + (blind,))
    with pytest.raises(PermissionError, match="locked"):
        preregistered.assert_access("future-blind", ResearchPurpose.PROMOTE)

    frozen = protocol(
        windows=DEVELOPMENT_WINDOWS + (blind,),
        candidate_commit="a" * 40,
        parameter_set_sha256="b" * 64,
        frozen_at=datetime(2026, 8, 31, 23, 59, tzinfo=UTC),
    )
    with pytest.raises(PermissionError, match="locked"):
        frozen.assert_access("future-blind", ResearchPurpose.PROMOTE)
    assert (
        frozen.assert_access(
            "future-blind",
            ResearchPurpose.PROMOTE,
            blind_authorized_at=datetime(2027, 5, 1, tzinfo=UTC),
        ).role
        is DataRole.BLIND
    )
    with pytest.raises(PermissionError, match="not completed"):
        frozen.assert_access(
            "future-blind",
            ResearchPurpose.PROMOTE,
            blind_authorized_at=datetime(2027, 4, 30, 23, 59, tzinfo=UTC),
        )
    with pytest.raises(ValueError, match="expressed in UTC"):
        frozen.assert_access(
            "future-blind",
            ResearchPurpose.PROMOTE,
            blind_authorized_at=datetime(2027, 5, 1),
        )


def test_protocol_rejects_overlap_unbounded_horizons_and_partial_freeze():
    overlap = DataWindow("overlap", date(2024, 1, 1), date(2024, 8, 1), DataRole.SELECTION)
    with pytest.raises(ValueError, match="overlap"):
        protocol(windows=(DEVELOPMENT_WINDOWS[0], overlap))
    with pytest.raises(ValueError, match="bounded and positive"):
        protocol(maximum_holding_ms=0)
    with pytest.raises(ValueError, match="complete or absent"):
        protocol(candidate_commit="a" * 40)
    with pytest.raises(ValueError, match="max_trials"):
        protocol(max_trials=True)
    with pytest.raises(ValueError, match="expressed in UTC"):
        protocol(
            candidate_commit="a" * 40,
            parameter_set_sha256="b" * 64,
            frozen_at=datetime(2026, 8, 31, 23, 59, tzinfo=timezone(timedelta(hours=3))),
        )
    with pytest.raises(ValueError, match="before every blind"):
        protocol(
            windows=DEVELOPMENT_WINDOWS
            + (DataWindow("future", date(2026, 9, 1), date(2027, 5, 1), DataRole.BLIND),),
            candidate_commit="a" * 40,
            parameter_set_sha256="b" * 64,
            frozen_at=datetime(2026, 9, 1, tzinfo=UTC),
        )


def test_protocol_fingerprint_is_stable_and_binds_trial_budget():
    first = protocol()
    second = protocol()

    assert first.fingerprint() == second.fingerprint()
    assert first.fingerprint() != replace(second, max_trials=25).fingerprint()


def test_preregistration_fingerprint_excludes_only_post_selection_freeze():
    blind = DataWindow("future-blind", date(2026, 9, 1), date(2027, 5, 1), DataRole.BLIND)
    first = protocol(
        windows=DEVELOPMENT_WINDOWS + (blind,),
        candidate_commit="a" * 40,
        parameter_set_sha256="b" * 64,
        frozen_at=datetime(2026, 8, 30, tzinfo=UTC),
    )
    second = replace(
        first,
        candidate_commit="c" * 40,
        parameter_set_sha256="d" * 64,
        frozen_at=datetime(2026, 8, 31, tzinfo=UTC),
    )

    assert first.preregistration_fingerprint() == second.preregistration_fingerprint()
    assert first.fingerprint() != second.fingerprint()
    assert first.preregistration_fingerprint() != replace(first, max_trials=25).preregistration_fingerprint()
