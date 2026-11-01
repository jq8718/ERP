param(
    [Parameter(Mandatory = $true)][string]$BaseRef,
    [Parameter(Mandatory = $true)][string]$Version,
    [Parameter(Mandatory = $true)][string]$OutputDir
)

$ErrorActionPreference = "Stop"
$repoRoot = Split-Path -Parent $PSScriptRoot
if (Test-Path -LiteralPath $OutputDir) {
    throw "Output directory already exists; choose a new release directory: $OutputDir"
}
$resolvedParent = (Resolve-Path -LiteralPath (Split-Path -Parent $OutputDir)).Path
$resolvedRepo = (Resolve-Path -LiteralPath $repoRoot).Path
if ($resolvedParent.StartsWith($resolvedRepo + "\", [StringComparison]::OrdinalIgnoreCase)) {
    throw "Output directory must be outside the repository."
}

Push-Location $repoRoot
try {
    & git rev-parse --verify "$BaseRef^{commit}" | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "Unknown base ref: $BaseRef" }
    $changed = @(& git diff --name-only --diff-filter=ACMRT $BaseRef HEAD)
    if ($LASTEXITCODE -ne 0) { throw "git diff failed" }
    $paths = @($changed | Where-Object {
        $_ -match '^(accounts|approvals|bom|files|finance|inventory|masterdata|notifications|production|purchase|sales|system|templates|config)/' -or
        $_ -eq 'requirements.txt' -or
        $_ -eq 'manage.py' -or
        $_ -eq 'installer/update-erp.ps1' -or
        $_ -eq 'installer/erp-update-launcher.ps1' -or
        $_ -eq 'installer/register-scheduled-tasks.ps1'
    } | Where-Object { Test-Path -LiteralPath $_ -PathType Leaf })
    $paths += @('installer/update-erp.ps1', 'installer/erp-update-launcher.ps1', 'ERP-Update.exe')
    $paths = @($paths | Sort-Object -Unique)
    if ($paths.Count -lt 4) { throw "No application changes found since $BaseRef" }
    New-Item -ItemType Directory -Path $OutputDir | Out-Null
    foreach ($relative in $paths) {
        $target = Join-Path $OutputDir $relative
        New-Item -ItemType Directory -Force -Path (Split-Path -Parent $target) | Out-Null
        Copy-Item -LiteralPath (Join-Path $repoRoot $relative) -Destination $target
    }
    Set-Content -LiteralPath (Join-Path $OutputDir 'VERSION') -Encoding ascii -Value $Version
    Set-Content -LiteralPath (Join-Path $OutputDir 'INCREMENTAL_UPDATE') -Encoding ascii -Value $BaseRef
    $manifest = foreach ($relative in @($paths + 'VERSION' | Sort-Object -Unique)) {
        $hash = (Get-FileHash -Algorithm SHA256 -LiteralPath (Join-Path $OutputDir $relative)).Hash
        "$hash  $relative"
    }
    Set-Content -LiteralPath (Join-Path $OutputDir 'UPDATE_MANIFEST.sha256') -Encoding ascii -Value $manifest
    Write-Host "Incremental package ready: $OutputDir ($($paths.Count) files)"
} finally {
    Pop-Location
}
