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
    [string]$RiskAcceptedBy = "系统管理员",
    [string]$AdminUsername = "admin",
    [string]$AdminEmail = "admin@example.com",
    [string]$AdminDisplayName = "系统管理员",
    [switch]$SkipTests
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
    [System.Security.Cryptography.RandomNumberGenerator]::Fill($bytes)
    return [Convert]::ToBase64String($bytes)
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
    $ServerHost = Read-Host "请输入服务器固定 IP 或内网主机名，例如 192.168.1.10"
}

$repoRoot = Split-Path -Parent $PSScriptRoot
$sourceRoot = (Resolve-Path -LiteralPath $repoRoot).Path
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
    $postgresPassword = Read-RequiredSecret "请输入 PostgreSQL 应用账号 $PostgresUser 的密码"
    $adminPassword = Read-RequiredSecret "请输入 ERP 初始管理员 $AdminUsername 的密码"
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
    Set-Content -LiteralPath $envPath -Value $envContent -Encoding UTF8
    Write-Host "Created .env: $envPath"
} else {
    Write-Host ".env already exists; keeping existing file."
}

Push-Location $InstallDir
try {
    if (-not (Test-Path -LiteralPath ".\.venv\Scripts\python.exe")) {
        py -3.12 -m venv .venv
    }
    .\.venv\Scripts\python.exe -m pip install --upgrade pip
    .\.venv\Scripts\pip.exe install -r requirements.txt

    .\.venv\Scripts\python.exe manage.py migrate
    .\.venv\Scripts\python.exe manage.py collectstatic --noinput
    .\.venv\Scripts\python.exe manage.py bootstrap_admin --username $AdminUsername --email $AdminEmail --display-name $AdminDisplayName --password-env ERP_BOOTSTRAP_ADMIN_PASSWORD --noinput
    .\.venv\Scripts\python.exe manage.py bootstrap_admin --username $AdminUsername --check-only
    .\.venv\Scripts\python.exe manage.py production_preflight --strict --skip-release-gate-report

    if (-not $SkipTests) {
        .\.venv\Scripts\python.exe manage.py release_gate --operator $AdminUsername --include-tests --report-file docs\latest-release-gate-report.md
    }
} finally {
    Pop-Location
}

Write-Host ""
Write-Host "ERP installation finished."
Write-Host "Next: run installer\register-windows-service.ps1, then open http://$ServerHost`:8000/"
