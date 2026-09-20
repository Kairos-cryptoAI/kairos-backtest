# Liquidation absorption V1 — data qualification

This directory contains a preregistered, **performance-blind** public-data
qualification for a possible future microstructure study.  It is not a
strategy, a backtest, a simulator, or permission to trade.

`plan.json` is the immutable contract.  Its canonical logical SHA-256 is
checked by `kairos_backtest.liquidation_absorption_preflight` before a
recorder may be introduced.  The plan begins no earlier than 2026-09-21 UTC
and requires 90 calendar days of raw event-time observations across BTCUSDT,
ETHUSDT, SOLUSDT, BNBUSDT, and XRPUSDT.

## What is collected

The future research-only recorder may use Bybit's documented public linear
WebSocket topics for order-book depth, public trades, tickers, and all
liquidations.  It must retain each original UTF-8 frame with hashes, source
and local timestamps, channel sequence state, and an append-only global hash
chain.  A reconnect, sequence anomaly, malformed frame, or queue overflow is
a barrier: a later event whose measurement window crosses it is ineligible.

The displayed book is explicitly only a liquidity proxy.  Bybit documents
that its order-book feed excludes RPI orders, while its all-liquidation feed
uses `S=Buy` for a liquidated long and `S=Sell` for a liquidated short.  The
future study must retain those semantics rather than reinterpret them as
fillable prices.

- [Bybit order-book WebSocket](https://bybit-exchange.github.io/docs/v5/websocket/public/orderbook)
- [Bybit public-trade WebSocket](https://bybit-exchange.github.io/docs/v5/websocket/public/trade)
- [Bybit ticker WebSocket](https://bybit-exchange.github.io/docs/v5/websocket/public/ticker)
- [Bybit all-liquidation WebSocket](https://bybit-exchange.github.io/docs/v5/websocket/public/all-liquidation)

## Boundaries

This phase cannot contact EVEDEX, use credentials, call an LLM or paid feed,
calculate PnL, generate an intent, fit a model, or submit an order.  It does
not claim that a Bybit observation is executable on EVEDEX.  Only after the
entire tape passes its integrity checks may a separate, preregistered
statistical study be proposed; any resulting candidate remains subject to the
independent alpha, EVEDEX, PAPER, and LIVE gates.

## Recorder operation

`python -m kairos_backtest.liquidation_absorption_capture` is a public-only recorder.  It refuses
to start before the plan's UTC boundary, validates the exact plan hash, allows
only one recent writer lease, and writes immutable gzip segments with a global
event hash chain.  A malformed source frame, source-time/update regression,
known update gap, reconnect, non-text frame, transport failure, or
unclean restart becomes a barrier; an eligible future study must exclude a
window that crosses one.

The recorder is intentionally bounded by an explicit duration, so a supervisor
can leave an inspectable receipt and restart safely rather than silently
claiming indefinite uptime.  Example for an isolated runtime location after
the UTC start time:

```powershell
uv run --locked python -m kairos_backtest.liquidation_absorption_capture `
  --output-directory D:\Kairos\runtime\liquidation-absorption-v1 `
  --duration-seconds 86400
```

It prints only coverage/integrity state, never a signal, outcome, PnL, or
credential.  Verify a fully sealed stopped tape without reopening a network
connection:

```powershell
uv run --locked python -m kairos_backtest.liquidation_absorption_capture `
  --output-directory D:\Kairos\runtime\liquidation-absorption-v1 `
  --verify
```
