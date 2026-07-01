@echo off
setlocal EnableExtensions

cd /d "%~dp0"
set "START_LOG=%CD%\start.log"

call :log ============================================================
call :log Starting TudaSuda recognizer from %CD%
call :log Log file: %START_LOG%
call :log ============================================================

if not exist "requirements.txt" (
    call :fail requirements.txt was not found in %CD%.
    exit /b 1
)

if not exist "app.py" (
    call :fail app.py was not found in %CD%.
    exit /b 1
)

set "PYTHON_CMD="
call :try_python py -3
if not defined PYTHON_CMD call :try_python python
if not defined PYTHON_CMD call :try_python python3

if not defined PYTHON_CMD (
    call :fail Python 3 was not found. Install Python 3 from https://www.python.org/downloads/windows/ and enable "Add python.exe to PATH".
    exit /b 1
)

call :log Using Python command: %PYTHON_CMD%
%PYTHON_CMD% --version >>"%START_LOG%" 2>&1
if errorlevel 1 (
    call :fail Python command was found but did not start correctly: %PYTHON_CMD%
    exit /b 1
)

if not exist "venv\Scripts\python.exe" (
call :log Starting Streamlit on http://localhost:8501/
"venv\Scripts\python.exe" -m streamlit run app.py --server.port 8501 2>>"%START_LOG%"
if errorlevel 1 (
    call :fail Streamlit stopped with an error. See %START_LOG% for setup details.
    exit /b 1
)

exit /b 0

:try_python
%* --version >>"%START_LOG%" 2>&1
if not errorlevel 1 set "PYTHON_CMD=%*"
exit /b 0

:log
echo %*
echo %*>>"%START_LOG%"
exit /b 0

:fail
echo ERROR: %*
echo ERROR: %*>>"%START_LOG%"
echo.
echo Startup failed. Open this log file and send its contents if you need help:
echo %START_LOG%
echo.
pause
exit /b 1
