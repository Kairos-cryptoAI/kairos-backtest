# Conditional quarter-hour execution overlay

Status: `PREREGISTERED_NOT_RUN`.

Canonical plan SHA-256:
`637e9240545f7dfcd10a21989bff761ce55ef4eed9f51d7eea4e6415af0ff073`.

This is the only permitted cost-aware follow-up to the lag replication. It is
conditional on the parent v2 result being `STATISTICAL_COMPONENT_CONFIRMED`.
If the parent result fails any gate, this experiment is not run and is recorded
as `NOT_RUN_PARENT_COMPONENT_REJECTED`.

The overlay cannot generate direction or add a trade. It receives the exact
immutable `regime_aligned_right_tail_v1` intent due at the 01:00 UTC boundary.
When the clean phase-zero forecast is adverse to that intent, entry is delayed
by exactly ten seconds; otherwise the base entry clock is retained. Stop,
target, timeout, symbol, side and expiry remain unchanged. Missing or dirty
microstructure evidence falls back to the base entry clock rather than
silently deleting a trade.

The screen is a single reused-data A/B test with paired TCA and complete
lifecycle economics. It can only reject the overlay or freeze it for a new
forward lineage. It cannot amend the already sealed Trial 15 campaign and
cannot set ALPHA, PAPER or LIVE permissions.
