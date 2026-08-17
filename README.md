# kairos-backtest

Deterministic event replay, historical evaluation and execution-cost simulation
for Kairos. The package is strictly offline during tests; downloading Binance
archives is an explicit CLI/runtime operation.

The current multi-sleeve research design, risk contract and no-peeking rules are
documented in [`STRATEGY_V2.md`](STRATEGY_V2.md). No backtest or LLM response is
treated as authorization for real orders.

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

## Strategy v2 development screen

The first bounded screen evaluates exactly three preregistered pullback-depth
variants on reused research data, across five symbols and fixed baseline and
stress assumptions. It runs offline and writes its immutable plan before
loading the cache:

```sh
uv run --locked kairos-development-screen \
  --cache-dir data/historical \
  --plan-output reports/development-screen/plan.json \
  --result-output reports/development-screen/result.json
```

The 2023-01-01 through 2023-06-30 screen returned `REJECT_ALL`. The medium
variant reached 105 baseline pullback trades but lost 0.7125%; shallow gained
0.1249% baseline but lost 0.0568% under stress and had only 55/16 trades; deep
gained 0.0406% baseline and 0.0267% under stress but had only 38/10 trades.
None met the fixed requirement of at least 100 pullback trades with positive
return, profit factor and expectancy in both scenarios.

The committed [report](reports/development-screen/REPORT.md), compact
[summary](reports/development-screen/summary.json), and immutable
[plan](reports/development-screen/plan.json) are development diagnostics only.
The full replay evidence is intentionally ignored because it is about 88.6 MB;
the report records its byte length and SHA-256 so a local reproduction can be
checked exactly. This result cannot authorize shadow or live trading.

## Reproducibility contract

- Candle inputs are canonicalized chronologically and conflicting timestamps
  are rejected.
- Every stochastic execution path is derived from an explicit seed.
- A signal becomes eligible only after its closed-candle timestamp plus
  configured latency. Execution uses the first one-minute candle whose open is
  at or after that eligibility time; fill capacity comes from the preceding
  fully closed candle, never the execution candle's future total volume.
- The legacy strategy baseline requires twelve consecutive five-minute confirmations,
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
a purge gap. The legacy `evaluate_promotion` function remains available only to
reproduce historical diagnostics and now always returns
`real_api_allowed=false`. The v2 gate adds a sealed trial inventory,
synchronized portfolio evidence, nested temporal selection, stress and
diversification checks, a separately locked terminal holdout, and an external
signed-attestation requirement. Offline evidence can authorize at most shadow
operation. Missing dataset audits, checksum/inventory shortfalls, invalid rows,
gaps, incomplete coverage or unverifiable provenance fail closed. Synthetic
fixtures validate methodology only and are not evidence of profitability.

The current saved historical strategy results are a research baseline, not a
production claim. A report with failed gates must be labelled `needs_revision`
and must not be promoted to a live venue.

## Frozen legacy offline validation (data through 2026-07-31)

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
- In the then-untouched July holdout, all five symbols are negative: the equal-weight
  mean is -1.0760% baseline and -1.5781% stress (69 trades). The corresponding
  equal-weight buy-and-hold benchmark is +6.8286%.

The promotion gate is therefore `needs_revision` and `real_api_allowed=false`.
Blocking reasons include negative OOS return/expectancy, benchmark
underperformance, negative cost sensitivity, insufficient July trade counts,
unavailable historical EVEDEX funding, and the documented gaps/invalid row in
the broader research cache. Assumed stress funding is deliberately not accepted
as historical evidence. July was used once by that legacy campaign and is now
reused robustness data; it cannot be presented as an untouched holdout for any
new candidate.
