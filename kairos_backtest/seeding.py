"""Stable sub-seeds for independent symbols, scenarios and time segments."""

from __future__ import annotations

import hashlib


def derive_seed(base_seed: int, *labels: object) -> int:
    """Derive a stable 64-bit seed without Python's process-randomized ``hash``."""
    payload = "\0".join([str(base_seed), *(str(label) for label in labels)]).encode()
    return int.from_bytes(hashlib.sha256(payload).digest()[:8], byteorder="big")
