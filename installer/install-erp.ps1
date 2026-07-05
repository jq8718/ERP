param(
    [string]$InstallDir = "D:\ERP\app",
    [string]$DataDir = "D:\ERP\data",
    [string]$BackupDir = "D:\ERP\backups",
    [string]$LogDir = "D:\ERP\logs",
    [string]$ServerHost = "",
    [string]$PostgresDb = "erp_db",
    [string]$PostgresUser = "erp_app",
    [string]$PostgresHost = "127.0.0.1",
    [string]$PostgresPort = "5432",
    [string]$RiskAcceptedBy = "System Administrator",
    [string]$AdminUsername = "admin",
    [string]$AdminEmail = "admin@example.com",
    [string]$AdminDisplayName = "System Administrator",
    [switch]$RunReleaseGate,
    [switch]$SkipTests,
    [switch]$AllowOnlinePipInstall
)

$ErrorActionPreference = "Stop"

function New-Directory {
    param([string]$Path)
    if (-not (Test-Path -LiteralPath $Path)) {
        New-Item -ItemType Directory -Path $Path | Out-Null
    }
}

function New-RandomSecret {
    $bytes = New-Object byte[] 48
    $rng = New-Object System.Security.Cryptography.RNGCryptoServiceProvider
    try {
        $rng.GetBytes($bytes)
        return [Convert]::ToBase64String($bytes)
    } finally {
        $rng.Dispose()
    }
}

function Write-Utf8NoBom {
    param(
        [string]$Path,
        [string]$Content
    )
    $encoding = New-Object System.Text.UTF8Encoding($false)
    [System.IO.File]::WriteAllText($Path, $Content, $encoding)
}

function Get-EnvFileValue {
    param(
        [string]$Path,
        [string]$Name
    )
    if (-not (Test-Path -LiteralPath $Path)) {
        return ""
    }
    foreach ($line in Get-Content -LiteralPath $Path) {
        $trimmed = $line.TrimStart()
        if ($trimmed.StartsWith("#") -or -not $line.Contains("=")) {
            continue
        }
        $index = $line.IndexOf("=")
        $key = $line.Substring(0, $index).Trim()
        if ($key -eq $Name) {
            return $line.Substring($index + 1)
        }
    }
    return ""
}

function Test-MissingEnvValue {
    param([string]$Value)
    return [string]::IsNullOrWhiteSpace($Value) -or $Value.Trim().StartsWith("{{")
}

function Set-EnvFileValues {
    param(
        [string]$Path,
        [hashtable]$Values
    )

    $lines = New-Object System.Collections.Generic.List[string]
    if (Test-Path -LiteralPath $Path) {
        foreach ($line in Get-Content -LiteralPath $Path) {
            $lines.Add($line)
        }
    }

    $seen = @{}
    for ($i = 0; $i -lt $lines.Count; $i++) {
        $line = $lines[$i]
        $trimmed = $line.TrimStart()
        if ($trimmed.StartsWith("#") -or -not $line.Contains("=")) {
            continue
        }
        $index = $line.IndexOf("=")
        $key = $line.Substring(0, $index).Trim()
        if ($Values.ContainsKey($key)) {
            $lines[$i] = "$key=$($Values[$key])"
            $seen[$key] = $true
        }
    }

    foreach ($key in $Values.Keys) {
        if (-not $seen.ContainsKey($key)) {
            $lines.Add("$key=$($Values[$key])")
        }
    }

    $encoding = New-Object System.Text.UTF8Encoding($false)
    [System.IO.File]::WriteAllLines($Path, [string[]]$lines, $encoding)
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
        throw "Offline wheels folder not found: $WheelDir. Copy installer\wheels from the ERP package, or rerun setup with -AllowOnlinePipInstall."
    }

    $wheelFiles = @(Get-ChildItem -LiteralPath $WheelDir -File | Where-Object {
        $_.Name -like "*.whl" -or $_.Name -like "*.tar.gz" -or $_.Name -like "*.zip"
    })
    if ($wheelFiles.Count -eq 0) {
        throw "Offline wheels folder is empty: $WheelDir. Copy installer\wheels from the ERP package, or rerun setup with -AllowOnlinePipInstall."
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
        throw "Offline wheels are incomplete. Missing packages: $($missing -join ', '). Add the missing wheel files to $WheelDir, or rerun setup with -AllowOnlinePipInstall."
    }

    return $wheelFiles
}

function Get-PackageVersion {
    param([string]$Root)
    $versionPath = Join-Path $Root "VERSION"
    if (-not (Test-Path -LiteralPath $versionPath)) {
        return ""
    }
    return (Get-Content -LiteralPath $versionPath -Raw).Trim()
}

function Read-RequiredSecret {
    param([string]$Prompt)
    $secure = Read-Host $Prompt -AsSecureString
    $bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
    try {
        return [Runtime.InteropServices.Marshal]::PtrToStringBSTR($bstr)
    } finally {
        [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr)
    }
}

