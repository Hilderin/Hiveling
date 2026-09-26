<#
.SYNOPSIS
    Start (or stop/status) several Hiveling workers, each in its own folder.

.DESCRIPTION
    Every worker gets its own folder. The worker resolves --workspace and
    --log-dir relative to its current directory, so running it from that folder
    keeps job data (<folder>\.data\worker) and logs (<folder>\.data\logs)
    isolated. Ports are assigned sequentially.

    Workers are started detached with Start-Process, so they keep running once
    this script (and the shell that launched it) has exited.

.PARAMETER Action
    Start (default), Stop, Restart or Status.

.PARAMETER Count
    Number of workers (default 3). Ignored when -Names is supplied.

.PARAMETER StartPort
    Port of the first worker; following ones use StartPort+1, +2, ... (default 8787).

.PARAMETER BaseDir
    Base folder holding one subfolder per worker. Relative paths are resolved
    against the repository root (default: .data\workers).

.PARAMETER Names
    Explicit worker names (default: w1, w2, ...).

.PARAMETER BindHost
    Listen interface passed to the worker (default 0.0.0.0).

.PARAMETER Python
    Python executable. Default: <repo>\.venv\Scripts\python.exe if it exists,
    otherwise 'python' from PATH.

.PARAMETER OpencodeBin
    Optional explicit path to the opencode binary (passed as --opencode-bin).

.PARAMETER WorkersFile
    When set, writes the matching workers.yaml to this path (an existing file is
    backed up once as <path>.bak).

.PARAMETER HealthTimeoutSec
    Seconds to wait for each worker to answer /health after starting (default 20).

.EXAMPLE
    .\worker\start-multi.ps1

.EXAMPLE
    .\worker\start-multi.ps1 -Count 3 -StartPort 8790 -WorkersFile .data\workers.yaml

.EXAMPLE
    .\worker\start-multi.ps1 -Action Status
    .\worker\start-multi.ps1 -Action Stop
#>
[CmdletBinding()]
param(
    [ValidateSet('Start', 'Stop', 'Restart', 'Status')]
    [string]$Action = 'Start',

    [int]$Count = 3,
    [int]$StartPort = 8787,
    [string]$BaseDir = '.data\workers',
    [string[]]$Names,
    [string]$BindHost = '0.0.0.0',
    [string]$Python,
    [string]$OpencodeBin,
    [string]$WorkersFile,
    [int]$HealthTimeoutSec = 20
)

$ErrorActionPreference = 'Stop'

function Find-RepoRoot([string]$start) {
    $dir = (Resolve-Path -LiteralPath $start).Path
    while ($dir) {
        if (Test-Path -LiteralPath (Join-Path $dir 'worker\run.py')) { return $dir }
        $parent = Split-Path -Parent $dir
        if (-not $parent -or $parent -eq $dir) { break }
        $dir = $parent
    }
    throw "Hiveling repository not found (no worker\run.py above '$start')."
}

function ConvertTo-Arg([string]$value) {
    if ($value -match '\s') { return '"' + $value + '"' }
    return $value
}

function Write-Utf8NoBom([string]$path, [string]$text) {
    $utf8 = New-Object System.Text.UTF8Encoding($false)
    [System.IO.File]::WriteAllText($path, $text, $utf8)
}

function Get-Health([int]$port) {
    try { return Invoke-RestMethod -Uri "http://127.0.0.1:$port/health" -TimeoutSec 3 }
    catch { return $null }
}

# ------------------------------------------------------------------ setup
$RepoRoot = Find-RepoRoot $PSScriptRoot
$WorkerScript = Join-Path $RepoRoot 'worker\run.py'

if (-not $Python) {
    $venvPython = Join-Path $RepoRoot '.venv\Scripts\python.exe'
    if (Test-Path -LiteralPath $venvPython) { $Python = $venvPython } else { $Python = 'python' }
}

