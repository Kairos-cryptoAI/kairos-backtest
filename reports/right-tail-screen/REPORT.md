# Right-tail trend reused-data screen

## Decision

`REJECT_REUSED_DATA_SCREEN`

The single preregistered `right_tail_trend_v1` candidate did not satisfy every
gate. Its robustness stress profit factor was `1.0382706977`; the exact rule
required a value strictly above `1.05`. The candidate is not eligible for a
future freeze, PAPER, alpha promotion, or LIVE trading. No parameter, cost
assumption, symbol, window, or threshold was changed after observing the data.

## Evidence boundary

- Plan SHA-256: `4b98938b7880c4a799a528a1f7f3e0a83fbd4bf2b4cb606ff51bf6daec1ecef4`
- Attempt payload SHA-256: `9e01b505f701b336db861db84c57e07c85dd2bec560134aa84db71e76240aa40`
- Attempt file SHA-256: `c4c5c46d73fba8e5d843817ec236457dc1476794a2750a87c79d1a43d1a31e7b`
- Summary file SHA-256: `1785f08ff5d226885580ec70a5f2267062917ef701c909b417b01e910b8374f6`
- Summary bytes: `38879`
- Preregistered code commit: `b29020a64ae84d7e574715dd4df7e50007c56f97`
- Strategy source commit: `331d8751901a8566fc4c99afd25d18cfd6db2f8f`
- Strategy source-tree SHA-256: `039c53945d35299c15be553bcc3d90007410e8f3d7d0434653b814772b480878`
- Result permissions: `alpha_ready=false`, `paper_allowed=false`,
  `promotion_eligible=false`, `live_allowed=false`

The attempt ledger was created and fsynced before the first market archive
access. A crash or failure could not release lineage trial 11 for another run.

## Data quality

All 305 expected Binance archives were present and passed their official
SHA-256 sidecars and ZIP CRC. The audit covered 13,355,999 valid one-minute rows
and 531,027,699 compressed bytes, with 99.8922919284% overall coverage. It
retained five historical gap boundaries, 14,400 missing minutes and one invalid
XRP row. As preregistered, the incomplete SOL and XRP research cells were marked
unavailable. Selection and robustness were complete for all five symbols; no
bar was filled or repaired for the evaluation.

## Portfolio results

| Window | Scenario | Return | Drawdown | HAC Sharpe | Profit factor | Expectancy/trade | Trades | Win rate |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| Research | Baseline | -0.8067% | 3.6336% | -0.0866 | 0.9815 | -$0.61 | 799 | 30.66% |
| Research | Stress | -7.1080% | 7.4911% | -1.2051 | 0.8028 | -$5.40 | 790 | 28.35% |
| Selection | Baseline | 5.5692% | 1.2308% | 1.8788 | 1.3756 | $12.13 | 459 | 34.42% |
| Selection | Stress | 2.3086% | 1.2801% | 1.0063 | 1.1834 | $5.03 | 459 | 32.68% |
| Robustness | Baseline | 4.0820% | 1.4780% | 1.2625 | 1.2624 | $8.26 | 494 | 34.41% |
| Robustness | Stress | 0.4973% | 1.7652% | 0.2121 | **1.0383** | $1.02 | 489 | 32.72% |

The intended right-tail shape appeared: median trades lost a little more than
`1R`, while the 90th percentile returned about `3.35R` to `3.82R`. Trade count,
direction balance, drawdown, diversification, return, expectancy and HAC Sharpe
passed every gated selection and robustness cell. Only the robustness stress
profit-factor gate failed, but one failure is sufficient for rejection.

## Interpretation

This candidate is materially better than the earlier zero-signal proxy and
demonstrates that the shared engine can express a causal, diversified,
approximately 1.25-trades-per-day portfolio. It still does not demonstrate a
stable edge. The negative older window shows regime dependence, while stress
costs reduced the newest-window return from 4.0820% to 0.4973% and made BTC and
SOL expectancy negative. These results are far below the requested high-return
objective and too fragile to justify execution risk.

The failure must not be converted into a pass by lowering the `1.05` threshold,
removing losing symbols, changing the 24-hour score, or adjusting stop/target
parameters on the same data. Any successor requires a distinct economic
hypothesis and a separately preregistered lineage; the exact candidate remains
available only for reproducibility.