if (-not $ServerHost) {
    $ServerHost = Read-Host "Enter server static IP or intranet host name, for example 192.168.1.10"
}

$repoRoot = Split-Path -Parent $PSScriptRoot
$sourceRoot = (Resolve-Path -LiteralPath $repoRoot).Path
$packageVersion = Get-PackageVersion -Root $sourceRoot
if ($packageVersion) {
    Write-Host "ERP package version: $packageVersion"
} else {
    Write-Host "ERP package version: not set"
}
$targetRoot = if (Test-Path -LiteralPath $InstallDir) { (Resolve-Path -LiteralPath $InstallDir).Path } else { "" }
if ($sourceRoot -ne $targetRoot) {
    Write-Host "Copying ERP files to $InstallDir ..."
    New-Directory -Path $InstallDir
    robocopy $repoRoot $InstallDir /E /XD .git .venv .tmp backups logs logs-test media staticfiles packages work __pycache__ /XF .env db.sqlite3 *.pyc | Out-Host
    if ($LASTEXITCODE -ge 8) {
        throw "robocopy failed with exit code $LASTEXITCODE"
    }
}

New-Directory -Path $DataDir
New-Directory -Path (Join-Path $DataDir "attachments")
New-Directory -Path $BackupDir
New-Directory -Path $LogDir

$envPath = Join-Path $InstallDir ".env"
if (-not (Test-Path -LiteralPath $envPath)) {
    $postgresPassword = Read-RequiredSecret "Enter PostgreSQL application user password for $PostgresUser"
    $adminPassword = Read-RequiredSecret "Enter ERP initial administrator password for $AdminUsername"
    $templatePath = Join-Path $InstallDir "installer\templates\intranet.env.template"
    if (-not (Test-Path -LiteralPath $templatePath)) {
        $templatePath = Join-Path $PSScriptRoot "templates\intranet.env.template"
    }
    $envContent = Get-Content -LiteralPath $templatePath -Raw
    $replacements = @{
        "{{DJANGO_SECRET_KEY}}" = (New-RandomSecret)
        "{{SERVER_HOST}}" = $ServerHost
        "{{POSTGRES_DB}}" = $PostgresDb
        "{{POSTGRES_USER}}" = $PostgresUser
        "{{POSTGRES_PASSWORD}}" = $postgresPassword
        "{{POSTGRES_HOST}}" = $PostgresHost
        "{{POSTGRES_PORT}}" = $PostgresPort
        "{{INSTALL_DIR}}" = $InstallDir
        "{{DATA_DIR}}" = $DataDir
        "{{BACKUP_DIR}}" = $BackupDir
        "{{LOG_DIR}}" = $LogDir
        "{{RISK_ACCEPTED_BY}}" = $RiskAcceptedBy
        "{{ADMIN_PASSWORD}}" = $adminPassword
    }
    foreach ($key in $replacements.Keys) {
        $envContent = $envContent.Replace($key, $replacements[$key])
    }
    Write-Utf8NoBom -Path $envPath -Content $envContent
    Write-Host "Created .env: $envPath"
} else {
    Write-Host ".env already exists; repairing deployment keys and keeping secrets."

    $postgresPassword = Get-EnvFileValue -Path $envPath -Name "POSTGRES_PASSWORD"
    if (Test-MissingEnvValue -Value $postgresPassword) {
        $postgresPassword = Read-RequiredSecret "Enter PostgreSQL application user password for $PostgresUser"
    }

    $secretKey = Get-EnvFileValue -Path $envPath -Name "DJANGO_SECRET_KEY"
    if (Test-MissingEnvValue -Value $secretKey) {
        $secretKey = New-RandomSecret
    }

    $updates = @{
        "DJANGO_ENV" = "production"
        "DJANGO_SECRET_KEY" = $secretKey
        "DJANGO_DEBUG" = "false"
        "DJANGO_ALLOWED_HOSTS" = $ServerHost
        "DJANGO_SESSION_COOKIE_SECURE" = "false"
        "DJANGO_CSRF_COOKIE_SECURE" = "false"
        "DJANGO_CSRF_TRUSTED_ORIGINS" = ""
        "DJANGO_SECURE_SSL_REDIRECT" = "false"
        "DJANGO_SECURE_HSTS_SECONDS" = "0"
        "DJANGO_SECURE_HSTS_INCLUDE_SUBDOMAINS" = "false"
        "DJANGO_SECURE_HSTS_PRELOAD" = "false"
        "DJANGO_LOGIN_URL" = "/login/"
        "DJANGO_LOGIN_REDIRECT_URL" = "/"
        "DJANGO_SESSION_COOKIE_AGE" = "28800"
        "DJANGO_LOG_LEVEL" = "INFO"
        "DB_ENGINE" = "postgres"
        "POSTGRES_DB" = $PostgresDb
        "POSTGRES_USER" = $PostgresUser
        "POSTGRES_PASSWORD" = $postgresPassword
        "POSTGRES_HOST" = $PostgresHost
        "POSTGRES_PORT" = $PostgresPort
        "DJANGO_STATIC_ROOT" = (Join-Path $InstallDir "staticfiles")
        "ERP_ATTACHMENT_DIR" = (Join-Path $DataDir "attachments")
        "ERP_BACKUP_DIR" = $BackupDir
        "DJANGO_LOG_DIR" = $LogDir
        "ERP_MAX_CSV_IMPORT_SIZE" = "5242880"
        "ERP_MAX_CSV_IMPORT_ROWS" = "5000"
        "ERP_ATTACHMENT_SCAN_COMMAND" = ""
        "ERP_ATTACHMENT_SCAN_TIMEOUT" = "30"
        "ERP_ATTACHMENT_SCAN_RISK_ACCEPTED_BY" = $RiskAcceptedBy
        "ERP_INTRANET_HTTP_RISK_ACCEPTED_BY" = $RiskAcceptedBy
        "ERP_RELEASE_GATE_REPORT_FILE" = "docs/latest-release-gate-report.md"
        "ERP_RELEASE_GATE_MAX_AGE_HOURS" = "24"
        "ERP_PRELAUNCH_BACKUP_MAX_AGE_HOURS" = "24"
        "ERP_PRELAUNCH_BACKUP_VERIFY_MAX_AGE_HOURS" = "24"
        "ERP_PRELAUNCH_RESTORE_DRILL_MAX_AGE_HOURS" = "168"
        "ERP_PRELAUNCH_EVENT_PROCESS_MAX_AGE_MINUTES" = "30"
        "ERP_PENDING_EVENT_MAX_RETRIES" = "3"
        "ERP_PENDING_EVENT_RETRY_BASE_MINUTES" = "5"
        "ERP_PENDING_EVENT_RUNNING_TIMEOUT_MINUTES" = "30"
        "ERP_BACKGROUND_JOB_RUNNING_TIMEOUT_MINUTES" = "120"
    }
    Set-EnvFileValues -Path $envPath -Values $updates
    Write-Host "Repaired .env deployment keys: $envPath"
}

