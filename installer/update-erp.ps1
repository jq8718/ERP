param(
    [string]$InstallDir = "D:\ERP\app",
    [string]$ServiceName = "ERPWeb",
    [switch]$AllowOnlinePipInstall,
    [switch]$SkipServiceRestart
)

$ErrorActionPreference = "Stop"

function New-Directory {
    param([string]$Path)
    if (-not (Test-Path -LiteralPath $Path)) {
        New-Item -ItemType Directory -Path $Path | Out-Null
    }
}

function Invoke-Checked {
    param(
        [string]$Step,
        [scriptblock]$Command
    )
    Write-Host "[RUN] $Step"
    & $Command
    $exitCode = $LASTEXITCODE
    if ($null -ne $exitCode -and $exitCode -ne 0) {
        throw "$Step failed with exit code $exitCode"
    }
}

function Get-RequirementNames {
    param([string]$Path)
    $names = @()
    foreach ($line in Get-Content -LiteralPath $Path) {
        $trimmed = $line.Trim()
        if (-not $trimmed -or $trimmed.StartsWith("#")) {
            continue
        }
        if ($trimmed -match "^\s*([A-Za-z0-9_.-]+)\s*==") {
            $names += $Matches[1].ToLowerInvariant().Replace("-", "_")
        }
    }
    return $names
}

function Assert-OfflineWheels {
    param(
        [string]$WheelDir,
        [string]$RequirementsPath
    )

    if (-not (Test-Path -LiteralPath $WheelDir)) {
        throw "Offline wheels folder not found: $WheelDir. Copy installer\wheels from the ERP package, or rerun update with -AllowOnlinePipInstall."
    }

    $wheelFiles = @(Get-ChildItem -LiteralPath $WheelDir -File | Where-Object {
        $_.Name -like "*.whl" -or $_.Name -like "*.tar.gz" -or $_.Name -like "*.zip"
    })
    if ($wheelFiles.Count -eq 0) {
        throw "Offline wheels folder is empty: $WheelDir. Copy installer\wheels from the ERP package, or rerun update with -AllowOnlinePipInstall."
    }

    $wheelNames = @($wheelFiles | ForEach-Object { $_.Name.ToLowerInvariant().Replace("-", "_") })
    $missing = @()
    foreach ($requirementName in Get-RequirementNames -Path $RequirementsPath) {
        $matched = $wheelNames | Where-Object { $_.StartsWith("$requirementName`_") } | Select-Object -First 1
        if (-not $matched) {
            $missing += $requirementName
        }
    }

    if ($missing.Count -gt 0) {
        throw "Offline wheels are incomplete. Missing packages: $($missing -join ', '). Add missing wheel files to $WheelDir, or rerun update with -AllowOnlinePipInstall."
    }
}

function Find-Nssm {
    param([string]$PackageRoot, [string]$TargetRoot)
    $candidates = @()
    if ($env:ERP_NSSM_EXE) {
        $candidates += $env:ERP_NSSM_EXE
    }
    $candidates += (Join-Path $PackageRoot "installer\tools\nssm.exe")
    $candidates += (Join-Path $TargetRoot "installer\tools\nssm.exe")
    $cmd = Get-Command nssm.exe -ErrorAction SilentlyContinue
    if ($cmd) {
        $candidates += $cmd.Source
    }
    foreach ($candidate in $candidates) {
        if ($candidate -and (Test-Path -LiteralPath $candidate)) {
            return $candidate
        }
    }
    return ""
}

function Get-PackageVersion {
    param([string]$Root)
    $versionPath = Join-Path $Root "VERSION"
    if (-not (Test-Path -LiteralPath $versionPath)) {
        return ""
    }
    return (Get-Content -LiteralPath $versionPath -Raw).Trim()
}

function Stop-ErpService {
    param([string]$Name, [string]$Nssm)
    $service = Get-Service -Name $Name -ErrorAction SilentlyContinue
    if (-not $service) {
        Write-Host "Service not found; skip stop: $Name"
        return
    }
    if ($service.Status -eq "Stopped") {
        Write-Host "Service already stopped: $Name"
        return
    }
    if ($Nssm) {
        & $Nssm stop $Name | Out-Host
    } else {
        Stop-Service -Name $Name -Force -ErrorAction Stop
    }
    $service.WaitForStatus("Stopped", "00:00:30")
    Write-Host "Service stopped: $Name"
}

if (-not (Test-Path -LiteralPath $InstallDir)) {
    throw "Install directory not found: $InstallDir"
}
if (-not (Test-Path -LiteralPath (Join-Path $InstallDir ".env"))) {
    throw "Existing .env not found in install directory: $InstallDir"
}

