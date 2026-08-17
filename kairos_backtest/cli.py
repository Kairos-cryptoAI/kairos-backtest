from __future__ import annotations

import argparse
import json
from bisect import bisect_left
from dataclasses import asdict
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from .data import BinanceArchiveLoader
from .evaluation import EvaluationResult, aggregate_results
from .harness import evaluate_window
from .provenance import runtime_manifest, source_fingerprint
from .reporting import write_reports
from .scenarios import SCENARIOS, SYMBOLS, Horizon, default_horizons
from .seeding import derive_seed
from .strategy import generate_signals

SEGMENT_WARMUP_DAYS = 35


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="Kairos dual-horizon historical evaluation")
    result.add_argument("--cache-dir", type=Path, default=Path("data/historical"))
    result.add_argument("--report-dir", type=Path, default=Path("reports/historical"))
    result.add_argument("--symbols", nargs="+", choices=SYMBOLS, default=list(SYMBOLS))
    result.add_argument("--horizons", nargs="+", choices=("5y", "12m"), default=["5y", "12m"])
    result.add_argument("--seed", type=int, default=42)
    result.add_argument("--as-of", type=date.fromisoformat)
    return result


def yearly_segments(horizon: Horizon) -> list[tuple[date, date]]:
    """Bound peak memory for independently evaluated temporal segments."""
    if horizon.name != "5y":
        return [(horizon.start, horizon.end)]
    segments: list[tuple[date, date]] = []
    start = horizon.start
    while start < horizon.end:
        end = min(date(start.year + 1, start.month, 1), horizon.end)
        segments.append((start, end))
        start = end
    return segments


def segment_warmup_start(horizon: Horizon, segment_start: date) -> date:
    """Return bounded indicator warm-up without preceding the research horizon."""
    if not horizon.start <= segment_start < horizon.end:
        raise ValueError("segment start must fall inside its horizon")
    return max(horizon.start, segment_start - timedelta(days=SEGMENT_WARMUP_DAYS))


def main() -> int:
    args = parser().parse_args()
    loader = BinanceArchiveLoader(args.cache_dir)
    rows: list[dict[str, object]] = []
    manifests: list[dict[str, object]] = []
    scenario_seeds: dict[str, list[int]] = {}
    initial_equity = 10_000.0
    as_of = args.as_of or datetime.now(UTC).date()
    for horizon in default_horizons(as_of):
        if horizon.name not in args.horizons:
            continue
        for symbol in args.symbols:
            scenario_segments: dict[str, list[EvaluationResult]] = {scenario: [] for scenario in SCENARIOS}
            for segment_start, segment_end in yearly_segments(horizon):
                warmup_start = segment_warmup_start(horizon, segment_start)
                candles, manifest = loader.load(symbol, warmup_start, segment_end)
                signals = generate_signals(candles)
                open_times = [candle.open_time_ms for candle in candles]
                evaluation_start_ms = int(
                    datetime.combine(segment_start, datetime.min.time(), UTC).timestamp() * 1000
                )
                start_index = bisect_left(open_times, evaluation_start_ms)
                if start_index >= len(candles) or candles[start_index].open_time_ms != evaluation_start_ms:
                    raise ValueError(f"incomplete segment boundary for {symbol} at {segment_start}")
                manifest_row = asdict(manifest)
                manifest_row["horizon"] = horizon.name
                manifest_row["warmup_start"] = warmup_start.isoformat()
                manifest_row["evaluation_start"] = segment_start.isoformat()
                manifest_row["evaluation_end"] = segment_end.isoformat()
                manifests.append(manifest_row)
                for scenario, execution in SCENARIOS.items():
                    scenario_seed = derive_seed(
                        args.seed,
                        horizon.name,
                        symbol,
                        scenario,
                        segment_start,
                        segment_end,
                    )
                    scenario_segments[scenario].append(
                        evaluate_window(
                            candles,
                            signals,
                            start_index=start_index,
                            end_index=len(candles),
                            initial_equity=initial_equity,
                            execution=execution,
                            seed=scenario_seed,
                            allow_incomplete_terminal=True,
                        )
                    )
                    seed_key = f"{horizon.name}:{symbol}:{scenario}"
                    scenario_seeds.setdefault(seed_key, []).append(scenario_seed)
                del signals, candles
            for scenario, segments in scenario_segments.items():
                result = aggregate_results(segments, initial_equity=initial_equity)
                rows.append(
                    {
                        "symbol": symbol,
                        "horizon": horizon.name,
                        "scenario": scenario,
                        "segments": len(segments),
                        "segment_seeds": scenario_seeds[f"{horizon.name}:{symbol}:{scenario}"],
                        "aggregation_method": "compounded_independent_temporal_segments",
                        "continuous_strategy_state_across_segments": len(segments) == 1,
                        "segment_boundary_liquidation": len(segments) > 1,
                        **result.to_dict(),
                    }
                )
    json_path, csv_path = write_reports(rows, args.report_dir)
    run_manifest = {
        "schema_version": 1,
        "as_of": as_of.isoformat(),
        "seed": args.seed,
        "source_sha256": source_fingerprint(),
        "runtime": runtime_manifest(),
        "symbols": args.symbols,
        "horizons": args.horizons,
        "datasets": manifests,
        "reports": [str(json_path), str(csv_path)],
        "memory_bounded_yearly_segments": True,
        "yearly_segment_warmup_days": SEGMENT_WARMUP_DAYS,
        "five_year_result_interpretation": (
            "independent yearly temporal diagnostics compounded geometrically; strategy state "
            "and positions do not continue across yearly boundaries"
        ),
        "no_live_orders": True,
    }
    (args.report_dir / "run-manifest.json").write_text(
        json.dumps(run_manifest, indent=2, sort_keys=True, allow_nan=False) + "\n",
        encoding="utf-8",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
