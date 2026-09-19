"""Reviewed source digest allowlist for the immutable quarter-hour V5 runtime.

This value is deliberately separate from the hashed runtime implementation.
Changing a feature or V5 compatibility implementation therefore fails closed;
an intentional update requires a new reviewed digest here as well.
"""

from __future__ import annotations

V5_COMPATIBLE_RUNTIME_SOURCE_SHA256 = "95ee8a5216e8960d7e2274b1e3bc0c29d9ade165c7202d6259cbda3037f78cdd"
