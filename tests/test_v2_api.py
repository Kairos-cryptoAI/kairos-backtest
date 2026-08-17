from __future__ import annotations

import kairos_backtest.v2 as v2


def test_v2_facade_exports_only_explicit_managed_research_contracts() -> None:
    assert v2.__all__ == sorted(v2.__all__)
    assert len(v2.__all__) == len(set(v2.__all__))
    assert all(hasattr(v2, name) for name in v2.__all__)
    assert "evaluate_offline_to_shadow" in v2.__all__
    assert "run_development_screen" in v2.__all__
    assert "finalize_rejection" not in v2.__all__
    assert not hasattr(v2, "evaluate_promotion")
