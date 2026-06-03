$ErrorActionPreference = "Stop"

$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $Root

$Python = $env:PYTHON
if (-not $Python) {
    $Candidate = "$env:LOCALAPPDATA\Python\bin\python.exe"
    if (Test-Path $Candidate) {
        $Python = $Candidate
    } else {
        $Python = "python"
    }
}

& $Python -m pip install -r requirements.txt
Start-Process "http://127.0.0.1:7860"
& $Python neo_rag_launcher.py --server
