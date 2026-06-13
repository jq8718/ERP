param(
    [string]$PostgresSuperUser = "postgres",
    [string]$PostgresHost = "127.0.0.1",
    [string]$PostgresPort = "5432",
    [string]$Database = "erp_db",
    [string]$AppUser = "erp_app"
)

$ErrorActionPreference = "Stop"

function Find-Psql {
    $candidates = @(
        "C:\Program Files\PostgreSQL\17\bin\psql.exe",
        "C:\Program Files\PostgreSQL\16\bin\psql.exe",
        "C:\Program Files\PostgreSQL\15\bin\psql.exe"
    )
    foreach ($candidate in $candidates) {
        if (Test-Path -LiteralPath $candidate) {
            return $candidate
        }
    }
    $cmd = Get-Command psql.exe -ErrorAction SilentlyContinue
    if ($cmd) {
        return $cmd.Source
    }
    throw "psql.exe not found. Install PostgreSQL first."
}

function Read-RequiredSecret {
    param([string]$Prompt)
    $secure = Read-Host $Prompt -AsSecureString
    $bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
    try {
        return [Runtime.InteropServices.Marshal]::PtrToStringBSTR($bstr)
    } finally {
        [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr)
    }
}

$psql = Find-Psql
$postgresPassword = Read-RequiredSecret "Enter PostgreSQL superuser password for $PostgresSuperUser"
$appPassword = Read-RequiredSecret "Enter password to set for application user $AppUser"

$env:PGPASSWORD = $postgresPassword
try {
    $escapedAppPassword = $appPassword.Replace("'", "''")
    $escapedDb = $Database.Replace('"', '""')
    $escapedUser = $AppUser.Replace('"', '""')

    $sql = @"
DO `$`$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '$AppUser') THEN
        CREATE ROLE "$escapedUser" LOGIN PASSWORD '$escapedAppPassword';
    ELSE
        ALTER ROLE "$escapedUser" WITH LOGIN PASSWORD '$escapedAppPassword';
    END IF;
END
`$`$;

SELECT 'CREATE DATABASE "$escapedDb" OWNER "$escapedUser"'
WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = '$Database')\gexec

ALTER DATABASE "$escapedDb" OWNER TO "$escapedUser";
ALTER ROLE "$escapedUser" CREATEDB;
"@

    $tempSql = New-TemporaryFile
    $utf8NoBom = New-Object System.Text.UTF8Encoding($false)
    [System.IO.File]::WriteAllText($tempSql.FullName, $sql, $utf8NoBom)

    & $psql -h $PostgresHost -p $PostgresPort -U $PostgresSuperUser -d postgres -v ON_ERROR_STOP=1 -f $tempSql.FullName
    if ($LASTEXITCODE -ne 0) {
        throw "psql failed with exit code $LASTEXITCODE. PostgreSQL database was not initialized."
    }
} finally {
    if ($tempSql -and (Test-Path -LiteralPath $tempSql.FullName)) {
        Remove-Item -LiteralPath $tempSql.FullName -Force
    }
    Remove-Item Env:\PGPASSWORD -ErrorAction SilentlyContinue
}

Write-Host "PostgreSQL database is ready: $Database / $AppUser"
