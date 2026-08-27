from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from kairos_core.contracts import (
    ClosedBarEventV1,
    ExitPlanV1,
    StrategyIntentV1,
    StrategyProvenanceV1,
)
from kairos_core.enums import Side

import kairos_backtest.forward_evaluation as evaluation
import kairos_backtest.forward_observation as observation
from kairos_backtest.forward_evaluation import ForwardNotEligibleError


class _Ledger:
    def __init__(self, *, duration: bool, watermark_ms: int | None = None) -> None:
        self.plan_sha256 = observation.plan_sha256(observation.expected_plan())
        self.duration = duration
        self.watermark_ms = watermark_ms
        self.verified = 0

    def verify_integrity(self) -> None:
        self.verified += 1

    def status(self) -> dict[str, object]:
        return {
            "complete_blind_days": 365 if self.duration else 0,
            "duration_gate_satisfied": self.duration,
            "watermark_ms": self.watermark_ms,
        }

    def sealed_dataset_sha256(self, watermark_ms: int) -> str:
        assert watermark_ms == self.watermark_ms
        return "d" * 64


def _lock(ledger: _Ledger) -> dict[str, object]:
    return {
        "files": {},
        "plan_sha256": ledger.plan_sha256,
        "schema_version": evaluation.LOCK_SCHEMA_VERSION,
    }


