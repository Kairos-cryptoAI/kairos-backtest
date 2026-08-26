# Kairos market anatomy study

Date: 2026-08-27

Decision: **NO_PROTOTYPE_PASSED_DESCRIPTIVE_GATES**

Classification: descriptive diagnostics on reused data

## Executive result

The market has enough movement to support active trading, but none of the four
simple directional hypotheses passed its fixed selection and robustness gates.
The current strategy problem is therefore not a shortage of volatility or
candidate bars. It is the absence of a stable rule that predicts the direction
of that movement after costs.

The closest family was causal 24-hour regime trend. Its aligned forward return
averaged +20.44 bps in selection and +11.68 bps in robustness over the next 24
hours, but hit rates were 49.4% and 49.1%. The robustness mean also missed the
predeclared +15 bps hurdle. The positive mean with a sub-50% hit rate is
consistent with an asymmetric right tail, not a high-accuracy signal. The gate
is not changed after seeing this result.

All permissions remain false. The study can neither authorize a strategy nor
be relabelled as out-of-sample evidence.

## Verified data boundary

The one-shot study ran from signed Git commit
`81da251c9e233020c062e83c06d05a064e06cebf`. It inspected five symbols from
2021-07-01 inclusive through 2026-08-01 exclusive:

| Evidence | Result |
| --- | ---: |
| Monthly archives | 305/305 present |
| Official SHA-256 sidecars | 305/305 verified |
| Valid one-minute rows | 13,355,999 |
| Complete one-hour bars | 222,599 |
| Known invalid rows | 1 XRP row |
| Incomplete hours dropped | 1 |
| Historical gap boundaries | 5 |

BTC, ETH and BNB were complete. SOL retained two historical boundaries. XRP
retained three boundaries, including the hour containing its single known
invalid row. No value was repaired, imputed or carried across a gap.

## Market structure found in the data

Selection contained materially different symbol outcomes: BTC returned about
+70% and XRP +370%, while ETH lost about 28%. In robustness all five symbols
lost value, with returns from approximately -11% to -53%. The same fixed study
therefore observed both broadly favorable and broadly adverse market states.

Hourly correlations in robustness ranged from 0.72 to 0.86. BTC/ETH was 0.86,
ETH/SOL 0.85 and BTC/SOL 0.81. Five symbols cannot consequently be treated as
five independent sources of risk; common crypto beta must be allocated once at
portfolio level.

The movement upper bound was not small:

| Window | Symbol range of median absolute 1h move | Share of 1h moves above 15 bps | Median absolute 24h move | Share of 24h moves above 25 bps |
| --- | ---: | ---: | ---: | ---: |
| Selection | 23.39-46.04 bps | 66-83% | 130.80-254.50 bps | 88-95% |
| Robustness | 19.50-34.18 bps | 59-76% | 117.83-211.51 bps | 87-93% |

These are close-to-close absolute movements, not executable profits. They show
that cost-sized movement exists; they do not reveal its direction in advance.

## Frozen family results

The table reports aligned mean forward return. Positive values favor the
hypothesis; the preregistered gates also required at least 1,000 observations
per window and a hit rate of 51%.

| Family | Horizon | Selection mean | Selection hit rate | Robustness mean | Robustness hit rate | Decision |
| --- | ---: | ---: | ---: | ---: | ---: | --- |
| Regime trend | 4h | +2.44 bps | 46.3% | +1.44 bps | 47.7% | Insufficient |
| Regime trend | 24h | +20.44 bps | 49.4% | +11.68 bps | 49.1% | Insufficient |
| 24h breakout | 4h | -2.81 bps | 44.9% | +5.21 bps | 46.3% | Insufficient |
| 24h breakout | 24h | +1.85 bps | 46.2% | +8.82 bps | 50.6% | Insufficient |
| Range-shock reversion | 4h | -22.65 bps | 47.0% | -7.87 bps | 51.6% | Insufficient |
| Range-shock reversion | 24h | -13.31 bps | 52.2% | -21.69 bps | 46.7% | Insufficient |
| Taker-flow alignment | 1h | -0.97 bps | 46.5% | +0.23 bps | 47.8% | Insufficient |
| Taker-flow alignment | 4h | -1.76 bps | 47.6% | +1.70 bps | 48.9% | Insufficient |

The earlier order-flow strategy failed after execution costs; this descriptive
study independently shows that one-hour aggregate taker imbalance has almost
no directional mean at one and four hours. It may still help at a shorter
latency as an execution feature, but the existing minute archive cannot prove
that claim.

Range-shock reversion was negative at four and 24 hours in both later windows.
Blindly fading a large move that begins after a range is therefore especially
unsafe: on average the shock continued more than it reversed under this
definition.

## Consequence for the next research step

Do not create another price-threshold variation. The price/volume archive has
now rejected direct breakout, coarse taker flow and simple post-shock reversal,
while regime trend remains suggestive but below its frozen gate.

The next evidence track must add an economically distinct state dimension:

1. historical premium/basis and funding;
2. open-interest and leverage change;
3. liquidation or forced-flow evidence where an official causal source is
   available;
4. later, official news timing as a separately measured overlay.

That study must distinguish a directional trend signal from a risk overlay.
For example, elevated basis/open interest may veto or reduce a trend candidate
without being asked to predict direction on its own. Any new prototype remains
separately preregistered and cannot use the present result as blind evidence.

## Integrity

| Artifact | SHA-256 / identity |
| --- | --- |
| Plan | `07b6d8f4a79eec0781719d5777db88b31a00310a095f58267e71fad014e9c149` |
| Result | `6f0a17e0edd702afff2a83d0b9bdb810f0f75d049993c39399751c5dfc227b5f` |
| Inventory | recorded in `result.json` |
| Permissions | `alpha_ready=false`, `paper_allowed=false`, `promotion_eligible=false`, `live_allowed=false` |

The full [result](result.json), compact [summary](summary.json), immutable
[plan](plan.json) and research interpretation above are the durable audit
surface.
