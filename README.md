# kairos-backtest

Deterministic event replay, historical evaluation and execution-cost simulation
for Kairos. The package is strictly offline during tests; downloading Binance
archives is an explicit CLI/runtime operation.

## Reproducible local checks

The lock requires `uv` 0.12.3 and Python 3.11. CI also blocks on Linux Python
3.14 and Windows Python 3.11.

```sh
uv sync --locked
uv run --locked ruff check kairos_backtest tests
uv run --locked ruff format --check kairos_backtest tests
uv run --locked mypy kairos_backtest
uv run --locked bandit -q -r kairos_backtest
uv run --locked pytest -q --tb=short
uv build --no-sources
```

`make check` runs the same sequence. Internal dependencies resolve from the
exact `kairos-core` and `kairos-quant-scouts` commits recorded in
`pyproject.toml` and `uv.lock`.

## Reproducibility contract

- Candle inputs are canonicalized chronologically and conflicting timestamps
  are rejected.
- Every stochastic execution path is derived from an explicit seed.
- Signals only execute after their close timestamp plus configured latency.
- The evaluator and replay engine share one seeded fill model: adverse spread,
  slippage, fees, volume-participation limits, partial fills, and terminal close
  costs are applied consistently. Closed-trade PnL includes entry and exit fees.
- Aggregated strategy frames contain complete, closed one-minute buckets only.
- Historical datasets use inclusive start and exclusive end boundaries and
  carry a SHA-256 fingerprint.
- CLI horizons can be fixed with `--as-of YYYY-MM-DD`; manifests record that
  value so a campaign can be reproduced later.
- Run manifests include a source-tree SHA-256, and each symbol/scenario/segment
  uses a stable sub-seed derived from the recorded base seed.
- Interpreter, platform and numeric dependency versions are captured because
  the lock may select different compatible wheels for Python 3.11 and 3.14.