if (-not [System.IO.Path]::IsPathRooted($BaseDir)) { $BaseDir = Join-Path $RepoRoot $BaseDir }
$BaseDir = [System.IO.Path]::GetFullPath($BaseDir)
$StatePath = Join-Path $BaseDir 'workers.json'

if ($Names -and $Names.Count -gt 0) { $Count = $Names.Count }
if (-not $Names -or $Names.Count -eq 0) { $Names = @(1..$Count | ForEach-Object { "w$_" }) }

$defs = for ($i = 0; $i -lt $Count; $i++) {
    [pscustomobject]@{
        Name = $Names[$i]
        Port = [int]($StartPort + $i)
        Dir  = Join-Path $BaseDir $Names[$i]
    }
}

function Read-StateDefs {
    if (-not (Test-Path -LiteralPath $StatePath)) { return $null }
    try {
        $saved = Get-Content -LiteralPath $StatePath -Raw | ConvertFrom-Json
        if ($saved -and $saved.workers) { return @($saved.workers) }
    } catch { }
    return $null
}

function Save-State($entries) {
    New-Item -ItemType Directory -Force -Path $BaseDir | Out-Null
    $byPort = @{}
    $existing = Read-StateDefs
    if ($existing) { foreach ($e in $existing) { $byPort[[int]$e.Port] = $e } }
    foreach ($e in $entries) { $byPort[[int]$e.Port] = $e }
    $payload = [pscustomobject]@{ workers = @($byPort.Values | Sort-Object { [int]$_.Port }) }
    Write-Utf8NoBom $StatePath ($payload | ConvertTo-Json -Depth 6)
}

function Show-Yaml($entries) {
    Write-Host ''
    Write-Host 'workers.yaml (server side):' -ForegroundColor Cyan
    Write-Host 'workers:'
    foreach ($w in $entries) {
        Write-Host ("  - name: {0}" -f $w.Name)
        Write-Host '    host: 127.0.0.1'
        Write-Host ("    port: {0}" -f $w.Port)
    }
}

function Write-WorkersFile($entries) {
    $path = $WorkersFile
    if (-not [System.IO.Path]::IsPathRooted($path)) { $path = Join-Path $RepoRoot $path }
    $path = [System.IO.Path]::GetFullPath($path)
    $parent = Split-Path -Parent $path
    if ($parent) { New-Item -ItemType Directory -Force -Path $parent | Out-Null }
    if (Test-Path -LiteralPath $path) { Copy-Item -LiteralPath $path -Destination "$path.bak" -Force }

    $sb = New-Object System.Text.StringBuilder
    [void]$sb.AppendLine('# Generated by worker/start-multi.ps1')
    [void]$sb.AppendLine('workers:')
    foreach ($w in $entries) {
        [void]$sb.AppendLine("  - name: $($w.Name)")
        [void]$sb.AppendLine('    host: 127.0.0.1')
        [void]$sb.AppendLine("    port: $($w.Port)")
    }
    Write-Utf8NoBom $path $sb.ToString()
    Write-Host ''
    Write-Host ("workers.yaml written to {0}" -f $path) -ForegroundColor Green
}

