param(
    [string]$InstallDir = "D:\ERP\app",
    [string]$ServiceName = "ERPWeb",
    [string]$DisplayName = "ERP Web Service",
    [string]$Listen = "0.0.0.0:8000",
    [int]$Threads = 8
)

$ErrorActionPreference = "Stop"

function Find-Nssm {
    $expanded = Join-Path $PSScriptRoot "packages\nssm-2.24\win64\nssm.exe"
    if (Test-Path -LiteralPath $expanded) {
        return $expanded
    }

    $zip = Join-Path $PSScriptRoot "packages\nssm-2.24.zip"
    if (Test-Path -LiteralPath $zip) {
        Expand-Archive -LiteralPath $zip -DestinationPath (Join-Path $PSScriptRoot "packages") -Force
        if (Test-Path -LiteralPath $expanded) {
            return $expanded
        }
    }

    $cmd = Get-Command nssm.exe -ErrorAction SilentlyContinue
    if ($cmd) {
        return $cmd.Source
    }
    throw "nssm.exe not found. Download it with installer\download-prerequisites.ps1 or install NSSM manually."
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

& $nssm start $ServiceName | Out-Null

Write-Host "Windows service is running: $ServiceName"
Get-Service -Name $ServiceName
