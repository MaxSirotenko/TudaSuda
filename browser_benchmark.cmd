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
"%PYTHON%" scripts\run_browser_map_benchmark.py %*
exit /b %ERRORLEVEL%
