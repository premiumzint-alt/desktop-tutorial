[CmdletBinding(SupportsShouldProcess = $true)]
param()

$ErrorActionPreference = "Stop"
$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$principalCheck = [Security.Principal.WindowsPrincipal]::new($identity)
$isAdministrator = $principalCheck.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $isAdministrator -and -not $WhatIfPreference) {
    throw "Run this installer from PowerShell opened with Run as administrator."
}

$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$pythonPath = Join-Path $projectRoot "collector\.venv\Scripts\python.exe"
$backendEnvPath = Join-Path $projectRoot "backend\.env"
$collectorScript = Join-Path $PSScriptRoot "run_collector.ps1"
$backendScript = Join-Path $PSScriptRoot "run_backend.ps1"
$dashboardScript = Join-Path $PSScriptRoot "run_dashboard.ps1"
$powershellPath = Join-Path $env:WINDIR "System32\WindowsPowerShell\v1.0\powershell.exe"
$conhostPath = Join-Path $env:WINDIR "System32\conhost.exe"

foreach ($requiredPath in @($pythonPath, $backendEnvPath, $collectorScript, $backendScript, $dashboardScript)) {
    if (-not (Test-Path -LiteralPath $requiredPath -PathType Leaf)) {
        throw "Required project file was not found: $requiredPath"
    }
}

$user = $identity.Name
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $user
$settings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -RestartCount 3 `
    -RestartInterval (New-TimeSpan -Minutes 1)

$taskDefinitions = @(
    @{ Name = "Server Health Monitor API"; Script = $backendScript; RunLevel = "Limited"; Description = "Start the local FastAPI service at user logon." },
    @{ Name = "Server Health Monitor Dashboard"; Script = $dashboardScript; RunLevel = "Limited"; Description = "Start the local dashboard web server at user logon." },
    @{ Name = "Server Health Monitor Collector"; Script = $collectorScript; RunLevel = "Highest"; Description = "Start the local security collector with elevated permissions at user logon." }
)

foreach ($definition in $taskDefinitions) {
    if ($definition.RunLevel -eq "Highest") {
        # The collector must read the Windows Security log. An interactive "highest" task only
        # gets the admin token when the session that triggers it is elevated, so the collector
        # runs as the local system account instead and always has the rights it needs.
        $principal = New-ScheduledTaskPrincipal `
            -UserId "SYSTEM" `
            -LogonType ServiceAccount `
            -RunLevel Highest
    } else {
        $principal = New-ScheduledTaskPrincipal `
            -UserId $user `
            -LogonType Interactive `
            -RunLevel $definition.RunLevel
    }
    $arguments = '--headless "{0}" -NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File "{1}"' -f $powershellPath, $definition.Script
    $action = New-ScheduledTaskAction `
        -Execute $conhostPath `
        -Argument $arguments `
        -WorkingDirectory $projectRoot

    if ($PSCmdlet.ShouldProcess($definition.Name, "Register logon task for $user")) {
        Register-ScheduledTask `
            -TaskName $definition.Name `
            -Action $action `
            -Trigger $trigger `
            -Principal $principal `
            -Settings $settings `
            -Description $definition.Description `
            -Force | Out-Null
        Write-Output "Registered: $($definition.Name)"
    }
}

Write-Output "Tasks start at the next user logon; currently running processes are unchanged."