# Trial 15 independent forward campaign

Status as of 2026-08-27: `PREREGISTERED_NOT_STARTED`.

The exact `regime_aligned_right_tail_v1` candidate passed its one permitted
reused-data screen, but that evidence cannot establish alpha. This campaign
collects independent Binance USD-M one-minute bars beginning on 2026-09-01.
Bars from 2026-07-23 through 2026-08-31 are feature-only warmup and can never
contribute a scored trade.

The canonical plan SHA-256 is
`15cc52c1356cce349c623dd4753c1ca6b91de386041b132b016949add43f2528`.
It binds the five-symbol universe, exact generator source-tree SHA-256, config,
daily decision clock, 40-day runtime window, execution scenarios and gates.

The append-only SQLite observer:

- accepts only strict, closed `ClosedBarEventV1` values;
- requires the first bar of every symbol at the exact warmup boundary;
- rejects and persistently quarantines gaps, reorders and conflicts;
- stores every bar in a per-symbol SHA-256 chain;
- invokes the shared strategy code only after the `00:00–00:59 UTC` hour;
- stores deterministic `StrategyIntentV1` payloads only after the blind start;
- performs no exchange mutation and makes no EVEDEX, LLM or X API calls;
- exposes coverage and intent counts, but no interim performance.

The duration gate cannot be reached before 2027-09-01. A final one-shot
evaluation is eligible only when both 365 complete days and 500 simulated
closed trades are present. Passing can produce only an alpha candidate that
still requires a separate PAPER approval. Until then `ALPHA_READY=false`,
`PAPER_ALLOWED=false` and `LIVE_ALLOWED=false`.
