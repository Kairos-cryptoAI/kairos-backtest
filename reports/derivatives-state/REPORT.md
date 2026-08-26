# `derivatives_state_v1` result

## Decision

`NO_PROTOTYPE_PASSED_DESCRIPTIVE_GATES`

This one-shot descriptive study did not authorize a new strategy prototype or
overlay. It also cannot authorize alpha, PAPER or LIVE by construction. The
result does not change any previously rejected strategy decision.

## Reproducibility

- Plan SHA-256: `4072f504942ffeb993fccaea7e26fad2d5e2459b33611b36c7f22ac5d00bb309`
- Logical result SHA-256: `c0e5769d26670cdfe1fd224bb39b39850c4a2ca3b68e7750156fe3c55205ed76`
- Factor inventory SHA-256: `b9e4c038adfd2ec70fadcdbce46e6ed040cb8e2e855b0335c7bb6552140ad2cc`
- Executed from signed commit: `a47f3daaa0f7f166b8ad38b4930020d6247b250a`
- Paid API spend: `$0`

The immutable machine-readable evidence is in [result.json](result.json). The
smaller [summary.json](summary.json) repeats the decision, permissions and main
24-hour diagnostics.

## Data evidence

The study verified all 4,415 official Binance USD-M archives and adjacent
SHA-256 files: 305 funding archives, 305 premium-index archives and 3,805 daily
metrics archives. ZIP CRC and strict schemas were also checked.

Across the five symbols, the parser read 27,930 funding rows, 221,928 hourly
premium rows and 1,095,815 five-minute leverage rows. It quarantined 585 zero
open-interest observations as source-data outages rather than false
deleveraging. It counted 325 observations with absent optional positioning
ratios and never filled them with zero. A valid observation still existed for
every required hourly join, yielding 8,760 hours per symbol in selection and
9,480 per symbol in robustness.

## Fixed 24-hour diagnostics

Values are signed forward log returns in basis points before trading costs.
Hit rate is the fraction whose signed return is positive.

| Fixed family | Selection mean / hit / n | Robustness mean / hit / n |
| --- | ---: | ---: |
| Funding contrarian | -26.34 / 49.33% / 14,480 | +1.62 / 50.23% / 8,088 |
| Premium contrarian | -6.60 / 52.03% / 16,002 | -8.55 / 49.33% / 18,895 |
| Trend after >=5% OI deleveraging | -28.01 / 45.39% / 2,842 | -8.84 / 50.85% / 2,893 |

None met the frozen requirement of at least 500 observations, +15 bps mean and
51% hit rate in both windows.

The crowding hypothesis also failed in the opposite direction. Removing states
with at least 5% OI growth plus trend-aligned premium or funding reduced the
base trend mean by 12.30 bps in selection and 1.71 bps in robustness. Crowded
states themselves had +88.51 and +26.71 bps mean, but their hit rates were
51.27% and 49.18%. The right tail is interesting market anatomy, but the
robustness hit rate and the preregistered veto direction both fail. Reversing
the rule after seeing these values would be a new, contaminated hypothesis.

## Interpretation

- Extreme funding is not a stable standalone directional reversal signal in
  these windows.
- Premium contrarian behavior changes across windows and remains negative on
  average.
- A material OI reduction does not validate following the prior 24-hour trend;
  the negative means are consistent with reversal or unstable shock dynamics,
  but this study did not preregister that opposite trade.
- Crowded trends sometimes contain large continuation moves. Their low and
  unstable hit rate means they need a different causal risk/exit design and new
  evidence, not a post-hoc entry rule.

## What this permits

Nothing is promoted. The evidence narrows the next research work: preserve
official funding/premium/OI as market-state context, begin collecting blind
forward observations, and design any liquidation, carry or crowded-trend
hypothesis before its validation window is opened. Existing `ALPHA_READY`,
PAPER-strategy and LIVE permissions remain false.
