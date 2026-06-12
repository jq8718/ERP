$ErrorActionPreference = "Stop"

function Test-Command {
    param([string]$Command)
    return [bool](Get-Command $Command -ErrorAction SilentlyContinue)
}

function Write-Result {
    param(
        [string]$Name,
        [bool]$Ok,
        [string]$Message
    )
    if ($Ok) {
        Write-Host "[OK]   $Name - $Message" -ForegroundColor Green
    } else {
        Write-Host "[MISS] $Name - $Message" -ForegroundColor Yellow
    }
}

$pythonOk = Test-Command "py"
if ($pythonOk) {
    $pythonVersion = (& py -3.12 --version 2>$null)
    Write-Result "Python 3.12" ($LASTEXITCODE -eq 0) ($pythonVersion -join " ")
} else {
    Write-Result "Python launcher" $false "py.exe not found"
}

$gitOk = Test-Command "git"
if ($gitOk) {
    Write-Result "Git" $true ((& git --version) -join " ")
} else {
    Write-Result "Git" $false "git.exe not found"
}

$psqlCandidates = @(
    "C:\Program Files\PostgreSQL\17\bin\psql.exe",
    "C:\Program Files\PostgreSQL\16\bin\psql.exe",
    "C:\Program Files\PostgreSQL\15\bin\psql.exe"
)
$psqlPath = $psqlCandidates | Where-Object { Test-Path -LiteralPath $_ } | Select-Object -First 1
if ($psqlPath) {
    Write-Result "PostgreSQL client" $true ((& $psqlPath --version) -join " ")
} else {
    Write-Result "PostgreSQL client" $false "psql.exe not found in Program Files"
}

$pgService = Get-Service -Name "postgresql*" -ErrorAction SilentlyContinue | Select-Object -First 1
if ($pgService) {
    Write-Result "PostgreSQL service" ($pgService.Status -eq "Running") "$($pgService.Name) / $($pgService.Status)"
} else {
    Write-Result "PostgreSQL service" $false "No postgresql* service found"
}

$nssmPath = Join-Path $PSScriptRoot "packages\nssm-2.24\win64\nssm.exe"
$nssmZip = Join-Path $PSScriptRoot "packages\nssm-2.24.zip"
if (Test-Path -LiteralPath $nssmPath) {
    Write-Result "NSSM" $true $nssmPath
} elseif (Test-Path -LiteralPath $nssmZip) {
    Write-Result "NSSM" $true "Zip exists; register script can extract it"
} else {
    Write-Result "NSSM" $false "nssm package not found"
}

Write-Host ""
Write-Host "If any required item is missing, install it from installer\packages, then rerun this script."
