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

& $Python -m pip install -r requirements.txt

& $Python -m PyInstaller `
    --noconfirm `
    --clean `
    --windowed `
    --onefile `
    --name "Neo RAG-Anything" `
    --icon "art\werewolf-icon.ico" `
    --add-data "webui;webui" `
    --add-data "art;art" `
    --collect-all pypdf `
    --collect-all PIL `
    --collect-all chromadb `
    --collect-all onnxruntime `
    --exclude-module pytesseract `
    --exclude-module pandas `
    --exclude-module scipy `
    --exclude-module torch `
    --exclude-module torchvision `
    --exclude-module transformers `
    --exclude-module cv2 `
    neo_rag_launcher.py

Write-Host ""
Write-Host "Build complete: $Root\dist\Neo RAG-Anything.exe"