$repoRoot = Split-Path -Parent $PSScriptRoot
$sourceRoot = (Resolve-Path -LiteralPath $repoRoot).Path
$targetRoot = (Resolve-Path -LiteralPath $InstallDir).Path
if ($sourceRoot.StartsWith($targetRoot + "\", [System.StringComparison]::OrdinalIgnoreCase)) {
    throw "Update package is inside the ERP install directory. Move the whole update package to a separate folder, for example D:\ERP-update, and rerun ERP-Update.exe."
}
$packageDirName = -join ([char[]](0x0045, 0x0052, 0x0050, 0x5B89, 0x88C5, 0x5305))
$planFileName = -join ([char[]](0x0045, 0x0052, 0x0050, 0x6A21, 0x5757, 0x5EFA, 0x8BBE, 0x8BA1, 0x5212, 0x002E, 0x006D, 0x0064))
$zipFileName = "$packageDirName.zip"
$packageVersion = Get-PackageVersion -Root $sourceRoot
if ($packageVersion) {
    Write-Host "ERP package version: $packageVersion"
} else {
    Write-Host "ERP package version: not set"
}

$nssm = Find-Nssm -PackageRoot $sourceRoot -TargetRoot $targetRoot
Stop-ErpService -Name $ServiceName -Nssm $nssm

$backupRoot = Join-Path (Split-Path -Parent $targetRoot) "update-backups"
New-Directory -Path $backupRoot
$stamp = Get-Date -Format "yyyyMMdd-HHmmss"
$backupDir = Join-Path $backupRoot "erp-app-before-update-$stamp"
Write-Host "Backing up current application to $backupDir ..."
New-Directory -Path $backupDir
robocopy $targetRoot $backupDir /E /XD .git .venv .tmp staticfiles media logs backups __pycache__ /XF *.pyc | Out-Host
if ($LASTEXITCODE -ge 8) {
    throw "backup robocopy failed with exit code $LASTEXITCODE"
}

if ($sourceRoot -ne $targetRoot) {
    Write-Host "Copying new ERP files to $targetRoot ..."
    $excludeDirs = @(".git", ".venv", ".tmp", "backups", "logs", "logs-test", "media", "staticfiles", "dist", "work", "__pycache__", "tests_safety", "packages", $packageDirName)
    $excludeFiles = @(".env", "db.sqlite3", "*.pyc", "~*.DDF", "*.docx", $planFileName, $zipFileName)
    $robocopyArgs = @($sourceRoot, $targetRoot, "/MIR", "/XD") + $excludeDirs + @("/XF") + $excludeFiles
    & robocopy @robocopyArgs | Out-Host
    if ($LASTEXITCODE -ge 8) {
        throw "update robocopy failed with exit code $LASTEXITCODE"
    }
} else {
    Write-Host "Update source and target are the same folder; skip file copy."
}

Push-Location $targetRoot
try {
    if (-not (Test-Path -LiteralPath ".\.venv\Scripts\python.exe")) {
        Invoke-Checked "Create Python virtual environment" { py -3.12 -m venv .venv }
    }

    $wheelDir = Join-Path $targetRoot "installer\wheels"
    try {
        Assert-OfflineWheels -WheelDir $wheelDir -RequirementsPath (Join-Path $targetRoot "requirements.txt")
        Invoke-Checked "Install or update Python dependencies from offline wheels" {
            .\.venv\Scripts\pip.exe install --no-index --find-links $wheelDir -r requirements.txt
        }
    } catch {
        if (-not $AllowOnlinePipInstall) {
            throw
        }
        Write-Host $_.Exception.Message
        Invoke-Checked "Install or update Python dependencies from pip index" {
            .\.venv\Scripts\pip.exe install -r requirements.txt
        }
    }

    Invoke-Checked "Apply database migrations" { .\.venv\Scripts\python.exe manage.py migrate }
    Invoke-Checked "Collect static files" { .\.venv\Scripts\python.exe manage.py collectstatic --noinput }
    Invoke-Checked "Run Django system check" { .\.venv\Scripts\python.exe manage.py check }
    if ($packageVersion) {
        Invoke-Checked "Record ERP release version" {
            .\.venv\Scripts\python.exe manage.py record_release $packageVersion --summary "ERP update package" --ignore-existing
        }
    } else {
        Write-Host "Release version record skipped because VERSION file is missing."
    }
} finally {
    Pop-Location
}

if (-not $SkipServiceRestart) {
    $registerScript = Join-Path $targetRoot "installer\register-windows-service.ps1"
    if (-not (Test-Path -LiteralPath $registerScript)) {
        throw "Service registration script not found after update: $registerScript"
    }
    & $registerScript -InstallDir $targetRoot -ServiceName $ServiceName
} else {
    Write-Host "Service restart skipped."
}

$scheduledTaskScript = Join-Path $targetRoot "installer\register-scheduled-tasks.ps1"
if (-not (Test-Path -LiteralPath $scheduledTaskScript)) {
    throw "Scheduled task registration script not found after update: $scheduledTaskScript"
}
& $scheduledTaskScript -InstallDir $targetRoot

Write-Host ""
Write-Host "ERP update finished."
Write-Host "Application backup: $backupDir"
