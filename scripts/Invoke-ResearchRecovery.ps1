param(
    [Parameter(Mandatory)][ValidateSet('Forward', 'QuarterHour')][string]$Track,
    [string]$RuntimeRoot = 'D:\Kairos\runtime',
    [ValidateRange(1, 4)][int]$Workers = 4,
    [switch]$PrepareArchives,
    [ValidateSet('v4', 'v5')][string]$QuarterHourLineage = 'v4',
    [switch]$Resume
)

$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path $PSScriptRoot -Parent
$python = Join-Path $projectRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $python)) { throw 'Restore the locked Python environment first.' }
$runId = (Get-Date).ToUniversalTime().ToString('yyyyMMddTHHmmssZ') + '-' + $PID
$controlRoot = Join-Path $RuntimeRoot 'research-recovery'
New-Item -ItemType Directory -Path $controlRoot -Force | Out-Null
$trackKey = if ($Track -eq 'QuarterHour') {
    'quarterhour-' + $QuarterHourLineage
} else {
    $Track.ToLowerInvariant()
}
$statusPath = Join-Path $controlRoot ($trackKey + '.status.json')
$lockPath = Join-Path $controlRoot ($trackKey + '.lock')
# FileShare.None is an OS lock, released even if the supervisor crashes.
$lock = [IO.File]::Open($lockPath, [IO.FileMode]::OpenOrCreate, [IO.FileAccess]::ReadWrite, [IO.FileShare]::None)
$state = [ordered]@{
    schema_version = 'kairos.research-recovery.v1'
    track = $Track
    quarter_hour_lineage = if ($Track -eq 'QuarterHour') { $QuarterHourLineage } else { $null }
    workers = if ($Track -eq 'QuarterHour') { $Workers } else { $null }
    prepare_archives = [bool]$PrepareArchives
    resume_requested = [bool]$Resume
    run_id = $runId
    supervisor_pid = $PID
    started_at_utc = [DateTime]::UtcNow.ToString('o')
    updated_at_utc = $null
    state = 'STARTING'
    phase = 'preflight'
    child_pid = $null
    child_started_at_utc = $null
    last_output_at_utc = $null
    exit_code = $null
    error = $null
    artifacts = [ordered]@{}
    completed_phases = @()
}
$runDir = Join-Path $controlRoot $runId
$child = $null

function Write-JsonAtomically([object]$Payload, [string]$Destination) {
    $parent = Split-Path -Parent $Destination
    if (-not (Test-Path -LiteralPath $parent)) {
        New-Item -ItemType Directory -Path $parent -Force | Out-Null
    }
    $temporary = $Destination + '.tmp-' + $PID + '-' + [Guid]::NewGuid().ToString('N')
    [IO.File]::WriteAllText(
        $temporary,
        ($Payload | ConvertTo-Json -Depth 12),
        [Text.UTF8Encoding]::new($false)
    )
    if (Test-Path -LiteralPath $Destination) {
        [IO.File]::Replace($temporary, $Destination, ($Destination + '.previous'))
    } else {
        [IO.File]::Move($temporary, $Destination)
    }
}

function Save-State {
    $state.updated_at_utc = [DateTime]::UtcNow.ToString('o')
    foreach ($destination in @($statusPath, (Join-Path $runDir 'status.json'))) {
        Write-JsonAtomically $state $destination
    }
}

function Set-StateProperty([object]$Target, [string]$Name, [object]$Value) {
    $property = $Target.PSObject.Properties[$Name]
    if ($null -eq $property) {
        $Target | Add-Member -NotePropertyName $Name -NotePropertyValue $Value
    } else {
        $property.Value = $Value
    }
}

function Get-StateProperty([object]$Target, [string]$Name) {
    $property = $Target.PSObject.Properties[$Name]
    if ($null -eq $property) { return $null }
    return $property.Value
}

