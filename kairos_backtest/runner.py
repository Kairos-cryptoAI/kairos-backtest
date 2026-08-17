from __future__ import annotations

import json
from dataclasses import asdict
from datetime import UTC, date, datetime
from pathlib import Path

from .data import BinanceArchiveLoader
from .evaluation import evaluate
from .execution import ExecutionConfig, FundingConfig
from .provenance import runtime_manifest, source_fingerprint
from .seeding import derive_seed
from .strategy import generate_signals

SYMBOLS = ("BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT")
SCENARIOS = {
    "baseline": ExecutionConfig(
        fee_bps=4.5,
        spread_bps=2.0,
        slippage_bps=2.0,
        latency_ms=250,
        funding=FundingConfig(),
    ),
    "stress": ExecutionConfig(
        fee_bps=4.5,
        spread_bps=4.0,
        slippage_bps=4.0,
        latency_ms=500,
        funding=FundingConfig(
            rate_8h_bps=5.0,
            source="assumed_adverse_stress",
            evidence="assumed",
        ),
    ),
}


def run_horizon(
    *,
    start: date,
    end: date,
    horizon: str,
    cache_dir: Path,
    report_dir: Path,
    initial_equity: float = 10_000.0,
    seed: int = 42,
) -> list[dict[str, object]]:
    loader = BinanceArchiveLoader(cache_dir)
    rows: list[dict[str, object]] = []
    report_dir.mkdir(parents=True, exist_ok=True)
    for symbol in SYMBOLS:
        candles, manifest = loader.load(symbol, start, end)
        signals = generate_signals(candles)
        for scenario_name, execution in SCENARIOS.items():
            scenario_seed = derive_seed(seed, horizon, symbol, scenario_name, start, end)
            result = evaluate(
                candles,
                signals,
                initial_equity=initial_equity,
                execution=execution,
                seed=scenario_seed,
            )
            report = {
                "schema_version": 1,
                "symbol": symbol,
                "horizon": horizon,
                "scenario": scenario_name,
                "initial_equity": initial_equity,
                "seed": scenario_seed,
                "source_sha256": source_fingerprint(),
                "runtime": runtime_manifest(),
                "dataset": asdict(manifest),
                "execution": asdict(execution),
                "result": result.to_dict(),
            }
            path = report_dir / f"{symbol}-{horizon}-{scenario_name}.json"
            path.write_text(
                json.dumps(report, indent=2, sort_keys=True, allow_nan=False) + "\n",
                encoding="utf-8",
            )
            rows.append(report)
    index = report_dir / f"index-{horizon}.json"
    index.write_text(
        json.dumps(rows, indent=2, sort_keys=True, allow_nan=False) + "\n",
        encoding="utf-8",
    )
    return rows


def full_year_bounds(today: date | None = None) -> tuple[date, date, date]:
    now = today or datetime.now(UTC).date()
    end = date(now.year, 1, 1)
    return date(end.year - 5, 1, 1), date(end.year - 1, 1, 1), end
