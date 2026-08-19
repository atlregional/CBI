param(
    [switch]$SkipAzure
)

$ErrorActionPreference = "Stop"

Write-Host "==============================================================" -ForegroundColor Cyan
Write-Host " CBI PYTHON DEPENDENCY SETUP" -ForegroundColor Cyan
Write-Host "==============================================================" -ForegroundColor Cyan
Write-Host ""

# Prefer the Python launcher if available.
if (Get-Command py -ErrorAction SilentlyContinue) {
    $Python = "py"
}
elseif (Get-Command python -ErrorAction SilentlyContinue) {
    $Python = "python"
}
else {
    throw "Python was not found in PATH."
}

Write-Host "Using Python:" -ForegroundColor Yellow
& $Python --version
Write-Host ""

# Upgrade package-management tooling first.
Write-Host "Upgrading pip, setuptools, and wheel..." -ForegroundColor Yellow
& $Python -m pip install --upgrade pip setuptools wheel
if ($LASTEXITCODE -ne 0) {
    throw "Failed to upgrade pip/setuptools/wheel."
}

# The obsolete package named 'docx' conflicts with python-docx.
# The traceback showing site-packages\docx.py is the signature of that problem.
Write-Host ""
Write-Host "Removing obsolete/conflicting 'docx' package if installed..." -ForegroundColor Yellow
& $Python -m pip uninstall -y docx

Write-Host ""
Write-Host "Installing core CBI dependencies..." -ForegroundColor Yellow
& $Python -m pip install --upgrade `
    numpy `
    scipy `
    polars `
    connectorx `
    "psycopg[binary]" `
    python-docx `
    pyarrow

if ($LASTEXITCODE -ne 0) {
    throw "Core dependency installation failed."
}

if (-not $SkipAzure) {
    Write-Host ""
    Write-Host "Installing optional Azure / Fabric export dependencies..." -ForegroundColor Yellow
    & $Python -m pip install --upgrade `
        azure-identity `
        azure-storage-file-datalake

    if ($LASTEXITCODE -ne 0) {
        throw "Azure/Fabric dependency installation failed."
    }
}

Write-Host ""
Write-Host "Verifying imports..." -ForegroundColor Yellow

$verify = @'
import sys
import numpy
import scipy
import polars
import connectorx
import psycopg
import docx
import pyarrow

print("Python:", sys.version.split()[0])
print("numpy:", numpy.__version__)
print("scipy:", scipy.__version__)
print("polars:", polars.__version__)
print("connectorx:", connectorx.__version__)
print("psycopg:", psycopg.__version__)
print("python-docx:", docx.__version__)
print("pyarrow:", pyarrow.__version__)
print("Core dependency check: OK")
'@

$verify | & $Python -
if ($LASTEXITCODE -ne 0) {
    throw "Core import verification failed."
}

if (-not $SkipAzure) {
    $verifyAzure = @'
import azure.identity
import azure.storage.filedatalake
print("Azure/Fabric dependency check: OK")
'@
    $verifyAzure | & $Python -
    if ($LASTEXITCODE -ne 0) {
        throw "Azure/Fabric import verification failed."
    }
}

Write-Host ""
Write-Host "==============================================================" -ForegroundColor Green
Write-Host " ALL CBI DEPENDENCIES INSTALLED SUCCESSFULLY" -ForegroundColor Green
Write-Host "==============================================================" -ForegroundColor Green
Write-Host ""
Write-Host "You can now run:" -ForegroundColor Green
Write-Host "  .\run_cbi_pipeline.bat"
Write-Host ""
Read-Host "Press Enter to close"
