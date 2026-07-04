param(
    [string]$DistDir = "$PSScriptRoot\dist"
)

$ErrorActionPreference = "Stop"

function Find-Csc {
    $candidates = @(
        "$env:WINDIR\Microsoft.NET\Framework64\v4.0.30319\csc.exe",
        "$env:WINDIR\Microsoft.NET\Framework\v4.0.30319\csc.exe"
    )
    foreach ($candidate in $candidates) {
        if (Test-Path -LiteralPath $candidate) {
            return $candidate
        }
    }
    $cmd = Get-Command csc.exe -ErrorAction SilentlyContinue
    if ($cmd) {
        return $cmd.Source
    }
    throw "csc.exe not found. Cannot build launcher exe files."
}

if (-not (Test-Path -LiteralPath $DistDir)) {
    New-Item -ItemType Directory -Path $DistDir | Out-Null
}

$repoRoot = Split-Path -Parent $PSScriptRoot
$source = Join-Path $PSScriptRoot "launcher\ErpLauncher.cs"
$csc = Find-Csc

$setupDist = Join-Path $DistDir "ERP-Setup.exe"
$updateDist = Join-Path $DistDir "ERP-Update.exe"
$uninstallDist = Join-Path $DistDir "ERP-Uninstall.exe"

& $csc /nologo /target:winexe /out:$setupDist $source
& $csc /nologo /target:winexe /out:$updateDist $source
& $csc /nologo /target:winexe /out:$uninstallDist $source

Copy-Item -LiteralPath $setupDist -Destination (Join-Path $repoRoot "ERP-Setup.exe") -Force
Copy-Item -LiteralPath $updateDist -Destination (Join-Path $repoRoot "ERP-Update.exe") -Force
Copy-Item -LiteralPath $uninstallDist -Destination (Join-Path $repoRoot "ERP-Uninstall.exe") -Force

Get-Item -LiteralPath $setupDist, $updateDist, $uninstallDist, (Join-Path $repoRoot "ERP-Setup.exe"), (Join-Path $repoRoot "ERP-Update.exe"), (Join-Path $repoRoot "ERP-Uninstall.exe") |
    Select-Object FullName, Length, LastWriteTime |
    Format-Table -AutoSize
