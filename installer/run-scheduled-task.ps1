param(
    [string]$InstallDir = "D:\ERP\app",
    [Parameter(Mandatory = $true)]
    [string]$CommandName
)

$ErrorActionPreference = "Stop"

$python = Join-Path $InstallDir ".venv\Scripts\python.exe"
$manage = Join-Path $InstallDir "manage.py"
$logDir = Join-Path $InstallDir "logs"

if (-not (Test-Path -LiteralPath $python)) {
    throw "Python virtualenv not found: $python"
}
if (-not (Test-Path -LiteralPath $manage)) {
    throw "manage.py not found: $manage"
}
if (-not (Test-Path -LiteralPath $logDir)) {
    New-Item -ItemType Directory -Path $logDir | Out-Null
}

$safeCommandName = $CommandName -replace "[^A-Za-z0-9_-]", "_"
$logPath = Join-Path $logDir "scheduled-$safeCommandName.log"
$stamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"

Push-Location $InstallDir
try {
    Add-Content -LiteralPath $logPath -Encoding UTF8 -Value "[$stamp] START $CommandName"
    & $python $manage $CommandName --trigger schedule *>> $logPath
    $exitCode = $LASTEXITCODE
    $stamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    Add-Content -LiteralPath $logPath -Encoding UTF8 -Value "[$stamp] END $CommandName exit=$exitCode"
    exit $exitCode
} catch {
    $stamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    Add-Content -LiteralPath $logPath -Encoding UTF8 -Value "[$stamp] ERROR $CommandName $($_.Exception.Message)"
    exit 1
} finally {
    Pop-Location
}
