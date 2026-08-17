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
`pyproject.toml` and `uv.lock`. Frozen validation also rejects an installed
`kairos-quant-scouts` distribution whose PEP 610 `direct_url.json` does not
contain that exact Git URL and commit.

## Reproducibility contract

- Candle inputs are canonicalized chronologically and conflicting timestamps
  are rejected.
- Every stochastic execution path is derived from an explicit seed.
- A signal becomes eligible only after its closed-candle timestamp plus
  configured latency. Execution uses the first one-minute candle whose open is
  at or after that eligibility time; fill capacity comes from the preceding
  fully closed candle, never the execution candle's future total volume.
- Production state changes require twelve consecutive five-minute confirmations,
  a four-hour minimum hold, and confidence of at least 0.67. Lower-timeframe
  disagreement vetoes entry but does not repeatedly close a valid senior trend.
- The evaluator and replay engine share one seeded fill model: adverse spread,
  slippage, fees, volume-participation limits, partial fills, and terminal close
  costs are applied consistently. Target orders are immediate-or-cancel: an
  unfilled remainder is not silently retried, and reports expose attempts,
  partial-fill counts and aggregate fill ratio. Closed-trade PnL includes entry
  and exit fees.
- Aggregated strategy frames contain complete, closed one-minute buckets only.
- Historical datasets use inclusive start and exclusive end boundaries and
  carry a SHA-256 fingerprint.
- CLI horizons can be fixed with `--as-of YYYY-MM-DD`; manifests record that
  value so a campaign can be reproduced later.
- Run manifests include a source-tree SHA-256, and each symbol/scenario/segment
  uses a stable sub-seed derived from the recorded base seed.
- Interpreter, platform and numeric dependency versions are captured because
  the lock may select different compatible wheels for Python 3.11 and 3.14.
- The memory-bounded 5y CLI uses 35 days of indicator warm-up before each later
  annual evaluation segment, but it liquidates and resets state at annual
  boundaries. Its compounded result is explicitly an independent-segment
  temporal diagnostic, not a continuous five-year backtest.

## Venue assumptions and promotion gate

The default fee is the EVEDEX base **taker** fee, 0.045% (4.5 bps) per fill; no
maker rebate or cashback is assumed. Funding is computed by EVEDEX over eight
hours and settled hourly at one eighth of that rate. Binance candle archives do
not contain historical EVEDEX funding, so baseline reports record
`funding_source="unavailable"`; stress uses an explicit adverse assumed rate.
An unavailable rate is never silently represented as observed zero funding.

Cached Binance ZIPs are CRC-checked and parsed rows are fingerprinted. When an
official adjacent `.CHECKSUM` sidecar exists, its SHA-256 is verified and any
mismatch fails closed. Without a sidecar the manifest says
`checksum_status="unavailable"`, not verified.

`evaluate_sensitivity` replays identical causal signals under multiple cost
assumptions. `evaluate_walk_forward` keeps training and test data disjoint with
a purge gap. `evaluate_promotion` is the machine-readable final gate: real API
promotion is denied for non-positive OOS return or expectancy, excessive
drawdown, insufficient trades, missing historical funding, or unstable/negative
sensitivity results. Missing dataset audits, checksum/inventory shortfalls,
invalid rows, gaps, or incomplete coverage also fail closed. Synthetic fixtures
validate methodology only and are not evidence of profitability.

The current saved historical strategy results are a research baseline, not a
production claim. A report with failed gates must be labelled `needs_revision`
and must not be promoted to a live venue.

## Frozen offline validation (data through 2026-07-31)

The reproducible machine-readable result is
[`reports/strategy-validation-2026-08-01/evaluation.json`](reports/strategy-validation-2026-08-01/evaluation.json).
It freezes `confirmation_bars=12`, `minimum_hold_bars=48`,
`minimum_confidence=0.67`, a 25% allocation per independent symbol account, and
`kairos-quant-scouts` commit `c74b9853bd97597b2104b2d9c4bcd5b7c6cefb24`.

- All 300 pre-holdout and five July archives passed their official SHA-256 and
  ZIP CRC. The July holdout is complete: 223,200/223,200 valid one-minute rows,
  no gaps and no invalid rows.
- The older five-year research inventory has 99.8905% valid-row coverage. It
  contains two documented historical gaps for each of SOL and XRP plus one
  checksum-valid but internally inconsistent XRP row. It is reported, not
  guessed or silently repaired. The 12-month and holdout windows have no gaps.
- In the 12-month diagnostic, the equal-weight mean return across independent
  symbol accounts is -4.2317% baseline and -9.7632% stress. Only ETH is
  positive under baseline; no symbol is positive under stress.
- Across three disjoint post-selection temporal folds, the mean independently
  compounded symbol return is +0.2400% baseline and -2.9871% stress (433
  trades). Because the frozen parameters were chosen after inspecting the same
  12-month window, these folds are a stability diagnostic, not OOS promotion
  evidence and not a synchronized portfolio return. XRP's terminal close is
  explicitly flagged as liquidity-incomplete in both fold scenarios; its
  residual is marked, not silently counted as a closed trade.
- In the untouched July holdout, all five symbols are negative: the equal-weight
  mean is -1.0760% baseline and -1.5781% stress (69 trades). The corresponding
  equal-weight buy-and-hold benchmark is +6.8286%.

The promotion gate is therefore `needs_revision` and `real_api_allowed=false`.
Blocking reasons include negative OOS return/expectancy, benchmark
underperformance, negative cost sensitivity, insufficient July trade counts,
unavailable historical EVEDEX funding, and the documented gaps/invalid row in
the broader research cache. Assumed stress funding is deliberately not accepted
as historical evidence. Only the untouched July window is used as OOS evidence
by the promotion gate.
