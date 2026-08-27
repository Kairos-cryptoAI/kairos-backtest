# Four-hour SMA200 long/flat — trial 14

## Decision

`four_hour_sma200_long_v1` is **rejected as a standalone Kairos strategy**.
The single preregistered attempt returned `REJECT_REUSED_DATA_SCREEN`;
`ALPHA_READY`, `PAPER_ALLOWED` and `LIVE_ALLOWED` remain `false`.

This trial reproduced the exact causal open-code rule at external source commit
`5acae6b7a4ff53bacb47a348233060f6a7090b24`: BTC is long at 1x only while the
last closed four-hour close is strictly above its 200-bar simple moving average,
and the target becomes effective on the next four-hour bar. Kairos did not
search another timeframe, horizon, band, stop, target or volatility scaler.

| Window | Economics | Return | PF | Sharpe | Max DD | Changes | BTC buy/hold return |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: |
| Selection | Published spot | 37.61% | 1.0960 | 1.1162 | 27.39% | 53 | 70.44% |
| Selection | Futures actual | 31.49% | 1.0839 | 0.9810 | 27.87% | 53 | 60.13% |
| Selection | Futures stress | -11.56% | 0.9841 | -0.1967 | 34.88% | 53 | -7.51% |
| Robustness | Published spot | -17.75% | 0.9366 | -0.7186 | 28.65% | 66 | -41.36% |
| Robustness | Futures actual | -19.25% | 0.9299 | -0.7967 | 29.65% | 66 | -43.70% |
| Robustness | Futures stress | -43.17% | 0.8135 | -2.2758 | 48.79% | 66 | -68.97% |
| Source-unseen Apr–Jul 2026 | Published spot | -1.88% | 0.9910 | -0.1122 | 13.27% | 22 | -7.98% |
| Source-unseen Apr–Jul 2026 | Futures actual | -2.10% | 0.9888 | -0.1401 | 13.68% | 22 | -8.73% |
| Source-unseen Apr–Jul 2026 | Futures stress | -14.04% | 0.8702 | -1.7379 | 20.49% | 22 | -24.12% |

## What the trial established

The mechanism is real but insufficient. In both the robustness window and the
source-unseen subwindow, the rule lost materially less than continuously holding
BTC. That supports retaining slow trend state as a defensive allocation or risk
context: it can remove exposure during part of a falling market.

It did not reproduce the stronger claim required for Kairos. Selection failed
the preregistered Sharpe-relative and drawdown gates even before futures stress.
Robustness was negative in all three cost cells, and its spot drawdown exceeded
the fixed 25% ceiling. The source-unseen spot slice was close to flat but not
profitable. An adverse five-basis-point funding shock per settlement made both
annual windows and the source-unseen slice fail decisively.

The result also confirms that fewer trades is not itself an edge. The candidate
changed allocation 53 and 66 times per annual window, yet chop plus funding and
turnover still dominated. Adding a confirmation band, changing SMA length,
volatility sizing or using the result only in favorable symbols would be a new
hypothesis and cannot rescue this consumed attempt.

## Interpretation boundary

This is an external reproduction, not independent alpha validation. The source
author had observed data through 31 March 2026 and had compared multiple SMA
horizons; only April through July 2026 was unseen by that source, and no interval
was blind to Kairos. The open-code paper's reported full-history performance is
therefore not transferable evidence for PAPER approval.

The useful retained proposition is narrow: `close > 4h SMA200` can be a
low-frequency crash-avoidance state. It is not a complete high-return strategy,
does not supply an immutable lifecycle SL/TP/timeout, and cannot meet the user's
return objective on its own.

## Immutable lineage

- Research lineage trial: `14`.
- Strategy source at evaluation: `c7a9c7e296e3e6ad530706320d503b7e989d2da6`.
- Rejected registry source: `0e5a78ff20a4eb008b306f9204bc567870d8ecc9`.
- External source: `5acae6b7a4ff53bacb47a348233060f6a7090b24`.
- Plan SHA-256: `15446c0f1bcb9edc94bf6032831c9d9880d9c9881b81846196c220f682d0584a`.
- Attempt canonical SHA-256: `44f95f5a5a9879916b2b4224b6c4f6b3f1703b3598ef856001692fd5b1d6accf`.
- Result canonical SHA-256: `1c2ecaeb2a961c9c858583878f5169dc000222b81342d80e79ab62a39841d83d`.
- Price preflight receipt SHA-256:
  `908ba2b469bb5c2811e4763d07c34bde9e97fda4b64d5e277496af637400ea62`.
- Official factor inventory SHA-256:
  `b9e4c038adfd2ec70fadcdbce46e6ed040cb8e2e855b0335c7bb6552140ad2cc`.
- No paid API, LLM call, exchange mutation or PAPER order was used.

The attempt was consumed before price or funding archive access and cannot be
rerun. Any successor must use a new strategy ID, lineage trial and committed
plan before evaluation.
