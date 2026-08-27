# Quarter-hour lag replication

Status: `V1_INCOMPLETE_DATA`; performance-blind v2 amendment preregistered

This is a performance-blind, partial replication of the lag-only forecasting
result in *The Quarter-Hour Effect*.  It is not a strategy backtest and cannot
authorize ALPHA, PAPER, or LIVE.

The committed plan fixes the source, sample, Kairos universe, causal target,
12-lag model, rolling six-month/monthly-refit protocol, lambda grid, placebo
phases, gap handling, metrics, and rejection gates before the historical
result is opened.

The replication is partial because the source paper studied BTC, ETH, XRP,
SOL, DOGE, and ADA.  Kairos studies BTC, ETH, SOL, BNB, and XRP.  Four assets
overlap; BNB is explicitly treated as an independent extension, while DOGE
and ADA are not silently replaced.

The authors explicitly describe the forecast as an execution or liquidity
input, not a standalone trading strategy: its reported mean gross predictable
component is about 0.5 basis points per boundary, below ordinary taker and
maker round-trip fees.  Even a successful replication therefore advances
only a statistical component.  A separate, preregistered cost-aware lifecycle
test would still be required.

Primary source:

- <https://arxiv.org/abs/2607.09426v2>
- <https://github.com/binance/binance-public-data>

## Immutable v1 outcome

V1 stopped before fitting a model or evaluating any return metric. The first
rejected batch, BTCUSDT 2021-02, contains 22,806 missing aggregate-trade IDs in
three exact gaps. The largest gap covers 22,785 IDs and 27 minutes on
2021-02-09. The official monthly ZIP passed its adjacent SHA-256 and ZIP CRC.

This was not a monthly-archive packaging defect. Independently downloaded,
checksum-verified official daily archives reproduce the same three endpoint
IDs and timestamps: 22,793 missing IDs on 2021-02-09 and 13 on 2021-02-24.
Consequently the original zero-gap requirement failed and the immutable
[v1 result](result.json) is `INCOMPLETE_DATA`. It contains no R2, DM, accuracy,
return, PnL, or trading claim.

The failure exposed source-native gaps, not strategy performance. Before any
model metric was opened, a separate [v2 plan](../quarter-hour-lag-replication-v2/plan.json)
was therefore frozen. V2 never fills or guesses a missing trade. It requires
every aggregate-ID gap to have an exact proof from official daily archives,
excludes affected targets, and lets the twelve-lag builder remove every row
whose causal predictor chain crosses an excluded target. All-target metrics
become diagnostics; clean-target metrics are the authoritative gates.

## Performance-blind monthly canary

A January 2021, five-symbol canary was used only to qualify throughput and data
shape. It opened no model metric and created no trading result.

- 5 official monthly ZIPs, 149,199,030 parsed aggregate trades;
- 59,456 measured phase windows out of 59,520 scheduled windows;
- 5 first-boundary windows lacked a preceding-month reference;
- 59 forward windows were empty, all on SOL/BNB/XRP;
- zero missing aggregate-trade IDs across all five archives;
- 29,332 missing raw-trade IDs in the source stream;
- 183 measured targets (0.3078%) crossed a raw-ID gap and are marked for the
  preregistered clean-target sensitivity pass;
- 10 minutes 9 seconds wall time with four workers, including cached BTC and
  four new downloads.

The canary ledger is intentionally superseded: a subsequent integrity audit
extended the batch hash to bind the last cross-month trade and every SQL query
column used by the model. Historical collection therefore restarts under that
new source fingerprint rather than migrating or trusting the older ledger.
