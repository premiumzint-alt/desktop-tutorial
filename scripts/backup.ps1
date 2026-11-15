$ErrorActionPreference = "Stop"
$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$backupRoot = Join-Path $projectRoot "backups"
$stamp = Get-Date -Format "yyyyMMdd-HHmmss"
$target = Join-Path $backupRoot $stamp
$pythonPath = Join-Path $projectRoot "collector\.venv\Scripts\python.exe"
$keepDays = 14

New-Item -ItemType Directory -Path $target -Force | Out-Null

# The SQLite database holds the accounts, thresholds, rules, channels and alert history.
$databasePath = Join-Path $projectRoot "backend\monitor.db"
if (Test-Path -LiteralPath $databasePath -PathType Leaf) {
    & $pythonPath -c "import sqlite3; src=sqlite3.connect(r'$databasePath'); dst=sqlite3.connect(r'$target\monitor.db'); src.backup(dst); dst.close(); src.close()"
    if ($LASTEXITCODE -ne 0) { throw "SQLite backup failed." }
    Write-Output "Saved backend\monitor.db"
}

foreach ($item in @("backend\.env", "collector\state\file_hashes.json", "collector\state\log_offsets.json")) {
    $source = Join-Path $projectRoot $item
    if (Test-Path -LiteralPath $source -PathType Leaf) {
        $destination = Join-Path $target ($item -replace '[\\/:*?"<>|]', '_')
        Copy-Item -LiteralPath $source -Destination $destination -Force
        Write-Output ("Saved " + $item)
    }
}

Get-ChildItem -LiteralPath $backupRoot -Directory -ErrorAction SilentlyContinue |
    Where-Object { $_.LastWriteTime -lt (Get-Date).AddDays(-$keepDays) } |
    ForEach-Object {
        Remove-Item -LiteralPath $_.FullName -Recurse -Force -ErrorAction SilentlyContinue
        Write-Output ("Removed old backup " + $_.Name)
    }

Write-Output ("Backup complete: " + $target)
