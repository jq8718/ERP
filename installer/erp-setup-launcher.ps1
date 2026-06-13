$ErrorActionPreference = "Stop"

$logDir = Join-Path $PSScriptRoot "logs"
if (-not (Test-Path -LiteralPath $logDir)) {
    New-Item -ItemType Directory -Path $logDir | Out-Null
}
$logPath = Join-Path $logDir ("erp-setup-" + (Get-Date -Format "yyyyMMdd-HHmmss") + ".log")
Start-Transcript -LiteralPath $logPath -Force | Out-Null

function Ensure-Admin {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = New-Object Security.Principal.WindowsPrincipal($identity)
    if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
        throw "Administrator permission is required. Right-click ERP-Setup.exe and choose Run as administrator."
    }
}

function Get-DefaultServerHost {
    try {
        $hostFromGateway = Get-NetIPConfiguration -ErrorAction Stop |
            Where-Object { $_.IPv4DefaultGateway -and $_.IPv4Address } |
            ForEach-Object { $_.IPv4Address.IPAddress } |
            Where-Object { $_ -and $_ -notlike "127.*" -and $_ -notlike "169.254.*" } |
            Select-Object -First 1
        if ($hostFromGateway) {
            return $hostFromGateway
        }
    } catch {
    }

    try {
        $hostFromAddress = Get-NetIPAddress -AddressFamily IPv4 -ErrorAction Stop |
            Where-Object { $_.IPAddress -notlike "127.*" -and $_.IPAddress -notlike "169.254.*" } |
            Select-Object -First 1 -ExpandProperty IPAddress
        if ($hostFromAddress) {
            return $hostFromAddress
        }
    } catch {
    }

    return ""
}

try {
    Ensure-Admin

    $root = Split-Path -Parent $PSScriptRoot
    $defaultServerHost = Get-DefaultServerHost
    if ($defaultServerHost) {
        Write-Host "Detected local IPv4: $defaultServerHost"
        Write-Host "For multi-user ERP access, this IP must be static or reserved in DHCP."
        Write-Host "A dynamic DHCP IP may change after reboot, causing browser access to fail."
        $serverHost = Read-Host "Enter ERP server IP or intranet host name [default: $defaultServerHost]"
        if (-not $serverHost) {
            $serverHost = $defaultServerHost
        }
    } else {
        $serverHost = Read-Host "Enter ERP server IP or intranet host name, for example 192.168.1.10"
    }
    if (-not $serverHost) {
        throw "Server IP or host name is required."
    }

    Push-Location $root
    try {
        & "$root\installer\preflight-prerequisites.ps1"
        Write-Host ""
        Write-Host "If Python, PostgreSQL, PostgreSQL service, or NSSM is missing, install it from installer\packages and rerun this setup."
        Write-Host "Git is optional if the full ERP package folder has already been copied to this server."
        $continue = Read-Host "Continue to initialize PostgreSQL and ERP? Enter Y to continue"
        if ($continue -ne "Y") {
            Write-Host "Cancelled."
            exit 0
        }

        & "$root\installer\setup-postgres-db.ps1"
        & "$root\installer\install-erp.ps1" -ServerHost $serverHost
        & "$root\installer\register-windows-service.ps1"
        & "$root\installer\register-scheduled-tasks.ps1"
        & "$root\installer\create-desktop-shortcut.ps1" -ServerHost $serverHost

        Write-Host ""
        Write-Host "ERP setup completed. URL: http://$serverHost`:8000/"
        Start-Process "http://$serverHost`:8000/"
    } finally {
        Pop-Location
    }

    Write-Host ""
    Write-Host "Setup log: $logPath"
    Read-Host "Press Enter to exit"
} catch {
    Write-Host ""
    Write-Host "ERP setup failed:" -ForegroundColor Red
    Write-Host $_.Exception.Message -ForegroundColor Red
    Write-Host ""
    Write-Host "Send this log file to the developer:" -ForegroundColor Yellow
    Write-Host $logPath -ForegroundColor Yellow
    Write-Host ""
    Read-Host "Press Enter to exit"
    exit 1
} finally {
    try {
        Stop-Transcript | Out-Null
    } catch {
    }
}
