$ErrorActionPreference = "Stop"

$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$pythonPath = Join-Path $projectRoot "collector\.venv\Scripts\python.exe"
$frontendPath = Join-Path $projectRoot "frontend"
$logDirectory = Join-Path $projectRoot "frontend\logs"
$outputLogPath = Join-Path $logDirectory "dashboard.log"
$errorLogPath = Join-Path $logDirectory "dashboard-errors.log"

if (-not (Test-Path -LiteralPath $pythonPath -PathType Leaf)) {
    throw "Project Python environment was not found: $pythonPath"
}

New-Item -ItemType Directory -Path $logDirectory -Force | Out-Null
foreach ($path in @($outputLogPath, $errorLogPath)) {
    if ((Get-Item -LiteralPath $path -ErrorAction SilentlyContinue).Length -gt 20MB) {
        Remove-Item -LiteralPath ($path + ".1") -Force -ErrorAction SilentlyContinue
        Move-Item -LiteralPath $path -Destination ($path + ".1") -Force
    }
}
Set-Location $projectRoot
$arguments = '-m http.server 8001 --bind 127.0.0.1 --directory "{0}"' -f $frontendPath
$process = Start-Process `
    -FilePath $pythonPath `
    -ArgumentList $arguments `
    -WorkingDirectory $projectRoot `
    -NoNewWindow `
    -PassThru `
    -Wait `
    -RedirectStandardOutput $outputLogPath `
    -RedirectStandardError $errorLogPath
exit $process.ExitCode