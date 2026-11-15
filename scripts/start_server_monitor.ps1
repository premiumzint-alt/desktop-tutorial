$ErrorActionPreference = "Stop"

$apiTaskName = "Server Health Monitor API"
$dashboardTaskName = "Server Health Monitor Dashboard"
$collectorTaskName = "Server Health Monitor Collector"

function Test-ApiReady {
    try {
        $health = Invoke-RestMethod -Uri "http://127.0.0.1:8000/health" -TimeoutSec 2
        return $health.status -eq "ok" -and $health.mongodb -eq "ok"
    } catch {
        return $false
    }
}

function Test-DashboardReady {
    try {
        $response = Invoke-WebRequest -Uri "http://127.0.0.1:8001/" -UseBasicParsing -TimeoutSec 2
        return $response.StatusCode -eq 200
    } catch {
        return $false
    }
}

function Wait-Until {
    param(
        [scriptblock]$Condition,
        [int]$TimeoutSeconds = 20
    )

    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    do {
        if (& $Condition) {
            return $true
        }
        Start-Sleep -Seconds 1
    } while ((Get-Date) -lt $deadline)

    return $false
}

$mongo = Get-Service -Name "MongoDB" -ErrorAction SilentlyContinue
if (-not $mongo -or $mongo.Status -ne "Running") {
    throw "MongoDB is not running. Start the MongoDB service, then run this launcher again."
}

if (-not (Test-ApiReady)) {
    Start-ScheduledTask -TaskName $apiTaskName
    if (-not (Wait-Until -Condition { Test-ApiReady })) {
        throw "The API did not become ready. Check backend/logs/backend.log."
    }
}

if (-not (Test-DashboardReady)) {
    Start-ScheduledTask -TaskName $dashboardTaskName
    if (-not (Wait-Until -Condition { Test-DashboardReady })) {
        throw "The Dashboard did not become ready. Check frontend/logs/dashboard-errors.log."
    }
}

$collectorTask = Get-ScheduledTask -TaskName $collectorTaskName -ErrorAction Stop
if ($collectorTask.State -ne "Running") {
    $collectorProcess = Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
        Where-Object { $_.CommandLine -match "collector\.py|run_collector\.ps1" } |
        Select-Object -First 1
    if (-not $collectorProcess) {
        Start-ScheduledTask -TaskName $collectorTaskName
    }
}

Start-Process "http://127.0.0.1:8001/"
Write-Output "Server Health Monitor is ready at http://127.0.0.1:8001/"