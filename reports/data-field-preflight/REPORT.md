# Exact-slice archive preflight

## Result

`DATA_PREFLIGHT_PASSED` for the committed `PRICE_ONLY` v2 plan.

This is data evidence only. No strategy generator, signal, position, return,
funding replay or portfolio metric ran, and no research attempt was consumed.
All permissions remain false.

| Measure | Result |
| --- | ---: |
| Exact slices | 10 |
| Profiled minute rows | 10,735,200 |
| Official checksum verifications | 245 |
| In-slice gaps | 0 |
| Quarantined source rows | 1 |

The ten slices are five assets over selection plus 365-day warm-up and the same
five assets over robustness plus 365-day warm-up. Their overlap is intentional:
each future evaluation window is independently bound to an exact normalized-row
SHA-256.

## V1 failure and V2 boundary

The first `PRICE_VOLUME` plan failed on the checksum-verified XRP November 2023
row before writing a success receipt. In addition to taker-buy volume exceeding
total base volume, its quote volume is inconsistent with base volume and OHLC.
That failure is preserved in [`failure-v1.json`](failure-v1.json); it consumed
no performance trial.

V2 does not invent corrected volume. Its `PRICE_ONLY` view preserves OHLC and
sets base, quote and both taker-volume fields to zero for every row. The single
contradictory source row remains counted in quarantine. Therefore only a model
proven not to read any volume field may reference this receipt.

The immutable success receipt is [`result-v2.json`](result-v2.json), with plan
SHA-256 `217195a5c940e9fbc6da6fe8d4a8aebb23bfa9f30d5792e3c70148aa02cb977b`
and result SHA-256
`908ba2b469bb5c2811e4763d07c34bde9e97fda4b64d5e277496af637400ea62`.

## V3 price-and-volume qualification

Trial 15 needs base and quote volume for execution-capacity simulation even
though its strategy features use price only. A second performance-blind plan
therefore qualifies the exact selection and robustness windows plus 40 days of
warm-up under `PRICE_VOLUME`. This profile preserves OHLC, base volume and quote
volume while zeroing unused taker fields.

All ten slices passed: 6,055,200 minute rows, 145 official checksum
verifications, zero gaps and zero quarantined rows. The older November 2023 XRP
source anomaly is outside both exact slices. No strategy generator, intent,
position, return or PnL calculation ran during this preflight.

The immutable artifacts are [`plan-v3.json`](plan-v3.json) and
[`result-v3.json`](result-v3.json), with plan SHA-256
`cdec435d635495d897e3e0b78a9e17b4cec38ff58bd2a62ed097bcacdbee3ae5`
and result SHA-256
`91b1331fead7a7392b7a21f406f67e95e57c3ad1fd370f2c0a472c71d276a4dd`.
