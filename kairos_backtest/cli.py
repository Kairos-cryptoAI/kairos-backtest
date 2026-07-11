from __future__ import annotations

import argparse
import json
from dataclasses import asdict
from datetime import UTC, date, datetime
from pathlib import Path

from .data import BinanceArchiveLoader
from .evaluation import EvaluationResult, aggregate_results, evaluate
from .reporting import write_reports
from .scenarios import SCENARIOS, SYMBOLS, Horizon, default_horizons
from .strategy import generate_signals


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="Kairos dual-horizon historical evaluation")
    result.add_argument("--cache-dir", type=Path, default=Path("data/historical"))
    result.add_argument("--report-dir", type=Path, default=Path("reports/historical"))
    result.add_argument("--symbols", nargs="+", choices=SYMBOLS, default=list(SYMBOLS))
    result.add_argument("--horizons", nargs="+", choices=("5y", "12m"), default=["5y", "12m"])
    result.add_argument("--seed", type=int, default=42)
    return result


def yearly_segments(horizon: Horizon) -> list[tuple[date, date]]:
    """Bound peak memory while preserving chronological compounding."""
    if horizon.name != "5y":
        return [(horizon.start, horizon.end)]
    segments: list[tuple[date, date]] = []
    start = horizon.start
    while start < horizon.end:
        end = min(date(start.year + 1, start.month, 1), horizon.end)
        segments.append((start, end))
        start = end
    return segments


def main() -> int:
    args = parser().parse_args()
    loader = BinanceArchiveLoader(args.cache_dir)
    rows: list[dict[str, object]] = []
    manifests: list[dict[str, object]] = []
    initial_equity = 10_000.0
    for horizon in default_horizons():
        if horizon.name not in args.horizons:
            continue
        for symbol in args.symbols:
            scenario_segments: dict[str, list[EvaluationResult]] = {scenario: [] for scenario in SCENARIOS}
            for segment_start, segment_end in yearly_segments(horizon):
                candles, manifest = loader.load(symbol, segment_start, segment_end)
                signals = generate_signals(candles)
                manifest_row = asdict(manifest)
                manifest_row["horizon"] = horizon.name
                manifests.append(manifest_row)
                for scenario, execution in SCENARIOS.items():
                    scenario_segments[scenario].append(
                        evaluate(
                            candles,
                            signals,
                            initial_equity=initial_equity,
                            execution=execution,
                        )
                    )
                del signals, candles
            for scenario, segments in scenario_segments.items():
                result = aggregate_results(segments, initial_equity=initial_equity)
                rows.append(
                    {
                        "symbol": symbol,
                        "horizon": horizon.name,
                        "scenario": scenario,
                        "segments": len(segments),
                        **result.to_dict(),
                    }
                )
    json_path, csv_path = write_reports(rows, args.report_dir)
    run_manifest = {
        "schema_version": 1,
        "created_at": datetime.now(UTC).isoformat(),
        "seed": args.seed,
        "symbols": args.symbols,
        "horizons": args.horizons,
        "datasets": manifests,
        "reports": [str(json_path), str(csv_path)],
        "memory_bounded_yearly_segments": True,
        "no_live_orders": True,
    }
    (args.report_dir / "run-manifest.json").write_text(
        json.dumps(run_manifest, indent=2, sort_keys=True) + "\n"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
