param(
    [string]$PackageDir = "$PSScriptRoot\packages"
)

$ErrorActionPreference = "Stop"

$requiredFiles = @(
    "python-3.12.10-amd64.exe",
    "postgresql-17.10-1-windows-x64.exe",
    "Git-2.51.0-64-bit.exe",
    "nssm-2.24.zip"
)

$rows = @()
foreach ($file in $requiredFiles) {
    $path = Join-Path $PackageDir $file
    if (-not (Test-Path -LiteralPath $path)) {
        throw "Missing package: $path"
    }
    $item = Get-Item -LiteralPath $path
    $hash = Get-FileHash -Algorithm SHA256 -LiteralPath $path
    $rows += [pscustomobject]@{
        Name = $item.Name
        Length = $item.Length
        SHA256 = $hash.Hash
    }
}

$rows | Format-Table -AutoSize
Write-Host "All prerequisite packages exist."
