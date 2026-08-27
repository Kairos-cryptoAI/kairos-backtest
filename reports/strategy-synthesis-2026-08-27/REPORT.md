# Strategy research synthesis after lineage trials 1–14

Date: 2026-08-27

## Decision

Kairos does not yet have a strategy suitable for automatic PAPER trading.
`ALPHA_READY`, `PAPER_ALLOWED` and `LIVE_ALLOWED` remain `false` for every
evaluated candidate. The research was nevertheless productive: it eliminated
three recurring failure classes and isolated two mechanisms worth combining in
one new, separately preregistered candidate.

This synthesis does not reinterpret a failed gate, release a consumed attempt,
or create performance evidence. Every historical result below remains frozen.

## Complete lineage map

| Trial(s) | Frozen hypothesis | Result | Durable observation |
| --- | --- | --- | --- |
| 1–3 | Pullback/reclaim: shallow, medium, deep | `REJECT_ALL` | More entries did not create edge. Deep stayed positive under stress but had only 38 baseline and 10 stress trades. |
| 4–6 | Order-flow volatility: impulse, persistence, flip-release | `REJECT_ALL` | Persistence reached 387 baseline trades but lost 2.90%; the directional move was already being chased. |
| 7–9 | Regime/retest: structural, reacceleration, absorption | `REJECT_ALL` | Hard conjunction reduced 41,741 breakout candidates to 12 intents and one losing trade. |
| 10 | Quarter-hour one-minute flow proxy | `REJECT_REUSED_DATA_SCREEN` | Zero intents. This rejects the proxy, not the source paper's first-ten-second signal. |
| 11 | Daily right-tail trend | `REJECT_REUSED_DATA_SCREEN` | Strongest broad result: positive selection and robustness, but robustness stress PF `1.0383` missed the strict `>1.05` gate. |
| 12 | Crowded-trend continuation | `REJECT_REUSED_DATA_SCREEN` | Positive in all aggregate cells, but stress PF `1.0499`/`1.0458` and short breadth were insufficient. |
| 13 | Published Donchian ensemble | `INCONCLUSIVE_DATA_INTEGRITY` | No performance was observed; the one-shot attempt stopped on an official-source field contradiction. |
| 14 | Four-hour SMA200 long/flat | `REJECT_REUSED_DATA_SCREEN` | Standalone return failed, but falling-market exposure was reduced in robustness and source-unseen slices. |

The descriptive `market_anatomy_v1` and `derivatives_state_v1` studies also
returned `NO_PROTOTYPE_PASSED_DESCRIPTIVE_GATES`. Simple hourly trend,
breakout, shock reversal, taker-flow direction, funding/premium contrarian and
open-interest deleveraging rules were not stable across the reused windows.

## What the experiments established

### 1. Frequency is capacity, not alpha

The engine can generate hundreds of trades. The 387-trade order-flow variant
and 105-trade pullback variant prove that low frequency was not an
infrastructure limitation. Their negative pre-cost or after-cost expectancy
shows why a target number of daily trades must remain a sufficiency band rather
than an optimization objective.

### 2. Coarse taker flow is not a standalone direction

One- and five-minute Binance taker volume arrived after much of the price move
and was too coarse to reproduce the cited ten-second quarter-hour effect. It
may remain a timing, venue-quality or veto feature, but the evaluated versions
do not justify directional authority. A faithful quarter-hour reproduction
requires official aggregate trades at the first ten seconds of each boundary
and its own future protocol.

### 3. Hard intersections destroy statistical power

Requiring regime, expansion, retest, reclaim, flow confirmation and economic
admission simultaneously produced almost no executable sample. Independent
weak evidence should be scored or used by separate sleeves; it should not be
stacked into a single chain of mandatory predicates.

### 4. Small gross edges are dominated by execution economics

Fees, spread, slippage and adverse funding erased several superficially
positive candidates. The right-tail and crowded-trend results were the only
broad candidates that stayed positive in both later annual windows, yet their
stress margins were too close to one to tolerate model or venue error.

### 5. Positive skew is the strongest repeated strategy shape

`right_tail_trend_v1` produced 459/494 baseline trades in selection/robustness,
approximately 1.25 portfolio trades per day. Baseline returns were 5.57% and
4.08%, with PF 1.38 and 1.26. Its win rate was only about 34%; the median trade
lost roughly 1R and the smaller set of winners carried the result. This is the
only tested shape with useful frequency, diversification and positive later
window economics, but its stress robustness return fell to 0.50% and PF
1.0383. It is evidence for a research direction, not an accepted strategy.

