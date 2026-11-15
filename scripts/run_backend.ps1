$ErrorActionPreference = "Stop"

$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$backendPath = Join-Path $projectRoot "backend"
$pythonPath = Join-Path $projectRoot "collector\.venv\Scripts\python.exe"
$logDirectory = Join-Path $backendPath "logs"
$outputLogPath = Join-Path $logDirectory "backend.log"
$errorLogPath = Join-Path $logDirectory "backend-errors.log"

if (-not (Test-Path -LiteralPath $pythonPath -PathType Leaf)) {
    throw "Project Python environment was not found: $pythonPath"
}

New-Item -ItemType Directory -Path $logDirectory -Force | Out-Null
# Keep one rotated copy so a long-running API cannot fill the disk.
foreach ($path in @($outputLogPath, $errorLogPath)) {
    if ((Get-Item -LiteralPath $path -ErrorAction SilentlyContinue).Length -gt 20MB) {
        Remove-Item -LiteralPath ($path + ".1") -Force -ErrorAction SilentlyContinue
        Move-Item -LiteralPath $path -Destination ($path + ".1") -Force
    }
}
Set-Location $backendPath
$process = Start-Process `
    -FilePath $pythonPath `
    -ArgumentList "-m uvicorn app.main:app --host 127.0.0.1 --port 8000" `
    -WorkingDirectory $backendPath `
    -NoNewWindow `
    -PassThru `
    -Wait `
    -RedirectStandardOutput $outputLogPath `
    -RedirectStandardError $errorLogPath
exit $process.ExitCode