function Reconcile-PreviousSupervisorState([object]$Previous, [string]$PreviousStatusPath) {
    $priorRunId = [string](Get-StateProperty $Previous 'run_id')
    if ([string]::IsNullOrWhiteSpace($priorRunId)) {
        throw 'Prior supervisor status has no run_id; refusing to replace an unverifiable record.'
    }
    $previousState = [string](Get-StateProperty $Previous 'state')
    $childPid = Get-StateProperty $Previous 'child_pid'
    $childStartedAt = Get-StateProperty $Previous 'child_started_at_utc'
    $reason = $null
    if ($childPid) {
        $expectedStart = $null
        try {
            $expectedStart = ([DateTimeOffset]$childStartedAt).UtcDateTime
        } catch {
            $reason = 'RECOVERY_SUPERVISOR_CHILD_IDENTITY_INCOMPLETE'
        }
        if ($null -eq $reason) {
            $orphan = Get-Process -Id $childPid -ErrorAction SilentlyContinue
            if ($orphan) {
                try {
                    $actualStart = $orphan.StartTime.ToUniversalTime()
                } catch {
                    throw 'Unable to inspect the prior child identity; reconcile it manually before another supervisor.'
                }
                if ($actualStart -eq $expectedStart) {
                    throw 'The prior child is still running. Reconcile it before starting another supervisor.'
                }
                $reason = 'RECOVERY_SUPERVISOR_CHILD_PID_IDENTITY_MISMATCH'
            } else {
                $reason = 'RECOVERY_SUPERVISOR_CHILD_PID_NOT_FOUND'
            }
        }
    } elseif ($previousState -in @('STARTING', 'RUNNING')) {
        $reason = 'RECOVERY_SUPERVISOR_RUNNING_WITHOUT_CHILD_IDENTITY'
    }
    if ($null -eq $reason) { return $null }

    $reconciledAt = [DateTime]::UtcNow.ToString('o')
    $reconciliation = [ordered]@{
        classification = 'ORPHANED_CHILD'
        parent_run_id = $priorRunId
        prior_child_pid = $childPid
        prior_child_started_at_utc = $childStartedAt
        reason = $reason
        reconciled_at_utc = $reconciledAt
        reconciled_by_run_id = $runId
    }
    Set-StateProperty $Previous 'state' 'INTERRUPTED'
    Set-StateProperty $Previous 'child_status' 'ORPHANED'
    Set-StateProperty $Previous 'child_pid' $null
    Set-StateProperty $Previous 'child_started_at_utc' $null
    Set-StateProperty $Previous 'error' $reason
    Set-StateProperty $Previous 'interrupted_at_utc' $reconciledAt
    Set-StateProperty $Previous 'orphaned_child_pid' $childPid
    Set-StateProperty $Previous 'orphaned_child_started_at_utc' $childStartedAt
    Set-StateProperty $Previous 'orphan_reconciliation' $reconciliation

    $priorRunStatusPath = Join-Path (Join-Path $controlRoot $priorRunId) 'status.json'
    $reconciliationRoot = Join-Path $controlRoot 'reconciliations'
    $receiptPath = Join-Path $reconciliationRoot ($priorRunId + '.orphaned-by-' + $runId + '.json')
    Write-JsonAtomically $Previous $PreviousStatusPath
    if ((Resolve-Path -LiteralPath $priorRunStatusPath -ErrorAction SilentlyContinue) -ne (Resolve-Path -LiteralPath $PreviousStatusPath -ErrorAction SilentlyContinue)) {
        Write-JsonAtomically $Previous $priorRunStatusPath
    }
    Write-JsonAtomically $reconciliation $receiptPath
    return $receiptPath
}

function Get-Sha256Hex([string]$Path) {
    $stream = [IO.File]::OpenRead($Path)
    try {
        $algorithm = [Security.Cryptography.SHA256]::Create()
        try {
            $bytes = $algorithm.ComputeHash($stream)
        } finally {
            $algorithm.Dispose()
        }
    } finally {
        $stream.Dispose()
    }
    return ([BitConverter]::ToString($bytes).Replace('-', '').ToLowerInvariant())
}

