# Kairos market and strategy research map

## Decision boundary

Kairos does not optimize a strategy for a requested monthly return. The
research objective is positive after-cost geometric growth subject to bounded
drawdown, survival, execution capacity and evidence that survives unseen data.
Strong positive months are desirable outcomes of favorable regimes, not a
fixed target that can override risk.

All strategy screens completed before this document remain rejected. This map
does not reinterpret their results, add a trial, authorize PAPER, or claim
alpha. Its first executable artifact is the preregistered, descriptive-only
[`market_anatomy_v1`](reports/market-anatomy/plan.json) study.

## How the market changed

The crypto market progressed from fragmented retail spot markets, through the
rapid adoption of perpetual futures and leveraged liquidation mechanics, to a
continuous institutional derivatives market. CME introduced 24/7 trading in
2026 and reported $459.2 billion of Q2 crypto futures and options notional
volume. Weekend volatility remains material even as the asset class matures.

This changes where a defensible edge can live:

- simple public indicator conjunctions face faster competition and do not
  become independent evidence merely because they use different names;
- perpetual returns incorporate spot movement, basis, funding demand and
  leverage constraints;
- liquidity, latency, fees, partial fills and forced liquidations are part of
  the strategy rather than implementation details;
- macro and official news can switch the operating regime before a slow price
  feature adapts;
- a five-asset crypto portfolio retains substantial common market exposure, so
  five simultaneous signals are not necessarily five independent bets.

Primary references:

