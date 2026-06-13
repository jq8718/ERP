param(
    [string]$ServerHost = "",
    [int]$Port = 8000,
    [string]$ShortcutName = "Open ERP System"
)

$ErrorActionPreference = "Stop"

if (-not $ServerHost) {
    $ServerHost = Read-Host "Enter server static IP or intranet host name, for example 192.168.1.10"
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
