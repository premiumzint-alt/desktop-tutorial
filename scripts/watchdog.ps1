param(
    [int]$CollectorMaxAgeMinutes = 10,
    [string]$ApiBaseUrl = "http://127.0.0.1:8000",
    [string]$DashboardUrl = "http://127.0.0.1:8001/"
)

$ErrorActionPreference = "Continue"

$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$backendPath = Join-Path $projectRoot "backend"
$pythonPath = Join-Path $projectRoot "collector\.venv\Scripts\python.exe"
$logDirectory = Join-Path $projectRoot "logs"
$logPath = Join-Path $logDirectory "watchdog.log"

New-Item -ItemType Directory -Path $logDirectory -Force | Out-Null

function Write-Log {
    param([string]$Message)
    $line = "{0} {1}" -f (Get-Date).ToString("yyyy-MM-dd HH:mm:ss"), $Message
    if ((Get-Item -LiteralPath $logPath -ErrorAction SilentlyContinue).Length -gt 10MB) {
        Remove-Item -LiteralPath ($logPath + ".1") -Force -ErrorAction SilentlyContinue
        Move-Item -LiteralPath $logPath -Destination ($logPath + ".1") -Force
    }
    Add-Content -LiteralPath $logPath -Value $line
    Write-Output $line
}

function Repair-MongoDb {
    # The API reports mongodb=unavailable while the service is down; restarting the API
    # cannot fix that, so start the service instead.
    $service = Get-Service -Name "MongoDB" -ErrorAction SilentlyContinue
    if (-not $service) {
        return $false
    }
    if ($service.Status -ne "Running") {
        Write-Log "MongoDB service is " + $service.Status + "; starting it."
        try {
            Start-Service -Name "MongoDB" -ErrorAction Stop
        } catch {
            Write-Log ("MongoDB service could not be started: " + $_.Exception.Message)
            return $false
        }
        $deadline = (Get-Date).AddSeconds(30)
        while ((Get-Date) -lt $deadline) {
            if ((Get-Service -Name "MongoDB").Status -eq "Running") {
                Write-Log "MongoDB service is running again."
                return $true
            }
            Start-Sleep -Seconds 2
        }
        Write-Log "MongoDB service did not reach the running state in time."
        return $false
    }
    return $true
}

function Test-ApiReady {
    try {
        $health = Invoke-RestMethod -Uri ($ApiBaseUrl + "/health") -TimeoutSec 5
        return $health.status -eq "ok" -and $health.mongodb -eq "ok"
    } catch {
        return $false
    }
}

function Test-DashboardReady {
    try {
        $response = Invoke-WebRequest -Uri $DashboardUrl -UseBasicParsing -TimeoutSec 5
        return $response.StatusCode -eq 200
    } catch {
        return $false
    }
}

function Test-CollectorFresh {
    if (-not (Test-Path -LiteralPath $pythonPath -PathType Leaf)) {
        Write-Log "Collector check skipped: python environment missing"
        return $true
    }

    $probe = @'
import sys
from datetime import datetime, timedelta, timezone

try:
    from pymongo import MongoClient
    from app.config import settings

    client = MongoClient(settings.mongodb_uri, serverSelectionTimeoutMS=3000)
    latest = client[settings.mongodb_database]["metrics"].find_one(sort=[("timestamp", -1)])
    client.close()
except Exception as exc:
    print("unknown:" + type(exc).__name__)
    sys.exit(0)

if not latest or latest.get("timestamp") is None:
    print("stale:no-samples")
    sys.exit(0)

stamp = latest["timestamp"]
if stamp.tzinfo is None:
    stamp = stamp.replace(tzinfo=timezone.utc)
age = datetime.now(timezone.utc) - stamp
seconds = int(age.total_seconds())
print((("fresh:" if age < timedelta(minutes=__MAX_AGE__) else "stale:")) + str(seconds))
'@

    $probe = $probe.Replace("__MAX_AGE__", [string]$CollectorMaxAgeMinutes)
    $probePath = Join-Path $env:TEMP ("sentinel-collector-probe-" + [guid]::NewGuid().ToString("N") + ".py")
    Set-Content -LiteralPath $probePath -Value $probe -Encoding UTF8
    $previousPythonPath = $env:PYTHONPATH
    $env:PYTHONPATH = $backendPath
    try {
        $result = (& $pythonPath $probePath 2>&1 | Select-Object -Last 1)
    } finally {
        $env:PYTHONPATH = $previousPythonPath
        Remove-Item -LiteralPath $probePath -ErrorAction SilentlyContinue
    }

    if ($result -like "fresh:*") {
        return $true
    }
    if ($result -like "unknown:*") {
        Write-Log ("Collector check inconclusive (" + $result + "); leaving the collector running")
        return $true
    }
    Write-Log ("Collector produced no recent sample (" + $result + ")")
    return $false
}

