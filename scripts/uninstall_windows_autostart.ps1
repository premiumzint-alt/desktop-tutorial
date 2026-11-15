[CmdletBinding(SupportsShouldProcess = $true)]
param()

$ErrorActionPreference = "Stop"
param([switch]$RemoveData)
$taskNames = @(
    "Server Health Monitor API",
    "Server Health Monitor Dashboard",
    "Server Health Monitor Collector",
    "Server Health Monitor Watchdog"
)
$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path

foreach ($taskName in $taskNames) {
    $task = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
    if ($task) {
        Stop-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
        if ($PSCmdlet.ShouldProcess($taskName, "Unregister scheduled task")) {
            Unregister-ScheduledTask -TaskName $taskName -Confirm:$false
            Write-Output "Removed: $taskName"
        }
    }
}
if ($RemoveData) {
    foreach ($item in @("data","backend\monitor.db","backend\logs","frontend\logs","collector\state")) {
        $path = Join-Path $projectRoot $item
        if (Test-Path -LiteralPath $path) {
            if ($PSCmdlet.ShouldProcess($path, "Remove data")) {
                Remove-Item -LiteralPath $path -Recurse -Force -ErrorAction SilentlyContinue
                Write-Output "Removed: $path"
            }
        }
    }
}