param(
    [string]$WheelDir = "$PSScriptRoot\wheels",
    [string]$Requirements = "$(Split-Path -Parent $PSScriptRoot)\requirements.txt",
    [string]$Python = "py"
)

$ErrorActionPreference = "Stop"

if (-not (Test-Path -LiteralPath $Requirements)) {
    throw "requirements.txt not found: $Requirements"
}

if (Test-Path -LiteralPath $WheelDir) {
    Remove-Item -LiteralPath $WheelDir -Recurse -Force
}
New-Item -ItemType Directory -Path $WheelDir | Out-Null

$pythonArgs = @()
if ((Split-Path -Leaf $Python) -ieq "py" -or (Split-Path -Leaf $Python) -ieq "py.exe") {
    $pythonArgs += "-3.12"
}
$pythonArgs += @("-m", "pip", "download", "--only-binary=:all:", "--dest", $WheelDir, "-r", $Requirements)

& $Python @pythonArgs
if ($LASTEXITCODE -ne 0) {
    throw "pip download failed with exit code $LASTEXITCODE"
}

Write-Host ""
Write-Host "Offline wheels are ready:"
Get-ChildItem -LiteralPath $WheelDir | Select-Object Name, Length, LastWriteTime | Format-Table -AutoSize
