# Kairos regime-retest development screen

Date: 2026-08-18

Decision: **REJECT_ALL**

Classification: development diagnostics on reused research data

## Executive result

The third frozen strategy family did not fail because the dataset was broken or
because fees erased a promising sample. It failed earlier: the stacked regime,
expansion, retest and admission requirements reduced 41,741 structural
breakout candidates to 12 structural intents, two flow-reacceleration intents,
zero absorption intents and only one executed baseline trade. Stress admitted
no trades at all.

The single baseline trade was a long XRPUSDT stop loss. It lost $8.00 at the
reference prices; $3.00 of modeled implementation shortfall worsened execution
gross PnL to -$11.00, and fees brought net PnL to -$15.49. One trade is not
statistical evidence about profitability; it is evidence that the frozen
strategy cannot meet the required portfolio frequency.

The correct decision is to keep promotion, shadow operation, live trading and
real API use disabled. Trials 7, 8 and 9 are consumed and must not be rerun or
retuned against this interval.

## The signal funnel is too restrictive

The common setup path rejected 39,626 of 41,741 breakout candidates at the
higher-timeframe regime veto (94.93%). Only 2,115 candidates reached the
expansion check, 184 armed a retest setup and 12 completed a structural
reclaim. In other words, only 0.029% of initial candidates reached a reclaim.

| Variant | Emitted intents | Evaluation intents | Baseline trades | Stress trades | Decision |
| --- | ---: | ---: | ---: | ---: | --- |
| Structural reclaim | 12 | 9 | 1 | 0 | Reject |
| Flow reacceleration | 2 | 2 | 0 | 0 | Reject |
| Absorption reclaim | 0 | 0 | 0 | 0 | Reject |

Direction was also structurally imbalanced. The generator saw 22,843 long and
18,898 short breakout candidates, but armed 181 long setups and only three
short setups. All 12 reclaims were long; no short reclaim survived. This is a
calibration failure of the hard veto/state-machine combination, not evidence
that shorts should be deleted after seeing the result.

## Cost-aware admission prevented weak geometry from trading

Nine structural intents belonged to the evaluation window. Baseline rejected
eight: seven were below the frozen reward/risk hurdle and one had a stop that
was too tight. Stress rejected all nine. Both flow-reacceleration intents were
also rejected on reward/risk; absorption emitted none.

The only admitted trade was XRPUSDT long on 2024-02-15. It hit its stop within
about ten minutes:

| Measure | Result |
| --- | ---: |
| Reference-price gross PnL | -$8.00 |
| Execution-price gross PnL | -$11.00 |
| Fees | -$4.50 |
| Implementation shortfall (bridge into execution gross) | -$3.00 |
| Net PnL | -$15.49 |
| R multiple | -1.63R |

The risk layer behaved correctly by refusing setups whose achievable reward
after the next-open entry no longer justified the stop. Loosening that gate to
manufacture more trades would admit setups that failed the frozen ex-ante
geometry hurdle; this run did not estimate their counterfactual PnL.

## Frozen trial results

| Variant | Scenario | Trades | Net return | Profit factor | Expectancy/trade | Reference gross PnL | HAC Sharpe | Max drawdown |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| Structural reclaim | Baseline | 1 | -0.015492% | 0.000 | -$15.49 | -$8.00 | -1.581 | 0.015492% |
| Structural reclaim | Stress | 0 | 0.000000% | N/A | $0.00 | $0.00 | N/A | 0.000000% |
| Flow reacceleration | Baseline | 0 | 0.000000% | N/A | $0.00 | $0.00 | N/A | 0.000000% |
| Flow reacceleration | Stress | 0 | 0.000000% | N/A | $0.00 | $0.00 | N/A | 0.000000% |
| Absorption reclaim | Baseline | 0 | 0.000000% | N/A | $0.00 | $0.00 | N/A | 0.000000% |
| Absorption reclaim | Stress | 0 | 0.000000% | N/A | $0.00 | $0.00 | N/A | 0.000000% |

