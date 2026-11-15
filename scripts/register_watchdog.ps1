param(
    [int]$IntervalMinutes = 5
)

$ErrorActionPreference = "Stop"

$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$watchdogPath = Join-Path $projectRoot "scripts\watchdog.ps1"
$taskName = "Server Health Monitor Watchdog"
$conhostPath = Join-Path $env:WINDIR "System32\conhost.exe"
if (-not (Test-Path -LiteralPath $conhostPath -PathType Leaf)) {
    throw "conhost.exe was not found at $conhostPath (required for hidden watchdog execution)"
}

if (-not (Test-Path -LiteralPath $watchdogPath -PathType Leaf)) {
    throw "watchdog.ps1 was not found at $watchdogPath"
}

$action = New-ScheduledTaskAction `
    -Execute $conhostPath `
    -Argument ('--headless "C:\WINDOWS\System32\WindowsPowerShell\v1.0\powershell.exe" -NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File "' + $watchdogPath + '"') `
    -WorkingDirectory $projectRoot

$logonTrigger = New-ScheduledTaskTrigger -AtLogOn
$repeatTrigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes($IntervalMinutes) `
    -RepetitionInterval (New-TimeSpan -Minutes $IntervalMinutes) `
    -RepetitionDuration (New-TimeSpan -Days 3650)

$principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Highest
$settings = New-ScheduledTaskSettingsSet `
    -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 15) `
    -StartWhenAvailable `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries

Register-ScheduledTask `
    -TaskName $taskName `
    -Action $action `
    -Trigger @($logonTrigger, $repeatTrigger) `
    -Principal $principal `
    -Settings $settings `
    -Description "Checks the Sentinel API, dashboard and collector every $IntervalMinutes minutes and restarts the matching scheduled task when one is down." `
    -Force | Out-Null

$task = Get-ScheduledTask -TaskName $taskName
Write-Output ("registered: " + $task.TaskName + " | state=" + $task.State)
Write-Output ("workdir: " + $task.Actions[0].WorkingDirectory)
Write-Output ("interval: " + $IntervalMinutes + " minutes (plus at logon)")
