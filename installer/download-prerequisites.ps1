param(
    [string]$PackageDir = "$PSScriptRoot\packages"
)

$ErrorActionPreference = "Stop"

function New-Directory {
    param([string]$Path)
    if (-not (Test-Path -LiteralPath $Path)) {
        New-Item -ItemType Directory -Path $Path | Out-Null
    }
}

function Download-File {
    param(
        [string]$Name,
        [string]$Url,
        [string]$Output
    )

    Write-Host "Downloading $Name..."
    if (Test-Path -LiteralPath $Output) {
        Write-Host "  Exists: $Output"
        return
    }

    Invoke-WebRequest -Uri $Url -OutFile $Output -UseBasicParsing
    Write-Host "  Saved: $Output"
}

New-Directory -Path $PackageDir

$packages = @(
    @{
        name = "Python 3.12.10 x64"
        file = "python-3.12.10-amd64.exe"
        url = "https://www.python.org/ftp/python/3.12.10/python-3.12.10-amd64.exe"
        note = "Install for all users. Add python.exe to PATH."
    },
    @{
        name = "PostgreSQL 17.10 Windows x64"
        file = "postgresql-17.10-1-windows-x64.exe"
        url = "https://get.enterprisedb.com/postgresql/postgresql-17.10-1-windows-x64.exe"
        note = "Install PostgreSQL service. Remember postgres superuser password."
    },
    @{
        name = "Git for Windows 2.51.0 x64"
        file = "Git-2.51.0-64-bit.exe"
        url = "https://github.com/git-for-windows/git/releases/download/v2.51.0.windows.1/Git-2.51.0-64-bit.exe"
        note = "Needed only if deploying by git clone or git pull on server."
    },
    @{
        name = "NSSM 2.24"
        file = "nssm-2.24.zip"
        url = "https://nssm.cc/release/nssm-2.24.zip"
        note = "Used to register Waitress as a Windows service."
    }
)

foreach ($pkg in $packages) {
    $output = Join-Path $PackageDir $pkg.file
    Download-File -Name $pkg.name -Url $pkg.url -Output $output
}

$manifestPath = Join-Path $PackageDir "manifest.json"
$manifest = [ordered]@{
    generated_at = (Get-Date).ToString("s")
    packages = $packages
}
$manifest | ConvertTo-Json -Depth 5 | Set-Content -Path $manifestPath -Encoding UTF8

Write-Host ""
Write-Host "Prerequisite packages are ready:"
Get-ChildItem -LiteralPath $PackageDir | Select-Object Name, Length, LastWriteTime | Format-Table -AutoSize