Every required frequency gate and every positive-economics gate failed. Some
risk ceilings passed trivially because stress executed no trades. The minimum
was 165 trades per scenario, at least 17 per symbol, at least 50 distinct exit
days, positive log growth, profit factor above 1.0, positive expectancy and
positive reference-price gross PnL. Each enabled direction also required at
least 50 trades with positive expectancy and profit factor above 1.0.

## Data quality supports rejection, not promotion

The selected slice is complete and internally valid:

- Five Binance USD-M symbols: BTCUSDT, ETHUSDT, SOLUSDT, BNBUSDT and XRPUSDT.
- Generation interval: 2023-12-01 inclusive through 2024-07-01 exclusive.
- Warm-up: 89,280 minutes per symbol; evaluation: 217,440 minutes per symbol.
- Total: 1,533,600/1,533,600 expected minute rows.
- All 35 ZIP archives, official SHA-256 sidecars and ZIP CRC values verified.
- Zero missing minutes, duplicate minutes, invalid rows or zero-volume rows.
- Zero non-finite values, invalid OHLC geometry, taker volume above total
  volume, or quote-volume contradictions.

November 2023 was excluded before the plan was frozen because a known upstream
XRP row in that month is invalid. It was not repaired or imputed. The selected
December-to-June slice contains no such anomaly.

This remains reused `RESEARCH/FIT` evidence. Binance flow is only a proxy for
EVEDEX, and historical EVEDEX funding, depth, trades, open interest and
liquidations are absent. The data are strong enough to reject the strategy;
they are not sufficient to authorize capital.

## Next decision

Do not create a fourth threshold variant of this family. The bottleneck is the
logical intersection of several individually rare gates. The next research
version should change the structure, not cosmetically relax every threshold:

1. Replace the hard regime veto with a signed regime score or sizing bias, so
   a candidate can survive one uncertain higher-timeframe feature.
2. Use independent evidence contributions rather than requiring regime,
   expansion, boundary retest, reclaim and flow confirmation simultaneously.
3. Freeze an explicit pre-cost signal-frequency band before PnL evaluation;
   for this five-symbol portfolio, target roughly 2-8 executable candidates
   per day while keeping positive stress expectancy as the actual objective.
4. Keep the cost-aware reward/risk admission gate. More trades should come
   from a broader causal signal, not from accepting uneconomic entries.
5. Use the remaining selection window only after the next candidate and its
   cumulative trial lineage are frozen. Do not call reused research results
   out-of-sample.

The architecture can support many trades, but it cannot make 30% monthly
returns a design requirement. The defensible objective remains positive
after-cost expectancy, controlled drawdown and evidence that survives an
untouched interval. API intelligence should later rank, veto or size an
already viable signal set; it should not be asked to invent edge from twelve
offline events.

## Integrity and one-shot status

The attempt was anchored to signed Git commit
`deba2568f3fbfde8c7dda75f36e74ac31d36cd29` after local Python 3.11/3.14
validation and successful GitHub CI. The screen wrote the immutable plan and
consumed attempt ledger before parsing cached market values.

| Artifact | Bytes | SHA-256 | Git policy |
| --- | ---: | --- | --- |
| `plan.json` | 21,783 | `2eaf31ce1d3e2f3432c52fb0a5141b746308e7285bb1ac489ae403a6b71e58a4` | Committed |
| `attempt.json` | 983 | `b21238a3aca5abb6f72a6daade9d8527921340f15454421e55512f5a9927e397` | Committed |
| `summary.json` | 65,231 | `4906b8fdd5c5e34f1919991b8a2af733336c026ee3947d867432b235c7c25787` | Committed |
| `result.json` | 289,890,537 | `2d863d5bae54ec6e9fa5e6dae76711efd17f8a999d55c27787d12dff04cd5be1` | Local and ignored |

The ignored full result retains every event, intent, disposition, fill, trade,
funding placeholder and daily equity snapshot. The compact [summary](summary.json),
[plan](plan.json), consumed [attempt](attempt.json) and [data-quality evidence](data-quality.json)
are the durable audit surface. The CLI must not be run again: the existing
canonical artifacts intentionally make a second attempt fail closed.
