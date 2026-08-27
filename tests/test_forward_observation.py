from __future__ import annotations

import json
from pathlib import Path

import pytest
from kairos_core.contracts import (
    ClosedBarEventV1,
    ExitPlanV1,
    StrategyIntentV1,
    StrategyProvenanceV1,
)
from kairos_core.enums import Side
from kairos_strategy.runtime_requirements import RuntimeRequirements

import kairos_backtest.forward_observation as forward
from kairos_backtest.forward_observation import (
    ForwardIntegrityError,
    ForwardLedger,
    IngestDisposition,
    expected_plan,
    load_plan,
    plan_sha256,
)


def _bar(open_time_ms: int, *, symbol: str = "BTCUSDT", close: float = 100.0) -> ClosedBarEventV1:
    return ClosedBarEventV1(
        source="quant-scouts",
        symbol=symbol,
        open_time_ms=open_time_ms,
        close_time_ms=open_time_ms + 59_999,
        open=close,
        high=close + 1,
        low=close - 1,
        close=close,
        base_volume=10,
        quote_volume=1_000,
        taker_buy_base_volume=5,
        taker_buy_quote_volume=500,
    )


def _intent(bar: ClosedBarEventV1) -> StrategyIntentV1:
    return StrategyIntentV1(
        source="strategy-engine",
        strategy_id=forward.STRATEGY_ID,
        strategy_revision="1",
        symbol=bar.symbol,
        side=Side.LONG,
        decision_ts_ms=bar.close_time_ms,
        entry_eligible_ts_ms=bar.close_time_ms + 1,
        entry_expires_ts_ms=bar.close_time_ms + 60 * 60 * 1_000,
        reference_price=100,
        signal_strength=0.5,
        gross_reward_bps=200,
        exit_plan=ExitPlanV1(stop_price=99, target_price=102, max_holding_ms=72 * 60 * 60 * 1_000),
        provenance=StrategyProvenanceV1(
            strategy_code_sha256="1" * 64,
            config_sha256="2" * 64,
            input_window_sha256="3" * 64,
            features_sha256="4" * 64,
            input_bar_sha256s=("5" * 64, "6" * 64),
        ),
    )


def test_plan_freezes_forward_boundary_lineage_and_permissions():
    plan = expected_plan()

    assert plan["data"]["blind_start_inclusive"] == "2026-09-01T00:00:00Z"
    assert plan["data"]["minimum_end_exclusive"] == "2027-09-01T00:00:00Z"
    assert plan["candidate"]["runtime_window_bars"] == 57_600
    assert plan["decision_rule"]["minimum_forward_trades"] == 500
    assert plan["protocol"]["early_performance_access"] is False
    assert not any(plan["permissions"].values())
    assert len(plan_sha256(plan)) == 64


def test_ledger_is_idempotent_and_status_discloses_no_performance(tmp_path: Path):
    path = tmp_path / "forward.sqlite3"
    bar = _bar(forward.WARMUP_START_MS)
    with ForwardLedger(path, expected_plan()) as ledger:
        assert ledger.ingest_bar(bar, as_of_ms=bar.close_time_ms) == (IngestDisposition.INSERTED, 0)
        assert ledger.ingest_bar(bar, as_of_ms=bar.close_time_ms) == (IngestDisposition.DUPLICATE, 0)
        ledger.verify_integrity()
        status = ledger.status()

    encoded = json.dumps(status, sort_keys=True)
    assert "pnl" not in encoded
    assert "profit_factor" not in encoded
    assert "net_return" not in encoded
    assert status["blind_performance_disclosed"] is False
    assert sum(item["bar_count"] for item in status["symbols"]) == 1


def test_gap_or_conflict_permanently_blocks_only_that_symbol(tmp_path: Path):
    path = tmp_path / "forward.sqlite3"
    first = _bar(forward.WARMUP_START_MS)
    with ForwardLedger(path, expected_plan()) as ledger:
        ledger.ingest_bar(first, as_of_ms=first.close_time_ms)
        gap = _bar(first.open_time_ms + 2 * 60_000)
        with pytest.raises(ForwardIntegrityError, match="gap or reorder"):
            ledger.ingest_bar(gap, as_of_ms=gap.close_time_ms)
        with pytest.raises(ForwardIntegrityError, match="is blocked"):
            ledger.ingest_bar(
                _bar(first.open_time_ms + 60_000),
                as_of_ms=first.close_time_ms + 60_000,
            )

        eth = _bar(forward.WARMUP_START_MS, symbol="ETHUSDT")
        assert ledger.ingest_bar(eth, as_of_ms=eth.close_time_ms)[0] is IngestDisposition.INSERTED


