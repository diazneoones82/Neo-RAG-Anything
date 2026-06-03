$ErrorActionPreference = "Stop"

$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $Root

$Python = $env:PYTHON
if (-not $Python) {
    $Candidate = "C:\Users\Admin\AppData\Local\Python\bin\python.exe"
    if (Test-Path $Candidate) {
        $Python = $Candidate
    } else {
        $Python = "python"
    }
}

& $Python -m pip install --upgrade pip
& $Python -m pip install -r requirements.txt

& powershell -ExecutionPolicy Bypass -File .\build_launcher.ps1

Write-Host ""
Write-Host "Build complete: $Root\dist\Neo RAG-Anything.exe"
