@echo off
setlocal
cd /d "%~dp0"

set "PYTHON_EXE="
if exist "%LocalAppData%\Python\bin\python.exe" set "PYTHON_EXE=%LocalAppData%\Python\bin\python.exe"
if "%PYTHON_EXE%"=="" (
  where python >nul 2>nul
  if %errorlevel%==0 set "PYTHON_EXE=python"
)
if "%PYTHON_EXE%"=="" (
  echo Python was not found.
  echo Install Python 3.11 or newer from https://www.python.org/downloads/windows/
  pause
  exit /b 1
)

"%PYTHON_EXE%" -m pip install -r requirements.txt
start "" http://127.0.0.1:7860
"%PYTHON_EXE%" neo_rag_launcher.py --server
pause
