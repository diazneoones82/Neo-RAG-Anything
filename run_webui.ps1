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

& $Python webui_server.py
