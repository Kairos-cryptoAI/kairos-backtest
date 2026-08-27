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

- Common watermark: `2026-08-26T00:00:00Z`
- Bars per symbol: `48,960`
- Total bars: `244,800`
- Daily-archive increment: `180,000` bars
- Daily files: 25 completed UTC days × 5 symbols
- Transport evidence: official Binance `.CHECKSUM` plus ZIP CRC
- Row gate: exactly 1,440 contiguous one-minute rows per file
- Field profile: `PRICE_VOLUME`
- Blocked symbols: none
- Stored candidate intents: zero; coverage remains feature-only warmup
- Ledger evidence SHA-256:
  `33b3f2dc77f1f3182b675f8231e94aa957b90afc62783b6c590912852c963b4a`

The archive for 2026-08-26 was not yet published when checked and returned
HTTP 404. The all-symbol staging gate prevented any ledger mutation during that
failed request. The successful import therefore ends at the latest fully
published exclusive boundary instead of substituting REST or partial data.

## Recovery evidence

- Backup:
  `D:\Kairos\runtime\backups\regime-aligned-forward-through-2026-08-26.sqlite3`
- Recovered copy:
  `D:\Kairos\runtime\recovery\regime-aligned-forward-through-2026-08-26.sqlite3`
- Backup/recovered file SHA-256:
  `e96aad46dff47034ea189345953f521ca14e4f2c7db7c1a65a562d23623c9464`
- Recovered evidence SHA-256:
  `33b3f2dc77f1f3182b675f8231e94aa957b90afc62783b6c590912852c963b4a`
- Primary unchanged during drill: true

The remaining warmup days must be appended only after their official daily
archives and checksum sidecars are published. No blind performance may be
evaluated before both the duration and closed-trade-count gates mature.
