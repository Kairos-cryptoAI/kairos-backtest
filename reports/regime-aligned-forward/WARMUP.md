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

## Recovery receipt: 2026-09-12

The prior receipts remain unchanged. Official archives for **2026-09-07 through
2026-09-11 inclusive** were retrieved retrospectively on **2026-09-12**. This
does not represent continuous online observation. The existing recovery
supervisor completed all six phases between `2026-09-12T13:12:59Z` and
`2026-09-12T13:19:25Z`.

- Official resumable `sync-latest`: 25 daily archives (5 days × 5 symbols).
- Appended bars: `36,000`; duplicates: `0`.
- Common exclusive watermark: `2026-09-12T00:00:00Z`.
- Bars per symbol: `73,440`; total bars: `367,200`.
- Transport/row checks: official SHA-256, ZIP CRC and contiguous full-day rows.
- Full ledger verification: `valid`; blocked symbols: none.
- Evidence SHA-256:
  `5464c9ac23bc9c1ed981da5fd7169efe1ffbc5ce804e6e413b038b13ae359440`.
- Warmup is complete; the ledger covers 11 complete blind-period days.
- Performance-blind eligibility: duration gate not satisfied; closed-trade-count
  gate not evaluated; no blind performance disclosed.
- Frozen plan, strategy, evaluator and promotion permissions are unchanged.

### September 12 backup and recovery

- Before-sync backup:
  `D:\Kairos\runtime\backups\forward-before-20260912T131258Z-34004.sqlite3`.
- Before-sync file SHA-256:
  `6569f5e3b1ab5c0f8cb798fc7456be852c19837fca4834f842bbbc1ac832ed2b`.
- After-sync backup:
  `D:\Kairos\runtime\backups\forward-after-20260912T131258Z-34004.sqlite3`.
- Recovered copy:
  `D:\Kairos\runtime\recovery\forward-20260912T131258Z-34004.sqlite3`.
- Identical backup/recovered file SHA-256:
  `ddd26f7883f17fd94fa93f0d68e0f82231800c98a4cb3fffd6c210ad9e0a608e`.
- Recovered evidence matches the September 12 ledger hash above.
- Primary unchanged during recovery drill: `true`.

The unchanged eligibility requirements remain 365 complete forward days and
500 closed simulated trades per scenario. Coverage and successful recovery
neither establish strategy profitability nor authorize PAPER or LIVE.

## Daily observation receipt: 2026-09-13

The prior receipts remain unchanged. All five official daily archives for
**2026-09-12** were retrieved on **2026-09-13**. This is retrospective daily-archive
collection, not continuous online operational uptime. The existing Forward
supervisor ran once, from `2026-09-13T11:03:18.3408624Z` to
`2026-09-13T11:06:44.5318785Z`, completing all six phases.

- Run: `20260913T110318Z-8568`; logs and phase status:
  `D:\Kairos\runtime\research-recovery\20260913T110318Z-8568`.
- Official resumable `sync-latest`: five daily archives, one complete day per symbol.
- Appended bars: `7,200`; duplicates: `0`.
- Common exclusive watermark: `2026-09-13T00:00:00Z`.
- Bars per symbol: `74,880`; total bars: `374,400`.
- Transport/row checks: official SHA-256, ZIP CRC and 1,440 contiguous rows per archive.
- Full ledger verification: `valid`; blocked symbols: none.
- Evidence SHA-256:
  `9344fc0cf82987eda1f61627eee9c3b780305fb09d86e36b7e88e807fb0d6bc9`.
- The ledger covers 12 complete blind-period days; duration gate remains unsatisfied.
- Performance-blind eligibility did not evaluate closed-trade counts or disclose PnL.
- Frozen strategy/configuration, plan, evaluator lock and promotion permissions are unchanged.

### September 13 backup and recovery

- Before-sync backup:
  `D:\Kairos\runtime\backups\forward-before-20260913T110318Z-8568.sqlite3`.
- Before-sync SHA-256:
  `ddd26f7883f17fd94fa93f0d68e0f82231800c98a4cb3fffd6c210ad9e0a608e`.
- After-sync backup:
  `D:\Kairos\runtime\backups\forward-after-20260913T110318Z-8568.sqlite3`.
- Recovered copy:
  `D:\Kairos\runtime\recovery\forward-20260913T110318Z-8568.sqlite3`.
- Identical backup/recovered SHA-256:
  `d47cfdbb76e2edca69ddb4fa47311534576381373dd3aef6b20ff9e8a426812b`.
- Recovered evidence matches the September 13 ledger evidence hash above.
- Primary unchanged during the recovery drill: `true`.

The independent 365-day and 500-closed-trade gates remain in force. This receipt
records coverage/integrity/recovery only, not strategy performance or trading readiness.

