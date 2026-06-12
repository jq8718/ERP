param(
    [string]$ServerHost = "",
    [int]$Port = 8000,
    [string]$ShortcutName = "打开 ERP 系统"
)

$ErrorActionPreference = "Stop"

if (-not $ServerHost) {
    $ServerHost = Read-Host "请输入服务器固定 IP 或内网主机名，例如 192.168.1.10"
}

$desktop = [Environment]::GetFolderPath("Desktop")
$shortcutPath = Join-Path $desktop "$ShortcutName.url"
$url = "http://$ServerHost`:$Port/"

$content = @"
[InternetShortcut]
URL=$url
"@
Set-Content -LiteralPath $shortcutPath -Value $content -Encoding ASCII

Write-Host "Shortcut created: $shortcutPath"
Write-Host $url
