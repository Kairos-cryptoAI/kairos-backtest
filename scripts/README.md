# Research recovery supervision

Use Windows PowerShell after restoring the locked `.venv` and verifying a clean
`main`. Each track owns an exclusive OS lock and refuses an existing live child
or an independently running command for the same research track.

```powershell
./scripts/Invoke-ResearchRecovery.ps1 -Track Forward
./scripts/Invoke-ResearchRecovery.ps1 -Track QuarterHour
```

Launch long sessions with `Start-Process -WindowStyle Hidden`, using the absolute
script path and `-NoProfile -File`. The default runtime root is
`D:\Kairos\runtime`. Inspect `research-recovery/forward.status.json` and
`research-recovery/quarterhour.status.json` there. Each run also retains its own
status and stdout/stderr files. Status distinguishes `RUNNING`, `FAILED` and
`COMPLETED`, records the phase and child identity, and separates heartbeat time
from last log-output time. A stale heartbeat is not completion; reconcile the
recorded process before resuming. No supervisor performs Git publication.

The Forward track makes a verified pre-sync backup, resumes official daily
archives, verifies the ledger, checks performance-blind eligibility, and creates
a unique post-sync backup and recovery copy. It never invokes final performance
evaluation. Record actual retrieval time and retrospectively appended periods
in the coverage report after completion.

Before starting QuarterHour, preserve and verify a backup of its existing
ledger and repair any invalid cached archive without modifying accepted batches.
The track resumes collection with four workers, deeply verifies the result, and
runs the fixed V2 replication. A pre-existing result stops the track for review;
it is never overwritten. An external failure stops subsequent phases. Inspect
the complete result and its parent gate before any conditional overlay work.

If concurrent archive reads repeatedly time out, first verify the committed
chain, preserve a new backup and confirm all prior processes have exited. Then
use `-Track QuarterHour -Workers 1` for serial transport/extraction. The default
remains four; the supported range is 1–4 and the selected value is in status.
This changes only concurrency, not frozen source, archive checks, feature
fingerprints or evaluator rules. A timeout still stops the run for inspection;
there is no unlimited retry loop or checksum-error fallback.

`Test-ResearchRecovery.ps1` exercises real harmless child processes, exit-code
propagation, atomic status replacement and exclusive locking on Windows. It
does not open market data, launch collection or call a paid service.
