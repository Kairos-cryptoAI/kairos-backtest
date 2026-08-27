# kairos-backtest

> Strategy generators are owned by
> [`kairos-strategy-engine`](https://github.com/Kairos-cryptoAI/kairos-strategy-engine).
> This repository imports those exact modules and exposes only research,
> execution-simulation, and promotion adapters. No rejected sleeve is enabled
> for PAPER trading.

Deterministic event replay, historical evaluation and execution-cost simulation
for Kairos. The package is strictly offline during tests; downloading Binance
archives is an explicit CLI/runtime operation.

Historical consumers must declare a field profile and pass an exact-slice,
performance-blind preflight before consuming a research attempt. The rationale,
quarantine semantics and mandatory order are in
[`DATA_QUALITY_POLICY.md`](DATA_QUALITY_POLICY.md).

The first five-asset `PRICE_VOLUME` preflight failed without consuming a
research attempt because the known XRP row also has inconsistent quote volume;
its immutable evidence is in
[`failure-v1.json`](reports/data-field-preflight/failure-v1.json). The narrower
`PRICE_ONLY` v2 plan can be verified without opening the archive cache, then
executed as a data-only qualification:

```sh
uv run --locked kairos-data-preflight \
  --plan reports/data-field-preflight/plan-v2.json \
  --verify-plan

uv run --locked kairos-data-preflight \
  --cache data/historical \
  --plan reports/data-field-preflight/plan-v2.json \
  --result reports/data-field-preflight/result-v2.json
```

V2 passed all ten slices: 10,735,200 profiled minute rows, 245 official
checksum verifications, no in-slice gaps and one explicitly quarantined source
row. See the [preflight report](reports/data-field-preflight/REPORT.md).

The current multi-sleeve research design, risk contract and no-peeking rules are
documented in [`STRATEGY_V2.md`](STRATEGY_V2.md). No backtest or LLM response is
treated as authorization for real orders.

The plain-language comparison of the three completed strategy screens is in
[`reports/strategy-runs-report.md`](reports/strategy-runs-report.md), with a
visual overview in
[`reports/strategy-runs-report-overview.png`](reports/strategy-runs-report-overview.png).
The economic strategy-family map, market evolution and return/capacity
boundary are recorded in [`MARKET_RESEARCH.md`](MARKET_RESEARCH.md). Its first
descriptive-only executable study is preregistered in
[`reports/market-anatomy/plan.json`](reports/market-anatomy/plan.json).

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

Verify the frozen market-anatomy plan without opening historical values:

```sh
uv run --locked kairos-market-anatomy --verify-plan
```

After the study code and plan are committed in a clean worktree, run the
one-shot descriptive analysis with:

```sh
uv run --locked kairos-market-anatomy
```

The result can only identify a family for a separately preregistered prototype;
it cannot authorize PAPER, alpha promotion or LIVE trading.

The completed study returned `NO_PROTOTYPE_PASSED_DESCRIPTIVE_GATES`: causal
regime trend had a positive 24-hour right tail but failed the frozen stability
and hit-rate requirements, while breakout, post-shock reversion and hourly
taker-flow alignment were weaker. See the committed
[`report`](reports/market-anatomy/REPORT.md),
[`summary`](reports/market-anatomy/summary.json) and full
[`result`](reports/market-anatomy/result.json). The next research dimension is
historical basis/funding and leverage state, not another price-threshold trial.

That follow-up is now fixed as `derivatives_state_v1`. It evaluates official
Binance funding, premium-index and leverage archives without paid APIs. Its
fixed downloader, causal alignment, diagnostic families and fail-closed
permissions are documented in [MARKET_RESEARCH.md](MARKET_RESEARCH.md) and
committed before the factor cache is opened.

That study completed with `NO_PROTOTYPE_PASSED_DESCRIPTIVE_GATES`: none of the
fixed funding, premium, deleveraging or crowding-veto hypotheses was stable in
both reused windows. See its [report](reports/derivatives-state/REPORT.md),
[summary](reports/derivatives-state/summary.json) and immutable
[result](reports/derivatives-state/result.json).

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

## Order-flow volatility development screen

The next structurally distinct screen evaluates exactly three mutually
exclusive taker-flow hypotheses—impulse, three-bar persistence and flip
release—on reused 2022 research data. It uses complete closed five-minute
candles, prior-only rolling features, bounded ATR exits and the same managed
execution, cost and risk contracts:

```sh
uv run --locked kairos-orderflow-screen \
  --cache-dir data/historical \
  --plan-output reports/orderflow-screen/plan.json \
  --result-output reports/orderflow-screen/result.json \
  --summary-output reports/orderflow-screen/summary.json \
  --overwrite
```

The 2022-07-01 through 2022-12-31 screen also returned `REJECT_ALL`.
Persistence supplied 387 baseline and 301 stress trades, but returned -2.9005%
and -3.2540%; impulse returned -1.4559%/-1.2052% on 124/92 trades, and flip
release returned -0.6687%/-0.5590% on 80/60 trades. Every scenario had negative
expectancy and profit factor below 1.0. Frequency is therefore no longer the
main blocker: the standalone flow-continuation signal lacks net edge.

The committed [report](reports/orderflow-screen/REPORT.md), [compact
summary](reports/orderflow-screen/summary.json), immutable
[plan](reports/orderflow-screen/plan.json), and [data-quality
evidence](reports/orderflow-screen/data-quality.json) are development-only.
The 7.8 MB full replay remains ignored and is bound by byte length and SHA-256.
No API/model call or real order was made, and all promotion, shadow and live
permissions remain `false`.

## Regime/retest development screen

The third frozen experiment also returned `REJECT_ALL`. An expansion candle
only armed a setup; the strategy waited up to three complete five-minute bars
for a retest of the prior 12-bar boundary and a close back through that frozen
level. The three registered trials tested structural reclaim alone,
same-direction taker-flow reacceleration, and opposing-flow absorption.

The one-shot screen uses 62 days of warm-up from 2023-12-01 and evaluates the
reused `RESEARCH/FIT` interval `[2024-02-01, 2024-07-01)`.  This clean interval
starts after the known invalid XRP minute in November 2023; that source row is
excluded by the fixed boundary, never repaired or imputed. Trials are recorded
as cumulative research attempts 7, 8 and 9, and the consumed attempt must not
be rerun.

The common funnel reduced 41,741 structural breakout candidates to 184 armed
setups and 12 structural reclaims. Only one baseline trade was admitted and it
lost 0.015492%; stress admitted none. The flow and absorption variants
produced no trades. The hard conjunction of regime, expansion, retest, reclaim
and admission gates is therefore too restrictive for the intended frequency.

The screen requires positive baseline and stress economics, at least 165
closed trades per scenario, 17 per symbol, 50 distinct exit days, and 50
profitable-economics trades in each direction.  Trade count is a sufficiency
gate, never the ranking target.  The result remains development-only even if a
candidate passes; promotion, shadow and live permissions are always `false`.
The committed [report](reports/regime-retest-screen/REPORT.md), [compact
summary](reports/regime-retest-screen/summary.json), immutable
[plan](reports/regime-retest-screen/plan.json), consumed
[attempt](reports/regime-retest-screen/attempt.json), and [data-quality
evidence](reports/regime-retest-screen/data-quality.json) preserve the result.

## Quarter-hour flow reused-data screen

The next single-candidate experiment is preregistered in the committed
[plan](reports/quarter-hour-screen/plan.json). It evaluates the exact
`quarter_hour_flow_v1` generator imported from `kairos-strategy-engine`; there
is no research/runtime copy. The strategy is a deliberately disclosed 1-minute
proxy for a first-10-second market-microstructure result, not a replication of
the source paper.

Verify the plan without opening the historical cache:

```sh
uv run --locked kairos-quarter-hour-screen --verify-plan
```

After the preregistration commit is clean, run the one permitted reused-data
screen:

```sh
uv run --locked kairos-quarter-hour-screen \
  --cache-dir data/historical \
  --plan reports/quarter-hour-screen/plan.json \
  --result reports/quarter-hour-screen/summary.json
```

The screen requires official Binance checksums, evaluates baseline and stress
execution over fixed research, selection and robustness roles, and refuses to
overwrite its result. It can only return `REJECT_REUSED_DATA_SCREEN` or
`FORWARD_FREEZE_CANDIDATE`; both keep alpha, PAPER promotion and LIVE
permissions false. Any later alpha claim still requires a candidate frozen
before new blind data and at least eight months plus 500 forward trades.

## Right-tail trend reused-data screen

`right_tail_trend_v1` is an independently motivated, single-candidate test of
positive-skew time-series trend capture. It samples one closed-hour decision at
each UTC day boundary, uses a 24-hour return-to-realized-variation score, and
attaches a symmetric 2 ATR stop, 4R target and 72-hour timeout. There is no
volume, order-flow, funding, LLM, symbol-specific or side-specific threshold.

The earlier `market_anatomy_v1` decision remains
`NO_PROTOTYPE_PASSED_DESCRIPTIVE_GATES`; this candidate does not reinterpret
that result as authorization. Because its feature definition has already been
observed on all data through July 2026, the one permitted screen can only reject
the candidate or freeze it before future evidence. It cannot produce alpha,
PAPER or LIVE permission.

Verify the exact committed plan without opening the historical cache:

```sh
uv run --locked kairos-right-tail-screen --verify-plan
```

After the preregistration commit is clean, run the consumed reused-data screen:

```sh
uv run --locked kairos-right-tail-screen \
  --cache-dir data/historical \
  --plan reports/right-tail-screen/plan.json \
  --attempt reports/right-tail-screen/attempt.json \
  --result reports/right-tail-screen/summary.json
```

The attempt ledger is created and fsynced before the first archive access; a
crash or evaluation failure does not release the consumed trial. No alternative
parameter trial is allowed. Even `FORWARD_FREEZE_CANDIDATE`
requires at least 365 future days and 500 closed trades before a separate alpha
decision.

The consumed attempt returned `REJECT_REUSED_DATA_SCREEN`: robustness stress
profit factor was `1.0382706977`, below the preregistered `>1.05` gate. The
immutable evidence and interpretation are retained in the
[right-tail report](reports/right-tail-screen/REPORT.md). The exact candidate
must not be rerun or tuned into a passing result.

## Regime-aligned right-tail forward observation

Trial 15 preserves that right-tail lifecycle and admits it only when the last
complete four-hour close is on the matching side of its SMA200. Its one-shot
reused-data screen passed every preregistered absolute and base-improvement
gate, so the exact candidate is `FORWARD_FROZEN`, not alpha or PAPER-approved.
The independent period starts no earlier than `2026-09-01T00:00:00Z` and must
contain both 365 complete days and at least 500 simulated closed trades.

The committed [forward plan](reports/regime-aligned-forward/plan.json) has
SHA-256 `38fe7512b4e4c318e5bc8dd6baa66b48eedd63112a4a447eaaf36c1175f623e8`.
The observer accepts only strict `ClosedBarEventV1` JSONL, starts with the
feature-only warmup boundary, and writes a per-symbol append-only SHA-256 chain
to SQLite. The `PRICE_VOLUME` profile deterministically discards transport
envelope and taker-only fields before hashing, matching the historical screen.
It has no exchange, model, bus-publish or order code. Status exposes
coverage and intent counts but deliberately withholds PnL and performance until
the sealed gate is eligible.

```sh
uv run --locked kairos-forward-observer init \
  --ledger runtime/regime-aligned-forward.sqlite3

uv run --locked kairos-forward-observer ingest \
  --ledger runtime/regime-aligned-forward.sqlite3 \
  --input closed-bars.jsonl

uv run --locked kairos-forward-observer ingest-monthly-archives \
  --ledger runtime/regime-aligned-forward.sqlite3 \
  --cache-dir data/historical \
  --start 2026-07-23 \
  --end-exclusive 2026-08-01

uv run --locked kairos-forward-observer verify \
  --ledger runtime/regime-aligned-forward.sqlite3

uv run --locked kairos-forward-observer backup \
  --ledger runtime/regime-aligned-forward.sqlite3 \
  --output runtime/backups/regime-aligned-forward.sqlite3

uv run --locked kairos-forward-observer recovery-drill \
  --ledger runtime/regime-aligned-forward.sqlite3 \
  --backup runtime/backups/regime-aligned-forward.sqlite3 \
  --recovered runtime/recovery/regime-aligned-forward.sqlite3
```

A gap, reorder or conflicting final bar permanently quarantines that symbol.
Archive import is offline-only: every required ZIP and official `.CHECKSUM`
must already be present, every minute must be complete, and downloads are
disabled inside the observer.
Restart first verifies the immutable campaign identity; a different plan cannot
reuse the same ledger. No status or future evaluator may change the frozen
parameters, five-symbol universe, baseline/stress costs or blind boundary.
Backup uses SQLite's online backup API, refuses to overwrite any existing file,
and verifies the complete hash chain before returning. The recovery drill
restores only to a new path, compares a sealed evidence fingerprint across the
primary, backup and restored databases, and proves the primary remained
unchanged.

Monthly archives are not published early enough to bridge the last warmup month
into the blind start. The isolated daily collector therefore downloads every
completed UTC day from Binance's official public archive, requires the matching
official `.CHECKSUM`, ZIP CRC, exactly 1,440 contiguous minutes and the frozen
`PRICE_VOLUME` profile, then submits strict bars to the same ledger. It stages
the complete five-symbol request before advancing any symbol and is safely
idempotent after interruption:

```sh
uv run --locked python -m kairos_backtest.forward_collection \
  --ledger runtime/regime-aligned-forward.sqlite3 \
  --cache-dir data/forward-daily \
  --start 2026-08-01 \
  --end-exclusive 2026-09-01
```

The end is exclusive and cannot exceed the current UTC date, so the command can
be rerun daily with the next completed date. The collector contains no exchange
mutation, LLM, strategy decision or performance path. A conflicting official
bar is still a permanent ledger integrity failure.

The final evaluator is frozen before the blind start by
[its semantic-source lock](reports/regime-aligned-forward/evaluator-lock.json).
The lock covers the simulator, cost/risk model, portfolio metrics, scenario
definitions, observer, evaluator and dependency lock. `eligibility` remains
performance-blind: before 365 complete days it does not run the simulator; after
that boundary it may disclose only baseline/stress closed-trade counts. It does
not create an attempt until both counts reach 500.

```sh
uv run --locked kairos-forward-evaluator eligibility \
  --ledger runtime/regime-aligned-forward.sqlite3

uv run --locked kairos-forward-evaluator evaluate \
  --ledger runtime/regime-aligned-forward.sqlite3 \
  --attempt reports/regime-aligned-forward/final-attempt.json \
  --result reports/regime-aligned-forward/final-result.json
```

The final command regenerates every daily candidate from its exact rolling
40-day runtime window, verifies the complete stored intent inventory, and
evaluates the unchanged base under the same windows. Once eligible it fsyncs an
exclusive attempt before calculating any final metric. A crash permanently
consumes the attempt. A pass is only
`ALPHA_CANDIDATE_REQUIRES_SEPARATE_PAPER_APPROVAL`; it does not set alpha,
PAPER or LIVE permission. A failed gate returns `REJECT_FORWARD_EVIDENCE`.

## Crowded-trend continuation reused-data screen

`crowded_trend_continuation_v1` tests the one disclosed post-hoc observation
from `derivatives_state_v1`: an established 24-hour trend may continue while
open interest expands by at least 5% and either premium or funding is aligned
with that trend. The thresholds are copied unchanged, apply to all five symbols
and both directions, and cannot be searched or selectively disabled. The
generator accepts explicit timestamped factor observations; it owns no data or
exchange client.

Every complete UTC hour is eligible for a decision. Entry starts only on the
next minute, with one managed position per symbol, the existing 2 ATR / 4R
geometry and a 24-hour timeout matching the descriptive horizon. The screen
evaluates the fixed selection and robustness years under baseline and stress
execution, including fees, spread, slippage and adverse funding. Its attempt is
consumed before either price or factor archives are opened.

```sh
uv run --locked kairos-crowded-trend-screen --verify-plan

uv run --locked kairos-crowded-trend-screen \
  --price-cache data/historical \
  --factor-cache data/historical-factors \
  --plan reports/crowded-trend-screen/plan.json \
  --attempt reports/crowded-trend-screen/attempt.json \
  --result reports/crowded-trend-screen/summary.json
```

Because the direction came from already observed data, even a passing reused
screen only freezes this exact candidate for at least 365 future days and 500
trades. It never sets alpha, PAPER or LIVE permission.

The consumed attempt returned `REJECT_REUSED_DATA_SCREEN`. All four aggregate
cells were positive, but selection/robustness stress PF was only `1.0499` and
`1.0458` versus the strict `>1.05` gate, and short activity was below the fixed
minimum. This is the strongest tested candidate so far, but the thin stress
margin is not production alpha. The immutable result and interpretation are in
the [crowded-trend report](reports/crowded-trend-screen/REPORT.md); the exact
attempt must not be rerun or repaired post-hoc.

## Published Donchian ensemble reused-data screen

Trial 13 transcribes the independent long-only model in
[Catching Crypto Trends](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=5209907):
nine daily close-based Donchian horizons, monotonic mid-channel stops, 90-day
volatility targeting at 25%, a 2x cap and a relative 20% volatility-resize
deadband. The five Kairos assets are equal-capital sleeves, rebalanced monthly.

The paper models spot execution; Kairos must use perpetuals. Therefore the
screen additionally charges official historical Binance funding, 10 bps per
baseline allocation change and, under stress, 25 bps plus 5 bps adverse funding
per settlement. Targets decided at one daily close become effective on the next
UTC day. These adaptations and the fixed-universe limitation are explicit in
the immutable plan.

```sh
uv run --locked kairos-donchian-screen --verify-plan

uv run --locked kairos-donchian-screen \
  --price-cache data/historical \
  --factor-cache data/historical-factors \
  --plan reports/donchian-screen/plan.json \
  --attempt reports/donchian-screen/attempt.json \
  --result reports/donchian-screen/summary.json
```

The allocation contract is deliberately outside the current static SL/TP order
route. This reused-data screen can only reject or forward-freeze the exact model;
it cannot enable PAPER until both future evidence and a dynamic execution
lifecycle exist.

The one permitted attempt ended `INCONCLUSIVE_DATA_INTEGRITY` before any
portfolio metric was persisted: one checksum-verified official XRP archive has
taker-buy volume greater than total volume. The attempt remains consumed and
must not be rerun. Exact evidence and the research consequence are recorded in
the [immutable trial report](reports/donchian-screen/REPORT.md).

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
