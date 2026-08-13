from __future__ import annotations

import io
import zipfile
from datetime import UTC, date, datetime

import numpy as np
import pytest
from kairos_core.enums import Side
from kairos_quant.candles import Candle

from kairos_backtest.data import BinanceArchiveLoader
from kairos_backtest.evaluation import evaluate
from kairos_backtest.execution import ExecutionConfig
from kairos_backtest.provenance import runtime_manifest, source_fingerprint
from kairos_backtest.seeding import derive_seed
from kairos_backtest.strategy import StrategySignal, _rsi_series, generate_signals
from kairos_backtest.validation import canonical_candles


def candles(count: int, *, start_ms: int = 0) -> list[Candle]:
    return [
        Candle(
            symbol="BTCUSDT",
            timeframe="1m",
            open_time_ms=start_ms + index * 60_000,
            close_time_ms=start_ms + (index + 1) * 60_000 - 1,
            open=100 + index / 100,
            high=101 + index / 100,
            low=99 + index / 100,
            close=100.5 + index / 100,
            volume=100,
            quote_volume=10_000,
            taker_buy_volume=55,
        )
        for index in range(count)
    ]


def test_derived_seed_is_stable_and_namespaced():
    first = derive_seed(42, "BTCUSDT", "baseline", date(2025, 1, 1))
    assert first == derive_seed(42, "BTCUSDT", "baseline", date(2025, 1, 1))
    assert first != derive_seed(42, "ETHUSDT", "baseline", date(2025, 1, 1))
    assert first != derive_seed(43, "BTCUSDT", "baseline", date(2025, 1, 1))


def test_flat_price_series_has_neutral_rsi():
    result = _rsi_series(np.full(30, 100.0))

    assert np.all(result == 50.0)


def test_source_fingerprint_is_path_order_independent(tmp_path):
    first = tmp_path / "a.py"
    second = tmp_path / "nested" / "b.py"
    second.parent.mkdir()
    second.write_text("B = 2\n", encoding="utf-8")
    first.write_text("A = 1\n", encoding="utf-8")

    digest = source_fingerprint(tmp_path)

    assert digest == source_fingerprint(tmp_path)
    second.write_text("B = 3\n", encoding="utf-8")
    assert digest != source_fingerprint(tmp_path)


def test_runtime_manifest_captures_numeric_environment():
    manifest = runtime_manifest()

    assert manifest["python"]
    assert manifest["implementation"] == "CPython"
    packages = manifest["packages"]
    assert isinstance(packages, dict)
    assert set(packages) == {
        "kairos-backtest",
        "kairos-core",
        "kairos-quant-scouts",
        "numpy",
    }


def test_evaluation_uses_seed_for_jitter_and_canonical_signal_order():
    source = candles(300)
    signals = [
        StrategySignal(source[200].close_time_ms, Side.FLAT, 0.0, ("exit",)),
        StrategySignal(source[10].close_time_ms, Side.LONG, 1.0, ("entry",)),
    ]
    execution = ExecutionConfig(slippage_jitter_bps=3)

    first = evaluate(source, signals, initial_equity=10_000, execution=execution, seed=7)
    repeated = evaluate(source, list(reversed(signals)), initial_equity=10_000, execution=execution, seed=7)
    different_seed = evaluate(source, signals, initial_equity=10_000, execution=execution, seed=8)

    assert first == repeated
    assert first != different_seed


def test_strategy_prefix_is_unchanged_by_future_candles():
    prefix = candles(48_100)
    extended = candles(48_600)

    expected = generate_signals(prefix)
    actual = [
        signal for signal in generate_signals(extended) if signal.timestamp_ms <= prefix[-1].close_time_ms
    ]

    assert actual == expected


def test_candle_boundaries_reject_duplicates_overlap_and_mixed_symbols():
    source = candles(2)
    with pytest.raises(ValueError, match="duplicate"):
        canonical_candles([source[0], source[0]])

    overlap = Candle(
        symbol="BTCUSDT",
        timeframe="1m",
        open_time_ms=source[0].close_time_ms,
        close_time_ms=source[0].close_time_ms + 60_000,
        open=100,
        high=101,
        low=99,
        close=100,
        volume=1,
    )
    with pytest.raises(ValueError, match="overlap"):
        canonical_candles([source[0], overlap])

    other = Candle(
        symbol="ETHUSDT",
        timeframe="1m",
        open_time_ms=source[1].open_time_ms,
        close_time_ms=source[1].close_time_ms,
        open=100,
        high=101,
        low=99,
        close=100,
        volume=1,
    )
    with pytest.raises(ValueError, match="mix"):
        canonical_candles([source[0], other])


def _archive(rows: list[tuple[int, int]]) -> bytes:
    lines = []
    for opened, closed in rows:
        lines.append(f"{opened},100,101,99,100.5,10,{closed},1000,1,5")
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("BTCUSDT-1m-2025-01.csv", "\n".join(lines))
    return buffer.getvalue()


def test_cached_archive_respects_inclusive_start_and_exclusive_end(tmp_path):
    start, end = date(2025, 1, 2), date(2025, 1, 3)
    start_ms = int(datetime(2025, 1, 2, tzinfo=UTC).timestamp() * 1000)
    end_ms = int(datetime(2025, 1, 3, tzinfo=UTC).timestamp() * 1000)
    rows = [
        (start_ms - 60_000, start_ms - 1),
        (start_ms, start_ms + 59_999),
        (end_ms - 60_000, end_ms - 1),
        (end_ms, end_ms + 59_999),
    ]
    target = tmp_path / "BTCUSDT" / "1m" / "BTCUSDT-1m-2025-01.zip"
    target.parent.mkdir(parents=True)
    target.write_bytes(_archive(rows))

    loaded, manifest = BinanceArchiveLoader(tmp_path).load("BTCUSDT", start, end)

    assert [candle.open_time_ms for candle in loaded] == [start_ms, end_ms - 60_000]
    assert manifest.requested_start == "2025-01-02"
    assert manifest.requested_end == "2025-01-03"
    assert manifest.actual_start_ms == start_ms
    assert manifest.actual_end_ms == end_ms - 1
    assert manifest.rows == 2
    assert manifest.sha256
