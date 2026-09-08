$ErrorActionPreference = 'Stop'
$scriptPath = Join-Path $PSScriptRoot 'Invoke-ResearchRecovery.ps1'
$tokens = $null
$errors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile($scriptPath, [ref]$tokens, [ref]$errors)
if ($errors) { throw ($errors | Out-String) }
# Validate the operational throttle through PowerShell's actual parameter binder,
# without executing the supervisor body or touching a research ledger.
$parameterProbe = [scriptblock]::Create($ast.ParamBlock.Extent.Text + "`nreturn `$Workers")
if ((& $parameterProbe -Track QuarterHour) -ne 4) { throw 'Default worker count changed.' }
if ((& $parameterProbe -Track QuarterHour -Workers 1) -ne 1) { throw 'Serial recovery unavailable.' }
foreach ($invalid in @(0, 5)) {
    $rejected = $false
    try { & $parameterProbe -Track QuarterHour -Workers $invalid | Out-Null } catch { $rejected = $true }
    if (-not $rejected) { throw 'Invalid worker count accepted.' }
}
# Exercise the real phase/status functions against a harmless child process.
# Do not run the collector or open a market database.
foreach ($name in @('Save-State', 'Invoke-Phase')) {
    $functionAst = $ast.Find({ param($node) $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -eq $name }, $true)
    . ([scriptblock]::Create($functionAst.Extent.Text))
}
$projectRoot = Split-Path $PSScriptRoot -Parent
$python = Join-Path $projectRoot '.venv\Scripts\python.exe'
$runDir = Join-Path ([IO.Path]::GetTempPath()) ('kairos-supervisor-test-' + [Guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $runDir | Out-Null
$statusPath = Join-Path $runDir 'latest.json'
$state = [ordered]@{ updated_at_utc = $null; phase = ''; state = ''; artifacts = @{}; child_pid = $null; child_started_at_utc = $null; last_output_at_utc = $null; exit_code = $null; completed_phases = @() }
Invoke-Phase 'success' @('-c', 'print(123)')
if ($state.exit_code -ne 0 -or $state.completed_phases -notcontains 'success') { throw 'Success was not recorded.' }
$stored = Get-Content -LiteralPath $statusPath -Raw | ConvertFrom-Json
if ($stored.child_pid -or $stored.completed_phases -notcontains 'success') { throw 'Persisted success state is invalid.' }
$failed = $false
try { Invoke-Phase 'failure' @('-c', 'raise SystemExit(7)') } catch { $failed = $true }
if (-not $failed -or $state.exit_code -ne 7 -or $state.completed_phases -contains 'failure') { throw 'A failed child was accepted.' }
$lockPath = Join-Path $runDir 'exclusive.lock'
$first = [IO.File]::Open($lockPath, [IO.FileMode]::OpenOrCreate, [IO.FileAccess]::ReadWrite, [IO.FileShare]::None)
try {
    $rejected = $false
    try { $second = [IO.File]::Open($lockPath, [IO.FileMode]::OpenOrCreate, [IO.FileAccess]::ReadWrite, [IO.FileShare]::None); $second.Dispose() } catch { $rejected = $true }
    if (-not $rejected) { throw 'Concurrent lock acquisition was accepted.' }
} finally { $first.Dispose() }
Write-Output 'PASS: phase success/failure, atomic status publication and exclusive locking.'
