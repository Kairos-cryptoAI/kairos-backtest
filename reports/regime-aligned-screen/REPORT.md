# Regime-aligned right-tail trend — trial 15

## Decision

`FORWARD_FREEZE_CANDIDATE`

The single preregistered reused-data attempt passed every absolute and
incremental gate. `regime_aligned_right_tail_v1` is now eligible to be frozen
unchanged for genuinely future observation beginning no earlier than
2026-09-01.

This is not an alpha pass. The synthesis was selected after both components
and all evaluated archives had been observed. `ALPHA_READY`, `PAPER_ALLOWED`,
`PROMOTION_ELIGIBLE` and `LIVE_ALLOWED` remain `false`; no automated order may
use this result.

## Candidate versus the exact base

The candidate preserves the daily `right_tail_trend_v1` direction and its
2 ATR stop, 4R target and 72-hour timeout. It accepts a long only when the last
complete four-hour close is above SMA200, and a short only when it is below.
The benchmark was replayed again on the same exact slices, execution model,
volume and costs rather than copied from an older report.

| Window | Scenario | Candidate return | Candidate PF | Candidate HAC Sharpe | Candidate DD | Candidate trades | Base return | Base PF | Base DD | Base trades |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| Selection | Baseline | 3.7322% | 1.3719 | 1.4231 | 1.1569% | 312 | 5.5692% | 1.3756 | 1.2308% | 459 |
| Selection | Stress | 1.7354% | 1.2040 | 0.8541 | 1.0891% | 312 | 2.3079% | 1.1833 | 1.2801% | 459 |
| Robustness | Baseline | 3.3046% | 1.3011 | 1.0798 | 1.7651% | 342 | 4.0817% | 1.2624 | 1.4780% | 494 |
| Robustness | Stress | 0.9812% | 1.1071 | 0.4239 | 1.6233% | 339 | 0.4968% | 1.0382 | 1.7652% | 489 |

The regime retained 67.97% of selection stress trades and 69.33% of
robustness stress trades, both above the frozen 50% minimum. Long/short counts
were 159/153 in selection and 161/178 in robustness stress, so the result was
not produced by deleting one direction.

The added state achieved its intended stress effect in both windows:

- stress profit factor rose from 1.1833 to 1.2040 in selection and from 1.0382
  to 1.1071 in robustness;
- stress drawdown fell from 1.2801% to 1.0891% and from 1.7652% to 1.6233%;
- both stress cells stayed positive, above PF 1.05 and above the minimum trade,
  direction, symbol, breadth and retention gates.

## Limitations visible in the result

This is a better candidate, not a demonstrated production strategy. Selection
baseline return fell from 5.57% to 3.73% and its PF was marginally lower than
the base. Robustness baseline drawdown increased from 1.48% to 1.77%. The gate
was deliberately aimed at adverse-cost survival, so these observations do not
reverse the pass, but they rule out claiming universal improvement.

Robustness stress had positive expectancy on only ETH, BNB and XRP. BTC lost
0.282% and SOL lost 0.661% in their equal-capital cells. Three positive symbols
meets the preregistered floor exactly; it is not comfortable breadth. The
portfolio still has a roughly 33% win rate, median result near -1.10R and p90
near +3.55R. Long losing sequences remain part of its intended right-tailed
distribution.

The annual reused-data returns are also far below the user's aspirational
monthly target. Raising leverage against these numbers would magnify model
uncertainty and drawdown rather than create evidence.

## Data and integrity

- Ten exact Binance USD-M `PRICE_VOLUME` slices passed the performance-blind
  preflight: 6,055,200 minute rows, 145 official checksum verifications, zero
  gaps and zero quarantined rows.
- The strategy sees only closed price bars. Volume is retained solely for the
  execution-capacity simulation; unused taker fields are zeroed.
- The attempt ledger was persisted after receipt validation and before the
  first market archive access.
- No API, LLM, network download, exchange mutation or paid service was used.

Immutable lineage:

- Strategy source: `8b00b82ed5d5dd5149532c596bed5ec8a825aadd`.
- Preregistration source: `aba92a3145484bde89fd7580a5193f994e6d70e6`.
- Plan SHA-256: `aae0730019cb5f78099b0b3e89afbe21fe1d4bb9ef8f247c74e53f349fc31730`.
- Preflight result SHA-256:
  `91b1331fead7a7392b7a21f406f67e95e57c3ad1fd370f2c0a472c71d276a4dd`.
- Attempt payload SHA-256:
  `4d5f0f83f8e5c12c2e026058f9f303e9152638e7b00770ef3925abff44289e5f`.
- Attempt canonical SHA-256:
  `e7c16126287f4adab5c63d76f63df82e6f03596ee7dd4bde9c262bf71259dbff`.
- Result canonical SHA-256:
  `bc31c3134b296a80a234ed2d87a3851a5e6f409666f87ffe4fb8646a5367fd53`.

## Forward gate

Freeze the exact source, configuration, universe and lifecycle. Beginning no
earlier than 2026-09-01, collect candidates and counterfactual execution in a
durable, read-only forward ledger. Do not modify the rule during the gate. The
minimum qualification horizon is 365 days and 500 trades, after which the same
cost, breadth, drawdown and integrity requirements must be evaluated once.

Until that succeeds, the candidate remains non-trading research. Any new
parameter, symbol rule, news overlay or derivatives filter is a separate
lineage and cannot inherit this result.

