$ErrorActionPreference = "Stop"

function Ensure-Admin {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = New-Object Security.Principal.WindowsPrincipal($identity)
    if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
        throw "请右键以管理员身份运行 ERP-Setup.exe"
    }
}

Ensure-Admin

$root = Split-Path -Parent $PSScriptRoot
$serverHost = Read-Host "请输入 ERP 服务器固定 IP 或内网主机名，例如 192.168.1.10"
if (-not $serverHost) {
    throw "服务器 IP 或主机名不能为空"
}

Push-Location $root
try {
    & "$root\installer\preflight-prerequisites.ps1"
    Write-Host ""
    Write-Host "如上方显示缺少 Python、PostgreSQL、PostgreSQL service 或 NSSM，请先从 installer\packages 手动安装后重新运行。"
    Write-Host "Git 是可选项；如果 ERP 文件夹已经完整复制到服务器，可以不安装 Git。"
    $continue = Read-Host "是否继续初始化 PostgreSQL 和 ERP？输入 Y 继续"
    if ($continue -ne "Y") {
        Write-Host "已取消。"
        exit 0
    }

    & "$root\installer\setup-postgres-db.ps1"
    & "$root\installer\install-erp.ps1" -ServerHost $serverHost
    & "$root\installer\register-windows-service.ps1"
    & "$root\installer\register-scheduled-tasks.ps1"
    & "$root\installer\create-desktop-shortcut.ps1" -ServerHost $serverHost

    Write-Host ""
    Write-Host "ERP 安装完成。访问地址：http://$serverHost`:8000/"
    Start-Process "http://$serverHost`:8000/"
} finally {
    Pop-Location
}

Read-Host "按回车退出"
