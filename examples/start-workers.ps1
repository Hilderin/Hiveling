# Starts 3 Hiveling workers, one per folder. Paths are relative to this script.
# Detached (Start-Process): they keep running after this script exits.
# Each worker logs to <folder>\logs\worker.log.

$repo = Split-Path -Parent $PSScriptRoot          # examples\ -> repository root
$python = Join-Path $repo ".venv\Scripts\python.exe"
$worker = Join-Path $repo "worker\run.py"

$dir1 = Join-Path $repo ".data\workers\w1"
$dir2 = Join-Path $repo ".data\workers\w2"
$dir3 = Join-Path $repo ".data\workers\w3"

New-Item -ItemType Directory -Force -Path $dir1, $dir2, $dir3 | Out-Null

# w1 - port 8787
Start-Process $python -WindowStyle Hidden -WorkingDirectory $dir1 `
    -RedirectStandardOutput (Join-Path $dir1 "console.out.log") `
    -RedirectStandardError (Join-Path $dir1 "console.err.log") `
    -ArgumentList $worker, "--host", "0.0.0.0", "--port", "8787", `
        "--workspace", (Join-Path $dir1 "work"), `
        "--log-dir", (Join-Path $dir1 "logs")

# w2 - port 8788
Start-Process $python -WindowStyle Hidden -WorkingDirectory $dir2 `
    -RedirectStandardOutput (Join-Path $dir2 "console.out.log") `
    -RedirectStandardError (Join-Path $dir2 "console.err.log") `
    -ArgumentList $worker, "--host", "0.0.0.0", "--port", "8788", `
        "--workspace", (Join-Path $dir2 "work"), `
        "--log-dir", (Join-Path $dir2 "logs")

# w3 - port 8789
Start-Process $python -WindowStyle Hidden -WorkingDirectory $dir3 `
    -RedirectStandardOutput (Join-Path $dir3 "console.out.log") `
    -RedirectStandardError (Join-Path $dir3 "console.err.log") `
    -ArgumentList $worker, "--host", "0.0.0.0", "--port", "8789", `
        "--workspace", (Join-Path $dir3 "work"), `
        "--log-dir", (Join-Path $dir3 "logs")

Start-Sleep -Seconds 3

foreach ($port in 8787, 8788, 8789) {
    try {
        Invoke-RestMethod "http://127.0.0.1:$port/health" -TimeoutSec 2 | Out-Null
        Write-Host "port $port -> up"
    } catch {
        Write-Host "port $port -> DOWN"
    }
}
