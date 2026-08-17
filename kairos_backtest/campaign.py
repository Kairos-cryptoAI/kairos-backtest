from __future__ import annotations

import argparse
import json
from dataclasses import asdict
from datetime import date
from pathlib import Path

from kairos_quant.candles import Candle

from .config import BASELINE, STRESS, SYMBOLS, CostScenario
from .data import BinanceArchiveLoader
from .evaluation import evaluate
from .execution import ExecutionConfig, FundingConfig
from .provenance import runtime_manifest, source_fingerprint
from .seeding import derive_seed
from .strategy import generate_signals


def _execution(scenario: CostScenario) -> ExecutionConfig:
    return ExecutionConfig(
        latency_ms=scenario.latency_ms,
        spread_bps=scenario.spread_bps,
        slippage_bps=scenario.slippage_bps,
        fee_bps=scenario.fee_bps,
        funding=FundingConfig(
            rate_8h_bps=scenario.funding_rate_8h_bps,
            source=scenario.funding_source,
            evidence=scenario.funding_evidence,
        ),
    )


def _number(value: object) -> float:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise TypeError(f"expected numeric report value, received {type(value).__name__}")
    return float(value)


def run_campaign(
    start: date,
    end: date,
    cache: Path,
    output: Path,
    label: str,
    *,
    seed: int = 42,
) -> dict[str, object]:
    loader = BinanceArchiveLoader(cache)
    output.mkdir(parents=True, exist_ok=True)
    reports: list[dict[str, object]] = []
    manifests: list[dict[str, object]] = []
    for symbol in SYMBOLS:
        source, manifest = loader.load(symbol, start, end)
        candles = [
            Candle(
                symbol=row.symbol,
                timeframe="1m",
                open_time_ms=row.open_time_ms,
                close_time_ms=row.close_time_ms,
                open=row.open,
                high=row.high,
                low=row.low,
                close=row.close,
                volume=row.volume,
                quote_volume=row.quote_volume,
                taker_buy_volume=row.taker_buy_volume,
            )
            for row in source
        ]
        signals = generate_signals(candles)
        manifests.append(asdict(manifest))
        for scenario in (BASELINE, STRESS):
            scenario_seed = derive_seed(seed, label, symbol, scenario.name, start, end)
            result = evaluate(
                candles,
                signals,
                initial_equity=10_000,
                execution=_execution(scenario),
                allocation=1.0,
                seed=scenario_seed,
            )
            row = result.to_dict()
            row.update(
                {
                    "symbol": symbol,
                    "horizon": label,
                    "scenario": scenario.name,
                    "seed": scenario_seed,
                    "classification": (
                        "promising"
                        if result.trades >= 30
                        and result.return_pct > 0
                        and result.metrics.max_drawdown < 0.25
                        and result.funding_evidence == "historical"
                        and result.funding_coverage_pct == 100
                        else "needs_revision"
                        if result.return_pct < 0
                        else "inconclusive_missing_historical_funding"
                        if result.funding_evidence != "historical"
                        else "inconclusive"
                    ),
                    "unavailable_features": [
                        "historical_order_book",
                        "open_interest",
                        "liquidations",
                        "news",
                        "historical_funding",
                    ],
                }
            )
            reports.append(row)
    portfolio = []
    for scenario in (BASELINE, STRESS):
        rows = [row for row in reports if row["scenario"] == scenario.name]
        portfolio.append(
            {
                "scenario": scenario.name,
                "initial_equity": 10_000,
                "allocation_per_symbol": 0.2,
                "return_pct": sum(_number(row["return_pct"]) for row in rows) / len(rows),
                "final_equity": 10_000
                * (1 + sum(_number(row["return_pct"]) for row in rows) / len(rows) / 100),
                "trades": sum(int(_number(row["trades"])) for row in rows),
            }
        )
    payload = {
        "schema_version": 1,
        "data_cutoff": end.isoformat(),
        "seed": seed,
        "source_sha256": source_fingerprint(),
        "runtime": runtime_manifest(),
        "horizon": label,
        "requested_start": start.isoformat(),
        "requested_end": end.isoformat(),
        "symbols": list(SYMBOLS),
        "cost_scenarios": [BASELINE.as_dict(), STRESS.as_dict()],
        "dataset_manifests": manifests,
        "results": reports,
        "portfolio": portfolio,
        "methodology": {
            "decision_path": "4h/1h regime; 30m/15m/5m setup; 3m/1m entry veto",
            "execution": "next available one-minute open after signal and latency",
            "look_ahead": "closed candles only",
            "initial_equity_per_symbol": 10_000,
        },
    }
    target = output / f"evaluation-{label}.json"
    target.write_text(
        json.dumps(payload, indent=2, sort_keys=True, allow_nan=False) + "\n",
        encoding="utf-8",
    )
    return payload


def main() -> None:
    parser = argparse.ArgumentParser(description="Run reproducible Kairos historical evaluation")
    parser.add_argument("--start", required=True, type=date.fromisoformat)
    parser.add_argument("--end", required=True, type=date.fromisoformat)
    parser.add_argument("--label", required=True)
    parser.add_argument("--cache", type=Path, default=Path(".cache/binance"))
    parser.add_argument("--output", type=Path, default=Path("reports"))
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args()
    run_campaign(args.start, args.end, args.cache, args.output, args.label, seed=args.seed)


if __name__ == "__main__":
    main()
