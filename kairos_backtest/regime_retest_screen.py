"""One-shot offline development screen for causal regime-veto retest candidates.

The screen is deliberately fail-closed.  It publishes an immutable plan before
the first parsed market value is read, then consumes the complete three-trial
attempt in an append-only ledger immediately before cache parsing.  A crash is
therefore evidence of a consumed attempt, never permission to rerun it.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib
import importlib.metadata
import json
import math
import os
import subprocess  # nosec B404
import tomllib
from collections import Counter
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from datetime import UTC, date, datetime, timedelta
from enum import Enum
from pathlib import Path
from types import MappingProxyType
from typing import cast

from kairos_core.enums import Side
from kairos_quant.candles import Candle

from .data import BinanceArchiveLoader, DatasetManifest, month_starts
from .managed_evaluation import IntentDispositionReason
from .orderflow_screen import OrderFlowScreenEnvironment
from .orderflow_screen import _assert_environment_stable as _assert_orderflow_environment_stable
from .portfolio import PortfolioEvidence, synchronize_cells
from .provenance import source_fingerprint
from .regime_retest_campaign import (
    DEFAULT_REGIME_RETEST_PROTOCOL,
    DEFAULT_REGIME_RETEST_SEED,
    REGIME_RETEST_EVALUATION_END,
    REGIME_RETEST_EVALUATION_START,
    REGIME_RETEST_GENERATION_START,
    REGIME_RETEST_INITIAL_EQUITY_USD,
    REGIME_RETEST_OPERATIONAL_HORIZON_MS,
    REGIME_RETEST_SLEEVE_ID,
    REGIME_RETEST_WINDOW_RATIONALE,
    RegimeRetestCampaignEvidence,
    RegimeRetestCandidate,
    RegimeRetestCellEvidence,
    regime_retest_scenarios,
    run_regime_retest_campaign,
)
from .research_protocol import DataRole, ResearchProtocol, ResearchPurpose
from .robustness import hac_sharpe
from .scenarios import SYMBOLS
from .sleeves.regime_retest_reclaim import (
    RegimeRetestGenerationCounters,
    RegimeRetestGenerationEvidence,
    RegimeRetestReclaimVariant,
    RegimeVetoRetestReclaimConfig,
)
from .strategy_models import TradeRecord

RegimeRetestVariant = RegimeRetestReclaimVariant

PLAN_VERSION = "kairos.regime-retest-development-screen.v2"
RESULT_SCHEMA_VERSION = 2
ATTEMPT_SCHEMA_VERSION = 1
GENERATION_DIAGNOSTICS_SCHEMA_VERSION = 1
CLASSIFICATION = "development_diagnostics_only"
REJECT_ALL = "REJECT_ALL"
WINDOW_NAME = "research"
GENERATION_START = REGIME_RETEST_GENERATION_START
EVALUATION_START = REGIME_RETEST_EVALUATION_START
EVALUATION_END = REGIME_RETEST_EVALUATION_END
WARMUP_DAYS = (EVALUATION_START - GENERATION_START).days
BASE_SEED = DEFAULT_REGIME_RETEST_SEED
INITIAL_EQUITY_USD = float(REGIME_RETEST_INITIAL_EQUITY_USD)
OPERATIONAL_HORIZON_MS = REGIME_RETEST_OPERATIONAL_HORIZON_MS
WINDOW_RATIONALE = REGIME_RETEST_WINDOW_RATIONALE
DECISION_CUTOFF_EXCLUSIVE = datetime.combine(EVALUATION_END, datetime.min.time(), UTC) - timedelta(
    milliseconds=OPERATIONAL_HORIZON_MS
)
SCENARIO_NAMES = ("baseline", "stress")
_CANONICAL_PROJECT_ROOT = Path(__file__).resolve().parents[1]
_CANONICAL_OUTPUT_DIRECTORY = Path("reports/regime-retest-screen")
_CANONICAL_OUTPUT_FILENAMES = ("plan.json", "attempt.json", "result.json", "summary.json")
EXPECTED_REPOSITORY_IDENTITY = "Kairos-cryptoAI/kairos-backtest"
EXPECTED_REPOSITORY_REMOTE_URL = "https://github.com/Kairos-cryptoAI/kairos-backtest.git"
EXPECTED_REPOSITORY_BRANCH = "main"
EXPECTED_DEPENDENCY_SOURCES = (
    (
        "kairos-core",
        "0.2.0",
        "https://github.com/Kairos-cryptoAI/kairos-core.git",
        "c2b9ba192521f9843b245e1eae8a501d408a6bfa",
    ),
    (
        "kairos-quant-scouts",
        "0.1.0",
        "https://github.com/Kairos-cryptoAI/kairos-quant-scouts.git",
        "c74b9853bd97597b2104b2d9c4bcd5b7c6cefb24",
    ),
)
EXPECTED_DEPENDENCY_IMPORTS = {
    "kairos-core": ("kairos_core", "49a08e329fd76a3cb2cd690364f0d4b85346a2fd4a85d1231ced2102ee060a2f", 22),
    "kairos-quant-scouts": (
        "kairos_quant",
        "483cb66afef2df138552505a08214857befc3d5affc5bb296ed399c90daea3e3",
        12,
    ),
}
EXPECTED_MONTHS = tuple(f"{month:%Y-%m}" for month in month_starts(GENERATION_START, EVALUATION_END))
EXPECTED_MONTHS_PER_SYMBOL = len(EXPECTED_MONTHS)
EXPECTED_ARCHIVES = len(SYMBOLS) * EXPECTED_MONTHS_PER_SYMBOL
EXPECTED_ROWS_PER_SYMBOL = (EVALUATION_END - GENERATION_START).days * 24 * 60
EXPECTED_WARMUP_ROWS_PER_SYMBOL = (EVALUATION_START - GENERATION_START).days * 24 * 60
EXPECTED_EVALUATION_ROWS_PER_SYMBOL = (EVALUATION_END - EVALUATION_START).days * 24 * 60

MINIMUM_CLOSED_TRADES_PER_SCENARIO = 165
MINIMUM_CLOSED_TRADES_PER_SYMBOL = 17
MINIMUM_DISTINCT_UTC_EXIT_DAYS = 50
MINIMUM_POSITIVE_EXPECTANCY_SYMBOLS = 3
MAXIMUM_ONE_SYMBOL_TRADE_SHARE = 0.50
MINIMUM_DIRECTION_CLOSED_TRADES = 50
MINIMUM_STRESS_TRADE_RETENTION = 0.65
MAXIMUM_STRESS_DRAWDOWN = 0.05

_TRIAL_DEFINITIONS = (
    (7, "STRUCTURAL", RegimeRetestVariant.STRUCTURAL_RECLAIM),
    (8, "FLOW_REACCELERATION", RegimeRetestVariant.FLOW_REACCELERATION),
    (9, "ABSORPTION", RegimeRetestVariant.ABSORPTION_RECLAIM),
)
TRIAL_VARIANTS = tuple(item[2] for item in _TRIAL_DEFINITIONS)

# This audit was intentionally candidate-agnostic and inspected only transport
# integrity.  It predated this plan, so the disclosure must travel with every
# result; only parsed-value access is claimed to happen after plan publication.
ARCHIVE_PREFLIGHT_DISCLOSURE: Mapping[str, object] = MappingProxyType(
    {
        "audited_on": "2026-08-17",
        "scope": "candidate_agnostic_archive_transport_integrity",
        "broader_audit_generation_start": "2023-07-01",
        "broader_audit_end_exclusive": "2024-07-01",
        "broader_audit_archives": 60,
        "broader_audit_checksum_sidecars_verified": 60,
        "broader_audit_zip_crc_verified": 60,
        "broader_audit_single_member_archives": 60,
        "broader_audit_archive_bytes": 102_941_725,
        "broader_audit_inventory_sha256": (
            "6354482f498a5fc229c95de526dd9e2452ed1dacd7a5c8573ded761077ce60e5"
        ),
        "fixed_slice_generation_start": GENERATION_START.isoformat(),
        "fixed_slice_end_exclusive": EVALUATION_END.isoformat(),
        "fixed_slice_expected_archives": EXPECTED_ARCHIVES,
        "fixed_slice_expected_archives_per_symbol": EXPECTED_MONTHS_PER_SYMBOL,
        "fixed_slice_expected_months": EXPECTED_MONTHS,
        "fixed_slice_symbols": len(SYMBOLS),
        "fixed_slice_covered_by_broader_transport_audit": True,
        "fixed_slice_inventory_not_recomputed_without_cache_access": True,
        "known_data_issue": "pre-existing invalid XRP minute in November 2023",
        "known_data_issue_treatment": (
            "exclude November by starting generation on 2023-12-01; no repair or imputation"
        ),
        "window_rationale": WINDOW_RATIONALE,
        "parsed_market_values": False,
        "strategy_signals_evaluated": False,
        "pnl_evaluated": False,
        "plan_precedes_first_archive_byte_access": False,
        "plan_precedes_first_parsed_market_value_access": True,
    }
)

LEDGER_DURABILITY_DISCLOSURE: Mapping[str, object] = MappingProxyType(
    {
        "exclusive_create": "O_CREAT|O_EXCL",
        "file_content_fsync": True,
        "directory_entry_fsync_supported": os.name != "nt",
        "canonical_directory_reparse_points_allowed": False,
        "adversarial_parent_reparse_race_protected": False,
        "filesystem_race_scope": (
            "requires a non-adversarial local filesystem during publication; Python path-based "
            "O_EXCL does not bind a Windows parent-directory handle against a concurrent reparse race"
        ),
        "windows_power_loss_durability_claimed": False,
        "windows_limitation": (
            "Python on Windows exposes no supported directory-descriptor fsync; file bytes are "
            "fsynced and verified, but directory-entry survival across sudden power loss is not claimed"
        ),
    }
)


def _json_ready(value: object) -> object:
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("screen datetimes must be timezone-aware")
        return value.isoformat()
    if type(value) is date:
        return value.isoformat()
    if isinstance(value, Mapping):
        if any(not isinstance(key, str) for key in value):
            raise TypeError("screen JSON mappings require string keys")
        return {key: _json_ready(value[key]) for key in sorted(value)}
    if isinstance(value, (tuple, list)):
        return [_json_ready(item) for item in value]
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("screen JSON cannot contain non-finite numbers")
        return 0.0 if value == 0 else value
    raise TypeError(f"unsupported screen JSON value: {type(value).__name__}")


def _canonical_hash_value(value: object) -> object:
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("hash datetimes must be timezone-aware")
        return value.isoformat()
    if type(value) is date:
        return value.isoformat()
    if isinstance(value, Mapping):
        if any(not isinstance(key, str) for key in value):
            raise TypeError("hash mappings require string keys")
        return {key: _canonical_hash_value(value[key]) for key in sorted(value)}
    if isinstance(value, (tuple, list)):
        return [_canonical_hash_value(item) for item in value]
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("hash payload cannot contain non-finite numbers")
        normalized = 0.0 if value == 0 else value
        if normalized.is_integer():
            return int(normalized)
        return {"__float_hex__": normalized.hex()}
    raise TypeError(f"unsupported hash value: {type(value).__name__}")


def _canonical_json_bytes(value: object) -> bytes:
    return json.dumps(
        _canonical_hash_value(value),
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()


def _sha256(value: object) -> str:
    return hashlib.sha256(_canonical_json_bytes(value)).hexdigest()


def _serialized_json(payload: object) -> bytes:
    return (
        json.dumps(
            _json_ready(payload),
            allow_nan=False,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode()
        + b"\n"
    )


def _file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _canonical_project_root() -> Path:
    try:
        root = _CANONICAL_PROJECT_ROOT.resolve(strict=True)
    except OSError as exc:
        raise RuntimeError("canonical regime-retest project root is unavailable") from exc
    if not root.is_dir():
        raise RuntimeError("canonical regime-retest project root is not a directory")
    return root


def _canonical_output_paths() -> tuple[Path, Path, Path, Path]:
    root = _canonical_project_root()
    unresolved_output_directory = root / _CANONICAL_OUTPUT_DIRECTORY
    output_directory = unresolved_output_directory.resolve(strict=False)
    if not output_directory.is_relative_to(root):
        raise RuntimeError("canonical regime-retest output directory escapes the project root")
    if output_directory != unresolved_output_directory:
        raise RuntimeError("canonical regime-retest output directory cannot use a symlink or junction")
    return cast(
        tuple[Path, Path, Path, Path],
        tuple(output_directory / filename for filename in _CANONICAL_OUTPUT_FILENAMES),
    )


def _assert_canonical_output_paths(paths: tuple[Path, Path, Path, Path]) -> None:
    if paths != _canonical_output_paths():
        raise RuntimeError("regime-retest artifacts must use the single canonical ledger path")


def _fsync_directory(path: Path) -> bool:
    """Fsync a directory where supported; Windows has no Python directory-fsync primitive."""

    if os.name == "nt":
        return False
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    return True


def _read_artifact_bytes(path: Path) -> bytes:
    return path.read_bytes()


def _assert_artifact_bytes(path: Path, expected: bytes, artifact_name: str) -> None:
    if path not in _canonical_output_paths():
        raise RuntimeError(f"{artifact_name} is outside the canonical ledger")
    try:
        observed = _read_artifact_bytes(path)
    except OSError as exc:
        raise RuntimeError(f"{artifact_name} disappeared after exclusive publication") from exc
    if observed != expected:
        raise RuntimeError(f"{artifact_name} changed after exclusive publication")


def _publish_exclusive_bytes(path: Path, expected: bytes, artifact_name: str) -> None:
    """Create an artifact exactly once, fsync its bytes, and verify the registered bytes."""

    if type(expected) is not bytes or not expected:
        raise TypeError("exclusive artifact payload must be non-empty bytes")
    if path not in _canonical_output_paths():
        raise RuntimeError(f"{artifact_name} is outside the canonical ledger")
    path.parent.mkdir(parents=True, exist_ok=True)
    if path not in _canonical_output_paths():
        raise RuntimeError(f"{artifact_name} parent changed while preparing the canonical ledger")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
    try:
        descriptor = os.open(path, flags, 0o600)
    except FileExistsError as exc:
        raise FileExistsError(f"refusing to overwrite existing output: {path}") from exc
    try:
        with os.fdopen(descriptor, "wb") as output:
            output.write(expected)
            output.flush()
            os.fsync(output.fileno())
        _fsync_directory(path.parent)
    except BaseException:
        # Never remove a partially published one-shot artifact: its existence
        # remains fail-closed evidence that this canonical attempt was touched.
        raise
    _assert_artifact_bytes(path, expected, artifact_name)


def _reject_duplicate_json_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate direct_url.json key: {key}")
        result[key] = value
    return result


def _expected_direct_url_payload(url: str, commit: str) -> dict[str, object]:
    return {
        "url": url,
        "vcs_info": {
            "commit_id": commit,
            "requested_revision": commit,
            "vcs": "git",
        },
    }


@dataclass(frozen=True, slots=True)
class RegimeRetestInstalledDependencyPin:
    distribution: str
    import_package: str
    version: str
    url: str
    vcs: str
    requested_revision: str
    commit_id: str
    direct_url_sha256: str
    source_sha256: str
    source_files: int

    def __post_init__(self) -> None:
        expected_rows = {row[0]: row[1:] for row in EXPECTED_DEPENDENCY_SOURCES}
        try:
            expected_version, expected_url, expected_commit = expected_rows[self.distribution]
        except KeyError as exc:
            raise ValueError("unregistered installed dependency provenance") from exc
        expected_import, expected_source_sha256, expected_source_files = EXPECTED_DEPENDENCY_IMPORTS[
            self.distribution
        ]
        exact = (
            (self.import_package, expected_import),
            (self.version, expected_version),
            (self.url, expected_url),
            (self.vcs, "git"),
            (self.requested_revision, expected_commit),
            (self.commit_id, expected_commit),
            (self.source_sha256, expected_source_sha256),
            (self.source_files, expected_source_files),
        )
        if any(actual != expected for actual, expected in exact):
            raise ValueError(f"installed {self.distribution} does not match its exact frozen Git pin")
        if (
            len(self.direct_url_sha256) != 64
            or self.direct_url_sha256 != self.direct_url_sha256.lower()
            or any(character not in "0123456789abcdef" for character in self.direct_url_sha256)
        ):
            raise ValueError("installed dependency direct_url SHA-256 must be normalized")

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def _read_installed_dependency_pin(distribution_name: str) -> RegimeRetestInstalledDependencyPin:
    expected_rows = {row[0]: row[1:] for row in EXPECTED_DEPENDENCY_SOURCES}
    if distribution_name not in expected_rows:
        raise ValueError("dependency provenance reader accepts only frozen screen dependencies")
    expected_version, expected_url, expected_commit = expected_rows[distribution_name]
    import_package, expected_source_sha256, expected_source_files = EXPECTED_DEPENDENCY_IMPORTS[
        distribution_name
    ]
    try:
        distribution = importlib.metadata.distribution(distribution_name)
    except importlib.metadata.PackageNotFoundError as exc:
        raise RuntimeError(f"required distribution is not installed: {distribution_name}") from exc
    document = distribution.read_text("direct_url.json")
    if document is None:
        raise RuntimeError(f"installed {distribution_name} has no direct_url.json provenance")
    try:
        payload = json.loads(document, object_pairs_hook=_reject_duplicate_json_keys)
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"installed {distribution_name} has invalid direct_url.json") from exc
    expected_payload = _expected_direct_url_payload(expected_url, expected_commit)
    if distribution.version != expected_version or payload != expected_payload:
        raise RuntimeError(f"installed {distribution_name} does not match its exact frozen Git pin")
    imported = importlib.import_module(import_package)
    module_file = getattr(imported, "__file__", None)
    if not isinstance(module_file, str):
        raise RuntimeError(f"installed {distribution_name} import has no filesystem origin")
    imported_root = Path(module_file).resolve(strict=True).parent
    distribution_root = Path(str(distribution.locate_file(import_package))).resolve(strict=True)
    if imported_root != distribution_root:
        raise RuntimeError(f"installed {distribution_name} import is shadowed by another source")
    source_files = len(tuple(imported_root.rglob("*.py")))
    installed_source_sha256 = source_fingerprint(imported_root)
    if source_files != expected_source_files or installed_source_sha256 != expected_source_sha256:
        raise RuntimeError(f"installed {distribution_name} source does not match its frozen commit")
    vcs_info = cast(dict[str, object], expected_payload["vcs_info"])
    return RegimeRetestInstalledDependencyPin(
        distribution=distribution_name,
        import_package=import_package,
        version=distribution.version,
        url=cast(str, expected_payload["url"]),
        vcs=cast(str, vcs_info["vcs"]),
        requested_revision=cast(str, vcs_info["requested_revision"]),
        commit_id=cast(str, vcs_info["commit_id"]),
        direct_url_sha256=hashlib.sha256(document.encode()).hexdigest(),
        source_sha256=installed_source_sha256,
        source_files=source_files,
    )


def _git_value(project_root: Path, *arguments: str) -> str | None:
    try:
        # Arguments are fixed provenance queries and never contain caller-controlled shell text.
        completed = subprocess.run(  # nosec B603
            ("git", *arguments),
            cwd=project_root,
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
    except subprocess.CalledProcessError as exc:
        raise RuntimeError("regime-retest screen cannot read Git repository identity") from exc
    except OSError as exc:
        raise RuntimeError("regime-retest screen requires an accessible Git executable") from exc
    value = completed.stdout.strip()
    return value or None


def _repository_identity(remote_url: str) -> str:
    prefixes = (
        "https://github.com/",
        "http://github.com/",
        "ssh://git@github.com/",
        "git@github.com:",
    )
    for prefix in prefixes:
        if remote_url.startswith(prefix):
            identity = remote_url[len(prefix) :].rstrip("/")
            return identity[:-4] if identity.endswith(".git") else identity
    raise RuntimeError("origin remote is not a recognized GitHub repository URL")


def _read_toml_document(path: Path) -> dict[str, object]:
    try:
        payload = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, tomllib.TOMLDecodeError) as exc:
        raise RuntimeError(f"cannot parse frozen dependency declaration: {path.name}") from exc
    if not isinstance(payload, dict):  # pragma: no cover - tomllib always returns a dict
        raise RuntimeError(f"frozen dependency declaration is not an object: {path.name}")
    return cast(dict[str, object], payload)


def _assert_declared_dependency_sources(project_root: Path) -> None:
    pyproject = _read_toml_document(project_root / "pyproject.toml")
    project = pyproject.get("project")
    tool = pyproject.get("tool")
    if not isinstance(project, dict) or project.get("name") != "kairos-backtest":
        raise RuntimeError("pyproject does not identify the canonical kairos-backtest project")
    if not isinstance(tool, dict) or not isinstance(tool.get("uv"), dict):
        raise RuntimeError("pyproject has no exact uv source declarations")
    sources = cast(dict[str, object], tool["uv"]).get("sources")
    if not isinstance(sources, dict):
        raise RuntimeError("pyproject has no exact uv source declarations")
    lock = _read_toml_document(project_root / "uv.lock")
    packages = lock.get("package")
    if not isinstance(packages, list):
        raise RuntimeError("uv.lock has no package inventory")
    for distribution, version, url, commit in EXPECTED_DEPENDENCY_SOURCES:
        if sources.get(distribution) != {"git": url, "rev": commit}:
            raise RuntimeError(f"pyproject source for {distribution} is not the frozen Git pin")
        matches = [item for item in packages if isinstance(item, dict) and item.get("name") == distribution]
        expected_lock_source = {"git": f"{url}?rev={commit}#{commit}"}
        if (
            len(matches) != 1
            or matches[0].get("version") != version
            or matches[0].get("source") != expected_lock_source
        ):
            raise RuntimeError(f"uv.lock source for {distribution} is not the frozen Git pin")


@dataclass(frozen=True, slots=True)
class _RegimeRetestEnvironmentOverlay:
    project_root: str
    repository_remote_url: str
    repository_identity: str
    repository_branch: str
    dependency_pins: tuple[RegimeRetestInstalledDependencyPin, ...]


def _read_environment_overlay(project_root: Path) -> _RegimeRetestEnvironmentOverlay:
    top_level = _git_value(project_root, "rev-parse", "--show-toplevel")
    if top_level is None or Path(top_level).resolve(strict=True) != project_root:
        raise RuntimeError("screen module is not rooted in the captured Git worktree")
    remote_url = _git_value(project_root, "config", "--get", "remote.origin.url")
    if remote_url is None:
        raise RuntimeError("canonical origin remote is missing")
    repository_identity = _repository_identity(remote_url)
    if remote_url != EXPECTED_REPOSITORY_REMOTE_URL or repository_identity != EXPECTED_REPOSITORY_IDENTITY:
        raise RuntimeError("origin remote does not identify Kairos-cryptoAI/kairos-backtest")
    repository_branch = _git_value(project_root, "branch", "--show-current")
    if repository_branch != EXPECTED_REPOSITORY_BRANCH:
        raise RuntimeError("regime-retest screen must run on the main branch")
    _assert_declared_dependency_sources(project_root)
    dependency_pins = tuple(
        _read_installed_dependency_pin(distribution) for distribution, *_ in EXPECTED_DEPENDENCY_SOURCES
    )
    return _RegimeRetestEnvironmentOverlay(
        project_root=project_root.as_posix(),
        repository_remote_url=remote_url,
        repository_identity=repository_identity,
        repository_branch=repository_branch,
        dependency_pins=dependency_pins,
    )


@dataclass(frozen=True, slots=True)
class RegimeRetestScreenEnvironment:
    source: OrderFlowScreenEnvironment
    project_root: str
    repository_remote_url: str
    repository_identity: str
    repository_branch: str
    dependency_pins: tuple[RegimeRetestInstalledDependencyPin, ...]
    environment_sha256: str = field(init=False)

    def __post_init__(self) -> None:
        if not isinstance(self.source, OrderFlowScreenEnvironment):
            raise TypeError("screen source environment must be OrderFlowScreenEnvironment")
        if self.project_root != _canonical_project_root().as_posix():
            raise ValueError("screen environment must bind the captured canonical project root")
        if self.repository_identity != EXPECTED_REPOSITORY_IDENTITY:
            raise ValueError("screen environment repository identity is not canonical")
        if self.repository_remote_url != EXPECTED_REPOSITORY_REMOTE_URL:
            raise ValueError("screen environment origin remote URL is not canonical")
        if self.repository_branch != EXPECTED_REPOSITORY_BRANCH:
            raise ValueError("screen environment must bind the main branch")
        if any(not isinstance(pin, RegimeRetestInstalledDependencyPin) for pin in self.dependency_pins):
            raise TypeError("screen dependency provenance must use immutable pin records")
        expected_names = tuple(row[0] for row in EXPECTED_DEPENDENCY_SOURCES)
        if tuple(pin.distribution for pin in self.dependency_pins) != expected_names:
            raise ValueError("screen dependency pins must be complete and ordered")
        object.__setattr__(self, "environment_sha256", _sha256(self._payload()))

    @classmethod
    def capture(cls) -> RegimeRetestScreenEnvironment:
        project_root = _canonical_project_root()
        source = OrderFlowScreenEnvironment.capture()
        first = _read_environment_overlay(project_root)
        second = _read_environment_overlay(project_root)
        if first != second:
            raise RuntimeError("repository or installed dependency provenance changed during capture")
        return cls(
            source=source,
            project_root=second.project_root,
            repository_remote_url=second.repository_remote_url,
            repository_identity=second.repository_identity,
            repository_branch=second.repository_branch,
            dependency_pins=second.dependency_pins,
        )

    def _payload(self) -> dict[str, object]:
        return {
            "dependency_pins": [pin.to_dict() for pin in self.dependency_pins],
            "project_root": self.project_root,
            "repository": {
                "branch": self.repository_branch,
                "identity": self.repository_identity,
                "origin_remote_url": self.repository_remote_url,
            },
            "source": self.source.to_dict(),
        }

    def to_dict(self) -> dict[str, object]:
        return {**self._payload(), "environment_sha256": self.environment_sha256}


def _capture_clean_environment() -> RegimeRetestScreenEnvironment:
    return RegimeRetestScreenEnvironment.capture()


def _assert_environment_stable(expected: RegimeRetestScreenEnvironment) -> None:
    if not isinstance(expected, RegimeRetestScreenEnvironment):
        raise TypeError("expected environment must be RegimeRetestScreenEnvironment")
    _assert_orderflow_environment_stable(expected.source)
    project_root = _canonical_project_root()
    first = _read_environment_overlay(project_root)
    second = _read_environment_overlay(project_root)
    registered = _RegimeRetestEnvironmentOverlay(
        project_root=expected.project_root,
        repository_remote_url=expected.repository_remote_url,
        repository_identity=expected.repository_identity,
        repository_branch=expected.repository_branch,
        dependency_pins=expected.dependency_pins,
    )
    if first != second or second != registered:
        raise RuntimeError("repository or installed dependency provenance changed during evaluation")
    _assert_orderflow_environment_stable(expected.source)


def _candidate_for_variant(variant: RegimeRetestVariant) -> RegimeRetestCandidate:
    if not isinstance(variant, RegimeRetestVariant):
        raise TypeError("screen variant must be a RegimeRetestVariant")
    return RegimeRetestCandidate(config=RegimeVetoRetestReclaimConfig(variant=variant))


@dataclass(frozen=True, slots=True)
class RegimeRetestScreenTrial:
    lineage_trial_number: int
    trial_id: str
    fixed_order: int
    variant: RegimeRetestVariant
    candidate: RegimeRetestCandidate
    candidate_sha256: str

    def __post_init__(self) -> None:
        if type(self.lineage_trial_number) is not int or type(self.fixed_order) is not int:
            raise TypeError("trial lineage and order must be integers")
        if not isinstance(self.variant, RegimeRetestVariant):
            raise TypeError("trial variant must be a RegimeRetestVariant")
        try:
            expected_order = TRIAL_VARIANTS.index(self.variant)
        except ValueError as exc:  # pragma: no cover - guarded by enum and fixed tuple
            raise ValueError("unregistered regime-retest variant") from exc
        lineage, trial_id, _ = _TRIAL_DEFINITIONS[expected_order]
        if (
            self.lineage_trial_number != lineage
            or self.trial_id != trial_id
            or self.fixed_order != expected_order
        ):
            raise ValueError("trial identity must match the exact registered lineage")
        expected_candidate = _candidate_for_variant(self.variant)
        if self.candidate != expected_candidate:
            raise ValueError("trial candidate must match its exact registered variant")
        if self.candidate_sha256 != self.candidate.candidate_sha256:
            raise ValueError("trial candidate SHA-256 does not bind its complete configuration")

    def to_dict(self) -> dict[str, object]:
        return {
            "candidate": self.candidate.to_dict(),
            "candidate_sha256": self.candidate_sha256,
            "fixed_order": self.fixed_order,
            "lineage_trial_number": self.lineage_trial_number,
            "trial_id": self.trial_id,
            "variant": self.variant.value,
        }


def _fixed_trials() -> tuple[RegimeRetestScreenTrial, ...]:
    rows: list[RegimeRetestScreenTrial] = []
    for fixed_order, (lineage, trial_id, variant) in enumerate(_TRIAL_DEFINITIONS):
        candidate = _candidate_for_variant(variant)
        rows.append(
            RegimeRetestScreenTrial(
                lineage_trial_number=lineage,
                trial_id=trial_id,
                fixed_order=fixed_order,
                variant=variant,
                candidate=candidate,
                candidate_sha256=candidate.candidate_sha256,
            )
        )
    return tuple(rows)


@dataclass(frozen=True, slots=True)
class RegimeRetestScreenPlan:
    version: str = PLAN_VERSION
    classification: str = CLASSIFICATION
    reused_data: bool = True
    out_of_sample: bool = False
    promotion_eligible: bool = False
    shadow_allowed: bool = False
    live_allowed: bool = False
    window_name: str = WINDOW_NAME
    role: DataRole = DataRole.RESEARCH
    purpose: ResearchPurpose = ResearchPurpose.FIT
    generation_start: date = GENERATION_START
    evaluation_start: date = EVALUATION_START
    evaluation_end: date = EVALUATION_END
    window_rationale: str = WINDOW_RATIONALE
    warmup_days: int = WARMUP_DAYS
    universe: tuple[str, ...] = SYMBOLS
    scenario_names: tuple[str, ...] = SCENARIO_NAMES
    seed: int = BASE_SEED
    initial_equity_usd: float = INITIAL_EQUITY_USD
    protocol: ResearchProtocol = DEFAULT_REGIME_RETEST_PROTOCOL
    trials: tuple[RegimeRetestScreenTrial, ...] = field(default_factory=_fixed_trials)
    environment: RegimeRetestScreenEnvironment = field(default_factory=lambda: _capture_clean_environment())
    plan_sha256: str = field(init=False)

    def __post_init__(self) -> None:
        exact = (
            (self.version, PLAN_VERSION),
            (self.classification, CLASSIFICATION),
            (self.window_name, WINDOW_NAME),
            (self.role, DataRole.RESEARCH),
            (self.purpose, ResearchPurpose.FIT),
            (self.generation_start, GENERATION_START),
            (self.evaluation_start, EVALUATION_START),
            (self.evaluation_end, EVALUATION_END),
            (self.window_rationale, WINDOW_RATIONALE),
            (self.warmup_days, WARMUP_DAYS),
            (self.universe, SYMBOLS),
            (self.scenario_names, SCENARIO_NAMES),
            (self.seed, BASE_SEED),
            (self.initial_equity_usd, INITIAL_EQUITY_USD),
            (self.protocol, DEFAULT_REGIME_RETEST_PROTOCOL),
            (self.trials, _fixed_trials()),
        )
        if any(actual != expected for actual, expected in exact):
            raise ValueError("screen plan must preserve every fixed preregistered value")
        flags = (
            (self.reused_data, True),
            (self.out_of_sample, False),
            (self.promotion_eligible, False),
            (self.shadow_allowed, False),
            (self.live_allowed, False),
        )
        if any(type(actual) is not bool or actual is not expected for actual, expected in flags):
            raise ValueError("screen classification flags are immutable")
        if not isinstance(self.environment, RegimeRetestScreenEnvironment):
            raise TypeError("screen environment must bind clean source provenance")
        object.__setattr__(self, "plan_sha256", _sha256(self._payload()))

    def _payload(self) -> dict[str, object]:
        return {
            "archive_preflight_disclosure": ARCHIVE_PREFLIGHT_DISCLOSURE,
            "classification": self.classification,
            "data": {
                "evaluation_end_exclusive": self.evaluation_end,
                "evaluation_decision_cutoff_exclusive_utc": DECISION_CUTOFF_EXCLUSIVE,
                "evaluation_start": self.evaluation_start,
                "expected_archives": EXPECTED_ARCHIVES,
                "expected_archives_per_symbol": EXPECTED_MONTHS_PER_SYMBOL,
                "expected_archive_months": EXPECTED_MONTHS,
                "expected_evaluation_rows_per_symbol": EXPECTED_EVALUATION_ROWS_PER_SYMBOL,
                "expected_rows_per_symbol": EXPECTED_ROWS_PER_SYMBOL,
                "expected_warmup_rows_per_symbol": EXPECTED_WARMUP_ROWS_PER_SYMBOL,
                "generation_start": self.generation_start,
                "no_downloads": True,
                "no_imputation": True,
                "no_repair": True,
                "out_of_sample": self.out_of_sample,
                "terminal_embargo_ms": OPERATIONAL_HORIZON_MS,
                "purpose": self.purpose,
                "reused_data": self.reused_data,
                "role": self.role,
                "universe": self.universe,
                "warmup_days": self.warmup_days,
                "window_rationale": self.window_rationale,
                "window_name": self.window_name,
            },
            "eligibility": {
                "each_baseline_and_stress": {
                    "distinct_utc_exit_days_at_least": MINIMUM_DISTINCT_UTC_EXIT_DAYS,
                    "each_direction": {
                        "closed_trades_at_least": MINIMUM_DIRECTION_CLOSED_TRADES,
                        "expectancy_usd_per_trade_above": 0.0,
                        "profit_factor_above": 1.0,
                    },
                    "expectancy_usd_per_trade_above": 0.0,
                    "hac_sharpe_above": 0.0,
                    "log_growth_above": 0.0,
                    "maximum_one_symbol_trade_share_at_most": MAXIMUM_ONE_SYMBOL_TRADE_SHARE,
                    "minimum_closed_trades_per_symbol": MINIMUM_CLOSED_TRADES_PER_SYMBOL,
                    "positive_expectancy_symbols_at_least": MINIMUM_POSITIVE_EXPECTANCY_SYMBOLS,
                    "profit_factor_above": 1.0,
                    "reference_gross_pnl_usd_above": 0.0,
                    "total_closed_trades_at_least": MINIMUM_CLOSED_TRADES_PER_SCENARIO,
                },
                "stress_maximum_drawdown_at_most": MAXIMUM_STRESS_DRAWDOWN,
                "stress_trade_retention_at_least": MINIMUM_STRESS_TRADE_RETENTION,
                "trade_count_is_ranking_objective": False,
            },
            "environment": self.environment.to_dict(),
            "execution": {
                "initial_equity_usd": self.initial_equity_usd,
                "scenario_factory": "regime_retest_scenarios",
                "scenario_names": self.scenario_names,
                "seed": self.seed,
            },
            "permissions": {
                "live_allowed": self.live_allowed,
                "promotion_eligible": self.promotion_eligible,
                "shadow_allowed": self.shadow_allowed,
            },
            "one_shot_ledger": {
                "artifacts": _CANONICAL_OUTPUT_FILENAMES,
                "canonical_output_directory": _CANONICAL_OUTPUT_DIRECTORY.as_posix(),
                "canonical_project_root": self.environment.project_root,
                "durability": LEDGER_DURABILITY_DISCLOSURE,
                "public_output_override_allowed": False,
            },
            "plan_version": self.version,
            "protocol": {
                "definition": asdict(self.protocol),
                "name": self.protocol.protocol_name,
                "sha256": self.protocol.fingerprint(),
            },
            "ranking": {
                "primary": "maximize_minimum_baseline_stress_log_growth",
                "secondary": "minimize_stress_maximum_drawdown",
                "tertiary": "fixed_STRUCTURAL_FLOW_REACCELERATION_ABSORPTION_order",
            },
            "trial_budget": {
                "adaptive_rerun_allowed": False,
                "lineage_trial_numbers": (7, 8, 9),
                "maximum_trials": 3,
                "no_fourth_trial": True,
                "one_shot_attempt_ledger_required": True,
            },
            "trials": [trial.to_dict() for trial in self.trials],
        }

    def to_dict(self) -> dict[str, object]:
        return cast(dict[str, object], _json_ready({**self._payload(), "plan_sha256": self.plan_sha256}))


@dataclass(frozen=True, slots=True)
class RegimeRetestAttemptLedger:
    plan_sha256: str
    plan_file_sha256: str
    plan_file_bytes: int
    trial_lineage: tuple[tuple[int, str, str], ...]
    consumed_at: datetime
    status: str = "consumed"
    schema_version: int = ATTEMPT_SCHEMA_VERSION
    attempt_sha256: str = field(init=False)

    def __post_init__(self) -> None:
        for name, digest in (
            ("plan_sha256", self.plan_sha256),
            ("plan_file_sha256", self.plan_file_sha256),
        ):
            if (
                len(digest) != 64
                or digest != digest.lower()
                or any(c not in "0123456789abcdef" for c in digest)
            ):
                raise ValueError(f"{name} must be a lowercase SHA-256")
        if type(self.plan_file_bytes) is not int or self.plan_file_bytes <= 0:
            raise ValueError("plan_file_bytes must be positive")
        expected = tuple(
            (lineage, trial_id, trial.candidate_sha256)
            for (lineage, trial_id, _), trial in zip(_TRIAL_DEFINITIONS, _fixed_trials(), strict=True)
        )
        if self.trial_lineage != expected:
            raise ValueError("attempt ledger must consume exact lineage trials 7, 8 and 9")
        if self.consumed_at.tzinfo is None or self.consumed_at.utcoffset() != UTC.utcoffset(self.consumed_at):
            raise ValueError("attempt consumed_at must be expressed in UTC")
        if self.status != "consumed" or self.schema_version != ATTEMPT_SCHEMA_VERSION:
            raise ValueError("attempt ledger status and schema are immutable")
        object.__setattr__(self, "attempt_sha256", _sha256(self._payload()))

    def _payload(self) -> dict[str, object]:
        return {
            "classification": CLASSIFICATION,
            "consumed_at": self.consumed_at,
            "consumption_point": "immediately_before_first_parsed_market_value_access",
            "crash_or_failure_releases_attempt": False,
            "plan_file": {"bytes": self.plan_file_bytes, "sha256": self.plan_file_sha256},
            "plan_sha256": self.plan_sha256,
            "rerun_allowed": False,
            "schema_version": self.schema_version,
            "status": self.status,
            "trial_lineage": [
                {"candidate_sha256": digest, "lineage_trial_number": lineage, "trial_id": trial_id}
                for lineage, trial_id, digest in self.trial_lineage
            ],
        }

    def to_dict(self) -> dict[str, object]:
        return cast(
            dict[str, object], _json_ready({**self._payload(), "attempt_sha256": self.attempt_sha256})
        )


def _profit_factor(trades: tuple[TradeRecord, ...]) -> float | None:
    gross_profit = math.fsum(max(0.0, trade.net_pnl_usd) for trade in trades)
    gross_loss = math.fsum(max(0.0, -trade.net_pnl_usd) for trade in trades)
    return gross_profit / gross_loss if gross_loss > 0 else None


@dataclass(frozen=True, slots=True)
class RegimeRetestDirectionMetrics:
    direction: Side
    trades: int
    profit_factor: float | None
    expectancy_usd_per_trade: float

    def __post_init__(self) -> None:
        if not isinstance(self.direction, Side):
            raise TypeError("direction metrics require a Side")
        if type(self.trades) is not int or self.trades < 0:
            raise ValueError("direction trade count must be non-negative")
        if self.profit_factor is not None and (
            not math.isfinite(self.profit_factor) or self.profit_factor < 0
        ):
            raise ValueError("direction profit factor must be finite and non-negative")
        if not math.isfinite(self.expectancy_usd_per_trade):
            raise ValueError("direction expectancy must be finite")

    def to_dict(self) -> dict[str, object]:
        return cast(dict[str, object], _json_ready(asdict(self)))


@dataclass(frozen=True, slots=True)
class RegimeRetestMetrics:
    trades: int
    net_return: float
    log_growth: float
    profit_factor: float | None
    expectancy_usd_per_trade: float
    reference_gross_pnl_usd: float
    maximum_drawdown: float
    hac_sharpe: float | None
    fees_usd: float
    shortfall_usd: float
    funding_usd: float
    directions: tuple[RegimeRetestDirectionMetrics, ...]
    rejection_dispositions: tuple[tuple[str, int], ...]

    def __post_init__(self) -> None:
        if type(self.trades) is not int or self.trades < 0:
            raise ValueError("metric trade count must be non-negative")
        finite = (
            self.net_return,
            self.log_growth,
            self.expectancy_usd_per_trade,
            self.reference_gross_pnl_usd,
            self.maximum_drawdown,
            self.fees_usd,
            self.shortfall_usd,
            self.funding_usd,
        )
        if any(not math.isfinite(value) for value in finite):
            raise ValueError("screen metrics must be finite")
        if self.net_return <= -1 or self.log_growth != math.log1p(self.net_return):
            raise ValueError("log growth must exactly match net return")
        if self.maximum_drawdown < 0 or min(self.fees_usd, self.shortfall_usd, self.funding_usd) < 0:
            raise ValueError("drawdown and modeled costs must be non-negative")
        if self.profit_factor is not None and (
            not math.isfinite(self.profit_factor) or self.profit_factor < 0
        ):
            raise ValueError("profit factor must be finite and non-negative")
        if self.hac_sharpe is not None and not math.isfinite(self.hac_sharpe):
            raise ValueError("HAC Sharpe must be finite when available")
        if tuple(item.direction for item in self.directions) != (Side.LONG, Side.SHORT):
            raise ValueError("direction metrics must contain ordered long and short evidence")
        if sum(item.trades for item in self.directions) != self.trades:
            raise ValueError("direction trade counts must reconcile to the combined count")

    def to_dict(self) -> dict[str, object]:
        return cast(dict[str, object], _json_ready(asdict(self)))


@dataclass(frozen=True, slots=True)
class RegimeRetestSymbolMetrics:
    symbol: str
    metrics: RegimeRetestMetrics

    def __post_init__(self) -> None:
        if self.symbol not in SYMBOLS or not isinstance(self.metrics, RegimeRetestMetrics):
            raise ValueError("symbol metrics must bind a registered symbol and metrics")

    def to_dict(self) -> dict[str, object]:
        return {"metrics": self.metrics.to_dict(), "symbol": self.symbol}


@dataclass(frozen=True, slots=True)
class RegimeRetestScenarioMetrics:
    scenario_name: str
    per_symbol: tuple[RegimeRetestSymbolMetrics, ...]
    combined: RegimeRetestMetrics
    distinct_utc_exit_days: int
    positive_expectancy_symbols: int
    maximum_one_symbol_trade_share: float

    def __post_init__(self) -> None:
        if self.scenario_name not in SCENARIO_NAMES:
            raise ValueError("scenario metrics require a registered scenario name")
        if tuple(item.symbol for item in self.per_symbol) != SYMBOLS:
            raise ValueError("scenario metrics require the complete ordered symbol grid")
        if sum(item.metrics.trades for item in self.per_symbol) != self.combined.trades:
            raise ValueError("per-symbol trades must reconcile to combined trades")
        if type(self.distinct_utc_exit_days) is not int or self.distinct_utc_exit_days < 0:
            raise ValueError("distinct exit days must be non-negative")
        if not 0 <= self.positive_expectancy_symbols <= len(SYMBOLS):
            raise ValueError("positive-expectancy symbol count is out of range")
        if not math.isfinite(self.maximum_one_symbol_trade_share) or not (
            0 <= self.maximum_one_symbol_trade_share <= 1
        ):
            raise ValueError("maximum symbol share must be within zero and one")

    def to_dict(self) -> dict[str, object]:
        return {
            "combined": self.combined.to_dict(),
            "distinct_utc_exit_days": self.distinct_utc_exit_days,
            "maximum_one_symbol_trade_share": self.maximum_one_symbol_trade_share,
            "per_symbol": [item.to_dict() for item in self.per_symbol],
            "positive_expectancy_symbols": self.positive_expectancy_symbols,
            "scenario_name": self.scenario_name,
        }


def _rejection_inventory() -> tuple[str, ...]:
    return tuple(
        reason.value for reason in IntentDispositionReason if reason is not IntentDispositionReason.ENTERED
    )


def _direction_metrics(trades: tuple[TradeRecord, ...], direction: Side) -> RegimeRetestDirectionMetrics:
    selected = tuple(trade for trade in trades if trade.intent.side is direction)
    expectancy = math.fsum(trade.net_pnl_usd for trade in selected) / len(selected) if selected else 0.0
    return RegimeRetestDirectionMetrics(
        direction=direction,
        trades=len(selected),
        profit_factor=_profit_factor(selected),
        expectancy_usd_per_trade=expectancy,
    )


def _metrics_for_cells(
    portfolio: PortfolioEvidence,
    cells: tuple[RegimeRetestCellEvidence, ...],
) -> RegimeRetestMetrics:
    trades = tuple(trade for cell in cells for trade in cell.result.cell.trades)
    if portfolio.trades != len(trades):
        raise ValueError("screen portfolio trade count does not match its managed cells")
    expectancy = math.fsum(trade.net_pnl_usd for trade in trades) / len(trades) if trades else 0.0
    try:
        robust_sharpe: float | None = hac_sharpe(portfolio.daily_returns)
    except ValueError:
        robust_sharpe = None
    rejection_counts = Counter(
        disposition.reason.value
        for cell in cells
        for disposition in cell.result.dispositions
        if disposition.reason is not IntentDispositionReason.ENTERED
    )
    reference_gross = math.fsum(trade.gross_pnl_usd + trade.implementation_shortfall_usd for trade in trades)
    return RegimeRetestMetrics(
        trades=len(trades),
        net_return=portfolio.total_return,
        log_growth=math.log1p(portfolio.total_return),
        profit_factor=_profit_factor(trades),
        expectancy_usd_per_trade=expectancy,
        reference_gross_pnl_usd=reference_gross,
        maximum_drawdown=portfolio.maximum_drawdown,
        hac_sharpe=robust_sharpe,
        fees_usd=math.fsum(cell.result.fees_usd for cell in cells),
        shortfall_usd=math.fsum(cell.result.implementation_shortfall_usd for cell in cells),
        funding_usd=math.fsum(cell.result.carry_cost_usd for cell in cells),
        directions=tuple(_direction_metrics(trades, side) for side in (Side.LONG, Side.SHORT)),
        rejection_dispositions=tuple((reason, rejection_counts[reason]) for reason in _rejection_inventory()),
    )


def _summarize_campaign(
    evidence: RegimeRetestCampaignEvidence,
) -> tuple[RegimeRetestScenarioMetrics, ...]:
    rows: list[RegimeRetestScenarioMetrics] = []
    for scenario in evidence.scenarios:
        per_symbol: list[RegimeRetestSymbolMetrics] = []
        exit_days: set[date] = set()
        for symbol in SYMBOLS:
            symbol_cells = tuple(cell for cell in scenario.cells if cell.symbol == symbol)
            if len(symbol_cells) != 1 or symbol_cells[0].sleeve_id != REGIME_RETEST_SLEEVE_ID:
                raise ValueError("screen requires exactly one regime-retest cell per symbol")
            cell = symbol_cells[0]
            per_symbol.append(
                RegimeRetestSymbolMetrics(
                    symbol=symbol,
                    metrics=_metrics_for_cells(synchronize_cells((cell.result.cell,)), symbol_cells),
                )
            )
            exit_days.update(
                datetime.fromtimestamp(trade.exit_timestamp_ms / 1_000, UTC).date()
                for trade in cell.result.cell.trades
            )
        combined = _metrics_for_cells(scenario.portfolio, scenario.cells)
        immutable_symbols = tuple(per_symbol)
        rows.append(
            RegimeRetestScenarioMetrics(
                scenario_name=scenario.scenario.name,
                per_symbol=immutable_symbols,
                combined=combined,
                distinct_utc_exit_days=len(exit_days),
                positive_expectancy_symbols=sum(
                    item.metrics.expectancy_usd_per_trade > 0 for item in immutable_symbols
                ),
                maximum_one_symbol_trade_share=(
                    max(item.metrics.trades for item in immutable_symbols) / combined.trades
                    if combined.trades
                    else 0.0
                ),
            )
        )
    result = tuple(rows)
    if tuple(item.scenario_name for item in result) != SCENARIO_NAMES:
        raise ValueError("screen campaign must contain ordered baseline and stress evidence")
    return result


def _validate_generation_sha256(name: str, value: str) -> None:
    if (
        type(value) is not str
        or len(value) != 64
        or value != value.lower()
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ValueError(f"{name} must be a normalized SHA-256 digest")


@dataclass(frozen=True, slots=True)
class RegimeRetestGenerationSymbolDiagnostics:
    symbol: str
    generation_evidence_sha256: str
    setup_inventory_sha256: str
    outcome_inventory_sha256: str
    long_counters: RegimeRetestGenerationCounters
    short_counters: RegimeRetestGenerationCounters
    total_counters: RegimeRetestGenerationCounters
    symbol_diagnostics_sha256: str = field(init=False)

    def __post_init__(self) -> None:
        if self.symbol not in SYMBOLS:
            raise ValueError("generation diagnostics symbol is outside the fixed universe")
        for name, value in (
            ("generation_evidence_sha256", self.generation_evidence_sha256),
            ("setup_inventory_sha256", self.setup_inventory_sha256),
            ("outcome_inventory_sha256", self.outcome_inventory_sha256),
        ):
            _validate_generation_sha256(name, value)
        counters = (self.long_counters, self.short_counters, self.total_counters)
        if any(not isinstance(item, RegimeRetestGenerationCounters) for item in counters):
            raise TypeError("generation diagnostics require typed immutable counters")
        if self.total_counters != self.long_counters + self.short_counters:
            raise ValueError("symbol generation total must equal long plus short counters")
        object.__setattr__(self, "symbol_diagnostics_sha256", _sha256(self._payload()))

    def _payload(self) -> dict[str, object]:
        return {
            "counters": {
                "long": self.long_counters.to_dict(),
                "short": self.short_counters.to_dict(),
                "total": self.total_counters.to_dict(),
            },
            "generation_evidence_sha256": self.generation_evidence_sha256,
            "outcome_inventory_sha256": self.outcome_inventory_sha256,
            "setup_inventory_sha256": self.setup_inventory_sha256,
            "symbol": self.symbol,
        }

    def to_dict(self) -> dict[str, object]:
        return {**self._payload(), "symbol_diagnostics_sha256": self.symbol_diagnostics_sha256}


def _sum_generation_counters(
    rows: tuple[RegimeRetestGenerationCounters, ...],
) -> RegimeRetestGenerationCounters:
    result = RegimeRetestGenerationCounters()
    for row in rows:
        result = result + row
    return result


@dataclass(frozen=True, slots=True)
class RegimeRetestGenerationDiagnostics:
    candidate_sha256: str
    config_sha256: str
    variant: RegimeRetestVariant
    symbols: tuple[RegimeRetestGenerationSymbolDiagnostics, ...]
    aggregate_long_counters: RegimeRetestGenerationCounters
    aggregate_short_counters: RegimeRetestGenerationCounters
    aggregate_total_counters: RegimeRetestGenerationCounters
    generation_diagnostics_sha256: str = field(init=False)

    def __post_init__(self) -> None:
        _validate_generation_sha256("candidate_sha256", self.candidate_sha256)
        _validate_generation_sha256("config_sha256", self.config_sha256)
        if not isinstance(self.variant, RegimeRetestVariant):
            raise TypeError("generation diagnostics variant must be RegimeRetestVariant")
        if not isinstance(self.symbols, tuple) or any(
            not isinstance(item, RegimeRetestGenerationSymbolDiagnostics) for item in self.symbols
        ):
            raise TypeError("generation diagnostics require typed per-symbol rows")
        if tuple(item.symbol for item in self.symbols) != SYMBOLS:
            raise ValueError("generation diagnostics require the ordered five-symbol universe")
        aggregate_counters = (
            self.aggregate_long_counters,
            self.aggregate_short_counters,
            self.aggregate_total_counters,
        )
        if any(not isinstance(item, RegimeRetestGenerationCounters) for item in aggregate_counters):
            raise TypeError("generation diagnostics aggregates must use typed counters")
        expected_long = _sum_generation_counters(tuple(item.long_counters for item in self.symbols))
        expected_short = _sum_generation_counters(tuple(item.short_counters for item in self.symbols))
        expected_total = _sum_generation_counters(tuple(item.total_counters for item in self.symbols))
        if (
            self.aggregate_long_counters != expected_long
            or self.aggregate_short_counters != expected_short
            or self.aggregate_total_counters != expected_total
            or self.aggregate_total_counters != self.aggregate_long_counters + self.aggregate_short_counters
        ):
            raise ValueError("generation diagnostics aggregate counters do not recompute exactly")
        object.__setattr__(self, "generation_diagnostics_sha256", _sha256(self._payload()))

    def _payload(self) -> dict[str, object]:
        return {
            "aggregate_counters": {
                "long": self.aggregate_long_counters.to_dict(),
                "short": self.aggregate_short_counters.to_dict(),
                "total": self.aggregate_total_counters.to_dict(),
            },
            "candidate_sha256": self.candidate_sha256,
            "config_sha256": self.config_sha256,
            "scenario_invariance_verified": list(SCENARIO_NAMES),
            "schema_version": GENERATION_DIAGNOSTICS_SCHEMA_VERSION,
            "symbols": [item.to_dict() for item in self.symbols],
            "variant": self.variant.value,
        }

    def to_dict(self) -> dict[str, object]:
        return {
            **self._payload(),
            "generation_diagnostics_sha256": self.generation_diagnostics_sha256,
        }


def _condense_generation_diagnostics(
    evidence: RegimeRetestCampaignEvidence,
    trial: RegimeRetestScreenTrial,
) -> RegimeRetestGenerationDiagnostics:
    if (
        len(evidence.scenarios) != 2
        or tuple(scenario.scenario.name for scenario in evidence.scenarios) != SCENARIO_NAMES
    ):
        raise ValueError("generation diagnostics require ordered baseline and stress evidence")
    baseline, stress = evidence.scenarios
    if (
        tuple(cell.symbol for cell in baseline.cells) != SYMBOLS
        or tuple(cell.symbol for cell in stress.cells) != SYMBOLS
    ):
        raise ValueError("generation diagnostics require ordered baseline/stress symbol grids")
    rows: list[RegimeRetestGenerationSymbolDiagnostics] = []
    for symbol, baseline_cell, stress_cell in zip(
        SYMBOLS,
        baseline.cells,
        stress.cells,
        strict=True,
    ):
        if not isinstance(baseline_cell, RegimeRetestCellEvidence) or not isinstance(
            stress_cell, RegimeRetestCellEvidence
        ):
            raise TypeError("generation diagnostics require RegimeRetestCellEvidence values")
        if baseline_cell.symbol != symbol or stress_cell.symbol != symbol:
            raise ValueError("generation diagnostics cell coordinates are inconsistent")
        if (
            baseline_cell.generation_evidence != stress_cell.generation_evidence
            or baseline_cell.generation_evidence_sha256 != stress_cell.generation_evidence_sha256
        ):
            raise ValueError("baseline and stress generation evidence must be identical")
        generation = baseline_cell.generation_evidence
        if not isinstance(generation, RegimeRetestGenerationEvidence):
            raise TypeError("generation diagnostics require RegimeRetestGenerationEvidence")
        for cell in (baseline_cell, stress_cell):
            if cell.candidate_sha256 != trial.candidate_sha256:
                raise ValueError("generation diagnostics cell does not match the trial candidate")
            if (
                cell.generation_evidence.config_sha256 != trial.candidate.config.fingerprint
                or cell.generation_evidence.variant is not trial.variant
            ):
                raise ValueError("generation diagnostics evidence does not match candidate/variant")
        rows.append(
            RegimeRetestGenerationSymbolDiagnostics(
                symbol=symbol,
                generation_evidence_sha256=baseline_cell.generation_evidence_sha256,
                setup_inventory_sha256=generation.setup_inventory_sha256,
                outcome_inventory_sha256=generation.outcome_inventory_sha256,
                long_counters=generation.long_counters,
                short_counters=generation.short_counters,
                total_counters=generation.total_counters,
            )
        )
    immutable_rows = tuple(rows)
    return RegimeRetestGenerationDiagnostics(
        candidate_sha256=trial.candidate_sha256,
        config_sha256=trial.candidate.config.fingerprint,
        variant=trial.variant,
        symbols=immutable_rows,
        aggregate_long_counters=_sum_generation_counters(
            tuple(item.long_counters for item in immutable_rows)
        ),
        aggregate_short_counters=_sum_generation_counters(
            tuple(item.short_counters for item in immutable_rows)
        ),
        aggregate_total_counters=_sum_generation_counters(
            tuple(item.total_counters for item in immutable_rows)
        ),
    )


def _eligibility_failures(
    scenarios: tuple[RegimeRetestScenarioMetrics, ...],
) -> tuple[str, ...]:
    if tuple(item.scenario_name for item in scenarios) != SCENARIO_NAMES:
        raise ValueError("eligibility requires ordered baseline and stress scenarios")
    failures: list[str] = []
    for scenario in scenarios:
        prefix = scenario.scenario_name
        metrics = scenario.combined
        if metrics.log_growth <= 0:
            failures.append(f"{prefix}_log_growth_not_positive")
        if metrics.profit_factor is None or metrics.profit_factor <= 1:
            failures.append(f"{prefix}_profit_factor_not_above_one")
        if metrics.expectancy_usd_per_trade <= 0:
            failures.append(f"{prefix}_expectancy_not_positive")
        if metrics.reference_gross_pnl_usd <= 0:
            failures.append(f"{prefix}_reference_gross_pnl_not_positive")
        if metrics.hac_sharpe is None or metrics.hac_sharpe <= 0:
            failures.append(f"{prefix}_hac_sharpe_not_positive")
        if metrics.trades < MINIMUM_CLOSED_TRADES_PER_SCENARIO:
            failures.append(f"{prefix}_closed_trades_below_minimum")
        for item in scenario.per_symbol:
            if item.metrics.trades < MINIMUM_CLOSED_TRADES_PER_SYMBOL:
                failures.append(f"{prefix}_{item.symbol}_closed_trades_below_minimum")
        if scenario.distinct_utc_exit_days < MINIMUM_DISTINCT_UTC_EXIT_DAYS:
            failures.append(f"{prefix}_distinct_utc_exit_days_below_minimum")
        if scenario.positive_expectancy_symbols < MINIMUM_POSITIVE_EXPECTANCY_SYMBOLS:
            failures.append(f"{prefix}_positive_expectancy_symbols_below_minimum")
        if scenario.maximum_one_symbol_trade_share > MAXIMUM_ONE_SYMBOL_TRADE_SHARE:
            failures.append(f"{prefix}_one_symbol_trade_share_above_maximum")
        for direction in metrics.directions:
            direction_name = direction.direction.value.lower()
            if direction.trades < MINIMUM_DIRECTION_CLOSED_TRADES:
                failures.append(f"{prefix}_{direction_name}_closed_trades_below_minimum")
            if direction.profit_factor is None or direction.profit_factor <= 1:
                failures.append(f"{prefix}_{direction_name}_profit_factor_not_above_one")
            if direction.expectancy_usd_per_trade <= 0:
                failures.append(f"{prefix}_{direction_name}_expectancy_not_positive")
    baseline, stress = scenarios
    retention = stress.combined.trades / baseline.combined.trades if baseline.combined.trades else 0.0
    if retention < MINIMUM_STRESS_TRADE_RETENTION:
        failures.append("stress_trade_retention_below_minimum")
    if stress.combined.maximum_drawdown > MAXIMUM_STRESS_DRAWDOWN:
        failures.append("stress_maximum_drawdown_above_maximum")
    return tuple(failures)


@dataclass(frozen=True, slots=True)
class RegimeRetestTrialResult:
    trial: RegimeRetestScreenTrial
    campaign_evidence: RegimeRetestCampaignEvidence
    scenarios: tuple[RegimeRetestScenarioMetrics, ...]
    generation_diagnostics: RegimeRetestGenerationDiagnostics = field(init=False)
    eligibility_failures: tuple[str, ...] = field(init=False)
    eligible: bool = field(init=False)
    worst_scenario_log_growth: float = field(init=False)

    def __post_init__(self) -> None:
        if not isinstance(self.trial, RegimeRetestScreenTrial):
            raise TypeError("screen result trial must be a RegimeRetestScreenTrial")
        evidence = self.campaign_evidence
        if not isinstance(evidence, RegimeRetestCampaignEvidence):
            raise TypeError("screen trial must retain full RegimeRetestCampaignEvidence")
        if evidence.candidate != self.trial.candidate:
            raise ValueError("campaign candidate does not match its preregistered trial")
        exact = (
            (evidence.protocol, DEFAULT_REGIME_RETEST_PROTOCOL),
            (evidence.protocol_name, DEFAULT_REGIME_RETEST_PROTOCOL.protocol_name),
            (evidence.protocol_sha256, DEFAULT_REGIME_RETEST_PROTOCOL.fingerprint()),
            (evidence.window_name, WINDOW_NAME),
            (evidence.role, DataRole.RESEARCH),
            (evidence.purpose, ResearchPurpose.FIT),
            (evidence.generation_start, GENERATION_START),
            (evidence.evaluation_start, EVALUATION_START),
            (evidence.evaluation_end, EVALUATION_END),
            (evidence.window_rationale, WINDOW_RATIONALE),
            (evidence.requested_initial_equity_usd, INITIAL_EQUITY_USD),
            (evidence.seed, BASE_SEED),
            (
                tuple(item.scenario for item in evidence.scenarios),
                regime_retest_scenarios(self.trial.candidate),
            ),
        )
        if any(actual != expected for actual, expected in exact):
            raise ValueError("campaign evidence violates the fixed regime-retest screen plan")
        generation_diagnostics = _condense_generation_diagnostics(evidence, self.trial)
        expected_scenarios = _summarize_campaign(evidence)
        if self.scenarios != expected_scenarios:
            raise ValueError("screen metrics must exactly match full campaign evidence")
        failures = _eligibility_failures(self.scenarios)
        object.__setattr__(self, "generation_diagnostics", generation_diagnostics)
        object.__setattr__(self, "eligibility_failures", failures)
        object.__setattr__(self, "eligible", not failures)
        object.__setattr__(
            self,
            "worst_scenario_log_growth",
            min(scenario.combined.log_growth for scenario in self.scenarios),
        )

    @property
    def stress_maximum_drawdown(self) -> float:
        return self.scenarios[1].combined.maximum_drawdown

    def to_dict(self) -> dict[str, object]:
        return {
            "campaign_evidence": self.campaign_evidence.to_dict(),
            "candidate_sha256": self.trial.candidate_sha256,
            "classification": CLASSIFICATION,
            "eligibility_failures": self.eligibility_failures,
            "eligible": self.eligible,
            "generation_diagnostics": self.generation_diagnostics.to_dict(),
            "lineage_trial_number": self.trial.lineage_trial_number,
            "live_allowed": False,
            "metrics": [scenario.to_dict() for scenario in self.scenarios],
            "out_of_sample": False,
            "promotion_eligible": False,
            "reused_data": True,
            "shadow_allowed": False,
            "trial_id": self.trial.trial_id,
            "variant": self.trial.variant.value,
            "worst_scenario_log_growth": self.worst_scenario_log_growth,
        }


@dataclass(frozen=True, slots=True)
class RegimeRetestScreenResult:
    plan: RegimeRetestScreenPlan
    attempt: RegimeRetestAttemptLedger
    attempt_file_sha256: str
    attempt_file_bytes: int
    trials: tuple[RegimeRetestTrialResult, ...]
    classification: str = field(init=False, default=CLASSIFICATION)
    reused_data: bool = field(init=False, default=True)
    out_of_sample: bool = field(init=False, default=False)
    promotion_eligible: bool = field(init=False, default=False)
    shadow_allowed: bool = field(init=False, default=False)
    live_allowed: bool = field(init=False, default=False)
    ranked_eligible_trial_ids: tuple[str, ...] = field(init=False)
    selected_trial_id: str = field(init=False)

    def __post_init__(self) -> None:
        if type(self.plan) is not RegimeRetestScreenPlan:
            raise TypeError("screen result requires its canonical plan")
        if type(self.attempt) is not RegimeRetestAttemptLedger:
            raise TypeError("screen result requires its consumed attempt ledger")
        if self.attempt.plan_sha256 != self.plan.plan_sha256:
            raise ValueError("attempt ledger does not bind the screen plan")
        canonical_plan_bytes = _serialized_json(self.plan.to_dict())
        if self.attempt.plan_file_sha256 != hashlib.sha256(
            canonical_plan_bytes
        ).hexdigest() or self.attempt.plan_file_bytes != len(canonical_plan_bytes):
            raise ValueError("attempt plan-file commitment does not match canonical plan bytes")
        canonical_attempt_bytes = _serialized_json(self.attempt.to_dict())
        if (
            len(self.attempt_file_sha256) != 64
            or self.attempt_file_sha256 != self.attempt_file_sha256.lower()
            or any(character not in "0123456789abcdef" for character in self.attempt_file_sha256)
        ):
            raise ValueError("attempt file SHA-256 must be normalized")
        if type(self.attempt_file_bytes) is not int or self.attempt_file_bytes <= 0:
            raise ValueError("attempt file size must be positive")
        if self.attempt_file_sha256 != hashlib.sha256(
            canonical_attempt_bytes
        ).hexdigest() or self.attempt_file_bytes != len(canonical_attempt_bytes):
            raise ValueError("attempt-file commitment does not match canonical attempt bytes")
        if type(self.trials) is not tuple:
            raise TypeError("screen trials must be an exact immutable tuple")
        if len(self.trials) != 3:
            raise ValueError("screen results must contain exactly three trials")
        if any(type(trial) is not RegimeRetestTrialResult for trial in self.trials):
            raise TypeError("screen trials must contain exact RegimeRetestTrialResult values")
        if any(
            result_trial.trial is not planned_trial
            for result_trial, planned_trial in zip(self.trials, self.plan.trials, strict=True)
        ):
            raise ValueError("screen trials must preserve exact planned ordering and identity")
        eligible = [trial for trial in self.trials if trial.eligible]
        eligible.sort(
            key=lambda trial: (
                -trial.worst_scenario_log_growth,
                trial.stress_maximum_drawdown,
                trial.trial.fixed_order,
            )
        )
        ranking = tuple(trial.trial.trial_id for trial in eligible)
        object.__setattr__(self, "ranked_eligible_trial_ids", ranking)
        object.__setattr__(self, "selected_trial_id", ranking[0] if ranking else REJECT_ALL)

    def to_dict(self) -> dict[str, object]:
        return cast(
            dict[str, object],
            _json_ready(
                {
                    "attempt": {
                        "attempt_sha256": self.attempt.attempt_sha256,
                        "bytes": self.attempt_file_bytes,
                        "file_sha256": self.attempt_file_sha256,
                        "status": self.attempt.status,
                    },
                    "classification": self.classification,
                    "live_allowed": self.live_allowed,
                    "out_of_sample": self.out_of_sample,
                    "permissions": {
                        "live_allowed": self.live_allowed,
                        "promotion_eligible": self.promotion_eligible,
                        "shadow_allowed": self.shadow_allowed,
                    },
                    "plan_sha256": self.plan.plan_sha256,
                    "plan_file": {
                        "bytes": self.attempt.plan_file_bytes,
                        "sha256": self.attempt.plan_file_sha256,
                    },
                    "promotion_eligible": self.promotion_eligible,
                    "ranking": {
                        "ranked_eligible_trial_ids": self.ranked_eligible_trial_ids,
                        "selected_trial_id": self.selected_trial_id,
                    },
                    "reused_data": self.reused_data,
                    "schema_version": RESULT_SCHEMA_VERSION,
                    "shadow_allowed": self.shadow_allowed,
                    "trials": [trial.to_dict() for trial in self.trials],
                }
            ),
        )


def compact_regime_retest_summary(result: RegimeRetestScreenResult) -> dict[str, object]:
    if not isinstance(result, RegimeRetestScreenResult):
        raise TypeError("compact summary requires RegimeRetestScreenResult")
    full_result_bytes = _serialized_json(result.to_dict())
    return cast(
        dict[str, object],
        _json_ready(
            {
                "attempt": {
                    "attempt_sha256": result.attempt.attempt_sha256,
                    "bytes": result.attempt_file_bytes,
                    "file_sha256": result.attempt_file_sha256,
                    "status": result.attempt.status,
                },
                "classification": result.classification,
                "data": {
                    "evaluation_end_exclusive": result.plan.evaluation_end,
                    "evaluation_decision_cutoff_exclusive_utc": DECISION_CUTOFF_EXCLUSIVE,
                    "evaluation_start": result.plan.evaluation_start,
                    "generation_start": result.plan.generation_start,
                    "out_of_sample": result.out_of_sample,
                    "reused_data": result.reused_data,
                    "terminal_embargo_ms": OPERATIONAL_HORIZON_MS,
                    "universe": result.plan.universe,
                    "warmup_days": result.plan.warmup_days,
                },
                "environment": result.plan.environment.to_dict(),
                "full_result": {
                    "bytes": len(full_result_bytes),
                    "schema_version": RESULT_SCHEMA_VERSION,
                    "sha256": hashlib.sha256(full_result_bytes).hexdigest(),
                },
                "permissions": {
                    "live_allowed": result.live_allowed,
                    "promotion_eligible": result.promotion_eligible,
                    "shadow_allowed": result.shadow_allowed,
                },
                "plan_sha256": result.plan.plan_sha256,
                "plan_file": {
                    "bytes": result.attempt.plan_file_bytes,
                    "sha256": result.attempt.plan_file_sha256,
                },
                "ranking": {
                    "ranked_eligible_trial_ids": result.ranked_eligible_trial_ids,
                    "selected_trial_id": result.selected_trial_id,
                },
                "schema_version": RESULT_SCHEMA_VERSION,
                "trials": [
                    {
                        "candidate_sha256": trial.trial.candidate_sha256,
                        "eligibility_failures": trial.eligibility_failures,
                        "eligible": trial.eligible,
                        "generation_diagnostics": trial.generation_diagnostics.to_dict(),
                        "lineage_trial_number": trial.trial.lineage_trial_number,
                        "metrics": [scenario.to_dict() for scenario in trial.scenarios],
                        "trial_id": trial.trial.trial_id,
                        "variant": trial.trial.variant,
                        "worst_scenario_log_growth": trial.worst_scenario_log_growth,
                    }
                    for trial in result.trials
                ],
            }
        ),
    )


def _validate_complete_cached_slice(
    symbol: str,
    candles: list[Candle],
    manifest: DatasetManifest,
) -> None:
    expected_rows = (EVALUATION_END - GENERATION_START).days * 24 * 60
    expected_start_ms = int(datetime.combine(GENERATION_START, datetime.min.time(), UTC).timestamp() * 1_000)
    expected_end_ms = int(datetime.combine(EVALUATION_END, datetime.min.time(), UTC).timestamp() * 1_000)
    expected_files = tuple(
        f"{symbol}-1m-{month:%Y-%m}.zip" for month in month_starts(GENERATION_START, EVALUATION_END)
    )
    if not isinstance(manifest, DatasetManifest):
        raise TypeError("offline loader must return a DatasetManifest")
    manifest_values = (
        (manifest.symbol, symbol),
        (manifest.interval, "1m"),
        (manifest.requested_start, GENERATION_START.isoformat()),
        (manifest.requested_end, EVALUATION_END.isoformat()),
        (manifest.actual_start_ms, expected_start_ms),
        (manifest.actual_end_ms, expected_end_ms - 1),
        (manifest.rows, expected_rows),
        (manifest.gaps, 0),
        (manifest.expected_files, len(expected_files)),
        (manifest.files, expected_files),
    )
    if any(actual != expected for actual, expected in manifest_values):
        raise ValueError(f"incomplete offline cache evidence for {symbol}")
    if (
        manifest.transport_verification != "zip_crc_and_parsed_rows_sha256"
        or manifest.checksum_status != "official_sha256_verified"
        or manifest.checksum_files_verified != manifest.expected_files
    ):
        raise ValueError(f"offline cache lacks verified official checksums and ZIP CRC for {symbol}")
    if manifest.csv_schema != "binance_futures_kline_v1_12_columns":
        raise ValueError(f"offline cache CSV schema is not the required twelve-column schema for {symbol}")
    if len(candles) != expected_rows:
        raise ValueError(f"incomplete offline candle slice for {symbol}")
    for index, candle in enumerate(candles):
        expected_open = expected_start_ms + index * 60_000
        if not isinstance(candle, Candle):
            raise TypeError("offline cache must contain Candle values")
        if (
            candle.symbol != symbol
            or candle.timeframe != "1m"
            or candle.open_time_ms != expected_open
            or candle.close_time_ms != expected_open + 59_999
        ):
            raise ValueError(f"offline cache has a missing, duplicate, or malformed minute for {symbol}")
        numeric = (
            candle.open,
            candle.high,
            candle.low,
            candle.close,
            candle.volume,
            candle.quote_volume,
            candle.taker_buy_volume,
        )
        if any(isinstance(value, bool) or not math.isfinite(value) for value in numeric):
            raise ValueError(f"offline cache has non-finite OHLC/volume/quote/taker data for {symbol}")
        if (
            min(candle.open, candle.high, candle.low, candle.close) <= 0
            or min(candle.volume, candle.quote_volume, candle.taker_buy_volume) < 0
            or candle.high < max(candle.open, candle.close)
            or candle.low > min(candle.open, candle.close)
            or candle.taker_buy_volume > candle.volume
        ):
            raise ValueError(f"offline cache has invalid OHLC/volume/quote/taker domains for {symbol}")
        tolerance = max(1e-8, abs(candle.quote_volume) * 1e-10)
        if (
            candle.quote_volume < candle.volume * candle.low - tolerance
            or candle.quote_volume > candle.volume * candle.high + tolerance
        ):
            raise ValueError(f"offline cache quote volume is inconsistent for {symbol}")
    digest = hashlib.sha256(
        json.dumps([asdict(candle) for candle in candles], separators=(",", ":")).encode()
    ).hexdigest()
    if manifest.sha256 != digest:
        raise ValueError(f"offline cache parsed-row SHA-256 mismatch for {symbol}")


def _load_fixed_cache(cache_dir: Path) -> dict[str, list[Candle]]:
    loader = BinanceArchiveLoader(cache_dir, allow_download=False)
    candles_by_symbol: dict[str, list[Candle]] = {}
    for symbol in SYMBOLS:
        candles, manifest = loader.load(symbol, GENERATION_START, EVALUATION_END, "1m")
        _validate_complete_cached_slice(symbol, candles, manifest)
        candles_by_symbol[symbol] = candles
    return candles_by_symbol


def _validate_output_paths() -> tuple[Path, Path, Path, Path]:
    paths = _canonical_output_paths()
    _assert_canonical_output_paths(paths)
    existing = tuple(path for path in paths if path.exists())
    if existing:
        names = ", ".join(str(path) for path in existing)
        raise FileExistsError(f"one-shot attempt is unavailable; existing output: {names}")
    return paths


def _now_utc() -> datetime:
    return datetime.now(UTC)


def _attempt_for_plan(plan: RegimeRetestScreenPlan, plan_bytes: bytes) -> RegimeRetestAttemptLedger:
    return RegimeRetestAttemptLedger(
        plan_sha256=plan.plan_sha256,
        plan_file_sha256=hashlib.sha256(plan_bytes).hexdigest(),
        plan_file_bytes=len(plan_bytes),
        trial_lineage=tuple(
            (trial.lineage_trial_number, trial.trial_id, trial.candidate_sha256) for trial in plan.trials
        ),
        consumed_at=_now_utc(),
    )


def _assert_registered_artifacts(
    plan_output: Path,
    attempt_output: Path,
    plan_bytes: bytes,
    attempt_bytes: bytes,
) -> None:
    paths = _canonical_output_paths()
    if (plan_output, attempt_output) != paths[:2]:
        raise RuntimeError("registered plan and attempt are not in the canonical ledger")
    _assert_canonical_output_paths(paths)
    _assert_artifact_bytes(plan_output, plan_bytes, "screen plan")
    _assert_artifact_bytes(attempt_output, attempt_bytes, "attempt ledger")


def run_regime_retest_screen(cache_dir: Path) -> RegimeRetestScreenResult:
    """Consume and run the exact one-shot offline development screen."""

    if not isinstance(cache_dir, Path):
        raise TypeError("cache_dir must be a pathlib.Path")
    plan_output, attempt_output, result_output, summary_output = _validate_output_paths()

    plan = RegimeRetestScreenPlan()
    registered_plan_bytes = _serialized_json(plan.to_dict())
    _assert_canonical_output_paths((plan_output, attempt_output, result_output, summary_output))
    _publish_exclusive_bytes(plan_output, registered_plan_bytes, "screen plan")
    _assert_artifact_bytes(plan_output, registered_plan_bytes, "screen plan")
    _assert_environment_stable(plan.environment)
    _assert_artifact_bytes(plan_output, registered_plan_bytes, "screen plan")

    # This is the irreversible consumption point.  Nothing that can parse a
    # market value is constructed or called before the ledger is exclusively
    # created, file-fsynced and byte-verified.  On Windows, Python cannot fsync
    # the parent directory entry; the plan explicitly disclaims power-loss
    # durability for that unsupported step.
    attempt = _attempt_for_plan(plan, registered_plan_bytes)
    registered_attempt_bytes = _serialized_json(attempt.to_dict())
    _publish_exclusive_bytes(attempt_output, registered_attempt_bytes, "attempt ledger")
    _assert_registered_artifacts(
        plan_output,
        attempt_output,
        registered_plan_bytes,
        registered_attempt_bytes,
    )

    candles_by_symbol = _load_fixed_cache(cache_dir)
    _assert_registered_artifacts(
        plan_output,
        attempt_output,
        registered_plan_bytes,
        registered_attempt_bytes,
    )
    _assert_environment_stable(plan.environment)
    trial_results: list[RegimeRetestTrialResult] = []
    for trial in plan.trials:
        _assert_registered_artifacts(
            plan_output,
            attempt_output,
            registered_plan_bytes,
            registered_attempt_bytes,
        )
        evidence = run_regime_retest_campaign(
            candles_by_symbol,
            candidate=trial.candidate,
            protocol=DEFAULT_REGIME_RETEST_PROTOCOL,
            initial_equity_usd=INITIAL_EQUITY_USD,
            scenarios=regime_retest_scenarios(trial.candidate),
            seed=BASE_SEED,
        )
        trial_results.append(
            RegimeRetestTrialResult(
                trial=trial,
                campaign_evidence=evidence,
                scenarios=_summarize_campaign(evidence),
            )
        )
        _assert_registered_artifacts(
            plan_output,
            attempt_output,
            registered_plan_bytes,
            registered_attempt_bytes,
        )

    _assert_environment_stable(plan.environment)
    _assert_registered_artifacts(
        plan_output,
        attempt_output,
        registered_plan_bytes,
        registered_attempt_bytes,
    )
    result = RegimeRetestScreenResult(
        plan=plan,
        attempt=attempt,
        attempt_file_sha256=hashlib.sha256(registered_attempt_bytes).hexdigest(),
        attempt_file_bytes=len(registered_attempt_bytes),
        trials=tuple(trial_results),
    )
    registered_result_bytes = _serialized_json(result.to_dict())
    _publish_exclusive_bytes(result_output, registered_result_bytes, "full result")
    _assert_registered_artifacts(
        plan_output,
        attempt_output,
        registered_plan_bytes,
        registered_attempt_bytes,
    )
    _assert_artifact_bytes(result_output, registered_result_bytes, "full result")
    registered_summary_bytes = _serialized_json(compact_regime_retest_summary(result))
    _publish_exclusive_bytes(summary_output, registered_summary_bytes, "summary")
    _assert_registered_artifacts(
        plan_output,
        attempt_output,
        registered_plan_bytes,
        registered_attempt_bytes,
    )
    _assert_artifact_bytes(result_output, registered_result_bytes, "full result")
    _assert_artifact_bytes(summary_output, registered_summary_bytes, "summary")
    return result


def _print_summary(result: RegimeRetestScreenResult) -> None:
    print("Development diagnostics only - reused RESEARCH/FIT data, never promotion evidence.")
    print("One-shot attempt ledger consumed lineage trials 7, 8 and 9 before cache parsing.")
    for trial in result.trials:
        baseline, stress = trial.scenarios
        status = "ELIGIBLE" if trial.eligible else "REJECT"
        print(
            f"{trial.trial.trial_id}: {status}; "
            f"baseline log={baseline.combined.log_growth:.6f}, "
            f"stress log={stress.combined.log_growth:.6f}, "
            f"stress DD={stress.combined.maximum_drawdown:.4%}, "
            f"trades={baseline.combined.trades}/{stress.combined.trades}"
        )
    print(f"Screen decision: {result.selected_trial_id}")
    print("promotion_eligible=false shadow_allowed=false live_allowed=false")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Consume and run the fixed three-variant regime-retest development screen"
    )
    parser.add_argument("--cache-dir", type=Path, default=Path("data/historical"))
    args = parser.parse_args()
    result = run_regime_retest_screen(args.cache_dir)
    _print_summary(result)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
