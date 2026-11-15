$ErrorActionPreference = "Stop"

$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$backendPath = Join-Path $projectRoot "backend"
$collectorPath = Join-Path $projectRoot "collector"
$venvPython = Join-Path $collectorPath ".venv\Scripts\python.exe"

if (Test-Path -LiteralPath $venvPython -PathType Leaf) {
    $pythonPath = $venvPython
} else {
    $pythonPath = "python"
}

Write-Output "Python: $pythonPath"
& $pythonPath --version
if ($LASTEXITCODE -ne 0) {
    throw "No usable Python interpreter found. Create collector/.venv with: python -m venv collector/.venv"
}

$exitCode = 0

Write-Output ""
Write-Output "== Compile check =="
$compileTargets = @(
    (Join-Path $backendPath "app"),
    (Join-Path $backendPath "tests"),
    (Join-Path $collectorPath "collector.py"),
    (Join-Path $collectorPath "security_checker.py"),
    (Join-Path $collectorPath "tests"),
    (Join-Path $projectRoot "scripts\watchdog_notify.py")
)
& $pythonPath -m compileall -q $compileTargets
if ($LASTEXITCODE -ne 0) {
    Write-Output "Compile check failed."
    $exitCode = 1
} else {
    Write-Output "Compile check passed."
}

Write-Output ""
Write-Output "== Unit tests =="
Push-Location $backendPath
try {
    & $pythonPath -m unittest discover -s tests -t . -v
    if ($LASTEXITCODE -ne 0) {
        Write-Output "Unit tests failed."
        $exitCode = 1
    }
} finally {
    Pop-Location
}

Push-Location $collectorPath
try {
    & $pythonPath -m unittest discover -s tests -t . -v
    if ($LASTEXITCODE -ne 0) {
        Write-Output "Collector tests failed."
        $exitCode = 1
    }
} finally {
    Pop-Location
}

Write-Output ""
Write-Output "== Lint =="
& $pythonPath -c "import importlib.util, sys; sys.exit(0 if importlib.util.find_spec('ruff') else 1)" | Out-Null
if ($LASTEXITCODE -eq 0) {
    & $pythonPath -m ruff check $backendPath $collectorPath (Join-Path $projectRoot "scripts")
    if ($LASTEXITCODE -ne 0) {
        Write-Output "Lint reported problems."
        $exitCode = 1
    }
} else {
    Write-Output "ruff is not installed; lint was skipped."
    Write-Output "Install it with: `"$pythonPath`" -m pip install -r backend/requirements-dev.txt"
}

Write-Output ""
if ($exitCode -eq 0) {
    Write-Output "All checks passed."
} else {
    Write-Output "Checks failed."
}
exit $exitCode
