"""Fail-closed compatibility evidence for the immutable quarter-hour V5 ledger.

V5 was started from a signed historical feature-source revision.  Later
operational repairs are allowed to resume that exact ledger only when this
module binds three independent facts together:

* the ledger still records V5's frozen source, plan, and schema identities;
* the caller is addressing the one named V5 runtime ledger; and
* the running feature implementation has the one reviewed compatibility
  digest below.

The reviewed allowlist deliberately lives outside the hashed runtime bundle;
the bundle itself includes this module plus ``aggtrades.py`` and
``quarter_hour_features.py``. Consequently, any edit to feature or
compatibility logic fails closed until a new reviewed allowlist record is
deliberately committed. This is not a general source-digest override for other
ledgers or future research lines.
"""

from __future__ import annotations

import sqlite3
from dataclasses import asdict, dataclass
from pathlib import Path

from .quarter_hour_v5_compatibility_allowlist import V5_COMPATIBLE_RUNTIME_SOURCE_SHA256

V5_COMPATIBILITY_SCHEMA_VERSION = "kairos.quarter-hour-v5-runtime-compatibility.v1"
V5_LEDGER_FILENAME = "quarter-hour-lag-features-v5.sqlite3"
V5_FROZEN_SOURCE_COMMIT = "55b9d20f15dedc5da070b81decdba016467d0ecc"
V5_FROZEN_FEATURE_SOURCE_SHA256 = "1df69cc8f73264e7fcaf1f9770219c63edba1dfcb64e24b8d6cc216910f15f0b"
V5_FROZEN_PLAN_SHA256 = "2c5d91f76dcf5fd2f8c5bcc1ccec1032fb56b967e131d6136fb9b437c86f425f"
V5_FROZEN_LEDGER_SCHEMA_VERSION = "kairos.quarter-hour-feature-ledger.v2"


class QuarterHourV5CompatibilityError(RuntimeError):
    """The current process is not authorised to resume the frozen V5 lineage."""


@dataclass(frozen=True)
class V5RuntimeCompatibilityEvidence:
    """Explicit provenance written with a V5-compatible collector result."""

    compatibility_schema_version: str
    frozen_feature_source_sha256: str
    frozen_plan_sha256: str
    frozen_source_commit: str
    ledger_path: str
    ledger_schema_version: str
    runtime_feature_source_sha256: str

    def to_dict(self) -> dict[str, str]:
        return asdict(self)


def _read_uri(path: Path) -> str:
    """Read without side effects while retaining committed WAL visibility."""
    wal = path.with_name(path.name + "-wal")
    suffix = "?mode=ro" if wal.is_file() else "?mode=ro&immutable=1"
    return path.resolve().as_uri() + suffix


def _read_metadata(path: Path) -> dict[str, str]:
    try:
        connection = sqlite3.connect(_read_uri(path), uri=True)
    except sqlite3.Error as exc:
        raise QuarterHourV5CompatibilityError("V5 ledger cannot be opened read-only") from exc
    try:
        connection.execute("PRAGMA query_only=ON")
        return {str(key): str(value) for key, value in connection.execute("SELECT key, value FROM metadata")}
    except sqlite3.Error as exc:
        raise QuarterHourV5CompatibilityError("V5 ledger metadata cannot be read") from exc
    finally:
        connection.close()


def require_v5_runtime_compatibility(
    *,
    ledger_path: Path,
    plan_sha256: str,
    runtime_feature_source_sha256: str,
) -> V5RuntimeCompatibilityEvidence:
    """Return verified V5 evidence or reject the resume before a ledger opens RW."""
    resolved_ledger = ledger_path.resolve()
    if resolved_ledger.name != V5_LEDGER_FILENAME:
        raise QuarterHourV5CompatibilityError(
            f"V5 compatibility is restricted to the named runtime ledger: {V5_LEDGER_FILENAME}"
        )
    if not resolved_ledger.is_file():
        raise QuarterHourV5CompatibilityError("V5 runtime ledger is missing")
    if plan_sha256 != V5_FROZEN_PLAN_SHA256:
        raise QuarterHourV5CompatibilityError("V5 plan_sha256 does not match the frozen V5 identity")
    if runtime_feature_source_sha256 != V5_COMPATIBLE_RUNTIME_SOURCE_SHA256:
        raise QuarterHourV5CompatibilityError(
            "current feature source is not the reviewed V5-compatible runtime digest"
        )

    metadata = _read_metadata(resolved_ledger)
    expected = {
        "feature_source_sha256": V5_FROZEN_FEATURE_SOURCE_SHA256,
        "plan_sha256": V5_FROZEN_PLAN_SHA256,
        "schema_version": V5_FROZEN_LEDGER_SCHEMA_VERSION,
    }
    for key, value in expected.items():
        if metadata.get(key) != value:
            raise QuarterHourV5CompatibilityError(f"V5 ledger {key} does not match the immutable V5 identity")
    return V5RuntimeCompatibilityEvidence(
        compatibility_schema_version=V5_COMPATIBILITY_SCHEMA_VERSION,
        frozen_feature_source_sha256=V5_FROZEN_FEATURE_SOURCE_SHA256,
        frozen_plan_sha256=V5_FROZEN_PLAN_SHA256,
        frozen_source_commit=V5_FROZEN_SOURCE_COMMIT,
        ledger_path=str(resolved_ledger),
        ledger_schema_version=V5_FROZEN_LEDGER_SCHEMA_VERSION,
        runtime_feature_source_sha256=runtime_feature_source_sha256,
    )
