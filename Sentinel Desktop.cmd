@echo off
setlocal
set "PROJECT_ROOT=%~dp0"
set "PYTHON=%PROJECT_ROOT%collector\.venv\Scripts\python.exe"
if not exist "%PYTHON%" (
  echo Sentinel is not installed yet. Run Install.cmd first.
  pause
  exit /b 1
)
start "" "%PYTHON%" "%PROJECT_ROOT%scripts\desktop_app.py"
