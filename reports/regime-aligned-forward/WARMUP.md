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