- [CME Q2 2026 cryptocurrency report](https://www.cmegroup.com/newsletters/quarterly-cryptocurrencies-report/2026-q2-cryptocurrency-highlights.html)
- [CME analysis of the 24/7 trading opportunity](https://www.cmegroup.com/articles/2026/aligning-cryptocurrency-derivatives-with-spot-markets-measuring-the-247-trading-opportunity.html)
- [Crypto carry, BIS Working Paper 1087](https://www.bis.org/publ/work1087.htm)
- [Anatomy of cryptocurrency perpetual futures returns](https://www.research.ed.ac.uk/en/publications/anatomy-of-cryptocurrency-perpetual-futures-returns/)

## Economically distinct strategy families

| Family | Economic premise | Expected favorable state | Structural failure mode | Kairos role |
| --- | --- | --- | --- | --- |
| Time-series trend | gradual positioning and underreaction | sustained directional move | chop and fast reversal | directional core candidate |
| Volatility breakout | transition from compression to expansion | new information and repricing | false breakout | candidate trigger inside trend sleeve |
| Conditional mean reversion | temporary price/liquidity displacement | liquid range or exhausted shock | averaging into a cascade | separate tactical sleeve |
| Basis/funding carry | leveraged directional demand meets limited arbitrage capital | elevated but executable basis | crash, margin, venue and leg risk | future hedged sleeve |
| Order flow | aggressive flow temporarily predicts near-term price pressure | deep, timely order book | latency and adverse selection | timing/veto feature |
| Liquidation response | forced orders create discontinuous impact | identifiable leverage/liquidity shock | heterogeneous exogenous event | risk overlay and future event sleeve |
| Market making | spread compensation exceeds inventory/adverse-selection cost | deep two-sided book | toxic flow and thin venue | excluded from current EVEDEX scope |
| Cross-venue arbitrage | segmented prices/funding converge | reliable balances and both executable legs | transfer, counterparty and leg risk | future infrastructure track |
| News/event | market interpretation is incomplete or delayed | official material event | stale, duplicate or false narrative | LLM review/veto/priority only |
| Options volatility | implied volatility differs from realized/tail risk | liquid volatility surface | wide spreads and short-vol tail | regime feature until venue support exists |
| ML/RL | nonlinear ranking of causal evidence | stable feature/label relationship | overfit and policy drift | ranker/calibrator, not authority |

Trend following has unusually broad historical evidence, but that evidence is
not proof that a particular intraday crypto implementation survives current
costs. Crypto carry can be large, but high carry also accompanies leveraged
trend chasing and predicts crash risk. Published LLM and microstructure results
are hypotheses to reproduce, not permission to extrapolate headline returns.

Further primary references:

- [A century of evidence on trend-following investing](https://www.aqr.com/Insights/Research/Journal-Article/A-Century-of-Evidence-on-Trend-Following-Investing)
- [Multi-level order-flow imbalance in a limit order book](https://arxiv.org/abs/1907.06230)
- [Sentiment trading with large language models](https://discovery.ucl.ac.uk/id/eprint/10190583/)
- [Event-heterogeneous liquidation-cascade warnings](https://arxiv.org/abs/2607.27070)

## Desk architecture

The target is a portfolio of independently validated sleeves rather than one
large rule with every condition conjoined:

1. A market-state process labels causal trend, range, volatility and shock
   evidence without choosing a trade.
2. Independent sleeves create immutable candidates and fixed exits.
3. The Aggregator ranks, allows, vetoes or defers candidates without changing
   their direction or economics.
4. The Macro Strategist allocates a bounded risk budget across sleeves and
   correlated symbols.
5. Deterministic risk evaluates loss at stop, total portfolio risk and current
   EVEDEX venue quality.
6. Execution owns entry, protection, timeout, reconciliation and TCA.
7. Performance attribution is reported by sleeve, regime, symbol, direction,
   cost and decision layer so a profitable portfolio cannot hide a broken
   component.

LLM evidence is tested as an overlay against the same frozen candidate stream.
It cannot rescue a negative standalone strategy, increase size from confidence,
or invent SL/TP levels.

## Return, capital and capacity

A 30% return in each calendar month compounds to approximately 2,230% per year
and multiplies capital by 23.3. A $100,000 monthly profit requires roughly
$333,000 at 30%, $1 million at 10%, or $2 million at 5%, before costs and taxes.
Those figures describe arithmetic, not achievable targets.

The portfolio objective is therefore a distribution:

- positive after-cost expectancy and geometric growth;
- explicit limits on drawdown, open loss and risk of ruin;
- enough capacity that higher capital does not erase percentage return;
- acceptance of flat and losing months;
- preserved convex upside during rare sustained trends or dislocations.

Optimizing directly for a 30% or 1,000% month selects leverage, concentration
and historical accidents. Kairos instead records every attempted hypothesis
and adjusts reported performance for multiple testing. See
[The Probability of Backtest Overfitting](https://papers.ssrn.com/sol3/Papers.cfm?abstract_id=2326253)
and [The Deflated Sharpe Ratio](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=2460551).

## `market_anatomy_v1`

The first study aggregates only complete 60-minute buckets from the existing
official Binance USD-M one-minute archive. It never fills gaps and measures:

- per-window return, realized volatility, drawdown and quote-volume capacity;
- pairwise hourly return correlation across the five symbols;
- causal 24-hour trend and volatility regimes;
- forward continuation after trend, 24-hour breakout and extreme taker flow;
- reversal after a four-hour shock that begins in a prior range;
- the share of absolute forward movement exceeding fixed 9, 15 and 25 basis
  point cost hurdles.

The study uses only reused research, selection and robustness data. Its gates
can return permission to preregister one new prototype family. They cannot
produce `ALPHA_READY`, PAPER permission, promotion evidence or LIVE authority.

The completed study returned `NO_PROTOTYPE_PASSED_DESCRIPTIVE_GATES`. Movement
was ample, but simple regime trend, 24-hour breakout, range-shock reversion and
hourly taker-flow alignment were not stable enough across the two later reused
windows. The full evidence and interpretation are in the
[`market_anatomy_v1` report](reports/market-anatomy/REPORT.md).

Historical EVEDEX basis, funding, depth, open interest and liquidations are not
present in the kline archive. Carry and liquidation hypotheses remain blocked
on a separately versioned data-acquisition study rather than being represented
as zero-valued features.

## `derivatives_state_v1`

The next descriptive study is fixed in the committed
[`derivatives_state_v1` plan](reports/derivatives-state/plan.json). It adds only
official Binance USD-M archives: eight-hour funding observations, one-hour
premium-index bars and five-minute open-interest/positioning metrics. Every ZIP
is bound to Binance's adjacent SHA-256 sidecar and checked with ZIP CRC.

The causal join uses the same complete hourly price close, the premium close
for that completed hour, the latest five-minute metrics observation no later
than the close and the latest funding observation no more than eight hours old.
It never bridges a missing hour. Zero open-interest observations are treated as
unusable venue-data outages, not deleveraging, and missing optional positioning
ratios are counted without zero imputation. The study measures fixed funding
and premium contrarian diagnostics, trend after material deleveraging, and
whether vetoing crowded trend states improves the already rejected trend
baseline.

This is reused-data hypothesis triage, not a backtest and not alpha evidence.
Passing a fixed descriptive gate can authorize only one separately
preregistered prototype or overlay. It can never authorize PAPER or LIVE.

Acquire and audit the fixed public-data inventory without using a paid API:

```sh
uv run --locked kairos-factor-data --download --workers 12
uv run --locked kairos-factor-data --audit
```

After the preregistration commit is clean, execute the one immutable study:

```sh
uv run --locked kairos-derivatives-state
```

The completed study returned `NO_PROTOTYPE_PASSED_DESCRIPTIVE_GATES`.
Funding and premium contrarian diagnostics and trend after material OI
deleveraging failed the fixed mean/hit gates across both reused windows. The
crowding veto reduced, rather than improved, trend continuation. Full data
quality, metrics and interpretation are preserved in the
[`derivatives_state_v1` report](reports/derivatives-state/REPORT.md).

## `right_tail_trend_v1`

The two descriptive studies above did not authorize a prototype. A subsequent
external time-series-trend hypothesis is therefore recorded as a new research
lineage, not as a passed `market_anatomy_v1` candidate. The economic premise is
that a deliberately low-turnover, positively skewed lifecycle may monetize
trend persistence even with a hit rate near one half. Research on crypto
momentum also warns that mean returns, leverage and liquidation assumptions can
reverse apparent profitability, so this candidate is tested with loss-at-stop
sizing and adverse costs rather than optimized for headline return:

- [Momentum in the Cryptocurrency Market under realistic assumptions](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=4675565)
- [A Decade of Evidence of Trend Following Investing in Cryptocurrencies](https://arxiv.org/abs/2009.12155)
- [Analytical results for EMA trend following and turnover costs](https://arxiv.org/abs/1308.5658)

The exact candidate is intentionally small: a 24-hour standardized trend score,
one UTC decision per day, 24-hour ATR, a symmetric 2 ATR stop, a 4R target and a
72-hour timeout. No parameter search, per-symbol rule, side asymmetry, LLM or
derivatives-state filter is permitted. Its use of an already observed feature
makes every archive through July 2026 reused development data. The committed
screen can return only `REJECT_REUSED_DATA_SCREEN` or
`FORWARD_FREEZE_CANDIDATE`; both leave PAPER and LIVE disabled.
