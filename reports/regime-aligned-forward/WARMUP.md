# Forward warmup evidence

This receipt records only data coverage and integrity. It contains no strategy
performance, return, PnL, trade-count or quality metric.

## Campaign

- Plan SHA-256: `38fe7512b4e4c318e5bc8dd6baa66b48eedd63112a4a447eaaf36c1175f623e8`
- Frozen strategy: `regime_aligned_right_tail_v1`, revision `1`
- Universe: BTCUSDT, ETHUSDT, SOLUSDT, BNBUSDT, XRPUSDT
- Required warmup: `2026-07-23T00:00:00Z` through `2026-09-01T00:00:00Z`
- Blind start: `2026-09-01T00:00:00Z`

## Verified coverage at 2026-08-27

- Common watermark: `2026-08-27T00:00:00Z`
- Bars per symbol: `50,400`
- Total bars: `252,000`
- Daily-archive increment: `187,200` bars
- Daily files: 26 completed UTC days × 5 symbols
- Transport evidence: official Binance `.CHECKSUM` plus ZIP CRC
- Row gate: exactly 1,440 contiguous one-minute rows per file
- Field profile: `PRICE_VOLUME`
- Blocked symbols: none
- Stored candidate intents: zero; coverage remains feature-only warmup
- Ledger evidence SHA-256:
  `c5173c95cc08c536feadac977111530e77d57ce08a75bfa33685fcef8666d2cc`

The archive for 2026-08-26 was published after the previous check and was
appended by the resumable all-symbol sync. The ledger still ends at the latest
fully published exclusive boundary; no REST substitute or partial day was used.

## Recovery evidence

- Backup:
  `D:\Kairos\runtime\backups\regime-aligned-forward-through-2026-08-27.sqlite3`
- Recovered copy:
  `D:\Kairos\runtime\recovery\regime-aligned-forward-through-2026-08-27.sqlite3`
- Backup/recovered file SHA-256:
  `74e1e425836751130d654631e98e9b846de8ede8235893189aae5974cd2f083f`
- Recovered evidence SHA-256:
  `c5173c95cc08c536feadac977111530e77d57ce08a75bfa33685fcef8666d2cc`
- Primary unchanged during drill: true

The remaining warmup days must be appended only after their official daily
archives and checksum sidecars are published. No blind performance may be
evaluated before both the duration and closed-trade-count gates mature.

## Recovery receipt: 2026-09-08

The preceding August receipt is retained as historical evidence. The collector
was offline after the Windows migration. Data for **2026-08-27 through
2026-09-06 inclusive** was retrieved retrospectively on **2026-09-08**, not
observed by a continuously running online process. The recovery supervisor ran
from `2026-09-08T04:17:44Z` to `2026-09-08T04:27:12Z`.

- Official resumable `sync-latest`: 55 daily archives (11 days × 5 symbols).
- Appended bars: `79,200`; duplicates: `0`.
- Common exclusive watermark: `2026-09-07T00:00:00Z`.
- Bars per symbol: `66,240`; total bars: `331,200`.
- Transport/row checks: official SHA-256, ZIP CRC and contiguous full-day rows.
- Full ledger verification: `valid`; blocked symbols: none.
- Evidence SHA-256:
  `ec93fa4afeced23d48ab676a4b6581c6ef3b9eca73dc0e970dd8dd6b5433941c`.
- Warmup is complete; the ledger covers 6 complete blind-period days.
- Performance-blind eligibility: duration gate not satisfied; closed-trade-count
  gate not evaluated; no blind performance disclosed.
- Frozen plan, strategy, evaluator and promotion permissions are unchanged.

### September backup and recovery

- Before-sync backup:
  `D:\Kairos\runtime\backups\forward-before-20260908T041744Z-32788.sqlite3`.
- Before-sync file SHA-256:
  `e87e7d50484f4fd32aea3a4b006432194b20ee97438248ba832889b1df9f3795`.
- After-sync backup:
  `D:\Kairos\runtime\backups\forward-after-20260908T041744Z-32788.sqlite3`.
- Recovered copy:
  `D:\Kairos\runtime\recovery\forward-20260908T041744Z-32788.sqlite3`.
- Identical backup/recovered file SHA-256:
  `6569f5e3b1ab5c0f8cb798fc7456be852c19837fca4834f842bbbc1ac832ed2b`.
- Recovered evidence matches the September ledger hash above.
- Primary unchanged during recovery drill: `true`.

The latest published complete boundary during this run was September 7. This
does not assert that September 7's archive will remain unavailable later.
Further observations remain subject to the unchanged 365-day and 500-closed-
trade gates. Retrospective coverage is not proof of online operational uptime.
