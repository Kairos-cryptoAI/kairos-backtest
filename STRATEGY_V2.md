# Kairos strategy v2

## Executive Summary

Kairos strategy v2 is designed to search for frequent, repeatable trades, but
trade count is not the optimization target. A trade is admissible only when its
expected gross move remains large enough after taker fees, spread, slippage,
funding assumptions and a safety buffer. No historical result or LLM output is
allowed to bypass that rule.

The previous multi-timeframe strategy is a negative research baseline. Its
twelve-month result was `-4.2317%` under baseline costs and `-9.7632%` under
stress costs. The new framework therefore changes the trade contract, risk
sizing, exits, portfolio accounting and promotion method before any real model
or venue API is tested.

## Current sleeve status

| Sleeve | Purpose | Current status |
| --- | --- | --- |
| `trend_breakout_v1` | Five-minute Donchian continuation in an hourly trend | Retained as a benchmark; no tested variant has a confirmed net edge |
| `range_mean_reversion_v1` | Return inside a prior VWAP/ATR band in a flat hourly regime | Retired from tuning and retained as a reproducible negative control |
| `trend_pullback_reclaim_v1` | Reclaim after a bounded pullback inside a confirmed hourly trend | First three-variant screen rejected; retained only as development evidence |
| `orderflow_volatility_expansion_v1` | Immediate continuation after a volume/range/flow expansion | Second three-variant screen rejected; frequent variant had no net edge |
| `regime_veto_retest_reclaim_v1` | Prior-boundary retest after expansion with separate long/short rules | Third screen rejected; the stacked gates produced one baseline trade and zero stress trades |

The pullback family has exactly three preregistered depth variants:
`shallow`, `medium` and `deep`. Shared boundaries belong to only one variant,
so the three tests cannot count the same setup twice. There is no fourth
fallback variant if all three fail.

## First bounded development result

The first screen used reused research data from January through June 2023,
with 35 days of indicator warm-up, five symbols, fixed baseline and stress
execution assumptions, seed 42 and $100,000 of equal fixed capital. All 40
monthly archives covering the generation horizon passed their official
SHA-256 and ZIP checks; 1,742,400 expected rows were present with no gaps or
invalid rows. This is development evidence, not out-of-sample evidence.

| Pullback depth | Baseline trades | Baseline return | Baseline PF | Stress trades | Stress return | Stress PF | Decision |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| Shallow | 55 | +0.1249% | 1.130 | 16 | -0.0568% | 0.840 | Reject |
| Medium | 105 | -0.7125% | 0.683 | 41 | -0.1080% | 0.887 | Reject |
| Deep | 38 | +0.0406% | 1.051 | 10 | +0.0267% | 1.104 | Reject |

The registered decision is `REJECT_ALL`. Medium produced the desired baseline
frequency but had negative economics. Shallow did not survive stress. Deep was
slightly positive in both scenarios but produced too little evidence. A higher
cost scenario can have fewer losses because the pre-trade hurdle rejects more
entries; that is not evidence that stress improves the strategy.

The next research iteration must be a structurally different hypothesis, not a
fourth pullback-depth band selected after seeing these results. The full
methodology and artifact hashes are in
[`reports/development-screen/REPORT.md`](reports/development-screen/REPORT.md).

The subsequent order-flow screen also returned `REJECT_ALL`: its most frequent
variant produced 387 baseline and 301 stress trades but lost 2.9005% and
3.2540%. The third frozen hypothesis then waited for a bounded retest and
reclaim instead of entering on the expansion bar. It also returned
`REJECT_ALL`: 41,741 breakout candidates became 184 armed setups, 12 structural
reclaims, one baseline trade and zero stress trades. The sole trade lost
0.015492%. Trials 7-9 are consumed, and the result must not be rerun or reframed
as out-of-sample evidence. Full evidence is in
[`reports/regime-retest-screen/REPORT.md`](reports/regime-retest-screen/REPORT.md).

## Decision path

1. A sleeve consumes only complete, closed candles and emits an immutable
   intent with a stop, target, expiry and maximum holding time.
2. The cost/risk gate recalculates reward from the actual simulated entry. It
   rejects a trade below the all-in cost hurdle or below the minimum net
   reward-to-risk ratio. Signal confidence cannot increase position size.
3. Position size is bounded by loss at the protective stop, total costs,
   notional allocation and leverage limits.
4. The managed evaluator applies next-open latency, preceding-candle liquidity,
   partial fills, resting protective barriers, application-managed retries,
   funding settlements and a finite liquidation deadline.
5. Cell equity is reconstructed from daily cash, marked position state and the
   closed-trade ledger. A portfolio is derived from synchronized cells rather
   than an average of per-symbol returns.
6. Every parameter trial is recorded in a sealed append-only registry. Reused
   development data can reject a candidate, but cannot authorize shadow or
   live trading.
7. Promotion requires nested temporal evidence, stress replay, diversification,
   a separately locked terminal holdout and an external signed attestation.
   Offline evidence can authorize at most shadow operation; live orders remain
   a separate canary decision.

## Fixed risk and execution assumptions

- Default risk budget: `0.25%` of one isolated cell per trade.
- Maximum cell notional: `25%` of cell equity at no more than `1x` leverage.
- Default minimum net reward-to-risk after modeled costs: `1.25`.
- Baseline EVEDEX taker fee assumption: `4.5 bps` per fill.
- Stop, target and activated trailing protection are modeled as resting venue
  orders. Timeout and residual retries include application latency.
- Every strategy has a finite holding horizon plus a separately named terminal
  liquidation grace. Incomplete liquidation fails closed.
- Binance history does not contain historical EVEDEX funding or order-book
  state. Baseline funding is labelled unavailable; a stress assumption is not
  presented as observed history.

## Data roles and no-peeking rules

All locally cached minute archives have already been inspected and are reused
development data:

- research: July 2021 through June 2024;
- selection: July 2024 through June 2025;
- robustness diagnostics: July 2025 through July 2026.

The cache contains official SHA-256 sidecars and ZIP CRC evidence, but also two
known SOL/XRP gap intervals and one checksum-valid invalid XRP row. Gaps are
hard segment boundaries and are never forward-filled or repaired.

A new blind test can begin only after the candidate, parameters, dependencies,
container and protocol have been signed before the first unseen month. The
earliest proposed boundary is September 1, 2026 if the candidate is frozen by
August 31. The blind test ends no earlier than both eight calendar months and
500 closed portfolio trades. Any failed blind interval is consumed and cannot
be reused as out-of-sample evidence.

## Promotion criteria

The final portfolio gate is intentionally stricter than a positive backtest.
It requires, among other checks:

- at least 365 synchronized blind days and 500 closed portfolio trades;
- profitable results in at least 75% of registered outer folds;
- baseline profit factor at least `1.25`, HAC Sharpe at least `1.0`, Sortino at
  least `1.25`, Calmar at least `1.0`, and maximum drawdown no greater than
  `12%`;
- positive stress return, stress profit factor at least `1.10`, stress HAC
  Sharpe at least `0.5`, stress drawdown no greater than `15%`, and retention of
  at least half of baseline log growth;
- deflated-Sharpe probability at least `0.95`, CSCV probability of backtest
  overfitting no greater than `0.05`, and probability of loss no greater than
  `0.10`;
- no single symbol or sleeve contributing more than `40%` of positive PnL and
  enough independently surviving cells.

These thresholds are promotion controls, not a promise of returns. A target
such as 30% per month is not used for parameter selection because optimizing to
that target would reward leverage and overfitting rather than a durable edge.
