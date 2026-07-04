$ErrorActionPreference = "Stop"

$logDir = Join-Path $PSScriptRoot "logs"
if (-not (Test-Path -LiteralPath $logDir)) {
    New-Item -ItemType Directory -Path $logDir | Out-Null
}
$logPath = Join-Path $logDir ("erp-update-" + (Get-Date -Format "yyyyMMdd-HHmmss") + ".log")
Start-Transcript -LiteralPath $logPath -Force | Out-Null

function Ensure-Admin {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = New-Object Security.Principal.WindowsPrincipal($identity)
    if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
        throw "Administrator permission is required. Right-click ERP-Update.exe and choose Run as administrator."
    }
}

try {
    Ensure-Admin

    $root = Split-Path -Parent $PSScriptRoot
    Write-Host "ERP update package root: $root"
    Write-Host "This updater keeps .env, database, attachments, backups, logs, and the Python virtualenv."
    Write-Host "It updates application code, templates, migrations, static files, and the Windows service."
    Write-Host ""

    $installDir = Read-Host "Enter existing ERP install directory [default: D:\ERP\app]"
    if (-not $installDir) {
        $installDir = "D:\ERP\app"
    }
    if (-not (Test-Path -LiteralPath $installDir)) {
        throw "Existing ERP install directory not found: $installDir"
    }
    if (-not (Test-Path -LiteralPath (Join-Path $installDir ".env"))) {
        throw "Existing .env not found in install directory: $installDir"
    }

    Write-Host ""
    Write-Host "Update source: $root"
    Write-Host "Update target: $installDir"
    $confirm = Read-Host "Confirm ERP update? Enter UPDATE to continue"
    if ($confirm -ne "UPDATE") {
        Write-Host "Cancelled."
        exit 0
    }

    Push-Location $root
    try {
        & "$root\installer\update-erp.ps1" -InstallDir $installDir
    } finally {
        Pop-Location
    }

    Write-Host ""
    Write-Host "ERP update completed."
    Write-Host "Update log: $logPath"
    Read-Host "Press Enter to exit"
} catch {
    Write-Host ""
    Write-Host "ERP update failed:" -ForegroundColor Red
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
