# Published Donchian ensemble — trial 13

## Outcome

`INCONCLUSIVE_DATA_INTEGRITY`

This is neither a strategy pass nor a strategy rejection. The single
preregistered attempt was consumed at `2026-08-27T03:09:36.131734Z` before the
archive cache was opened. It then stopped during strict domain validation of
the official Binance source, before a portfolio result was persisted or shown
to the operator. No performance metric was available for interpretation.

The attempt remains consumed. The committed plan explicitly states that a
crash or failure does not release the attempt and that a rerun is not allowed.
The exact model therefore cannot be repaired and silently rerun on these reused
windows.

## Data-integrity finding

The failing row is in
`data/historical/XRPUSDT/1m/XRPUSDT-1m-2023-11.zip`, line 42,517, at
`2023-11-30T12:35:00Z`:

| Field | Value |
| --- | ---: |
| Total base volume | 91,695.7 |
| Taker-buy base volume | 132,462.5 |
| Excess | 40,766.8 |

This is not a floating-point tolerance issue. The downloaded archive hashes to
`b817ca0a6478d73cfd50dfc224aab84ec7f15ed0129795c3df2c39e6ce08cc99`,
exactly matching Binance's official `.CHECKSUM` sidecar. The local file is
intact; the contradictory fields are present in the official archive.

The anomaly itself was not new: earlier inventory artifacts already recorded
this exact archive and line. The process failure was that trial 13 validated
inventory presence and checksums but did not preflight loader compatibility for
its exact 365-day warm-up before consuming the attempt. This is now treated as
a research-control defect, not as evidence against the strategy.

The immutable machine-readable evidence is in [`failure.json`](failure.json).
The raw row itself is not duplicated in that artifact; its UTF-8 SHA-256 is
recorded so the observation can be reproduced without making an editable copy
of market data.

## Research consequence

- `alpha_ready=false`, `paper_allowed=false`, `live_allowed=false`;
- no result may be inferred from the paper's published performance;
- no threshold, horizon, cost or parser rule may be changed and called the same
  trial;
- the allocation generator remains useful code, but this candidate has no
  Kairos performance qualification and stays fail-closed for PAPER.

Before another independent hypothesis is evaluated, archive-domain anomalies
must have a frozen, tested policy established without looking at that
hypothesis's performance. That policy must distinguish price/volume fields used
by the model from optional microstructure fields, preserve the original row and
make every exclusion or normalization auditable.
