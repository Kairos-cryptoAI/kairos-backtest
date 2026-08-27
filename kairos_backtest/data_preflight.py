"""Performance-blind qualification of exact cached archive slices."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess  # nosec B404
import tempfile
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from .data import ArchiveFieldProfile, BinanceArchiveLoader, month_starts

SCHEMA_VERSION = "kairos.archive-slice-preflight.v1"


@dataclass(frozen=True, slots=True)
class ArchiveSliceRequirement:
    """One exact input slice and the fields a future model is allowed to use."""

    symbol: str
    start: date
    end: date
    field_profile: ArchiveFieldProfile
    purpose: str

    def __post_init__(self) -> None:
        if not self.symbol or self.symbol != self.symbol.strip().upper():
            raise ValueError("preflight symbol must be normalized uppercase")
        if self.start >= self.end:
            raise ValueError("preflight slice start must be before end")
        if not isinstance(self.field_profile, ArchiveFieldProfile):
            raise ValueError("preflight field_profile must be an ArchiveFieldProfile")
        if not self.purpose or self.purpose != self.purpose.strip():
            raise ValueError("preflight purpose must be normalized and non-empty")


@dataclass(frozen=True, slots=True)
class ArchiveSliceEvidence:
    symbol: str
    start: str
    end: str
    field_profile: str
    purpose: str
    rows: int
    files: int
    checksum_files_verified: int
    normalized_rows_sha256: str
    quarantined_optional_rows: int
    quarantined_optional_samples: tuple[str, ...]


def preflight_cached_slices(
    cache_dir: Path,
    requirements: tuple[ArchiveSliceRequirement, ...],
) -> tuple[ArchiveSliceEvidence, ...]:
    """Open no network and prove exact slices are usable before a trial is consumed."""

    identities = {(item.symbol, item.start, item.end, item.field_profile) for item in requirements}
    if not requirements or len(identities) != len(requirements):
        raise ValueError("preflight requires a non-empty unique requirement set")
    evidence: list[ArchiveSliceEvidence] = []
    for requirement in requirements:
        candles, manifest = BinanceArchiveLoader(
            cache_dir,
            allow_download=False,
            field_profile=requirement.field_profile,
        ).load(requirement.symbol, requirement.start, requirement.end)
        expected_rows = (requirement.end - requirement.start).days * 24 * 60
        expected_files = len(month_starts(requirement.start, requirement.end))
        expected_start_ms = int(
            datetime.combine(requirement.start, datetime.min.time(), UTC).timestamp() * 1_000
        )
        expected_end_ms = int(datetime.combine(requirement.end, datetime.min.time(), UTC).timestamp() * 1_000)
        if (
            len(candles) != expected_rows
            or manifest.rows != expected_rows
            or manifest.actual_start_ms != expected_start_ms
            or manifest.actual_end_ms != expected_end_ms - 1
            or manifest.gaps != 0
            or manifest.expected_files != expected_files
            or manifest.checksum_files_verified != expected_files
            or manifest.checksum_status != "official_sha256_verified"
            or manifest.field_profile != requirement.field_profile.value
        ):
            raise ValueError(
                f"preflight slice is incomplete or unverified: {requirement.symbol} "
                f"{requirement.start}..{requirement.end}"
            )
        if requirement.field_profile is ArchiveFieldProfile.FULL_KLINE and manifest.quarantined_optional_rows:
            raise ValueError("FULL_KLINE preflight cannot quarantine source fields")
        evidence.append(
            ArchiveSliceEvidence(
                symbol=requirement.symbol,
                start=requirement.start.isoformat(),
                end=requirement.end.isoformat(),
                field_profile=requirement.field_profile.value,
                purpose=requirement.purpose,
                rows=manifest.rows,
                files=len(manifest.files),
                checksum_files_verified=manifest.checksum_files_verified,
                normalized_rows_sha256=manifest.sha256,
                quarantined_optional_rows=manifest.quarantined_optional_rows,
                quarantined_optional_samples=manifest.quarantined_optional_samples,
            )
        )
    return tuple(evidence)


def _canonical_bytes(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def _sha256(value: object) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def load_preflight_plan(path: Path) -> tuple[dict[str, Any], tuple[ArchiveSliceRequirement, ...]]:
    """Load a strict data-only plan without accessing the archive cache."""

    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or set(payload) != {
        "classification",
        "permissions",
        "requirements",
        "schema_version",
    }:
        raise ValueError("preflight plan has unknown or missing top-level fields")
    if (
        payload["schema_version"] != SCHEMA_VERSION
        or payload["classification"] != "performance_blind_data_only"
        or payload["permissions"]
        != {
            "alpha_ready": False,
            "live_allowed": False,
            "paper_allowed": False,
            "promotion_eligible": False,
        }
        or not isinstance(payload["requirements"], list)
        or not payload["requirements"]
    ):
        raise ValueError("preflight plan safety boundary is invalid")
    requirements: list[ArchiveSliceRequirement] = []
    for raw in payload["requirements"]:
        if not isinstance(raw, Mapping) or set(raw) != {
            "end",
            "field_profile",
            "purpose",
            "start",
            "symbol",
        }:
            raise ValueError("preflight requirement has unknown or missing fields")
        if any(
            not isinstance(raw[field], str)
            for field in ("end", "field_profile", "purpose", "start", "symbol")
        ):
            raise ValueError("preflight requirement fields must be strings")
        try:
            requirement = ArchiveSliceRequirement(
                symbol=raw["symbol"],
                start=date.fromisoformat(raw["start"]),
                end=date.fromisoformat(raw["end"]),
                field_profile=ArchiveFieldProfile(raw["field_profile"]),
                purpose=raw["purpose"],
            )
        except (TypeError, ValueError) as exc:
            raise ValueError("preflight requirement is invalid") from exc
        requirements.append(requirement)
    identities = {(item.symbol, item.start, item.end, item.field_profile) for item in requirements}
    if len(identities) != len(requirements):
        raise ValueError("preflight requirements must be unique")
    return payload, tuple(requirements)


def _atomic_write(path: Path, payload: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = json.dumps(payload, indent=2, sort_keys=True, allow_nan=False).encode() + b"\n"
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _git(project_root: Path, *arguments: str) -> str:
    completed = subprocess.run(  # nosec B603
        ("git", *arguments),
        cwd=project_root,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    return completed.stdout.strip()


def run_preflight(*, cache_dir: Path, plan_path: Path, result_path: Path) -> dict[str, object]:
    if result_path.exists():
        raise FileExistsError(f"preflight result already exists: {result_path}")
    project_root = Path(__file__).resolve().parents[1]
    if _git(project_root, "status", "--porcelain=v1", "--untracked-files=all"):
        raise RuntimeError("data preflight requires a clean Git worktree")
    plan, requirements = load_preflight_plan(plan_path)
    evidence = preflight_cached_slices(cache_dir, requirements)
    result: dict[str, object] = {
        "classification": "DATA_PREFLIGHT_PASSED",
        "environment": {
            "data_loader_sha256": hashlib.sha256(
                (project_root / "kairos_backtest" / "data.py").read_bytes()
            ).hexdigest(),
            "git_head_sha": _git(project_root, "rev-parse", "HEAD"),
            "git_tree_sha": _git(project_root, "rev-parse", "HEAD^{tree}"),
            "preflight_source_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        },
        "evidence": [asdict(item) for item in evidence],
        "permissions": plan["permissions"],
        "plan_sha256": _sha256(plan),
        "result_schema_version": SCHEMA_VERSION,
    }
    result["result_sha256"] = _sha256(result)
    _atomic_write(result_path, result)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="Performance-blind exact-slice archive preflight")
    parser.add_argument("--cache", type=Path, default=Path("data/historical"))
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--result", type=Path)
    parser.add_argument("--verify-plan", action="store_true")
    arguments = parser.parse_args()
    plan, _ = load_preflight_plan(arguments.plan)
    if arguments.verify_plan:
        print(f"plan_sha256={_sha256(plan)}")
        return 0
    if arguments.result is None:
        parser.error("--result is required unless --verify-plan is used")
    result = run_preflight(
        cache_dir=arguments.cache,
        plan_path=arguments.plan,
        result_path=arguments.result,
    )
    print(f"classification={result['classification']}")
    print(f"result_sha256={result['result_sha256']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
