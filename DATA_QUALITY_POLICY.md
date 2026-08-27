# Historical archive field policy

## Why this exists

Kairos stores checksum-verified official Binance USD-M archives. A valid ZIP
checksum proves transport integrity; it does not prove that every field inside
a source row is economically self-consistent. The five-asset inventory through
July 2026 contains one known row where taker-buy base volume exceeds total base
volume. That source anomaly was already visible in earlier audit artifacts, but
trial 13 consumed its one-shot attempt before proving that its exact 365-day
warm-up could be loaded. The trial correctly failed closed, but the failure was
preventable.

Every future research plan must declare one of these profiles before opening an
evaluation window:

| Profile | Preserved fields | Use | Behaviour on taker-field anomaly |
| --- | --- | --- | --- |
| `FULL_KLINE` | OHLC, base/quote volume, taker-buy base/quote volume | order-flow or microstructure model | reject the row and fail the exact slice |
| `PRICE_VOLUME` | OHLC and base/quote volume | model whose source and tests prove it never reads taker fields | omit taker semantics from every profiled row by setting both taker fields to zero; record source anomalies as quarantined evidence |

`PRICE_VOLUME` is not data repair. It is a deliberately narrower view in which
taker fields are zero for every row, whether or not the source row is anomalous.
The original ZIP and checksum remain unchanged, the normalized-view hash is
profile-bound, and every quarantined source row is counted with archive, line
and reason. A candidate using this profile may not claim taker/order-flow
evidence.

## Mandatory preflight order

1. Commit the hypothesis, exact windows, warm-up and field profile.
2. Run `preflight_cached_slices` on those exact slices with downloads disabled.
3. Persist the preflight evidence and its canonical hash.
4. Only after a successful preflight may a one-shot performance attempt be
   consumed.

Preflight checks official checksums, exact row count, minute continuity,
boundaries, normalized row hash and quarantined optional fields. It calculates
no signal, position, return or portfolio metric. A failed preflight consumes no
performance trial, while a failure after attempt consumption never releases
that attempt.