## Recovery receipt: 2026-09-19

The prior receipts remain unchanged. Official archives for **2026-09-13 through
2026-09-18 inclusive** were retrieved retrospectively on **2026-09-19**. This
does not represent continuous online operational uptime. The existing Forward
supervisor ran once, from `2026-09-19T14:12:38Z` to `2026-09-19T14:20:11Z`,
completing all six phases.

- Run: `20260919T141238Z-5240`; logs and phase status:
  `D:\Kairos\runtime\research-recovery\20260919T141238Z-5240`.
- Official resumable `sync-latest`: 30 daily archives (6 days × 5 symbols).
- Appended bars: `43,200`; duplicates: `0`; newly emitted frozen intents: `5`.
- Common exclusive watermark: `2026-09-19T00:00:00Z`.
- Bars per symbol: `83,520`; total bars: `417,600`.
- Transport/row checks: official SHA-256, ZIP CRC and 1,440 contiguous rows per archive.
- Full ledger verification: `valid`; blocked symbols: none.
- Evidence SHA-256:
  `f44ebc4da470f9abcc1099eadd2a9cd6aabc42094a2de415d2d52184fb733bb1`.
- The ledger covers 18 complete blind-period days; duration gate remains unsatisfied.
- Performance-blind eligibility did not evaluate closed-trade counts or disclose PnL.
- Frozen strategy/configuration, plan, evaluator lock and promotion permissions are unchanged.

### September 19 backup and recovery

- Before-sync backup:
  `D:\Kairos\runtime\backups\forward-before-20260919T141238Z-5240.sqlite3`.
- Before-sync SHA-256:
  `d47cfdbb76e2edca69ddb4fa47311534576381373dd3aef6b20ff9e8a426812b`.
- After-sync backup:
  `D:\Kairos\runtime\backups\forward-after-20260919T141238Z-5240.sqlite3`.
- Identical after-sync/recovered SHA-256:
  `8b6540284bed84b22e883080d5f9a89b4db092597382d10866c6b836ab02ecdb`.
- Recovered copy:
  `D:\Kairos\runtime\recovery\forward-20260919T141238Z-5240.sqlite3`.
- Recovered evidence matches the September 19 ledger evidence hash above.
- Primary unchanged during the recovery drill: `true`.

The independent 365-day and 500-closed-trade gates remain in force. This receipt
records coverage/integrity/recovery only, not strategy performance or trading readiness.

## Daily observation receipt: 2026-09-20

The prior receipts remain unchanged. All five official daily archives for
**2026-09-19** were retrieved on **2026-09-20**. This is retrospective daily-archive
collection, not continuous online operational uptime. The existing Forward
supervisor ran once, from `2026-09-20T11:43:49.2273981Z` to
`2026-09-20T11:47:57.2495478Z`, completing all six phases.

- Run: `20260920T114349Z-20160`; logs and phase status:
  `D:\Kairos\runtime\research-recovery\20260920T114349Z-20160`.
- Official resumable `sync-latest`: five daily archives, one complete day per symbol.
- Appended bars: `7,200`; duplicates: `0`.
- Common exclusive watermark: `2026-09-20T00:00:00Z`.
- Bars per symbol: `84,960`; total bars: `424,800`.
- Transport/row checks: official SHA-256, ZIP CRC and 1,440 contiguous rows per archive.
- Full ledger verification: `valid`; blocked symbols: none.
- Evidence SHA-256:
  `c7a1dc3467f13148c3497a1cae01982d501a7bcb2fdbb98b6cd4b75e34145412`.
- The ledger covers 19 complete blind-period days; duration gate remains unsatisfied.
- Performance-blind eligibility did not evaluate closed-trade counts or disclose PnL.
- Frozen strategy/configuration, plan, evaluator lock and promotion permissions are unchanged.

### September 20 backup and recovery

- Before-sync backup:
  `D:\Kairos\runtime\backups\forward-before-20260920T114349Z-20160.sqlite3`.
- Before-sync SHA-256:
  `8b6540284bed84b22e883080d5f9a89b4db092597382d10866c6b836ab02ecdb`.
- After-sync backup:
  `D:\Kairos\runtime\backups\forward-after-20260920T114349Z-20160.sqlite3`.
- Recovered copy:
  `D:\Kairos\runtime\recovery\forward-20260920T114349Z-20160.sqlite3`.
- Identical after-sync/recovered SHA-256:
  `d8df937cc1eeaf5b23c839c4598dd78321e5371e49e5f6c886bc06b2fd6e7e0c`.
- Recovered evidence matches the September 20 ledger evidence hash above.
- Primary unchanged during the recovery drill: `true`.

The independent 365-day and 500-closed-trade gates remain in force. This receipt
records coverage/integrity/recovery only, not strategy performance or trading readiness.

