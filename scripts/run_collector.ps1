$ErrorActionPreference = "Stop"

$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$backendEnvPath = Join-Path $projectRoot "backend\.env"
$collectorPath = Join-Path $projectRoot "collector"
$pythonPath = Join-Path $collectorPath ".venv\Scripts\python.exe"
$statePath = Join-Path $collectorPath "state"
$logPath = Join-Path $statePath "collector.log"

if (-not (Test-Path -LiteralPath $backendEnvPath -PathType Leaf)) {
    throw "Backend environment file was not found: $backendEnvPath"
}
if (-not (Test-Path -LiteralPath $pythonPath -PathType Leaf)) {
    throw "Project Python environment was not found: $pythonPath"
}

$keyLine = Get-Content -LiteralPath $backendEnvPath | Where-Object { $_ -match '^\s*COLLECTOR_API_KEY\s*=' } | Select-Object -First 1
if (-not $keyLine) {
    throw "COLLECTOR_API_KEY is missing from backend/.env"
}
$collectorKey = ($keyLine -split "=", 2)[1].Trim()
if ($collectorKey.Length -lt 32) {
    throw "COLLECTOR_API_KEY must contain at least 32 characters"
}

$env:COLLECTOR_API_KEY = $collectorKey
$env:MONITOR_API_URL = "http://127.0.0.1:8000"
# Keep the log readable: Python writes UTF-8 and must not buffer into a pipe.
$env:PYTHONIOENCODING = "utf-8"
$env:PYTHONUNBUFFERED = "1"
New-Item -ItemType Directory -Path $statePath -Force | Out-Null

# PowerShell's `*>>` writes UTF-16LE, which garbles the log for editors and grep.
# Convert an existing UTF-16 log once, then append UTF-8 from here on.
if (Test-Path -LiteralPath $logPath -PathType Leaf) {
    $bytes = [System.IO.File]::ReadAllBytes($logPath)
    if ($bytes.Length -ge 2 -and $bytes[0] -eq 0xFF -and $bytes[1] -eq 0xFE) {
        $existing = [System.IO.File]::ReadAllText($logPath, [System.Text.Encoding]::Unicode)
        [System.IO.File]::WriteAllText($logPath, $existing, (New-Object System.Text.UTF8Encoding($false)))
    }
}
# The collector appends to its log for as long as it runs, so keep one rotated copy.
if ((Get-Item -LiteralPath $logPath -ErrorAction SilentlyContinue).Length -gt 20MB) {
    Remove-Item -LiteralPath ($logPath + ".1") -Force -ErrorAction SilentlyContinue
    Move-Item -LiteralPath $logPath -Destination ($logPath + ".1") -Force
}
Set-Location $collectorPath
& $pythonPath .\collector.py 2>&1 | Out-File -LiteralPath $logPath -Append -Encoding utf8
exit $LASTEXITCODE