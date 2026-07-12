param(
    [string]$InstallDir = "D:\ERP\app"
)

$ErrorActionPreference = "Stop"

$python = Join-Path $InstallDir ".venv\Scripts\python.exe"
$manage = Join-Path $InstallDir "manage.py"
$runner = Join-Path $PSScriptRoot "run-scheduled-task.ps1"
if (-not (Test-Path -LiteralPath $python)) {
    throw "Python virtualenv not found: $python"
}
if (-not (Test-Path -LiteralPath $manage)) {
    throw "manage.py not found: $manage"
}
if (-not (Test-Path -LiteralPath $runner)) {
    throw "Scheduled task runner not found: $runner"
}

function Register-ErpTask {
    param(
        [string]$Name,
        [string]$Arguments,
        [string]$Schedule,
        [string]$Time = "",
        [string]$Modifier = "",
        [string]$Day = ""
    )

    $taskName = "ERP $Name"
    $command = "powershell.exe -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$runner`" -InstallDir `"$InstallDir`" -CommandName `"$Arguments`""
    $args = @("/Create", "/TN", $taskName, "/SC", $Schedule, "/TR", $command, "/F")
    if ($Time) {
        $args += @("/ST", $Time)
    }
    if ($Modifier) {
        $args += @("/MO", $Modifier)
    }
    if ($Day) {
        $args += @("/D", $Day)
    }
    & schtasks.exe @args | Out-Host
}

Register-ErpTask -Name "Backup Daily" -Arguments "backup_daily" -Schedule "DAILY" -Time "02:00"
Register-ErpTask -Name "Verify Backups" -Arguments "verify_backups" -Schedule "DAILY" -Time "03:00"
Register-ErpTask -Name "Process Events" -Arguments "process_pending_events" -Schedule "MINUTE" -Modifier "5"
Register-ErpTask -Name "Cleanup Backups" -Arguments "cleanup_backups" -Schedule "WEEKLY" -Day "SUN" -Time "04:00"
Register-ErpTask -Name "Restore Drill" -Arguments "restore_drill" -Schedule "WEEKLY" -Day "SUN" -Time "05:00"

Write-Host "ERP scheduled tasks registered."
