$ErrorActionPreference = "Stop"

$logDir = Join-Path $PSScriptRoot "logs"
if (-not (Test-Path -LiteralPath $logDir)) {
    New-Item -ItemType Directory -Path $logDir | Out-Null
}
$logPath = Join-Path $logDir ("erp-uninstall-" + (Get-Date -Format "yyyyMMdd-HHmmss") + ".log")
Start-Transcript -LiteralPath $logPath -Force | Out-Null

function Ensure-Admin {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = New-Object Security.Principal.WindowsPrincipal($identity)
    if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
        throw "Administrator permission is required. Right-click ERP-Uninstall.exe and choose Run as administrator."
    }
}

try {
    Ensure-Admin

    $serviceName = "ERPWeb"
    $tasks = @(
        "ERP Backup Daily",
        "ERP Verify Backups",
        "ERP Process Events",
        "ERP Cleanup Backups",
        "ERP Restore Drill"
    )

    Write-Host "This uninstaller removes Windows service and scheduled tasks only. It does not delete database, attachments, backups, or logs."
    $confirm = Read-Host "Confirm ERP service uninstall? Enter UNINSTALL to continue"
    if ($confirm -ne "UNINSTALL") {
        Write-Host "Cancelled."
        exit 0
    }

    $nssm = Join-Path $PSScriptRoot "tools\nssm.exe"
    if (-not (Test-Path -LiteralPath $nssm)) {
        $nssm = Join-Path $PSScriptRoot "packages\nssm-2.24\win64\nssm.exe"
        if (-not (Test-Path -LiteralPath $nssm)) {
            $zip = Join-Path $PSScriptRoot "packages\nssm-2.24.zip"
            if (Test-Path -LiteralPath $zip) {
                Expand-Archive -LiteralPath $zip -DestinationPath (Join-Path $PSScriptRoot "packages") -Force
            }
        }
    }
    if (-not (Test-Path -LiteralPath $nssm)) {
        $cmd = Get-Command nssm.exe -ErrorAction SilentlyContinue
        if ($cmd) {
            $nssm = $cmd.Source
        }
    }

    if (Get-Service -Name $serviceName -ErrorAction SilentlyContinue) {
        if (Test-Path -LiteralPath $nssm) {
            & $nssm stop $serviceName | Out-Null
            & $nssm remove $serviceName confirm | Out-Null
        } else {
            Stop-Service -Name $serviceName -ErrorAction SilentlyContinue
            sc.exe delete $serviceName | Out-Host
        }
        Write-Host "Service removed: $serviceName"
    }

    foreach ($task in $tasks) {
        schtasks.exe /Delete /TN $task /F 2>$null | Out-Null
    }
    Write-Host "ERP scheduled tasks removed."

    Write-Host ""
    Write-Host "Uninstall log: $logPath"
    Read-Host "Press Enter to exit"
} catch {
    Write-Host ""
    Write-Host "ERP uninstall failed:" -ForegroundColor Red
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
