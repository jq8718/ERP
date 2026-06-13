param(
    [string]$InstallDir = "D:\ERP\app",
    [string]$ServiceName = "ERPWeb",
    [string]$DisplayName = "ERP Web Service",
    [string]$Listen = "0.0.0.0:8000",
    [int]$Threads = 8
)

$ErrorActionPreference = "Stop"

function Find-Nssm {
    $candidates = @()
    if ($env:ERP_NSSM_EXE) {
        $candidates += $env:ERP_NSSM_EXE
    }
    $candidates += (Join-Path $PSScriptRoot "tools\nssm.exe")
    $candidates += (Join-Path $PSScriptRoot "packages\nssm-2.24\win64\nssm.exe")
    $candidates += "C:\nssm\nssm-2.24\win64\nssm.exe"
    $candidates += "C:\nssm\win64\nssm.exe"

    foreach ($candidate in $candidates) {
        if ($candidate -and (Test-Path -LiteralPath $candidate)) {
            return $candidate
        }
    }

    $zip = Join-Path $PSScriptRoot "packages\nssm-2.24.zip"
    if (Test-Path -LiteralPath $zip) {
        Expand-Archive -LiteralPath $zip -DestinationPath (Join-Path $PSScriptRoot "packages") -Force
        $expanded = Join-Path $PSScriptRoot "packages\nssm-2.24\win64\nssm.exe"
        if (Test-Path -LiteralPath $expanded) {
            return $expanded
        }
    }

    $cmd = Get-Command nssm.exe -ErrorAction SilentlyContinue
    if ($cmd) {
        return $cmd.Source
    }
    throw "nssm.exe not found. Extract NSSM to C:\nssm, set ERP_NSSM_EXE, or add nssm.exe to PATH."
}

function Get-ListenPort {
    param([string]$ListenAddress)
    if ($ListenAddress -match ":(\d+)$") {
        return [int]$Matches[1]
    }
    throw "Cannot parse port from listen address: $ListenAddress"
}

function Ensure-FirewallRule {
    param(
        [string]$RuleName,
        [int]$Port
    )

    try {
        $existingRule = Get-NetFirewallRule -DisplayName $RuleName -ErrorAction SilentlyContinue
        if ($existingRule) {
            Write-Host "Firewall rule exists; updating port: $RuleName / TCP $Port"
            Set-NetFirewallRule -DisplayName $RuleName -Enabled True -Direction Inbound -Action Allow -Profile Any | Out-Null
            $portFilters = $existingRule | Get-NetFirewallPortFilter
            foreach ($filter in $portFilters) {
                Set-NetFirewallPortFilter -InputObject $filter -Protocol TCP -LocalPort $Port | Out-Null
            }
        } else {
            New-NetFirewallRule -DisplayName $RuleName -Direction Inbound -Action Allow -Protocol TCP -LocalPort $Port -Profile Any | Out-Null
            Write-Host "Firewall rule created: $RuleName / TCP $Port"
        }
    } catch {
        Write-Host "PowerShell firewall cmdlets failed; trying netsh fallback."
        netsh advfirewall firewall delete rule name="$RuleName" | Out-Null
        netsh advfirewall firewall add rule name="$RuleName" dir=in action=allow protocol=TCP localport=$Port | Out-Null
        if ($LASTEXITCODE -ne 0) {
            throw "Failed to create firewall rule for TCP $Port"
        }
        Write-Host "Firewall rule created by netsh: $RuleName / TCP $Port"
    }
}

$python = Join-Path $InstallDir ".venv\Scripts\python.exe"
$waitress = Join-Path $InstallDir ".venv\Scripts\waitress-serve.exe"
if (-not (Test-Path -LiteralPath $python)) {
    throw "Python virtualenv not found: $python. Run install-erp.ps1 first."
}
if (-not (Test-Path -LiteralPath $waitress)) {
    throw "Waitress not found: $waitress. Run install-erp.ps1 first."
}

$nssm = Find-Nssm
$logs = Join-Path (Split-Path -Parent $InstallDir) "logs"
if (-not (Test-Path -LiteralPath $logs)) {
    New-Item -ItemType Directory -Path $logs | Out-Null
}

$existing = Get-Service -Name $ServiceName -ErrorAction SilentlyContinue
if ($existing) {
    Write-Host "Service exists; updating configuration: $ServiceName"
    & $nssm stop $ServiceName | Out-Null
} else {
    & $nssm install $ServiceName $waitress "--listen=$Listen" "--threads=$Threads" "config.wsgi:application" | Out-Null
}

& $nssm set $ServiceName DisplayName $DisplayName | Out-Null
& $nssm set $ServiceName AppDirectory $InstallDir | Out-Null
& $nssm set $ServiceName AppStdout (Join-Path $logs "erp-web.stdout.log") | Out-Null
& $nssm set $ServiceName AppStderr (Join-Path $logs "erp-web.stderr.log") | Out-Null
& $nssm set $ServiceName AppRotateFiles 1 | Out-Null
& $nssm set $ServiceName AppRotateOnline 1 | Out-Null
& $nssm set $ServiceName AppRotateBytes 10485760 | Out-Null
& $nssm set $ServiceName Start SERVICE_AUTO_START | Out-Null

$port = Get-ListenPort -ListenAddress $Listen
Ensure-FirewallRule -RuleName "ERP Web $port" -Port $port

& $nssm start $ServiceName | Out-Null

Write-Host "Windows service is running: $ServiceName"
Get-Service -Name $ServiceName
