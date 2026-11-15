param([string]$Name = "Sentinel Desktop")
$ErrorActionPreference = "Stop"
$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$pythonw = Join-Path $projectRoot "collector\.venv\Scripts\pythonw.exe"
$script = Join-Path $projectRoot "scripts\desktop_app.py"
$iconPath = Join-Path $projectRoot "assets\sentinel.ico"
$shortcutPath = Join-Path ([Environment]::GetFolderPath("Desktop")) "Sentinel.lnk"

if (-not (Test-Path -LiteralPath $pythonw -PathType Leaf)) {
    throw "Sentinel is not installed yet. Run Install.cmd first."
}

$shell = New-Object -ComObject WScript.Shell
$shortcut = $shell.CreateShortcut($shortcutPath)
$shortcut.TargetPath = $pythonw
$shortcut.Arguments = '"' + $script + '"'
$shortcut.WorkingDirectory = $projectRoot
$shortcut.Description = "Sentinel server health and security monitor"
if (Test-Path -LiteralPath $iconPath -PathType Leaf) {
    $shortcut.IconLocation = $iconPath + ",0"
}
$shortcut.Save()
Write-Output ("Created desktop shortcut: " + $shortcutPath)
