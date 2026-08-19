# ==============================================================================
# CBI — ONE-TIME MULTI-CORRIDOR MIGRATION RUNNER
#
# Run this exactly ONCE, before the first time you run run_cbi_pipeline.ps1.
# It applies sql/003_multicorridor_migration.sql via psql.
#
# Re-running it is NOT safe: the migration converts
# segment_recurring_bottlenecks.bottleneck_id to an identity column, and
# doing that twice will fail with a Postgres error (which is a safe failure,
# not a data-damaging one — but there is no need to run this more than once).
#
# Usage:
#   powershell -ExecutionPolicy Bypass -File setup_migration.ps1
# ==============================================================================

$ErrorActionPreference = "Stop"

# ---- EDIT THESE IF YOUR SETUP DIFFERS FROM THE DEFAULTS -------------------
$DbHost = if ($env:CBI_DB_HOST) { $env:CBI_DB_HOST } else { "localhost" }
$DbPort = if ($env:CBI_DB_PORT) { $env:CBI_DB_PORT } else { "5432" }
$DbName = if ($env:CBI_DB_NAME) { $env:CBI_DB_NAME } else { "CBI" }
$DbUser = if ($env:CBI_DB_USER) { $env:CBI_DB_USER } else { "postgres" }
$MigrationFile = Join-Path $PSScriptRoot "sql\003_multicorridor_migration.sql"
$CredentialsFile = Join-Path $PSScriptRoot "cbi_credentials.ini"

function Read-IniValue {
    param([string]$Path, [string]$Key)
    $line = Get-Content $Path | Where-Object { $_ -match "^\s*$Key\s*=" } | Select-Object -First 1
    if (-not $line) { return $null }
    return ($line -split "=", 2)[1].Trim()
}

$iniPassword = $null

if (Test-Path $CredentialsFile) {
    Write-Host "Loading credentials from cbi_credentials.ini..." -ForegroundColor Yellow
    $iniHostValue = Read-IniValue -Path $CredentialsFile -Key "host"
    $iniPort = Read-IniValue -Path $CredentialsFile -Key "port"
    $iniName = Read-IniValue -Path $CredentialsFile -Key "dbname"
    $iniUser = Read-IniValue -Path $CredentialsFile -Key "user"
    $iniPassword = Read-IniValue -Path $CredentialsFile -Key "password"

    if ($iniHostValue) { $DbHost = $iniHostValue }
    if ($iniPort) { $DbPort = $iniPort }
    if ($iniName) { $DbName = $iniName }
    if ($iniUser) { $DbUser = $iniUser }
    if ($iniPassword -eq "REPLACE_WITH_YOUR_PASSWORD") { $iniPassword = $null }
}
# -----------------------------------------------------------------------------

if (-not (Test-Path $MigrationFile)) {
    Write-Host "Could not find $MigrationFile" -ForegroundColor Red
    Write-Host "Run this script from the cbi_multicorridor_pipeline folder." -ForegroundColor Red
    exit 1
}

Write-Host "This will apply the multi-corridor schema migration to:" -ForegroundColor Yellow
Write-Host "  Host:     $DbHost`:$DbPort"
Write-Host "  Database: $DbName"
Write-Host "  User:     $DbUser"
Write-Host ""
Write-Host "This should only be run ONCE, ever." -ForegroundColor Yellow
$confirmation = Read-Host "Type YES to continue"

if ($confirmation -ne "YES") {
    Write-Host "Cancelled." -ForegroundColor Yellow
    exit 0
}

$securePassword = if ($iniPassword) {
    Write-Host "Using password from cbi_credentials.ini." -ForegroundColor Yellow
    ConvertTo-SecureString $iniPassword -AsPlainText -Force
}
else {
    Read-Host "CBI database password" -AsSecureString
}
$bstr = [System.Runtime.InteropServices.Marshal]::SecureStringToBSTR($securePassword)
$plainPassword = [System.Runtime.InteropServices.Marshal]::PtrToStringAuto($bstr)
[System.Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr)

$env:PGPASSWORD = $plainPassword

try {
    & psql -h $DbHost -p $DbPort -U $DbUser -d $DbName -f $MigrationFile

    if ($LASTEXITCODE -ne 0) {
        throw "psql exited with code $LASTEXITCODE"
    }

    Write-Host ""
    Write-Host "Migration applied successfully." -ForegroundColor Green
    Write-Host "You can now run run_cbi_pipeline.ps1 for regular batch runs." -ForegroundColor Green
}
catch {
    Write-Host ""
    Write-Host "Migration FAILED: $_" -ForegroundColor Red
    exit 1
}
finally {
    $env:PGPASSWORD = $null
    $plainPassword = $null
}
