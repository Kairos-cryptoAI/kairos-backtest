"""A deterministic clock advanced only by replayed events."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(slots=True)
class ReplayClock:
    now_ms: int = 0

    def advance_to(self, timestamp_ms: int) -> None:
        if timestamp_ms < self.now_ms:
            raise ValueError("replay clock cannot move backwards")
        self.now_ms = timestamp_ms

    def advance_by(self, milliseconds: int) -> None:
        if milliseconds < 0:
            raise ValueError("milliseconds must be non-negative")
        self.now_ms += milliseconds
