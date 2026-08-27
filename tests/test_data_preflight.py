from __future__ import annotations

import hashlib
import io
import zipfile
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from kairos_backtest.data import ArchiveFieldProfile
from kairos_backtest.data_preflight import (
    ArchiveSliceRequirement,
    _sha256,
    load_preflight_plan,
    preflight_cached_slices,
)


def _day_archive(*, anomalous_taker_row: int | None = None, missing_row: int | None = None) -> bytes:
    opened = int(datetime(2025, 1, 1, tzinfo=UTC).timestamp() * 1_000)
    lines: list[str] = []
    for index in range(1_440):
        if index == missing_row:
            continue
        taker_volume = 2 if index == anomalous_taker_row else 0.5
        lines.append(
            f"{opened + index * 60_000},100,101,99,100,1,"
            f"{opened + index * 60_000 + 59_999},100,1,{taker_volume},50,0"
        )
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("BTCUSDT-1m-2025-01.csv", "\n".join(lines))
    return output.getvalue()


def _cache_archive(tmp_path, payload: bytes) -> None:
    target = tmp_path / "BTCUSDT" / "1m" / "BTCUSDT-1m-2025-01.zip"
    target.parent.mkdir(parents=True)
    target.write_bytes(payload)
    target.with_name(f"{target.name}.CHECKSUM").write_text(
        f"{hashlib.sha256(payload).hexdigest()}  {target.name}\n",
        encoding="ascii",
    )


def _requirement(profile: ArchiveFieldProfile) -> ArchiveSliceRequirement:
    return ArchiveSliceRequirement(
        symbol="BTCUSDT",
        start=date(2025, 1, 1),
        end=date(2025, 1, 2),
        field_profile=profile,
        purpose="frozen_candidate_warmup_and_evaluation",
    )


def test_committed_plan_is_data_only_and_has_stable_identity():
    root = Path(__file__).resolve().parents[1]

    plan, requirements = load_preflight_plan(root / "reports" / "data-field-preflight" / "plan.json")

    assert len(requirements) == 10
    assert {item.field_profile for item in requirements} == {ArchiveFieldProfile.PRICE_VOLUME}
    assert plan["permissions"] == {
        "alpha_ready": False,
        "live_allowed": False,
        "paper_allowed": False,
        "promotion_eligible": False,
    }
    assert _sha256(plan) == "61f99ec85b4a2f1f4fb7f4cc2903228197532919148db126b638a82a471dd013"


def test_price_volume_preflight_quarantines_unused_taker_fields_with_evidence(tmp_path):
    _cache_archive(tmp_path, _day_archive(anomalous_taker_row=100))

    evidence = preflight_cached_slices(
        tmp_path,
        (_requirement(ArchiveFieldProfile.PRICE_VOLUME),),
    )

    assert len(evidence) == 1
    assert evidence[0].rows == 1_440
    assert evidence[0].checksum_files_verified == 1
    assert evidence[0].field_profile == "price_volume"
    assert evidence[0].quarantined_optional_rows == 1
    assert "taker-buy volume exceeds total volume" in evidence[0].quarantined_optional_samples[0]


def test_full_kline_preflight_fails_on_the_same_optional_field_anomaly(tmp_path):
    _cache_archive(tmp_path, _day_archive(anomalous_taker_row=100))

    with pytest.raises(ValueError, match="taker-buy volume exceeds total volume"):
        preflight_cached_slices(
            tmp_path,
            (_requirement(ArchiveFieldProfile.FULL_KLINE),),
        )


def test_preflight_rejects_a_gap_before_any_research_attempt(tmp_path):
    _cache_archive(tmp_path, _day_archive(missing_row=100))

    with pytest.raises(ValueError, match="incomplete or unverified"):
        preflight_cached_slices(
            tmp_path,
            (_requirement(ArchiveFieldProfile.PRICE_VOLUME),),
        )


def test_requirement_rejects_ambiguous_identity():
    with pytest.raises(ValueError, match="normalized uppercase"):
        ArchiveSliceRequirement(
            symbol="btcusdt",
            start=date(2025, 1, 1),
            end=date(2025, 1, 2),
            field_profile=ArchiveFieldProfile.PRICE_VOLUME,
            purpose="x",
        )
    with pytest.raises(ValueError, match="before end"):
        ArchiveSliceRequirement(
            symbol="BTCUSDT",
            start=date(2025, 1, 2),
            end=date(2025, 1, 2),
            field_profile=ArchiveFieldProfile.PRICE_VOLUME,
            purpose="x",
        )


def test_preflight_rejects_duplicate_slice_even_if_purpose_differs(tmp_path):
    first = _requirement(ArchiveFieldProfile.PRICE_VOLUME)
    duplicate = ArchiveSliceRequirement(
        symbol=first.symbol,
        start=first.start,
        end=first.end,
        field_profile=first.field_profile,
        purpose="different_label_same_data_slice",
    )

    with pytest.raises(ValueError, match="unique requirement"):
        preflight_cached_slices(tmp_path, (first, duplicate))
