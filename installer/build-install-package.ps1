param(
    [string]$OutputDir = $null
)

$ErrorActionPreference = "Stop"

$repoRoot = Split-Path -Parent $PSScriptRoot
$packageDirName = -join ([char[]](0x0045, 0x0052, 0x0050, 0x5B89, 0x88C5, 0x5305))
$planFileName = -join ([char[]](0x0045, 0x0052, 0x0050, 0x6A21, 0x5757, 0x5EFA, 0x8BBE, 0x8BA1, 0x5212, 0x002E, 0x006D, 0x0064))
$manualFileName = -join ([char[]](0x0057, 0x0069, 0x006E, 0x0064, 0x006F, 0x0077, 0x0073, 0x0031, 0x0030, 0x5185, 0x7F51, 0x0045, 0x0052, 0x0050, 0x4E00, 0x952E, 0x5B89, 0x88C5, 0x624B, 0x518C, 0x002E, 0x006D, 0x0064))
$zipFileName = "$packageDirName.zip"

if ([string]::IsNullOrWhiteSpace($OutputDir)) {
    $OutputDir = Join-Path $repoRoot $packageDirName
}

if (-not (Test-Path -LiteralPath $OutputDir)) {
    New-Item -ItemType Directory -Path $OutputDir | Out-Null
}

$excludeDirs = @(".git", ".venv", ".tmp", "backups", "logs", "logs-test", "media", "staticfiles", "dist", "work", "__pycache__", "tests_safety", "packages", $packageDirName)
$excludeFiles = @(".env", "db.sqlite3", "*.pyc", "~*.DDF", "*.docx", $planFileName, $zipFileName)
$robocopyArgs = @($repoRoot, $OutputDir, "/MIR", "/XD") + $excludeDirs + @("/XF") + $excludeFiles
& robocopy @robocopyArgs | Out-Host

if ($LASTEXITCODE -ge 8) {
    throw "robocopy failed with exit code $LASTEXITCODE"
}

$cleanupDirs = @(
    "installer\dist",
    "installer\work",
    "installer\packages",
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
    $planFileName,
    $zipFileName
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

$cleanupFilePatterns = @("*.docx")
foreach ($pattern in $cleanupFilePatterns) {
    Get-ChildItem -LiteralPath $OutputDir -Recurse -File -Filter $pattern | ForEach-Object {
        $resolvedPath = $_.FullName
        if (-not $resolvedPath.StartsWith($resolvedOutput, [System.StringComparison]::OrdinalIgnoreCase)) {
            throw "Refusing to remove path outside package directory: $resolvedPath"
        }
        Remove-Item -LiteralPath $resolvedPath -Force
    }
}

$installerLogsDir = Join-Path $OutputDir "installer\logs"
if (-not (Test-Path -LiteralPath $installerLogsDir)) {
    New-Item -ItemType Directory -Path $installerLogsDir | Out-Null
}
Set-Content -LiteralPath (Join-Path $installerLogsDir "README.txt") -Encoding UTF8 -Value @"
ERP installer logs will be written to this folder.

If ERP-Setup.exe or ERP-Uninstall.exe fails, send the newest .log file in this folder to the developer.
"@

$toolsDir = Join-Path $OutputDir "installer\tools"
if (-not (Test-Path -LiteralPath $toolsDir)) {
    New-Item -ItemType Directory -Path $toolsDir | Out-Null
}

$nssmSourceCandidates = @(
    (Join-Path $repoRoot "installer\packages\nssm-2.24\win64\nssm.exe"),
    (Join-Path $repoRoot "installer\packages\nssm.exe")
)
$nssmZip = Join-Path $repoRoot "installer\packages\nssm-2.24.zip"
$nssmSource = $nssmSourceCandidates | Where-Object { Test-Path -LiteralPath $_ } | Select-Object -First 1
if (-not $nssmSource -and (Test-Path -LiteralPath $nssmZip)) {
    $nssmExtractRoot = Join-Path ([System.IO.Path]::GetTempPath()) ("erp-nssm-" + [System.Guid]::NewGuid().ToString("N"))
    New-Item -ItemType Directory -Path $nssmExtractRoot | Out-Null
    try {
        Expand-Archive -LiteralPath $nssmZip -DestinationPath $nssmExtractRoot -Force
        $nssmSource = Join-Path $nssmExtractRoot "nssm-2.24\win64\nssm.exe"
        if (-not (Test-Path -LiteralPath $nssmSource)) {
            throw "nssm.exe not found after extracting $nssmZip"
        }
        Copy-Item -LiteralPath $nssmSource -Destination (Join-Path $toolsDir "nssm.exe") -Force
    } finally {
        if (Test-Path -LiteralPath $nssmExtractRoot) {
            Remove-Item -LiteralPath $nssmExtractRoot -Recurse -Force
        }
    }
} elseif ($nssmSource) {
    Copy-Item -LiteralPath $nssmSource -Destination (Join-Path $toolsDir "nssm.exe") -Force
}

$required = @(
    "ERP-Setup.exe",
    "ERP-Setup-Console.cmd",
    "ERP-Uninstall.exe",
    "ERP-Uninstall-Console.cmd",
    "manage.py",
    "requirements.txt",
    "installer\wheels\django-6.0.6-py3-none-any.whl",
    "installer\wheels\psycopg_binary-3.3.4-cp312-cp312-win_amd64.whl",
    "installer\templates\intranet.env.template",
    "installer\logs\README.txt",
    "installer\tools\nssm.exe",
    (Join-Path "docs" $manualFileName)
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
