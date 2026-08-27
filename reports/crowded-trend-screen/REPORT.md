# Crowded-trend continuation — consumed reused-data screen

## Decision

`crowded_trend_continuation_v1` is **rejected**. The single preregistered
attempt returned `REJECT_REUSED_DATA_SCREEN`; `ALPHA_READY`, `PAPER_ALLOWED`
and `LIVE_ALLOWED` remain `false`.

The candidate was economically positive in every aggregate cell, but it did
not pass every fixed gate. Stress profit factor was `1.0499425895` in selection
and `1.0458379844` in robustness, both below the preregistered strict `>1.05`
requirement. It also produced only 7 and 17 short trades, below the required 25
per window. Passing baseline numbers or proximity to a threshold cannot
override a failed gate.

| Window | Scenario | Return | PF | Trades | Expectancy/trade | HAC Sharpe | Max DD |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: |
| Selection | Baseline | 1.1887% | 1.1767 | 255 | $4.6614 | 0.9098 | 0.971% |
| Selection | Stress | 0.3191% | 1.0499 | 254 | $1.2563 | 0.2836 | 1.024% |
| Robustness | Baseline | 1.2849% | 1.2006 | 236 | $5.4444 | 0.9194 | 1.036% |
| Robustness | Stress | 0.2768% | 1.0458 | 236 | $1.1728 | 0.2395 | 1.368% |

## What the test established

The previously observed derivatives state was not a statistical mirage at the
portfolio level. After next-bar entry, immutable 2 ATR / 4R exits, one position
per symbol, fees, spread, slippage and adverse funding, both annual windows
remained positive. Baseline profit factor was 1.18–1.20 and the 90th-percentile
trade returned roughly 2.8–2.9R. This is the strongest tested Kairos candidate
so far and supports retaining derivatives crowding as a research dimension.

It is not yet a suitable production strategy. The stress edge is too thin:
small venue-cost or funding errors can erase it. Breadth is mixed. Selection
had positive expectancy on BTC, SOL and XRP; robustness on BTC, ETH, SOL and
XRP, while BNB was negative in both. Removing BNB, dropping shorts, weakening
the PF gate or changing exits after seeing this result would be post-hoc tuning
and is forbidden for this attempt.

The trade distribution is deliberately right-tailed: win rate was only
37–39%, median R was negative, and profits depended on a smaller number of
large winners. That behavior is consistent across both years, but it demands
reliable execution and enough capital/time to tolerate losing sequences.

## Immutable lineage

- Research lineage trial: `12`.
- Strategy source: `f92bd6dbc5414167557b6ee69eea1b768264f5ef`.
- Plan SHA-256: `169949a28b0608c880355ab004c9f0fdd7458d7f3e782b4793cd601a14585ccb`.
- Attempt canonical SHA-256: `74568812836b9577f6a826031984fafd66ec06caa27360d7dea41c9737bbac22`.
- Result canonical SHA-256: `f47a685563d6031ad71e0417646e9562e60db58baa808d6015744d3c5c2f7888`.
- Official factor inventory SHA-256:
  `b9e4c038adfd2ec70fadcdbce46e6ed040cb8e2e855b0335c7bb6552140ad2cc`.
- No paid API or exchange mutation was used.

The attempt was consumed before archive access and cannot be rerun. A successor
hypothesis must receive a new strategy ID, new lineage trial and a plan committed
before any evaluation. This result alone cannot authorize PAPER or LIVE.
