# Kairos strategy v2 development screen

Date: 2026-08-17

Decision: **REJECT_ALL**

Classification: development diagnostics on reused research data

## Executive result

None of the three preregistered `trend_pullback_reclaim_v1` depth variants
passed the fixed development gate. The medium variant produced the desired
baseline frequency, but its economics were negative. Shallow showed a small
baseline gain that disappeared under stress. Deep remained slightly positive
in both scenarios but generated too few closed trades to support selection.

This run does not authorize promotion, shadow operation or live trading. The
machine-readable result records all three permissions as `false`.

## Pullback results

| Variant | Scenario | Trades | Net return | Profit factor | Expectancy/trade | Max drawdown | Fees | Shortfall | Funding |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| Shallow | Baseline | 55 | +0.1249% | 1.130 | +$0.76 | 0.2926% | $81.10 | $54.07 | $0.00 |
| Shallow | Stress | 16 | -0.0568% | 0.840 | -$1.18 | 0.1954% | $21.83 | $29.10 | $0.68 |
| Medium | Baseline | 105 | -0.7125% | 0.683 | -$2.26 | 0.9611% | $153.07 | $102.04 | $0.00 |
| Medium | Stress | 41 | -0.1080% | 0.887 | -$0.88 | 0.3330% | $56.19 | $74.92 | $3.59 |
| Deep | Baseline | 38 | +0.0406% | 1.051 | +$0.36 | 0.2871% | $54.16 | $36.10 | $0.00 |
| Deep | Stress | 10 | +0.0267% | 1.104 | +$0.89 | 0.1847% | $12.14 | $16.19 | $0.70 |

The selection rule required at least 100 closed pullback trades, positive log
growth, profit factor above 1.0 and positive expectancy in each scenario.
Trade count was a sufficiency constraint, not the ranking objective.

## What the result means

- Medium proves that the architecture can generate more trades, but increasing
  frequency alone does not produce an edge after fees and implementation
  shortfall.
- Shallow is not robust to the registered stress assumptions.
- Deep is the only variant with positive economics in both scenarios, but 38
  baseline and 10 stress trades are insufficient. Treating it as a winner
  would be selecting noise.
- Stress sometimes has fewer losses because its stricter all-in cost hurdle
  rejects more candidates. This does not mean worse execution improves the
  strategy.
- The largest admission losses came from setups whose net reward-to-risk was
  below 1.25 or whose reward did not clear modeled costs. The gate is doing its
  job: it is refusing frequent but economically weak trades.

The next experiment must test a structurally different signal family. Adding a
fourth pullback-depth band after observing these outcomes would be post-hoc
tuning and is intentionally prohibited.

## Method and data

- Five symbols: BTCUSDT, ETHUSDT, SOLUSDT, BNBUSDT and XRPUSDT.
- Generation begins 2022-11-27; the 35-day warm-up is excluded from metrics.
- Evaluation interval: 2023-01-01 inclusive through 2023-07-01 exclusive.
- Data role: reused `RESEARCH`; it is neither selection nor blind OOS data.
- Three variants: shallow, medium and deep; fixed seed 42.
- Fixed baseline and adverse stress scenarios; $100,000 total initial capital.
- No downloads, API calls, imputation or live orders.
- All 40 month-aligned ZIP archives and official SHA-256 sidecars for the
  generation horizon were verified. The audit found 1,742,400/1,742,400 valid
  rows, zero gaps and zero invalid rows.
- Inventory SHA-256:
  `149057fb589d00b1b04bd12238aaee598ad2db309bb6484f0434ef15a80ef6c2`.

The trend-breakout and range-mean-reversion sleeves were retained as diagnostic
controls. Candidate eligibility and ranking used the pullback sleeve alone, so
the controls could neither rescue nor disqualify a pullback variant.

## Reproduction and integrity

Run from the repository root with the pinned lock and local historical cache:

```powershell
uv run --locked kairos-development-screen `
  --cache-dir data\historical `
  --plan-output reports\development-screen\plan.json `
  --result-output reports\development-screen\result.json
```

The command refuses to overwrite existing evidence unless `--overwrite` is
explicitly supplied. The immutable plan is written before cache evaluation.

| Artifact | Bytes | SHA-256 | Git policy |
| --- | ---: | --- | --- |
| `plan.json` | 7,608 | `96fae3da0d25893772018faad242b8c51cefcbd778d12e97bc0fe58578afffd6` | Committed |
| `summary.json` | compact | Verified by tests and review | Committed |
| `result.json` | 88,608,989 | `54c2c0204799a1b46262efd9e9a6de73439e1c97487f1f4a1a2e5ae76aab45a9` | Local and ignored |

The plan's internal SHA-256 is
`2d9c749e5b513576ba47a2e8668bb46962500e7247c29a7fb275e535cc028f86`.
Its source-tree SHA-256 is
`bec363e0110e0760d5c2c48e87689724145dcf48cf1e1e6d781cd6182577a7b3`,
which matches the final evaluated Python source. Runtime evidence records
CPython 3.11.15, NumPy 2.4.6 and the pinned project dependency versions.

The compact [summary](summary.json) preserves the exact unrounded metrics and
artifact hashes. The ignored full result contains daily equity, intent, fill,
funding and replay ledgers for every evaluated cell.

## Next gate

Any new hypothesis remains development-only until it passes its own registered
research and selection protocol. A future blind test must be physically
withheld, frozen before ingestion and evaluated only once. Even a successful
offline gate can permit at most shadow operation; live capital additionally
requires external signed attestation, venue-specific shadow evidence and a
separate canary decision.
