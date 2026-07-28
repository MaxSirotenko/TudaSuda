@echo off
setlocal
set "ROOT=%~dp0"
cd /d "%ROOT%" || exit /b 1
if exist ".venv\Scripts\python.exe" (
  set "PYTHON=.venv\Scripts\python.exe"
) else if exist "venv\Scripts\python.exe" (
  set "PYTHON=venv\Scripts\python.exe"
) else (
  where python >nul 2>nul || (echo ERROR: Python was not found in .venv, venv, or PATH. 1>&2 & exit /b 1)
  set "PYTHON=python"
)
"%PYTHON%" -m pip install -r requirements-browser-benchmark.txt
if errorlevel 1 exit /b %ERRORLEVEL%
echo Playwright package installed. No browser binary was downloaded.
echo If Chrome or Edge is unavailable, explicitly run: "%PYTHON%" -m playwright install chromium
