$ErrorActionPreference = "Stop"

function Ensure-Admin {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = New-Object Security.Principal.WindowsPrincipal($identity)
    if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
        throw "请右键以管理员身份运行 ERP-Uninstall.exe"
    }
}

Ensure-Admin

$serviceName = "ERPWeb"
$tasks = @(
    "ERP Backup Daily",
    "ERP Verify Backups",
    "ERP Process Events",
    "ERP Cleanup Backups",
    "ERP Restore Drill"
)

Write-Host "本卸载器只删除 Windows 服务和计划任务，不删除数据库、附件、备份和日志。"
$confirm = Read-Host "确认卸载 ERP 服务？输入 UNINSTALL 继续"
if ($confirm -ne "UNINSTALL") {
    Write-Host "已取消。"
    exit 0
}

$nssm = Join-Path $PSScriptRoot "packages\nssm-2.24\win64\nssm.exe"
if (-not (Test-Path -LiteralPath $nssm)) {
    $zip = Join-Path $PSScriptRoot "packages\nssm-2.24.zip"
    if (Test-Path -LiteralPath $zip) {
        Expand-Archive -LiteralPath $zip -DestinationPath (Join-Path $PSScriptRoot "packages") -Force
    }
}
if (-not (Test-Path -LiteralPath $nssm)) {
    $cmd = Get-Command nssm.exe -ErrorAction SilentlyContinue
    if ($cmd) {
        $nssm = $cmd.Source
    }
}

if (Get-Service -Name $serviceName -ErrorAction SilentlyContinue) {
    if (Test-Path -LiteralPath $nssm) {
        & $nssm stop $serviceName | Out-Null
        & $nssm remove $serviceName confirm | Out-Null
    } else {
        Stop-Service -Name $serviceName -ErrorAction SilentlyContinue
        sc.exe delete $serviceName | Out-Host
    }
    Write-Host "已删除服务：$serviceName"
}

foreach ($task in $tasks) {
    schtasks.exe /Delete /TN $task /F 2>$null | Out-Null
}
Write-Host "已删除 ERP 计划任务。"

Read-Host "按回车退出"
