# Quarter-hour lag replication

Status: `HISTORICAL_FEATURE_COLLECTION_IN_PROGRESS`

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