## Daily observation receipt: 2026-09-21

The prior receipts remain unchanged. All five official daily archives for
**2026-09-20** were retrieved on **2026-09-21**. This is retrospective daily-archive
collection, not continuous online operational uptime. The existing Forward
supervisor ran once, from `2026-09-21T11:16:37.7015816Z` to
`2026-09-21T11:21:27.7740402Z`, completing all six phases.

- Run: `20260921T111637Z-16472`; logs and phase status:
  `D:\Kairos\runtime\research-recovery\20260921T111637Z-16472`.
- Official resumable `sync-latest`: five daily archives, one complete day per symbol.
- Appended bars: `7,200`; duplicates: `0`.
- Common exclusive watermark: `2026-09-21T00:00:00Z`.
- Bars per symbol: `86,400`; total bars: `432,000`.
- Transport/row checks: official SHA-256, ZIP CRC and 1,440 contiguous rows per archive.
- Full ledger verification: `valid`; blocked symbols: none.
- Evidence SHA-256:
  `617ed730fec620077aba31f65d09045c05bd716113f7b3b03fb5433c711801da`.
- The ledger covers 20 complete blind-period days; duration gate remains unsatisfied.
- Performance-blind eligibility did not evaluate closed-trade counts or disclose PnL.
- Frozen strategy/configuration, plan, evaluator lock and promotion permissions are unchanged.

### September 21 backup and recovery

- Before-sync backup:
  `D:\Kairos\runtime\backups\forward-before-20260921T111637Z-16472.sqlite3`.
- Before-sync SHA-256:
  `d8df937cc1eeaf5b23c839c4598dd78321e5371e49e5f6c886bc06b2fd6e7e0c`.
- After-sync backup:
  `D:\Kairos\runtime\backups\forward-after-20260921T111637Z-16472.sqlite3`.
- Recovered copy:
  `D:\Kairos\runtime\recovery\forward-20260921T111637Z-16472.sqlite3`.
- Identical after-sync/recovered SHA-256:
  `356bf4cc5969a3f010f047aa68db73e8eb54a88e03b1ca4804e044cb85063064`.
- Recovered evidence matches the September 21 ledger evidence hash above.
- Primary unchanged during the recovery drill: `true`.

The independent 365-day and 500-closed-trade gates remain in force. This receipt
records coverage/integrity/recovery only, not strategy performance or trading readiness.

## Daily observation receipt: 2026-09-22

The prior receipts remain unchanged. All five official daily archives for
**2026-09-21** were retrieved on **2026-09-22**. This is retrospective daily-archive
collection, not continuous online operational uptime. The existing Forward
supervisor ran once, from `2026-09-22T14:21:51.1139445Z` to
`2026-09-22T14:26:06.6010284Z`, completing all six phases.

- Run: `20260922T142151Z-30024`; logs and phase status:
  `D:\Kairos\runtime\research-recovery\20260922T142151Z-30024`.
- Official resumable `sync-latest`: five daily archives, one complete day per symbol.
- Appended bars: `7,200`; duplicates: `0`.
- Common exclusive watermark: `2026-09-22T00:00:00Z`.
- Bars per symbol: `87,840`; total bars: `439,200`.
- Transport/row checks: official SHA-256, ZIP CRC and 1,440 contiguous rows per archive.
- Full ledger verification: `valid`; blocked symbols: none.
- Evidence SHA-256:
  `912228750846daf588e926281db052402a0e14b0ad2ba02d10f1355868afd686`.
- The ledger covers 21 complete blind-period days; duration gate remains unsatisfied.
- Performance-blind eligibility did not evaluate closed-trade counts or disclose PnL.
- Frozen strategy/configuration, plan, evaluator lock and promotion permissions are unchanged.

### September 22 backup and recovery

- Before-sync backup:
  `D:\Kairos\runtime\backups\forward-before-20260922T142151Z-30024.sqlite3`.
- Before-sync SHA-256:
  `356bf4cc5969a3f010f047aa68db73e8eb54a88e03b1ca4804e044cb85063064`.
- After-sync backup:
  `D:\Kairos\runtime\backups\forward-after-20260922T142151Z-30024.sqlite3`.
- Recovered copy:
  `D:\Kairos\runtime\recovery\forward-20260922T142151Z-30024.sqlite3`.
- Identical after-sync/recovered SHA-256:
  `e9f5a3d38609b7a141d92cfb2e37078a8678542996e4231d7c48348e986f43cb`.
- Recovered evidence matches the September 22 ledger evidence hash above.
- Primary unchanged during the recovery drill: `true`.

The independent 365-day and 500-closed-trade gates remain in force. This receipt
records coverage/integrity/recovery only, not strategy performance or trading readiness.
