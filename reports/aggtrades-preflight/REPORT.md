# Official aggregate-trade data preflight

## Decision

`DATA_PREFLIGHT_PASSED`

The exact one-day, five-symbol transport canary passed. Kairos can now ingest
official Binance USD-M `aggTrades` at transaction resolution, verify their
published checksums and produce a deterministic normalized manifest chain.

This is data evidence only. No directional statistic, fitted model, strategy
generator, simulated trade, return or PnL was calculated. Alpha, PAPER,
promotion and LIVE permissions remain `false`.

## Evidence

The plan was committed before the cache was opened. It is bound by SHA-256
`9593cd63db924a926df1a9bc1fa2344d1eeab93f79b8ba4f01c01f6b138202ff`
and ran from signed source commit
`f6c35d2ee57c6f68eb05f417eacdf26d8ca49c6d`.

| Symbol | Aggregate rows | Missing aggregate IDs | Missing raw trade IDs |
| --- | ---: | ---: | ---: |
| BTCUSDT | 2,326,880 | 0 | 16,292 |
| ETHUSDT | 1,768,286 | 0 | 20,996 |
| SOLUSDT | 469,325 | 0 | 19,012 |
| BNBUSDT | 356,444 | 0 | 9,846 |
| XRPUSDT | 429,169 | 0 | 19,650 |
| **Total** | **5,350,104** | **0** | **85,796** |

All five official archive SHA-256 sidecars, ZIP CRCs, sole-member names,
seven-field schemas, value domains, UTC-day bounds and ordering rules passed.
The append-only SQLite evidence chain ends at
`c2cfc5f4e0377fcd97fc747cec9964c553e153144e82d3920579499a1de55d87`.
The canonical result SHA-256 is
`1b6a6a61a13d7a0c3b899a93d2dad7ae5039bffc9f85e74565978ea3cb773229`.

## Source gaps are retained

Aggregate trade IDs are continuous in every archive, but the ranges of
underlying raw trade IDs contain 85,796 gaps. The loader neither fills nor
renumbers them. The result records these gaps explicitly because the
[official public-data repository](https://github.com/binance/binance-public-data)
has historically received reports of missing raw IDs inside checksum-valid
`aggTrades` files.

This does not by itself invalidate aggregate order-flow research: each retained
row is an official aggregate with price, quantity, timestamp and aggressor
side. It does mean that a later study must include source-gap sensitivity and
cannot describe the feed as a complete raw-trade tape.

## What this enables

The rejected `quarter_hour_flow_v1` was a one-minute proxy. The source
[Quarter-Hour Effect paper](https://arxiv.org/abs/2607.09426v2) instead defines
the opening reference as the latest transaction price at or before boundary
`T`, then measures transaction VWAP and taker flow over `(T,T+10s]`. The new
loader implements that exact causal interval and derives aggressor direction
from Binance's `is_buyer_maker` field.

The next permitted work is a separately preregistered statistical replication
that first tests phase-aligned predictability and placebo phases without
trading. Only if that survives source gaps and a held-out time interval may a
fixed SL/TP/timeout challenger be specified and evaluated after full Binance
and EVEDEX execution costs.

## What remains unproven

- One day qualifies transport and schema, not the paper's multi-year effect.
- The paper used BTC, ETH, XRP, SOL, DOGE and ADA. Kairos uses BTC, ETH, SOL,
  BNB and XRP; BNB is therefore an independent extension.
- The source venue is Binance while the intended execution venue is EVEDEX.
  Basis, book age, depth, latency and slippage remain mandatory entry gates.
- A forecastable ten-second return can still be smaller than round-trip costs.
  Published accuracy is not proof of executable profitability.
- The midnight boundary requires a prior-day transaction reference, so every
  future exact slice must include at least one preceding archive.

The machine-readable [plan](plan.json) and [result](result.json) are the
authoritative evidence. This report adds interpretation but no permission.
