<#
  Hiveling server launcher (Windows PowerShell).

  Starts server\dashboard.py: browser dashboard + JSON API + MCP endpoint.

  Usage:
    .\server\start.ps1                    # foreground on 127.0.0.1:8080
    .\server\start.ps1 -Background        # background (separate hidden process)
    .\server\start.ps1 -Port 9000 -ListenHost 0.0.0.0
    $env:HIVELING_PORT=9000; .\server\start.ps1

  Env vars (used when the matching parameter is absent):
    HIVELING_HOST HIVELING_PORT HIVELING_DATA_DIR HIVELING_LOG_DIR
    HIVELING_LOG_LEVEL HIVELING_HEARTBEAT HIVELING_KEEP_ALIVE HIVELING_TOKEN
#>
[CmdletBinding()]
param(
  [string]$ListenHost,
  [int]$Port = 0,
  [string]$DataDir = '',
  [string]$LogDir = '',
  [string]$LogLevel,
  [double]$Heartbeat = 0,
  [int]$KeepAlive = 0,
  [switch]$Background
)

$ErrorActionPreference = 'Stop'

$scriptDir   = $PSScriptRoot
$projectRoot = (Resolve-Path (Join-Path $scriptDir '..')).Path

# ---- defaults -------------------------------------------------------------
if (-not $ListenHost) { $ListenHost = $env:HIVELING_HOST; if (-not $ListenHost) { $ListenHost = '127.0.0.1' } }
if ($Port -le 0)      { $Port = $env:HIVELING_PORT;      if ($Port -le 0)      { $Port = 8080 } }
if (-not $DataDir)    { $DataDir = $env:HIVELING_DATA_DIR; if (-not $DataDir)   { $DataDir = Join-Path $projectRoot '.data' } }
if (-not $LogDir)     { $LogDir  = $env:HIVELING_LOG_DIR
                        if (-not $LogDir) { $LogDir = Join-Path $DataDir 'logs' } }
if ($Heartbeat -le 0) { $Heartbeat = $env:HIVELING_HEARTBEAT; if ($Heartbeat -le 0) { $Heartbeat = 60 } }
if ($KeepAlive -le 0) { $KeepAlive = $env:HIVELING_KEEP_ALIVE; if ($KeepAlive -le 0) { $KeepAlive = 300 } }
if (-not $LogLevel)   { $LogLevel = 'info' }

# ---- python ---------------------------------------------------------------
$py = Join-Path $projectRoot '.venv\Scripts\python.exe'
if (-not (Test-Path $py)) { $py = 'python' }

$script = Join-Path $scriptDir 'dashboard.py'
$serverArgs = @(
  '--host', $ListenHost,
  '--port', "$Port",
  '--data-dir', $DataDir,
  '--log-dir', $LogDir,
  '--log-level', $LogLevel,
  '--heartbeat', "$Heartbeat",
  '--keep-alive', "$KeepAlive"
)

New-Item -ItemType Directory -Force -Path $DataDir | Out-Null

if ($Background) {
  New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
  $outLog = Join-Path $LogDir 'server.out.log'
  $errLog = Join-Path $LogDir 'server.err.log'

  # Quote every argument so paths containing spaces survive Start-Process.
  $quoted = $serverArgs | ForEach-Object {
    if ($_ -match '\s') { '"' + $_ + '"' } else { $_ }
  }
  $argumentList = ('"' + $script + '"') + ' ' + ($quoted -join ' ')

  $proc = Start-Process -FilePath $py -ArgumentList $argumentList `
    -WorkingDirectory $projectRoot `
    -RedirectStandardOutput $outLog -RedirectStandardError $errLog `
    -WindowStyle Hidden -PassThru

  Set-Content -Path (Join-Path $DataDir 'server.pid') -Value $proc.Id
  Write-Host "Hiveling started in background (pid $($proc.Id)) on ${ListenHost}:${Port}"
  Write-Host "  stdout log: $outLog"
  Write-Host "  stderr log: $errLog"
  Write-Host "  pid file:   $DataDir\server.pid"
} else {
  Write-Host "Starting Hiveling on ${ListenHost}:${Port} (data: $DataDir)"
  & $py $script @serverArgs
}