# Quarter-hour flow reused-data screen

## Decision

`REJECT_REUSED_DATA_SCREEN`

The single preregistered `quarter_hour_flow_v1` candidate emitted no evaluated
intents in research, selection, or robustness. Consequently every baseline and
stress portfolio stayed flat with zero trades. The candidate is not eligible
for a forward freeze, PAPER, alpha promotion, or LIVE trading.

This is a rejection of the exact frozen one-minute proxy, not a refutation of
the first-ten-second result in the source paper. Kairos does not currently
retain first-ten-second aggregate trades, and the preregistered conjunction of
12 lagged boundary observations, 8/12 sign agreement, current imbalance,
volume and ATR controls was too restrictive on the available one-minute bars.
No threshold was changed after observing this result.

## Evidence boundary

- Plan SHA-256: `80cb424675a57a34cf195858a4742dfa891d819b177a1b83f3edd9114515a916`
- Summary SHA-256: `50a894650ce68cb291da3c92333075fa01615745153ac0220491bcab1af6c760`
- Summary bytes: `33887`
- Strategy source: `505012c70aed28608ee9edf10cb8338c2c02279d`
- Candidate config SHA-256: `65a7af7e722b8c7639f5f2fd1aadf1d94e6f1630a1c99e022703554b0bba7691`
- Result permissions: `alpha_ready=false`, `paper_allowed=false`,
  `promotion_eligible=false`, `live_allowed=false`

The first execution aborted before metrics because the screen incorrectly
required the complete five-year archive to be gap-free. That abort is retained
in `attempt-1-data-quality-failure.json`. The corrected execution did not fill
or delete any bar: affected SOL/XRP research cells were marked unavailable,
while selection and robustness retained strict complete-minute gates.

## Data quality

All 305 expected Binance archives passed their official SHA-256 sidecars and
ZIP CRC. Overall valid-row coverage was 99.8922919284%. The immutable audit
retains five gap boundaries, 14,400 missing minutes and one invalid XRP row:

- SOL research: two historical gaps, 7,200 missing minutes;
- XRP research: three boundaries, 7,200 missing minutes and one invalid row;
- selection and robustness slices for all five symbols: complete and verified.

The unavailable research cells are not represented as flat results or zero
signals. Research metrics use the three complete symbols; both gated roles use
all five.

## Results

| Window | Scenario | Return | Drawdown | HAC Sharpe | Trades | Active symbols |
| --- | --- | ---: | ---: | ---: | ---: | ---: |
| Research | Baseline | 0.0000% | 0.0000% | 0.0000 | 0 | 0 |
| Research | Stress | 0.0000% | 0.0000% | 0.0000 | 0 | 0 |
| Selection | Baseline | 0.0000% | 0.0000% | 0.0000 | 0 | 0 |
| Selection | Stress | 0.0000% | 0.0000% | 0.0000 | 0 | 0 |
| Robustness | Baseline | 0.0000% | 0.0000% | 0.0000 | 0 | 0 |
| Robustness | Stress | 0.0000% | 0.0000% | 0.0000 | 0 | 0 |

There are 40 explicit gate failures: insufficient total/per-symbol trades,
zero active symbols, non-positive return and HAC Sharpe, and unavailable profit
factor in both gated windows and both execution scenarios.

## Consequence

The strategy remains registered as `RESEARCH` solely for reproducibility and
continues to fail closed in PAPER. Any successor must be a separately reasoned,
separately preregistered hypothesis. It must not be presented as a parameter
adjustment or additional trial of this consumed plan. New blind evidence cannot
start before 2026-09-01 and would still require at least eight months and 500
trades before an alpha claim could be considered.