function Restart-Task {
    param([string]$TaskName)
    # Stop-ScheduledTask leaves the python child alive; kill the tree or every restart orphans a duplicate.
    $patterns = @{
        "Server Health Monitor API"       = @("*run_backend.ps1*", "*uvicorn app.main:app*")
        "Server Health Monitor Dashboard" = @("*run_dashboard.ps1*", "*http.server 8001*")
        "Server Health Monitor Collector" = @("*run_collector.ps1*", "*\collector.py*")
    }[$TaskName]
    try {
        Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
        Start-Sleep -Seconds 2
        if ($patterns) {
            foreach ($pattern in $patterns) {
                Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
                    Where-Object { $_.ProcessId -ne $PID -and $_.CommandLine -like $pattern } |
                    ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
            }
            Start-Sleep -Seconds 1
        }
        Start-ScheduledTask -TaskName $TaskName -ErrorAction Stop
        Write-Log ("Restarted task: " + $TaskName)
        return $true
    } catch {
        Write-Log ("Restart failed for " + $TaskName + ": " + $_.Exception.Message)
        return $false
    }
}

function Send-WatchdogNotification {
    param([string]$Message)

    $notifierPath = Join-Path $projectRoot "scripts\watchdog_notify.py"
    if (-not (Test-Path -LiteralPath $notifierPath -PathType Leaf)) {
        return
    }
    if (-not (Test-Path -LiteralPath $pythonPath -PathType Leaf)) {
        Write-Log "Notification skipped: python environment missing"
        return
    }

    $previousPythonPath = $env:PYTHONPATH
    $env:PYTHONPATH = $backendPath
    try {
        $result = (& $pythonPath $notifierPath --message $Message --host $env:COMPUTERNAME 2>&1 | Select-Object -Last 1)
    } finally {
        $env:PYTHONPATH = $previousPythonPath
    }
    Write-Log ("Notification: " + $result)
}

$checks = @(
    @{ Label = "API"; Task = "Server Health Monitor API"; Healthy = (Test-ApiReady); Recheck = { Test-ApiReady } },
    @{ Label = "Dashboard"; Task = "Server Health Monitor Dashboard"; Healthy = (Test-DashboardReady); Recheck = { Test-DashboardReady } },
    @{ Label = "Collector"; Task = "Server Health Monitor Collector"; Healthy = (Test-CollectorFresh); Recheck = { Test-CollectorFresh } }
)

$restarted = @()
foreach ($check in $checks) {
    if ($check.Healthy) {
        continue
    }
    # A service that is still booting after logon is not down; wait before restarting it.
    $graceDeadline = (Get-Date).AddSeconds(60)
    while ((Get-Date) -lt $graceDeadline) {
        Start-Sleep -Seconds 10
        if (& $check.Recheck) {
            $check.Healthy = $true
            break
        }
    }
    if ($check.Healthy) {
        Write-Log ("Recovered within the grace period: " + $check.Label)
        continue
    }
    if ($check.Label -eq "API") {
        # A stopped MongoDB makes the API unhealthy; fix the database before restarting the API.
        Repair-MongoDb | Out-Null
        Start-Sleep -Seconds 5
        if (Test-ApiReady) {
            Write-Log "API is healthy again after the MongoDB service was repaired."
            continue
        }
    }
    Write-Log ("DOWN " + $check.Label + " -> restarting " + $check.Task)
    if (Restart-Task -TaskName $check.Task) {
        $restarted += $check.Label
        Send-WatchdogNotification -Message ("Sentinel watchdog restarted " + $check.Label + " because it stopped responding.")
    } else {
        Send-WatchdogNotification -Message ("Sentinel watchdog could not restart " + $check.Label + ". Manual action is required.")
    }
}

if ($restarted.Count -gt 0) {
    Write-Log ("Watchdog restarted: " + ($restarted -join ", "))
} else {
    Write-Log "Watchdog check complete: all services healthy"
}
exit 0