Push-Location $InstallDir
try {
    if (-not (Test-Path -LiteralPath ".\.venv\Scripts\python.exe")) {
        Invoke-Checked "Create Python virtual environment" { py -3.12 -m venv .venv }
    }
    $wheelDir = Join-Path $InstallDir "installer\wheels"
    try {
        Assert-OfflineWheels -WheelDir $wheelDir -RequirementsPath (Join-Path $InstallDir "requirements.txt") | Out-Null
        Write-Host "Installing Python dependencies from offline wheels: $wheelDir"
        Invoke-Checked "Install Python dependencies from offline wheels" {
            .\.venv\Scripts\pip.exe install --no-index --find-links $wheelDir -r requirements.txt
        }
    } catch {
        if (-not $AllowOnlinePipInstall) {
            throw
        }
        Write-Host $_.Exception.Message
        Write-Host "Offline wheels not found. Installing Python dependencies from configured pip index."
        Invoke-Checked "Install Python dependencies from pip index" {
            .\.venv\Scripts\pip.exe install -r requirements.txt
        }
    }

    Invoke-Checked "Apply database migrations" { .\.venv\Scripts\python.exe manage.py migrate }
    Invoke-Checked "Collect static files" { .\.venv\Scripts\python.exe manage.py collectstatic --noinput }
    Invoke-Checked "Bootstrap ERP administrator" {
        .\.venv\Scripts\python.exe manage.py bootstrap_admin --username $AdminUsername --email $AdminEmail --display-name $AdminDisplayName --password-env ERP_BOOTSTRAP_ADMIN_PASSWORD --noinput
    }
    Invoke-Checked "Check ERP administrator" {
        .\.venv\Scripts\python.exe manage.py bootstrap_admin --username $AdminUsername --check-only
    }
    Invoke-Checked "Run production preflight" {
        .\.venv\Scripts\python.exe manage.py production_preflight --strict --skip-release-gate-report
    }
    if ($packageVersion) {
        Invoke-Checked "Record ERP release version" {
            .\.venv\Scripts\python.exe manage.py record_release $packageVersion --summary "ERP initial installation" --released-by $AdminUsername --ignore-existing
        }
    } else {
        Write-Host "Release version record skipped because VERSION file is missing."
    }

    if ($RunReleaseGate -and -not $SkipTests) {
        Invoke-Checked "Run release gate" {
            .\.venv\Scripts\python.exe manage.py release_gate --operator $AdminUsername --include-tests --report-file docs\latest-release-gate-report.md
        }
    } else {
        Write-Host "Release gate skipped during server installation. Run it on the release/development machine before packaging."
    }
} finally {
    Pop-Location
}

Write-Host ""
Write-Host "ERP installation finished."
Write-Host "Next: run installer\register-windows-service.ps1, then open http://$ServerHost`:8000/"
