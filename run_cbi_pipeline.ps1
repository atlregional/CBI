param(
    [string]$Only,
    [switch]$SkipReport,
    [switch]$ExportFabric
)

$ErrorActionPreference = "Stop"

$PythonExe = "C:\Users\Soheil\AppData\Local\Programs\Python\Python312\python.exe"
$VenvActivateScript = $null
$ScriptsFolder = Join-Path $PSScriptRoot "scripts"
$OutputFolder = "C:\Users\Soheil\Desktop\CBI\outputs\multi_corridor"

if (-not $env:CBI_DB_HOST) { $env:CBI_DB_HOST = "localhost" }
if (-not $env:CBI_DB_PORT) { $env:CBI_DB_PORT = "5432" }
if (-not $env:CBI_DB_NAME) { $env:CBI_DB_NAME = "CBI" }
if (-not $env:CBI_DB_USER) { $env:CBI_DB_USER = "postgres" }

$CredentialsFile = Join-Path $PSScriptRoot "cbi_credentials.ini"

function Read-IniValue {
    param(
        [string]$Path,
        [string]$Key
    )

    $line = Get-Content -LiteralPath $Path |
        Where-Object { $_ -match "^\s*$Key\s*=" } |
        Select-Object -First 1

    if (-not $line) {
        return $null
    }

    return ($line -split "=", 2)[1].Trim()
}

try {
    if (Test-Path -LiteralPath $CredentialsFile) {
        Write-Host "Loading credentials from cbi_credentials.ini..." -ForegroundColor Yellow

        $iniHost = Read-IniValue -Path $CredentialsFile -Key "host"
        $iniPort = Read-IniValue -Path $CredentialsFile -Key "port"
        $iniName = Read-IniValue -Path $CredentialsFile -Key "dbname"
        $iniUser = Read-IniValue -Path $CredentialsFile -Key "user"
        $iniPassword = Read-IniValue -Path $CredentialsFile -Key "password"

        if ($iniHost) { $env:CBI_DB_HOST = $iniHost }
        if ($iniPort) { $env:CBI_DB_PORT = $iniPort }
        if ($iniName) { $env:CBI_DB_NAME = $iniName }
        if ($iniUser) { $env:CBI_DB_USER = $iniUser }

        if ($iniPassword -and $iniPassword -ne "REPLACE_WITH_YOUR_PASSWORD") {
            $env:CBI_DB_PASSWORD = $iniPassword
        }
        else {
            Write-Host "cbi_credentials.ini has no password set; you will be prompted." -ForegroundColor Yellow
        }
    }
    else {
        Write-Host "No cbi_credentials.ini found." -ForegroundColor Yellow
        Write-Host "Copy cbi_credentials.example.ini to cbi_credentials.ini and fill it in." -ForegroundColor Yellow
    }

    Write-Host "==============================================================" -ForegroundColor Cyan
    Write-Host " CBI MULTI-CORRIDOR PIPELINE" -ForegroundColor Cyan
    Write-Host "==============================================================" -ForegroundColor Cyan
    Write-Host "Database: $($env:CBI_DB_USER)@$($env:CBI_DB_HOST):$($env:CBI_DB_PORT)/$($env:CBI_DB_NAME)"
    Write-Host ""

    if (-not (Test-Path -LiteralPath $ScriptsFolder)) {
        throw "Could not find scripts folder at: $ScriptsFolder"
    }

    if (-not $env:CBI_DB_PASSWORD) {
        $securePassword = Read-Host "CBI database password" -AsSecureString
        $bstr = [System.Runtime.InteropServices.Marshal]::SecureStringToBSTR($securePassword)

        try {
            $env:CBI_DB_PASSWORD =
                [System.Runtime.InteropServices.Marshal]::PtrToStringAuto($bstr)
        }
        finally {
            [System.Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr)
        }
    }

    if ($VenvActivateScript -and (Test-Path -LiteralPath $VenvActivateScript)) {
        Write-Host "Activating virtual environment..." -ForegroundColor Yellow
        & $VenvActivateScript
    }

    $pythonArgs = New-Object System.Collections.Generic.List[string]
    $pythonArgs.Add("cbi_run_all_corridors.py")

    if ($Only) {
        $pythonArgs.Add("--only")
        $pythonArgs.Add($Only)
    }

    if ($SkipReport) {
        $pythonArgs.Add("--skip-report")
    }

    if ($ExportFabric) {
        $pythonArgs.Add("--export-fabric")
    }

    Push-Location $ScriptsFolder

    try {
        $startStamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
        Write-Host "Starting pipeline run at $startStamp..." -ForegroundColor Green
        Write-Host ""

        & $PythonExe @pythonArgs
        $exitCode = $LASTEXITCODE

        Write-Host ""

        if ($exitCode -eq 0) {
            Write-Host "==============================================================" -ForegroundColor Green
            Write-Host " PIPELINE RUN FINISHED" -ForegroundColor Green
            Write-Host "==============================================================" -ForegroundColor Green

            if (Test-Path -LiteralPath $OutputFolder) {
                Write-Host "Opening output folder..." -ForegroundColor Green
                Start-Process explorer.exe $OutputFolder
            }
        }
        else {
            Write-Host "==============================================================" -ForegroundColor Red
            Write-Host " PIPELINE EXITED WITH ERRORS (code $exitCode)" -ForegroundColor Red
            Write-Host " Check $OutputFolder\pipeline_errors.txt for details." -ForegroundColor Red
            Write-Host "==============================================================" -ForegroundColor Red
            exit $exitCode
        }
    }
    finally {
        Pop-Location
    }
}
catch {
    Write-Host ""
    Write-Host "==============================================================" -ForegroundColor Red
    Write-Host "LAUNCHER ERROR" -ForegroundColor Red
    Write-Host "==============================================================" -ForegroundColor Red
    Write-Host $_.Exception.Message -ForegroundColor Red
    Write-Host ""
    Write-Host $_.ScriptStackTrace -ForegroundColor DarkRed
    exit 1
}
finally {
    $env:CBI_DB_PASSWORD = $null
}