def _scenario(*, trades: int = 500, total_return: float = 0.10) -> dict[str, object]:
    return {
        "active_symbols": 5,
        "direction_trades": {"LONG": 250, "SHORT": 250},
        "expectancy_usd_per_trade": 1.0,
        "hac_sharpe": 1.0,
        "maximum_drawdown": 0.05,
        "maximum_one_symbol_trade_share": 0.20,
        "per_symbol": [
            {"symbol": symbol, "trades": trades // len(observation.SYMBOLS)} for symbol in observation.SYMBOLS
        ],
        "positive_expectancy_symbols": 5,
        "profit_factor": 1.20,
        "total_return": total_return,
        "trades": trades,
    }


def _candidate(*, trades: int = 500) -> dict[str, dict[str, object]]:
    return {"baseline": _scenario(trades=trades), "stress": _scenario(trades=trades)}


def _base() -> dict[str, dict[str, object]]:
    baseline = _scenario(trades=800)
    stress = {**_scenario(trades=800), "maximum_drawdown": 0.06, "profit_factor": 1.10}
    return {"baseline": baseline, "stress": stress}


def test_duration_gate_never_opens_market_evaluation_or_performance() -> None:
    ledger = _Ledger(duration=False)

    def forbidden(*args, **kwargs):
        raise AssertionError("campaign evaluator must remain sealed")

    status = evaluation.evaluate_eligibility(ledger, campaign_evaluator=forbidden)

    assert status["duration_gate_satisfied"] is False
    assert status["trade_count_evaluated"] is False
    assert status["scenario_closed_trades"] is None
    encoded = json.dumps(status)
    assert "profit_factor" not in encoded
    assert "total_return" not in encoded
    assert "pnl" not in encoded.lower()


def test_insufficient_blind_trade_counts_do_not_consume_attempt(tmp_path: Path) -> None:
    ledger = _Ledger(duration=True, watermark_ms=observation.MINIMUM_END_MS)
    attempt = tmp_path / "attempt.json"
    result = tmp_path / "result.json"

    def evaluator(ledger, watermark_ms, dataset_sha256, include_base):
        assert include_base is False
        return _candidate(trades=499), None

    with pytest.raises(ForwardNotEligibleError, match="500 closed trades"):
        evaluation.evaluate_forward_ledger(
            ledger,  # type: ignore[arg-type]
            evaluator_lock=_lock(ledger),
            attempt_path=attempt,
            result_path=result,
            campaign_evaluator=evaluator,
        )

    assert not attempt.exists()
    assert not result.exists()


def test_attempt_is_durable_before_final_metrics_and_cannot_be_reused(tmp_path: Path) -> None:
    ledger = _Ledger(duration=True, watermark_ms=observation.MINIMUM_END_MS)
    attempt = tmp_path / "attempt.json"
    result = tmp_path / "result.json"
    calls: list[bool] = []

    def evaluator(ledger, watermark_ms, dataset_sha256, include_base):
        calls.append(include_base)
        if include_base:
            assert attempt.exists()
            return _candidate(), _base()
        assert not attempt.exists()
        return _candidate(), None

    summary = evaluation.evaluate_forward_ledger(
        ledger,  # type: ignore[arg-type]
        evaluator_lock=_lock(ledger),
        attempt_path=attempt,
        result_path=result,
        campaign_evaluator=evaluator,
        now=lambda: datetime(2027, 9, 1, tzinfo=UTC),
    )

    assert calls == [False, True]
    assert summary["classification"] == "ALPHA_CANDIDATE_REQUIRES_SEPARATE_PAPER_APPROVAL"
    assert not any(summary["permissions"].values())
    assert attempt.exists() and result.exists()
    with pytest.raises(FileExistsError, match="one-shot"):
        evaluation.evaluate_forward_ledger(
            ledger,  # type: ignore[arg-type]
            evaluator_lock=_lock(ledger),
            attempt_path=attempt,
            result_path=result,
            campaign_evaluator=evaluator,
        )


def test_final_failure_is_a_rejection_not_a_permission(tmp_path: Path) -> None:
    ledger = _Ledger(duration=True, watermark_ms=observation.MINIMUM_END_MS)
    attempt = tmp_path / "attempt.json"
    result = tmp_path / "result.json"

    def evaluator(ledger, watermark_ms, dataset_sha256, include_base):
        candidate = _candidate()
        if include_base:
            candidate["stress"] = {**candidate["stress"], "total_return": -0.01}
            return candidate, _base()
        return candidate, None

    summary = evaluation.evaluate_forward_ledger(
        ledger,  # type: ignore[arg-type]
        evaluator_lock=_lock(ledger),
        attempt_path=attempt,
        result_path=result,
        campaign_evaluator=evaluator,
    )

    assert summary["classification"] == "REJECT_FORWARD_EVIDENCE"
    assert "candidate.stress.total_return_not_positive" in summary["gate_failures"]
    assert not any(summary["permissions"].values())


def test_gate_requires_stress_retention_and_base_outperformance() -> None:
    candidate = _candidate()
    candidate["stress"] = {**candidate["stress"], "trades": 500, "profit_factor": 1.06}
    candidate["baseline"] = {**candidate["baseline"], "trades": 800}
    base = _base()
    base["stress"] = {**base["stress"], "trades": 1_100, "profit_factor": 1.07}

    failures = evaluation.gate_failures(candidate, base)

    assert "stress_trade_retention_below_minimum" in failures
    assert "candidate.stress.base_trade_retention_below_minimum" in failures
    assert "candidate.stress.profit_factor_not_above_base" in failures


def test_strict_to_raw_preserves_the_pure_candidate_identity() -> None:
    history = tuple(
        observation._price_volume_bar(
            ClosedBarEventV1(
                source="fixture",
                symbol="BTCUSDT",
                open_time_ms=observation.WARMUP_START_MS + index * 60_000,
                close_time_ms=observation.WARMUP_START_MS + index * 60_000 + 59_999,
                open=100,
                high=101,
                low=99,
                close=100,
                base_volume=10,
                quote_volume=1_000,
                taker_buy_base_volume=0,
                taker_buy_quote_volume=0,
            )
        )
        for index in range(2)
    )
    strict = StrategyIntentV1(
        source="strategy-engine",
        strategy_id=observation.STRATEGY_ID,
        strategy_revision="1",
        symbol="BTCUSDT",
        side=Side.LONG,
        decision_ts_ms=history[-1].close_time_ms,
        entry_eligible_ts_ms=history[-1].close_time_ms + 1,
        entry_expires_ts_ms=history[-1].close_time_ms + 3_600_001,
        reference_price=100,
        signal_strength=0.5,
        gross_reward_bps=200,
        exit_plan=ExitPlanV1(
            stop_price=99,
            target_price=102,
            max_holding_ms=72 * 3_600_000,
        ),
        provenance=StrategyProvenanceV1(
            strategy_code_sha256=observation.STRATEGY_SOURCE_TREE_SHA256,
            config_sha256=observation.STRATEGY_CONFIG_SHA256,
            input_window_sha256="3" * 64,
            features_sha256="4" * 64,
            input_bar_sha256s=tuple(item.bar_sha256 for item in history),
        ),
        metadata=(("feature_hash", "fixture"),),
    )

    raw = evaluation._strict_to_raw(strict)

    assert raw.sleeve_id == strict.strategy_id
    assert raw.metadata == strict.metadata
    assert raw.exit_plan.max_holding_ms == strict.exit_plan.max_holding_ms
