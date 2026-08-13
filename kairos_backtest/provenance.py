"""Stable source identity for run manifests, including dirty working trees."""

from __future__ import annotations

import hashlib
import importlib.metadata
import platform
import sys
from dataclasses import asdict, dataclass
from pathlib import Path


def source_fingerprint(package_dir: Path | None = None) -> str:
    """Hash Python source paths and bytes in deterministic order."""
    root = package_dir or Path(__file__).parent
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*.py")):
        digest.update(path.relative_to(root).as_posix().encode())
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


@dataclass(frozen=True, slots=True)
class RuntimeProvenance:
    python: str
    implementation: str
    platform: str
    packages: tuple[tuple[str, str], ...]

    def as_dict(self) -> dict[str, object]:
        result = asdict(self)
        result["packages"] = dict(self.packages)
        return result


def runtime_provenance() -> RuntimeProvenance:
    """Record interpreter and resolved packages that can affect numeric output."""
    packages = tuple(
        (name, importlib.metadata.version(name))
        for name in ("kairos-backtest", "kairos-core", "kairos-quant-scouts", "numpy")
    )
    return RuntimeProvenance(
        python=platform.python_version(),
        implementation=platform.python_implementation(),
        platform=sys.platform,
        packages=packages,
    )


def runtime_manifest() -> dict[str, object]:
    return runtime_provenance().as_dict()