`crowded_trend_continuation_v1` independently retained the same right-tailed
shape and positive aggregate economics, but its stress edge and short sample
were also insufficient. Crowding is therefore a possible ranking/context
feature, not a standalone promoted sleeve.

### 6. Slow trend state is defensive context, not return alpha

The exact four-hour SMA200 reproduction lost 17.75% spot in robustness and
1.88% in the source-unseen April–July 2026 slice, so it is rejected as a
standalone strategy. It nevertheless lost materially less than continuous BTC
exposure in both periods. The retained proposition is only that slow trend
state may suppress trades against a persistent market regime.

## General conclusion

The accumulated evidence supports a modular trend portfolio, not a highly
filtered intraday oracle:

1. a sparse causal trend trigger supplies immutable side and right-tailed
   SL/TP/timeout economics;
2. slow market state decides whether that direction is eligible;
3. derivative crowding and future exact microstructure evidence rank or veto,
   rather than manufacture, direction;
4. LLM review is evaluated later against the identical frozen candidate
   stream and cannot modify trade parameters;
5. portfolio risk treats the five correlated crypto symbols as shared market
   exposure rather than five independent bets.

This conclusion is useful, but it is not a profitability claim. Fourteen
lineage trials and additional descriptive studies create substantial selection
bias. No result on the archive ending 2026-08-01 can now be called blind.

## Next candidate: `regime_aligned_right_tail_v1`

The next permitted performance experiment is a structurally distinct synthesis
of the strongest broad signal and the only retained defensive state:

- evaluate once per complete UTC day;
- reuse the exact 24-hour standardized trend-score threshold of `+1/-1`;
- reuse 24-hour ATR, a 2 ATR stop, 4R target and 72-hour timeout unchanged;
- compute a 200-bar SMA from complete four-hour bars only;
- allow a long only when the last complete four-hour close is strictly above
  its SMA200;
- allow a short only when the last complete four-hour close is strictly below
  its SMA200;
- equality, missing history, a gap or a stale bar emits no intent;
- use no per-symbol exclusions, parameter sweep, LLM, news or derivatives
  threshold.

The directional rule is symmetric and fixed before evaluation. The SMA state
does not alter side, stop, target, timeout or size; it only rejects a base
candidate whose direction conflicts with the slow state.

Because both components and all available data have already been observed,
the next reused-data screen may return only `REJECT_REUSED_DATA_SCREEN` or
`FORWARD_FREEZE_CANDIDATE`. A pass cannot set `ALPHA_READY` or authorize PAPER.
It can only freeze exact code for new observations beginning no earlier than
2026-09-01. Qualification still requires the separately preregistered minimum
forward duration, trade count, cost stress, breadth and drawdown gates.

### Trial 15 outcome

The single screen returned `FORWARD_FREEZE_CANDIDATE`. The slow state retained
approximately 68–69% of stress trades and improved stress PF from 1.1833 to
1.2040 in selection and from 1.0382 to 1.1071 in robustness while reducing
stress drawdown in both windows. All frozen absolute and relative gates passed.

The outcome does not amend the evidence boundary above. It is a successful
reused-data synthesis, not alpha: robustness stress returned only 0.98%, and
BTC and SOL expectancy remained negative. The exact candidate is frozen for
future observation; PAPER and LIVE remain prohibited. Full evidence is in the
[`trial 15 report`](../regime-aligned-screen/REPORT.md).

## Research controls for the next run

- Commit strategy code, tests, exact data slices, costs and gates before market
  archives are opened by the performance command.
- Run exact-slice field preflight before consuming the one-shot attempt.
- Consume the attempt before the first price or funding archive access; a
  crash does not release it.
- Preserve one-position-per-symbol and next-bar-market semantics.
- Report selection and robustness separately under baseline and stress.
- Do not lower the PF gate, remove a symbol, change the SMA horizon, or adjust
  lifecycle values after observing the outcome.
- Keep every strategy status fail-closed for PAPER until genuinely new forward
  evidence passes its own frozen protocol.

## Source artifacts

- [`development-screen`](../development-screen/REPORT.md)
- [`orderflow-screen`](../orderflow-screen/REPORT.md)
- [`regime-retest-screen`](../regime-retest-screen/REPORT.md)
- [`quarter-hour-screen`](../quarter-hour-screen/REPORT.md)
- [`right-tail-screen`](../right-tail-screen/REPORT.md)
- [`crowded-trend-screen`](../crowded-trend-screen/REPORT.md)
- [`donchian-screen`](../donchian-screen/REPORT.md)
- [`sma200-screen`](../sma200-screen/REPORT.md)
- [`market-anatomy`](../market-anatomy/REPORT.md)
- [`derivatives-state`](../derivatives-state/REPORT.md)
