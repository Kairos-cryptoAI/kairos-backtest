param(
    [Parameter(Mandatory)][ValidateSet('Forward', 'QuarterHour')][string]$Track,
    [string]$RuntimeRoot = 'D:\Kairos\runtime',
    [ValidateRange(1, 4)][int]$Workers = 4
)

$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path $PSScriptRoot -Parent
$python = Join-Path $projectRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $python)) { throw 'Restore the locked Python environment first.' }
$runId = (Get-Date).ToUniversalTime().ToString('yyyyMMddTHHmmssZ') + '-' + $PID
$controlRoot = Join-Path $RuntimeRoot 'research-recovery'
New-Item -ItemType Directory -Path $controlRoot -Force | Out-Null
$statusPath = Join-Path $controlRoot ($Track.ToLowerInvariant() + '.status.json')
$lockPath = Join-Path $controlRoot ($Track.ToLowerInvariant() + '.lock')
# FileShare.None is an OS lock, released even if the supervisor crashes.
$lock = [IO.File]::Open($lockPath, [IO.FileMode]::OpenOrCreate, [IO.FileAccess]::ReadWrite, [IO.FileShare]::None)
$state = [ordered]@{
    schema_version = 'kairos.research-recovery.v1'
    track = $Track
    workers = if ($Track -eq 'QuarterHour') { $Workers } else { $null }
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

function Save-State {
    $state.updated_at_utc = [DateTime]::UtcNow.ToString('o')
    $json = $state | ConvertTo-Json -Depth 8
    foreach ($destination in @($statusPath, (Join-Path $runDir 'status.json'))) {
        $temporary = $destination + '.tmp-' + $PID
        [IO.File]::WriteAllText($temporary, $json, [Text.UTF8Encoding]::new($false))
        if (Test-Path -LiteralPath $destination) {
            [IO.File]::Replace($temporary, $destination, ($destination + '.previous'))
        } else {
            [IO.File]::Move($temporary, $destination)
        }
    }
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
    if (Test-Path -LiteralPath $statusPath) {
        $previous = Get-Content -LiteralPath $statusPath -Raw | ConvertFrom-Json
        if ($previous.child_pid) {
            $orphan = Get-Process -Id $previous.child_pid -ErrorAction SilentlyContinue
            if ($orphan -and $orphan.StartTime.ToUniversalTime() -eq ([DateTimeOffset]$previous.child_started_at_utc).UtcDateTime) {
                throw 'The prior child is still running. Reconcile it before starting another supervisor.'
            }
        }
    }
    $modulePattern = if ($Track -eq 'Forward') {
        '-m\s+kairos_backtest\.forward_(collection|observation|evaluation)\b'
    } else {
        '-m\s+kairos_backtest\.quarter_hour_(features|lag_replication)\b'
    }
    $existingWorkers = Get-CimInstance Win32_Process | Where-Object {
        $_.Name -match '^python(w)?\.exe$' -and $_.CommandLine -match $modulePattern
    }
    if ($existingWorkers) { throw 'An unsupervised research command is already running; reconcile it first.' }
    New-Item -ItemType Directory -Path $runDir | Out-Null
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
        $ledger = Join-Path $RuntimeRoot 'quarter-hour-lag-features-v4.sqlite3'
        $cache = Join-Path $RuntimeRoot 'quarter-hour-lag-archives'
        $result = Join-Path $projectRoot 'reports/quarter-hour-lag-replication-v2/result.json'
        if (Test-Path -LiteralPath $result) { throw 'V2 result already exists; review it instead of rerunning.' }
        Invoke-Phase 'collect' @('-u', '-m', 'kairos_backtest.quarter_hour_features', '--ledger', $ledger, '--cache-dir', $cache, '--workers', [string]$Workers)
        Invoke-Phase 'deep-verify' @('-u', '-m', 'kairos_backtest.quarter_hour_features', '--ledger', $ledger, '--cache-dir', $cache, '--verify', '--deep')
        if (Test-Path -LiteralPath $result) { throw 'V2 result appeared during collection; refusing another evaluation.' }
        Invoke-Phase 'replication' @('-u', '-m', 'kairos_backtest.quarter_hour_lag_replication', '--plan', 'reports/quarter-hour-lag-replication-v2/plan.json', '--ledger', $ledger, '--result', $result)
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
