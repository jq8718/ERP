param(
    [string]$DistDir = "$PSScriptRoot\dist",
    [string]$WorkDir = "$PSScriptRoot\work\iexpress"
)

$ErrorActionPreference = "Stop"

function New-Directory {
    param([string]$Path)
    if (-not (Test-Path -LiteralPath $Path)) {
        New-Item -ItemType Directory -Path $Path | Out-Null
    }
}

function Copy-ErpPayload {
    param([string]$PayloadDir)

    $repoRoot = Split-Path -Parent $PSScriptRoot
    New-Directory -Path $PayloadDir
    robocopy $repoRoot $PayloadDir /E /XD .git .venv .tmp backups logs logs-test media staticfiles dist work __pycache__ /XF .env db.sqlite3 *.pyc | Out-Host
    if ($LASTEXITCODE -ge 8) {
        throw "robocopy failed with exit code $LASTEXITCODE"
    }
}

function New-IExpressSed {
    param(
        [string]$SedPath,
        [string]$PackageName,
        [string]$OutputExe,
        [string]$InstallProgram
    )

    $files = Get-ChildItem -LiteralPath $PackageName -Recurse -File
    $fileList = @()
    $index = 0
    foreach ($file in $files) {
        $baseUri = [Uri]((Resolve-Path -LiteralPath $PackageName).Path.TrimEnd("\") + "\")
        $fileUri = [Uri]$file.FullName
        $relative = [Uri]::UnescapeDataString($baseUri.MakeRelativeUri($fileUri).ToString()).Replace("/", "\")
        $fileList += "FILE$index=`"$relative`""
        $index += 1
    }

    $sed = @"
[Version]
Class=IEXPRESS
SEDVersion=3
[Options]
PackagePurpose=InstallApp
ShowInstallProgramWindow=1
HideExtractAnimation=0
UseLongFileName=1
InsideCompressed=0
CAB_FixedSize=0
CAB_ResvCodeSigning=0
RebootMode=N
InstallPrompt=
DisplayLicense=
FinishMessage=
TargetName=$OutputExe
FriendlyName=$([IO.Path]::GetFileNameWithoutExtension($OutputExe))
AppLaunched=$InstallProgram
PostInstallCmd=<None>
AdminQuietInstCmd=
UserQuietInstCmd=
SourceFiles=SourceFiles
[Strings]
`"InstallProgram`"=`"$InstallProgram`"
[SourceFiles]
SourceFiles0=$PackageName
[SourceFiles0]
$($fileList -join "`r`n")
"@
    Set-Content -LiteralPath $SedPath -Value $sed -Encoding ASCII
}

function Build-Package {
    param(
        [string]$Name,
        [string]$Launcher
    )

    $payloadDir = Join-Path $WorkDir $Name
    if (Test-Path -LiteralPath $payloadDir) {
        Remove-Item -LiteralPath $payloadDir -Recurse -Force
    }
    Copy-ErpPayload -PayloadDir $payloadDir

    $bootstrap = Join-Path $payloadDir "run-$Name.cmd"
    $launcherRelative = "installer\$Launcher"
    $cmd = @"
@echo off
cd /d %~dp0
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0$launcherRelative"
"@
    Set-Content -LiteralPath $bootstrap -Value $cmd -Encoding ASCII

    $outputExe = Join-Path $DistDir "$Name.exe"
    $sedPath = Join-Path $WorkDir "$Name.sed"
    New-IExpressSed -SedPath $sedPath -PackageName $payloadDir -OutputExe $outputExe -InstallProgram "run-$Name.cmd"

    & "$env:SystemRoot\System32\iexpress.exe" /N /Q $sedPath
    if (-not (Test-Path -LiteralPath $outputExe)) {
        throw "Failed to build $outputExe"
    }
    Write-Host "Built: $outputExe"
}

New-Directory -Path $DistDir
New-Directory -Path $WorkDir

Build-Package -Name "ERP-Setup" -Launcher "erp-setup-launcher.ps1"
Build-Package -Name "ERP-Uninstall" -Launcher "erp-uninstall-launcher.ps1"

Get-ChildItem -LiteralPath $DistDir | Select-Object Name, Length, LastWriteTime | Format-Table -AutoSize
