# Kairos order-flow volatility development screen

Date: 2026-08-17

Decision: **REJECT_ALL**

Classification: development diagnostics on reused research data

## Executive result

None of the three preregistered `orderflow_volatility_expansion_v1`
hypotheses passed the fixed development gate. `PERSISTENCE` produced the
required frequency—387 baseline and 301 stress trades—but lost 2.90% and
3.25%, respectively. `IMPULSE` and `FLIP_RELEASE` traded less often and were
also negative in both scenarios.

This is not merely a fee problem. PnL measured at simulated execution prices
was already negative before fees and funding in all six trial/scenario cells.
The order-flow expansion event does not provide a reliable continuation edge
on this reused six-month sample. Promotion, shadow operation and live trading
remain disabled.

## The frequent variant has no net edge

| Variant | Scenario | Trades | Net return | Profit factor | Expectancy/trade | Max drawdown | Fees | Shortfall | Funding |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| Impulse | Baseline | 124 | -1.4559% | 0.356 | -$11.74 | 1.5654% | $546.80 | $364.53 | $0.00 |
| Impulse | Stress | 92 | -1.2052% | 0.354 | -$13.10 | 1.2914% | $407.03 | $542.70 | $14.99 |
| Persistence | Baseline | 387 | -2.9005% | 0.574 | -$7.49 | 2.9560% | $1,697.45 | $1,131.63 | $0.00 |
| Persistence | Stress | 301 | -3.2540% | 0.448 | -$10.81 | 3.2893% | $1,308.65 | $1,744.86 | $41.97 |
| Flip release | Baseline | 80 | -0.6687% | 0.542 | -$8.36 | 0.7627% | $352.52 | $235.02 | $0.00 |
| Flip release | Stress | 60 | -0.5590% | 0.522 | -$9.32 | 0.6374% | $262.29 | $349.71 | $8.14 |

Every scenario required positive log growth, profit factor above 1.0 and
positive expectancy. Frequency requirements were at least 200 portfolio
trades, 20 per symbol, 60 distinct exit days and three positive-expectancy
symbols. Stress drawdown had to remain at or below 5%. Trade count was a
sufficiency condition and never a ranking objective.

`PERSISTENCE` cleared the count, coverage, concentration and drawdown
conditions, but zero of five symbols had positive expectancy. Its high trade
count therefore strengthens the rejection rather than weakening it.

## What is wrong with the signal

- **The expansion is usually being chased after the directional move.** Stop
  loss was the most common exit: 78/124 impulse, 241/387 persistence and 50/80
  flip-release baseline trades. Take-profit hits were only 7, 47 and 10.
- **The raw directional edge is approximately flat.** Adding modeled
  implementation shortfall back to execution-price gross PnL leaves
  reference-price PnL of approximately -$71 for persistence baseline and
  -$158 under stress. Costs then turn that weak signal into a clear loss.
- **Short signals were materially worse in this reused sample.** Persistence
  baseline long trades made +$119 before fees/funding, while shorts lost
  $1,322. Flip-release longs made +$58 before fees/funding while shorts lost
  $374. This asymmetry is a hypothesis for a new experiment, not permission to
  delete losing rows from this one.
- **More intelligence downstream cannot manufacture a missing price edge.**
  No LLM or external API was used in this screen. A later model may supply a
  causal regime veto or context feature, but it must pass a new frozen test;
  it cannot retroactively rescue these results.

## Registered strategy and method

The signal used complete closed five-minute candles only. It required prior
volatility compression followed by range and volume expansion, a directional
close and buyer/seller taker imbalance. All rolling baselines excluded the
signal candle. Entry became eligible on the next minute, with a one-bar
expiry, a 1.25 ATR stop, a 3R target and a 60-minute maximum hold.

The three mutually exclusive hypotheses were evaluated in a fixed order:

1. `IMPULSE`: a strong current directional taker imbalance.
2. `PERSISTENCE`: directional flow sustained across three bars.
3. `FLIP_RELEASE`: current flow reverses a three-bar opposing imbalance.

Overlaps were assigned by the frozen priority `FLIP_RELEASE`, then
`PERSISTENCE`, then `IMPULSE`. One sleeve-symbol position could be pending or
open at a time. Baseline and stress used the same intent inventory; only
admission, fills and modeled costs could differ.