# ------------------------------------------------------------------ actions
function Start-Workers {
    New-Item -ItemType Directory -Force -Path $BaseDir | Out-Null
    Write-Host ("python: {0}" -f $Python) -ForegroundColor DarkGray
    $started = @()

    foreach ($w in $defs) {
        New-Item -ItemType Directory -Force -Path $w.Dir | Out-Null

        $listener = Get-NetTCPConnection -LocalPort $w.Port -State Listen -ErrorAction SilentlyContinue
        if ($listener) {
            Write-Warning ("port {0} already in use (pid {1}); skipping '{2}'" -f $w.Port, $listener.OwningProcess, $w.Name)
            continue
        }

        $argBits = @((ConvertTo-Arg $WorkerScript), '--host', (ConvertTo-Arg $BindHost), '--port', $w.Port)
        if ($OpencodeBin) { $argBits += @('--opencode-bin', (ConvertTo-Arg $OpencodeBin)) }

        $proc = Start-Process -FilePath $Python -ArgumentList ($argBits -join ' ') `
            -WorkingDirectory $w.Dir -WindowStyle Hidden -PassThru `
            -RedirectStandardOutput (Join-Path $w.Dir 'console.out.log') `
            -RedirectStandardError (Join-Path $w.Dir 'console.err.log')

        $started += [pscustomobject]@{
            Name      = $w.Name
            Port      = $w.Port
            Dir       = $w.Dir
            Pid       = $proc.Id
            StartedAt = (Get-Date).ToString('o')
        }
        Write-Host ("  started {0,-4} port {1,-5} pid {2,-6} {3}" -f $w.Name, $w.Port, $proc.Id, $w.Dir)
    }

    if ($started.Count -gt 0) { Save-State $started }

    # wait for /health so the summary reflects reality
    $deadline = (Get-Date).AddSeconds($HealthTimeoutSec)
    $up = @{}
    while ((Get-Date) -lt $deadline) {
        $allUp = $true
        foreach ($e in $started) {
            if (-not $up.ContainsKey($e.Port)) {
                if (Get-Health $e.Port) { $up[$e.Port] = $true } else { $allUp = $false }
            }
        }
        if ($allUp) { break }
        Start-Sleep -Milliseconds 500
    }

    Write-Host ''
    $rows = foreach ($e in $started) {
        $h = Get-Health $e.Port
        $status = if ($h) { if ($h.busy) { "up (busy: $($h.active_job))" } else { 'up' } } else { 'NO RESPONSE' }
        [pscustomobject]@{ Name = $e.Name; Port = $e.Port; Status = $status; Folder = $e.Dir }
    }
    $rows | Format-Table -AutoSize | Out-String -Width 220 | Write-Host

    Show-Yaml $defs
    if ($WorkersFile) { Write-WorkersFile $defs }
}

function Stop-Workers($entries) {
    foreach ($w in $entries) {
        $targets = @()
        $listener = Get-NetTCPConnection -LocalPort $w.Port -State Listen -ErrorAction SilentlyContinue
        if ($listener) { $targets += $listener.OwningProcess }
        if ($w.Pid) { $targets += [int]$w.Pid }
        $targets = @($targets | Sort-Object -Unique)

        if ($targets.Count -eq 0) {
            Write-Host ("  {0,-4} port {1,-5} not running" -f $w.Name, $w.Port)
            continue
        }
        foreach ($procId in $targets) {
            Stop-Process -Id $procId -Force -ErrorAction SilentlyContinue
        }
        Write-Host ("  stopped {0,-4} port {1,-5} (pids {2})" -f $w.Name, $w.Port, ($targets -join ', '))
    }
    if (Test-Path -LiteralPath $StatePath) { Remove-Item -LiteralPath $StatePath -Force -ErrorAction SilentlyContinue }
}

function Show-Status($entries) {
    $rows = foreach ($e in $entries) {
        $h = Get-Health $e.Port
        if ($h) {
            [pscustomobject]@{
                Name      = $e.Name
                Port      = $e.Port
                Reachable = 'yes'
                Busy      = [bool]$h.busy
                ActiveJob = $h.active_job
                Jobs      = $h.jobs
                Folder    = $e.Dir
            }
        } else {
            [pscustomobject]@{
                Name      = $e.Name
                Port      = $e.Port
                Reachable = 'no'
                Busy      = ''
                ActiveJob = ''
                Jobs      = ''
                Folder    = $e.Dir
            }
        }
    }
    $rows | Format-Table -AutoSize | Out-String -Width 220 | Write-Host
}

# ------------------------------------------------------------------ dispatch
$entries = Read-StateDefs
if (-not $entries) { $entries = $defs }

switch ($Action) {
    'Start' { Start-Workers }
    'Stop' { Stop-Workers $entries }
    'Status' { Show-Status $entries }
    'Restart' {
        Stop-Workers $entries
        Start-Sleep -Seconds 1
        Start-Workers
    }
}