function Invoke-Phase([string]$Name, [string[]]$CommandArguments) {
    $state.phase = $Name
    $state.state = 'RUNNING'
    $state.last_output_at_utc = $null
    $stdout = Join-Path $runDir ($Name + '.out.log')
    $stderr = Join-Path $runDir ($Name + '.err.log')
    $state.artifacts[$Name] = @{ stdout = $stdout; stderr = $stderr }
    $quoted = foreach ($argument in $CommandArguments) {
        if ($argument.Contains('"') -or $argument.EndsWith('\')) {
            throw 'Unsupported argument quoting; refusing to launch.'
        }
        '"' + $argument + '"'
    }
    $script:child = Start-Process -FilePath $python -ArgumentList ($quoted -join ' ') `
        -WorkingDirectory $projectRoot -WindowStyle Hidden -PassThru `
        -RedirectStandardOutput $stdout -RedirectStandardError $stderr
    # Keep the Windows process handle so PowerShell 5.1 retains ExitCode.
    $null = $child.Handle
    $state.child_pid = $child.Id
    $state.child_started_at_utc = $child.StartTime.ToUniversalTime().ToString('o')
    Save-State
    while (-not $child.WaitForExit(5000)) {
        $latest = Get-Item -LiteralPath $stdout, $stderr | Sort-Object LastWriteTimeUtc -Descending | Select-Object -First 1
        $state.last_output_at_utc = $latest.LastWriteTimeUtc.ToString('o')
        Save-State
    }
    $child.WaitForExit()
    $state.exit_code = $child.ExitCode
    $state.child_pid = $null
    $state.child_started_at_utc = $null
    if ($child.ExitCode -ne 0) { throw "Phase $Name failed with exit code $($child.ExitCode); inspect its preserved logs." }
    $state.completed_phases += $Name
    Save-State
}

try {
    if ($Resume -and ($Track -ne 'QuarterHour' -or $QuarterHourLineage -ne 'v5')) {
        throw '-Resume is reserved for an explicit V5 quarter-hour collector continuation.'
    }
    New-Item -ItemType Directory -Path $runDir | Out-Null
    if (Test-Path -LiteralPath $statusPath) {
        $previous = Get-Content -LiteralPath $statusPath -Raw | ConvertFrom-Json
        $priorReconciliation = Reconcile-PreviousSupervisorState $previous $statusPath
        if ($priorReconciliation) {
            $state.artifacts['prior_orphan_reconciliation'] = $priorReconciliation
        }
    }
    $modulePattern = if ($Track -eq 'Forward') {
        '-m\s+kairos_backtest\.forward_(collection|observation|evaluation)\b'
    } else {
        '-m\s+(kairos_backtest\.quarter_hour_(features|lag_replication)|scripts\.recover_quarter_hour)\b'
    }
    $existingWorkers = Get-CimInstance Win32_Process | Where-Object {
        $_.Name -match '^python(w)?\.exe$' -and $_.CommandLine -match $modulePattern
    }
    if ($existingWorkers) { throw 'An unsupervised research command is already running; reconcile it first.' }
    Save-State
    $dirty = & git -C $projectRoot status --porcelain=v1 --untracked-files=all
    if ($LASTEXITCODE -ne 0 -or $dirty) { throw 'Research recovery requires a clean Git worktree.' }

    if ($Track -eq 'Forward') {
        $ledger = Join-Path $RuntimeRoot 'regime-aligned-forward.sqlite3'
        $preBackup = Join-Path $RuntimeRoot ('backups\forward-before-' + $runId + '.sqlite3')
        $postBackup = Join-Path $RuntimeRoot ('backups\forward-after-' + $runId + '.sqlite3')
        $recovered = Join-Path $RuntimeRoot ('recovery\forward-' + $runId + '.sqlite3')
        $state.artifacts['pre_backup'] = $preBackup
        $state.artifacts['post_backup'] = $postBackup
        $state.artifacts['recovered'] = $recovered
        Invoke-Phase 'backup-before' @('-u', '-m', 'kairos_backtest.forward_observation', 'backup', '--ledger', $ledger, '--output', $preBackup)
        Invoke-Phase 'sync-latest' @('-u', '-m', 'kairos_backtest.forward_collection', '--plan', 'reports/regime-aligned-forward/plan.json', '--ledger', $ledger, '--cache-dir', (Join-Path $RuntimeRoot 'forward-daily'), '--sync-latest')
        Invoke-Phase 'verify' @('-u', '-m', 'kairos_backtest.forward_observation', 'verify', '--ledger', $ledger)
        Invoke-Phase 'eligibility' @('-u', '-m', 'kairos_backtest.forward_evaluation', 'eligibility', '--ledger', $ledger)
        Invoke-Phase 'backup-after' @('-u', '-m', 'kairos_backtest.forward_observation', 'backup', '--ledger', $ledger, '--output', $postBackup)
        Invoke-Phase 'recovery-drill' @('-u', '-m', 'kairos_backtest.forward_observation', 'recovery-drill', '--ledger', $ledger, '--backup', $postBackup, '--recovered', $recovered)
    } else {
        $ledger = Join-Path $RuntimeRoot ('quarter-hour-lag-features-' + $QuarterHourLineage + '.sqlite3')
        $cache = Join-Path $RuntimeRoot 'quarter-hour-lag-archives'
        $result = Join-Path $projectRoot 'reports/quarter-hour-lag-replication-v2/result.json'
        $immutableV4 = Join-Path $RuntimeRoot 'quarter-hour-lag-features-v4.sqlite3'
        if (Test-Path -LiteralPath $result) { throw 'V2 result already exists; review it instead of rerunning.' }
        if ($QuarterHourLineage -eq 'v5') {
            if (-not (Test-Path -LiteralPath $immutableV4)) {
                throw 'V5 lineage requires the preserved V4 ledger for provenance.'
            }
            $v4Hash = Get-Sha256Hex $immutableV4
            $state.artifacts['immutable_v4_ledger'] = $immutableV4
            $state.artifacts['immutable_v4_sha256_before'] = $v4Hash
            $state.artifacts['v5_ledger'] = $ledger
            $preflightClone = Join-Path $RuntimeRoot ('backups\quarterhour-v5-before-' + $runId + '.sqlite3')
            $preflightReceipt = $preflightClone + '.receipt.json'
            $state.artifacts['v5_recovery_clone'] = $preflightClone
            $state.artifacts['v5_recovery_receipt'] = $preflightReceipt
            Save-State
            Invoke-Phase 'v5-recovery-preflight' @(
                '-u', '-m', 'scripts.quarter_hour_recovery_preflight',
                '--ledger', $ledger,
                '--clone', $preflightClone,
                '--receipt', $preflightReceipt,
                '--plan', 'reports/quarter-hour-lag-replication-v2/plan.json'
            )
            if (-not $Resume) {
                $state.state = 'COMPLETED'
                $state.phase = 'v5-recovery-preflight'
                Save-State
                return
            }
        }
        $collectorModule = if ($PrepareArchives) { 'scripts.recover_quarter_hour' } else { 'kairos_backtest.quarter_hour_features' }
        Invoke-Phase 'collect' @('-u', '-m', $collectorModule, '--ledger', $ledger, '--cache-dir', $cache, '--workers', [string]$Workers)
        Invoke-Phase 'deep-verify' @('-u', '-m', 'kairos_backtest.quarter_hour_features', '--ledger', $ledger, '--cache-dir', $cache, '--verify', '--deep')
        if (Test-Path -LiteralPath $result) { throw 'V2 result appeared during collection; refusing another evaluation.' }
        Invoke-Phase 'replication' @('-u', '-m', 'kairos_backtest.quarter_hour_lag_replication', '--plan', 'reports/quarter-hour-lag-replication-v2/plan.json', '--ledger', $ledger, '--result', $result)
        if ($QuarterHourLineage -eq 'v5') {
            $v4HashAfter = Get-Sha256Hex $immutableV4
            if ($v4HashAfter -ne $state.artifacts['immutable_v4_sha256_before']) {
                throw 'The immutable V4 ledger changed while V5 was running; preserve both ledgers and investigate.'
            }
            $state.artifacts['immutable_v4_sha256_after'] = $v4HashAfter
        }
        $state.artifacts['result'] = $result
    }
    $state.state = 'COMPLETED'
    Save-State
} catch {
    if (Test-Path -LiteralPath $runDir) {
        $state.state = 'FAILED'
        $state.error = $_.Exception.Message
        Save-State
    }
    throw
} finally {
    $lock.Dispose()
}
