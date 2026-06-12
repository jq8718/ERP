param(
    [string]$OutputDir = "$(Split-Path -Parent $PSScriptRoot)\ERP安装包"
)

$ErrorActionPreference = "Stop"

$repoRoot = Split-Path -Parent $PSScriptRoot
if (-not (Test-Path -LiteralPath $OutputDir)) {
    New-Item -ItemType Directory -Path $OutputDir | Out-Null
}

robocopy $repoRoot $OutputDir /MIR `
    /XD .git .venv .tmp backups logs logs-test media staticfiles dist work __pycache__ tests_safety "ERP安装包" `
    /XF .env db.sqlite3 *.pyc "~*.DDF" "ERP模块建设计划.md" | Out-Host

if ($LASTEXITCODE -ge 8) {
    throw "robocopy failed with exit code $LASTEXITCODE"
}

$cleanupDirs = @(
    "installer\dist",
    "installer\work",
    ".git",
    ".venv",
    ".tmp",
    "backups",
    "logs",
    "logs-test",
    "media",
    "staticfiles",
    "tests_safety"
)

$resolvedOutput = (Resolve-Path -LiteralPath $OutputDir).Path
foreach ($relative in $cleanupDirs) {
    $path = Join-Path $OutputDir $relative
    if (Test-Path -LiteralPath $path) {
        $resolvedPath = (Resolve-Path -LiteralPath $path).Path
        if (-not $resolvedPath.StartsWith($resolvedOutput, [System.StringComparison]::OrdinalIgnoreCase)) {
            throw "Refusing to remove path outside package directory: $resolvedPath"
        }
        Remove-Item -LiteralPath $path -Recurse -Force
    }
}

$cleanupFiles = @(
    "ERP模块建设计划.md"
)

foreach ($relative in $cleanupFiles) {
    $path = Join-Path $OutputDir $relative
    if (Test-Path -LiteralPath $path) {
        $resolvedPath = (Resolve-Path -LiteralPath $path).Path
        if (-not $resolvedPath.StartsWith($resolvedOutput, [System.StringComparison]::OrdinalIgnoreCase)) {
            throw "Refusing to remove path outside package directory: $resolvedPath"
        }
        Remove-Item -LiteralPath $path -Force
    }
}

$required = @(
    "ERP-Setup.exe",
    "ERP-Uninstall.exe",
    "manage.py",
    "requirements.txt",
    "installer\packages\python-3.12.10-amd64.exe",
    "installer\packages\postgresql-17.10-1-windows-x64.exe",
    "installer\packages\nssm-2.24.zip",
    "installer\templates\intranet.env.template",
    "docs\Windows10内网ERP一键安装手册.md"
)

foreach ($relative in $required) {
    $path = Join-Path $OutputDir $relative
    if (-not (Test-Path -LiteralPath $path)) {
        throw "Missing required package file: $relative"
    }
}

Write-Host ""
Write-Host "ERP install package is ready:"
Get-Item -LiteralPath $OutputDir | Select-Object FullName, LastWriteTime | Format-Table -AutoSize
Get-ChildItem -LiteralPath $OutputDir | Select-Object Name, Length, LastWriteTime | Format-Table -AutoSize
