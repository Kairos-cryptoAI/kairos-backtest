"""Reviewed source digest allowlist for the immutable quarter-hour V5 runtime.

This value is deliberately separate from the hashed runtime implementation.
Changing a feature or V5 compatibility implementation therefore fails closed;
an intentional update requires a new reviewed digest here as well.
"""

from __future__ import annotations

V5_COMPATIBLE_RUNTIME_SOURCE_SHA256 = "01f06aa5663730fa770da75070896a49c0316022015a05afb093ec63d86c5171"