def test_conflicting_replay_is_not_treated_as_a_duplicate(tmp_path: Path):
    path = tmp_path / "forward.sqlite3"
    first = _bar(forward.WARMUP_START_MS)
    with ForwardLedger(path, expected_plan()) as ledger:
        ledger.ingest_bar(first, as_of_ms=first.close_time_ms)
        with pytest.raises(ForwardIntegrityError, match="conflicting"):
            ledger.ingest_bar(
                _bar(forward.WARMUP_START_MS, close=101),
                as_of_ms=first.close_time_ms,
            )


def test_unclosed_or_pre_warmup_bar_is_rejected_without_poisoning_state(tmp_path: Path):
    path = tmp_path / "forward.sqlite3"
    first = _bar(forward.WARMUP_START_MS)
    with ForwardLedger(path, expected_plan()) as ledger:
        with pytest.raises(ValueError, match="predates"):
            ledger.ingest_bar(_bar(forward.WARMUP_START_MS - 60_000))
        with pytest.raises(ValueError, match="not closed"):
            ledger.ingest_bar(first, as_of_ms=first.close_time_ms - 1)
        assert ledger.ingest_bar(first, as_of_ms=first.close_time_ms)[0] is IngestDisposition.INSERTED


def test_full_chain_verification_detects_database_mutation(tmp_path: Path):
    path = tmp_path / "forward.sqlite3"
    first = _bar(forward.WARMUP_START_MS)
    with ForwardLedger(path, expected_plan()) as ledger:
        ledger.ingest_bar(first, as_of_ms=first.close_time_ms)
        ledger.connection.execute(
            "UPDATE bars SET record_sha256=? WHERE symbol=? AND open_time_ms=?",
            ("f" * 64, first.symbol, first.open_time_ms),
        )
        with pytest.raises(ForwardIntegrityError, match="hash chain"):
            ledger.verify_integrity()


def test_reopen_refuses_a_different_campaign_plan(tmp_path: Path):
    path = tmp_path / "forward.sqlite3"
    plan = expected_plan()
    with ForwardLedger(path, plan):
        pass
    changed = dict(plan)
    changed["schema_version"] = "different"
    with pytest.raises(ForwardIntegrityError, match="metadata"):
        ForwardLedger(path, changed)


def test_decision_clock_is_the_close_of_the_0000_0059_utc_hour():
    assert forward._is_decision_bar(_bar(forward.BLIND_START_MS + 59 * 60_000))
    assert not forward._is_decision_bar(_bar(forward.BLIND_START_MS + 23 * 60 * 60_000 + 59 * 60_000))


def test_intent_is_persisted_only_after_blind_boundary_and_full_window(monkeypatch, tmp_path: Path):
    start = forward.WARMUP_START_MS
    monkeypatch.setattr(forward, "BLIND_START_MS", start)
    monkeypatch.setattr(forward, "MINIMUM_END_MS", start + 365 * 24 * 60 * 60 * 1_000)
    monkeypatch.setattr(forward, "OBSERVATION_WINDOW_BARS", 2)
    monkeypatch.setattr(
        forward,
        "get_runtime_requirements",
        lambda strategy_id: RuntimeRequirements(
            minimum_window_bars=2,
            decision_interval_bars=2,
            decision_phase_bars=0,
        ),
    )
    calls: list[tuple[ClosedBarEventV1, ...]] = []

    def generator(strategy_id, history):
        calls.append(history)
        return (_intent(history[-1]),)

    monkeypatch.setattr(forward, "generate_runtime_strategy_intents", generator)
    first = _bar(start)
    second = _bar(start + 60_000)
    path = tmp_path / "forward.sqlite3"
    with ForwardLedger(path, expected_plan()) as ledger:
        assert ledger.ingest_bar(first, as_of_ms=first.close_time_ms)[1] == 0
        assert ledger.ingest_bar(second, as_of_ms=second.close_time_ms)[1] == 1
        ledger.verify_integrity()
        assert sum(item["intent_count"] for item in ledger.status()["symbols"]) == 1

    assert len(calls) == 1
    assert calls[0] == (first, second)


def test_committed_plan_matches_executable_plan():
    root = Path(__file__).resolve().parents[1]
    plan = load_plan(root / forward.PLAN_FILENAME)
    assert plan_sha256(plan) == plan_sha256(expected_plan())
