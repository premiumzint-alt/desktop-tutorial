[CmdletBinding()]
param(
    [switch]$NoAutostart,
    [switch]$Reinstall
)

$ErrorActionPreference = "Stop"

$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$backendPath = Join-Path $projectRoot "backend"
$collectorPath = Join-Path $projectRoot "collector"
$venvPath = Join-Path $collectorPath ".venv"
$venvPython = Join-Path $venvPath "Scripts\python.exe"
$envPath = Join-Path $backendPath ".env"
$envExamplePath = Join-Path $backendPath ".env.example"
$apiBaseUrl = "http://127.0.0.1:8000"
$dashboardUrl = "http://127.0.0.1:8001/"
$taskNames = @(
    "Server Health Monitor API",
    "Server Health Monitor Dashboard",
    "Server Health Monitor Collector",
    "Server Health Monitor Watchdog"
)

function Write-Step([string]$Message) {
    Write-Output ""
    Write-Output ("== " + $Message + " ==")
}

function Test-Administrator {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = [Security.Principal.WindowsPrincipal]::new($identity)
    return $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

function Test-Port([string]$TargetHost, [int]$Port, [int]$TimeoutMilliseconds = 800) {
    $client = New-Object System.Net.Sockets.TcpClient
    try {
        $connect = $client.ConnectAsync($TargetHost, $Port)
        return $connect.Wait($TimeoutMilliseconds) -and $client.Connected
    } catch {
        return $false
    } finally {
        $client.Dispose()
    }
}

function Wait-Until {
    param(
        [scriptblock]$Condition,
        [int]$TimeoutSeconds
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

function New-RandomSecret {
    $rng = [System.Security.Cryptography.RandomNumberGenerator]::Create()
    try {
        $bytes = New-Object byte[] 32
        $rng.GetBytes($bytes)
        return (($bytes | ForEach-Object { $_.ToString("x2") }) -join "")
    } finally {
        $rng.Dispose()
    }
}

function Get-EnvValue([string]$Name) {
    if (-not (Test-Path -LiteralPath $envPath -PathType Leaf)) {
        return $null
    }
    $prefix = $Name + "="
    $line = Get-Content -LiteralPath $envPath | Where-Object { $_.StartsWith($prefix) } | Select-Object -First 1
    if (-not $line) {
        return $null
    }
    return $line.Substring($prefix.Length).Trim()
}

function Invoke-Native {
    # A native command that writes to stderr must not abort the installer, so the error
    # action is relaxed for the call. The exit code is published in $script:NativeExitCode
    # because the command output itself has to stay in the output stream.
    param(
        [Parameter(Mandatory = $true)][string]$FilePath,
        [string[]]$Arguments = @(),
        [switch]$Quiet
    )
    $previous = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try {
        if ($Quiet) {
            & $FilePath @Arguments *> $null
        } else {
            & $FilePath @Arguments
        }
        $script:NativeExitCode = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $previous
    }
}

function Test-DockerAvailable {
    if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
        return $false
    }
    & docker info --format "{{.ServerVersion}}" *> $null
    return $LASTEXITCODE -eq 0
}

function Test-PythonCommand {
    param([string]$Command)
    if (-not $Command) {
        return $false
    }
    try {
        $output = & $Command -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')" 2>$null
    } catch {
        return $false
    }
    if ($LASTEXITCODE -ne 0 -or -not $output) {
        return $false
    }
    try {
        return ([version]$output.Trim()) -ge [version]"3.11"
    } catch {
        return $false
    }
}

function Find-UsablePython {
    # The Microsoft Store alias in WindowsApps is a stub that opens the Store instead of
    # running Python, so every candidate has to be executed before it is used.
    $candidates = @()
    $uv = Get-Command uv -ErrorAction SilentlyContinue
    if ($uv) {
        try {
            $uvPython = (& uv python find 2>$null | Select-Object -First 1)
            if ($uvPython) {
                $candidates += $uvPython.Trim()
            }
        } catch {
            # uv is optional; fall through to the other candidates.
        }
    }
    foreach ($name in @("python3", "python", "py")) {
        $command = Get-Command $name -ErrorAction SilentlyContinue
        if ($command) {
            $candidates += $command.Source
        }
    }
    $candidates += (Get-ChildItem "$env:LOCALAPPDATA\Programs\Python\Python3*\python.exe" -ErrorAction SilentlyContinue |
        Sort-Object FullName -Descending | Select-Object -ExpandProperty FullName)
    $candidates += (Get-ChildItem "$env:APPDATA\uv\python\cpython-3*\python.exe" -ErrorAction SilentlyContinue |
        Sort-Object FullName -Descending | Select-Object -ExpandProperty FullName)
    $candidates += (Get-ChildItem "C:\Program Files\Python3*\python.exe" -ErrorAction SilentlyContinue |
        Sort-Object FullName -Descending | Select-Object -ExpandProperty FullName)

    foreach ($candidate in $candidates) {
        if ($candidate -and (Test-PythonCommand -Command $candidate)) {
            return $candidate
        }
    }
    return $null
}

if (-not $NoAutostart -and -not (Test-Administrator)) {
    Write-Output "Administrator rights are required to register autostart tasks."
    Start-Process -FilePath "powershell.exe" -Verb RunAs `
        -ArgumentList @("-NoProfile", "-NoExit", "-ExecutionPolicy", "Bypass", "-File", "`"$PSCommandPath`"")
    return
}

Write-Step "Checking MongoDB"
if (-not (Test-Port "127.0.0.1" 27017)) {
    $mongoService = Get-Service -Name "MongoDB" -ErrorAction SilentlyContinue
    if ($mongoService -and $mongoService.Status -ne "Running") {
        try {
            Start-Service -Name "MongoDB" -ErrorAction Stop
            Wait-Until -Condition { (Get-Service -Name "MongoDB").Status -eq "Running" } -TimeoutSeconds 30 | Out-Null
        } catch {
            Write-Output ("MongoDB service could not be started: " + $_.Exception.Message)
        }
    }
}
if (-not (Test-Port "127.0.0.1" 27017)) {
    if (Test-DockerAvailable) {
        $env:MONGO_IMAGE = "mongo:7"
        Write-Output "MongoDB is not running; starting it with Docker Compose."
        Push-Location $projectRoot
        try {
            & docker compose up -d
            if ($LASTEXITCODE -ne 0) { throw "docker compose up -d failed." }
            $deadline = (Get-Date).AddSeconds(60)
            while ((Get-Date) -lt $deadline) {
                $status = (& docker compose ps --format "{{.Status}}" 2>$null | Out-String)
                if ($status -match "healthy") { break }
                if ($status -match "Restarting|Exited") {
                    $composeLogs = (& docker compose logs 2>&1 | Out-String)
                    if ($composeLogs -match "AVX") {
                        Write-Output "This CPU has no AVX support; falling back to mongo:4.4."
                        $env:MONGO_IMAGE = "mongo:4.4"
                        & docker compose up -d
                        if ($LASTEXITCODE -ne 0) { throw "docker compose up -d failed for mongo:4.4." }
                    }
                    break
                }
                Start-Sleep -Seconds 5
            }
        } finally {
            Pop-Location
        }
        if (-not (Wait-Until -TimeoutSeconds 120 -Condition { Test-Port "127.0.0.1" 27017 })) {
            throw "MongoDB did not accept connections on 127.0.0.1:27017. Check with: docker compose ps"
        }
    } else {
        throw "MongoDB is not running and Docker is not available. Install MongoDB (winget install MongoDB.Server) or Docker Desktop, then re-run."
    }
}
Write-Output "MongoDB reachable on 127.0.0.1:27017."

Write-Step "Preparing Python environment"
if (-not (Test-Path -LiteralPath $venvPython -PathType Leaf)) {
    $basePython = Find-UsablePython
    if (-not $basePython) {
        throw "Python 3.11+ not found. Install it from https://www.python.org/downloads/ (with 'Add to PATH'), or run 'uv python install 3.12', then re-run this installer."
    }
    Write-Output "Creating $venvPath with $basePython"
    Invoke-Native -FilePath $basePython -Arguments @("-m", "venv", $venvPath) -Quiet
    if ($script:NativeExitCode -ne 0) { throw "Failed to create the virtual environment." }
}

$pythonVersion = & $venvPython -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')" 2>$null
if ([version]$pythonVersion -lt [version]"3.11") {
    throw "Python 3.11+ required (found $pythonVersion). Delete $venvPath and re-run."
}
Write-Output "Python $pythonVersion"

Invoke-Native -FilePath $venvPython -Arguments @("-c", "import fastapi, uvicorn, sqlalchemy, pymongo, pydantic_settings, jwt, pwdlib, psutil") -Quiet
$dependenciesMissing = ($script:NativeExitCode -ne 0)
if ($dependenciesMissing -or $Reinstall) {
    Write-Output "Installing Python dependencies."
    Invoke-Native -FilePath $venvPython -Arguments @("-m", "pip", "install", "--upgrade", "pip", "--timeout", "30", "--retries", "3")
    if ($script:NativeExitCode -ne 0) { throw "pip upgrade failed." }
    Invoke-Native -FilePath $venvPython -Arguments @("-m", "pip", "install", "--timeout", "30", "--retries", "3", "-r", (Join-Path $backendPath "requirements.txt"), "-r", (Join-Path $collectorPath "requirements.txt"))
    if ($script:NativeExitCode -ne 0) { throw "Dependency installation failed." }

    # The audit tool is optional: it needs a C compiler on Python versions without a
    # pyyaml wheel, so a failure here must not stop the installation. --only-binary keeps
    # pip from trying to build the missing wheels.
    Invoke-Native -FilePath $venvPython -Arguments @("-m", "pip", "install", "--only-binary", ":all:", "--timeout", "30", "--retries", "1", "-r", (Join-Path $collectorPath "requirements-audit.txt"))
    if ($script:NativeExitCode -ne 0) {
        Write-Output "pip-audit could not be installed (optional); the vulnerability audit stays disabled."
    }

    # The desktop shell is optional as well: pywebview needs the Edge WebView2 runtime.
    Invoke-Native -FilePath $venvPython -Arguments @("-m", "pip", "install", "--timeout", "30", "--retries", "1", "-r", (Join-Path $projectRoot "desktop\requirements.txt"))
    if ($script:NativeExitCode -ne 0) {
        Write-Output "pywebview could not be installed (optional); the desktop app stays unavailable."
    }
} else {
    Write-Output "Python dependencies already installed."
}

Write-Step "Checking backend configuration"
if (-not (Test-Path -LiteralPath $envPath -PathType Leaf)) {
    if (-not (Test-Path -LiteralPath $envExamplePath -PathType Leaf)) {
        throw "backend/.env.example was not found."
    }
    $content = Get-Content -LiteralPath $envExamplePath -Raw
    foreach ($key in @("JWT_SECRET_KEY", "COLLECTOR_API_KEY", "BOOTSTRAP_TOKEN")) {
        $content = [regex]::Replace($content, "(?m)^$key=.*$", ($key + "=" + (New-RandomSecret)))
    }
    Set-Content -LiteralPath $envPath -Value $content -Encoding ASCII
    Write-Output "Created backend/.env with fresh random secrets."
} else {
    Write-Output "backend/.env already exists; secrets left unchanged."
}

Write-Step "Registering autostart tasks"
if ($NoAutostart) {
    Write-Output "Skipped (-NoAutostart)."
} else {
    & (Join-Path $PSScriptRoot "install_windows_autostart.ps1")
    if ($LASTEXITCODE -ne 0) { throw "install_windows_autostart.ps1 failed." }
    & (Join-Path $PSScriptRoot "register_watchdog.ps1")
    if ($LASTEXITCODE -ne 0) { throw "register_watchdog.ps1 failed." }

    Write-Step "Starting services"
    foreach ($taskName in $taskNames) {
        Start-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
    }

    try {
        & (Join-Path $PSScriptRoot "create_desktop_shortcut.ps1")
    } catch {
        Write-Output ("Desktop shortcut skipped: " + $_.Exception.Message)
    }
}

if (-not $NoAutostart) {
    Write-Step "Waiting for API and dashboard"
    $apiReady = Wait-Until -TimeoutSeconds 90 -Condition {
        try {
            $health = Invoke-RestMethod -Uri ($apiBaseUrl + "/health") -TimeoutSec 2
            return $health.status -eq "ok" -and $health.mongodb -eq "ok"
        } catch { return $false }
    }
    if (-not $apiReady) { throw "API did not become ready. Check backend/logs/backend.log." }
    $dashboardReady = Wait-Until -TimeoutSeconds 60 -Condition {
        try { return (Invoke-WebRequest -Uri $dashboardUrl -UseBasicParsing -TimeoutSec 2).StatusCode -eq 200 }
        catch { return $false }
    }
    if (-not $dashboardReady) { throw "Dashboard did not become ready. Check frontend/logs/dashboard-errors.log." }

    Write-Step "Checking first administrator account"
    $userCount = 0
    $databasePath = Join-Path $backendPath "monitor.db"
    if (Test-Path -LiteralPath $databasePath -PathType Leaf) {
        $countText = & $venvPython -c "import sqlite3; print(sqlite3.connect(r'$databasePath').execute('select count(*) from users').fetchone()[0])" 2>$null
        $userCount = [int]$countText
    }
    if ($userCount -eq 0) {
        Write-Output "No administrator exists yet."
        $username = Read-Host "Admin username (Enter for 'admin')"
        if ([string]::IsNullOrWhiteSpace($username)) { $username = "admin" }
        $password = ""
        while ($password.Length -lt 12) {
            $secure = Read-Host ("Password for " + $username + " (min 12 chars)") -AsSecureString
            $bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
            try { $password = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($bstr) }
            finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr) }
            if ($password.Length -lt 12) { Write-Output "Password too short; try again." }
        }
        $body = @{ username = $username; password = $password } | ConvertTo-Json -Compress
        $headers = @{ "X-Bootstrap-Token" = (Get-EnvValue "BOOTSTRAP_TOKEN") }
        try {
            Invoke-RestMethod -Method Post -Uri ($apiBaseUrl + "/api/v1/auth/register") -Headers $headers -ContentType "application/json" -Body $body | Out-Null
            Write-Output ("Admin account created: " + $username)
        } catch {
            $status = $_.Exception.Response.StatusCode.value__
            if ($status -eq 409) { Write-Output "Administrator already exists; skipping." } else { throw }
        } finally {
            $password = $null
            $body = $null
        }
    } else {
        Write-Output "Administrator already exists; skipping."
    }
}

Write-Step "Done"
Write-Output ("Dashboard: " + $dashboardUrl)
if (-not $NoAutostart) { Start-Process $dashboardUrl }
