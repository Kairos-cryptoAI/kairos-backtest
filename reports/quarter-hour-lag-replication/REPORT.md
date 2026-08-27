# Quarter-hour lag replication

Status: `PREREGISTERED_NOT_RUN`

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