## Data quality supports the rejection, not promotion

- Five Binance USD-M symbols: BTCUSDT, ETHUSDT, SOLUSDT, BNBUSDT and XRPUSDT.
- Generation began 2022-05-27; the 35-day warm-up was excluded from metrics.
- Evaluation interval: 2022-07-01 inclusive through 2023-01-01 exclusive.
- `1,576,800/1,576,800` selected minute rows were present, aligned and valid.
- All 40 monthly ZIP archives, official SHA-256 sidecars and ZIP CRC values
  were verified; there were no gaps, duplicates or invalid taker-volume rows.
- A synchronized 35-minute zero-volume interval occurred only in warm-up. It
  generated no signal or capacity and forced a fresh 72-bar feature warm-up.
- Canonical selected-slice SHA-256:
  `61c543bbdcc3aceceefcfa820b9513c3feb2fc61af1ca7a94241a6d58e16fadf`.

The data remain reused `RESEARCH/FIT`, not selection or blind OOS evidence.
Binance taker volume is also only a proxy for EVEDEX venue flow. Historical
EVEDEX order book, trades, funding, open interest and liquidations are absent.

For full transparency, an implementation-performance benchmark inspected BTC
intent counts after the strategy thresholds were frozen in code but before the
formal plan artifact was written. It did not inspect PnL, fills or any other
symbol, and no threshold changed afterward. The actual five-symbol screen did
write and seal its plan before reading the cache, but this earlier engineering
check is another reason to treat the entire result strictly as development
evidence rather than preregistered or out-of-sample evidence.

## Next decision

Do not loosen this screen or add a fourth post-hoc variant. The next
development experiment should be a new versioned hypothesis that:

1. treats taker flow as a timing/context feature rather than a standalone
   continuation direction;
2. preregisters long and short logic separately because their observed
   behavior differs materially;
3. tests a causal regime filter and a non-chasing entry such as a bounded
   retest/reclaim, while keeping the same all-in cost and risk gates;
4. collects venue-specific EVEDEX flow and funding evidence for external
   validity; and
5. freezes a future data boundary before any shadow or API-backed evaluation.

The objective of the next cycle is positive stress expectancy with sufficient
trade frequency—not a cosmetic increase in the number of signals.

## Reproduction and integrity

Run from the repository root with the pinned lock and local historical cache:

```powershell
uv run --locked kairos-orderflow-screen `
  --cache-dir data\historical `
  --plan-output reports\orderflow-screen\plan.json `
  --result-output reports\orderflow-screen\result.json `
  --summary-output reports\orderflow-screen\summary.json `
  --overwrite
```

The command refuses dirty source state, writes the immutable plan before cache
access, uses no network, and rechecks source provenance before publishing the
result. The full replay evidence remains local and ignored.

| Artifact | Bytes | SHA-256 | Git policy |
| --- | ---: | --- | --- |
| `plan.json` | 12,103 | `e25b52e2fedf359815c82feb6f4ae6be34dce734217dd25bd16b5b5cd512279d` | Committed |
| `summary.json` | 32,332 | `af00dcdbe8be68c1ee93d6eae3c25045035b5537f4e3bbfff17169318c485007` | Committed |
| `result.json` | 7,813,861 | `d8237f5ed8501077e2406e61f2fa9f000073d71ef09e8375510be258a14fdc69` | Local and ignored |

The plan's internal SHA-256 is
`2e259224ee6d14eba67240f9a9f63a52d8ede0512c8a182f4523e00002c7f1db`.
The evaluated source was signed Git commit
`0bf8fd82c62d819c5fce6170f158717ca7d01d91`, tree
`05ca3ca9e784bb14cc073ac52b9968cc7f878b5c`, with package source SHA-256
`1a0b3104465945f8fa7975fe6f4fd41bdb43d49aac39e1804a5bcc054d332aba`.
Runtime evidence records CPython 3.11.15, NumPy 2.4.6 and the pinned project
dependency versions.

The compact [summary](summary.json), immutable [plan](plan.json) and detailed
[data-quality evidence](data-quality.json) are committed. The ignored full
result retains every intent, disposition, fill, funding event, trade and daily
equity snapshot with local replay verification.
