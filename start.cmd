@echo off
setlocal EnableExtensions

cd /d "%~dp0"
set "START_LOG=%CD%\start.log"

call :log ============================================================
call :log Starting TudaSuda recognizer from %CD%
call :log Log file: %START_LOG%
call :log ============================================================

call :update_from_git

if not exist "requirements.txt" (
    call :fail requirements.txt was not found in %CD%.
    exit /b 1
)

set "STREAMLIT_ENTRYPOINT=virtual_warehouse_app.py"

if not exist "%STREAMLIT_ENTRYPOINT%" (
    call :fail %STREAMLIT_ENTRYPOINT% was not found in %CD%.
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
    call :log Creating virtual environment...
    %PYTHON_CMD% -m venv venv >>"%START_LOG%" 2>&1
    if errorlevel 1 (
        call :fail Failed to create virtual environment. See %START_LOG% for details.
        exit /b 1
    )
)

if not exist "venv\Scripts\python.exe" (
    call :fail Virtual environment Python was not found after creation: %CD%\venv\Scripts\python.exe
    exit /b 1
)

call "venv\Scripts\activate.bat" >>"%START_LOG%" 2>&1
if errorlevel 1 (
    call :fail Failed to activate virtual environment. See %START_LOG% for details.
    exit /b 1
)

for /f "usebackq delims=" %%H in (`python -c "from pathlib import Path; import hashlib; p=Path('requirements.txt'); print(hashlib.sha256(p.read_bytes()).hexdigest())"`) do set "REQ_HASH=%%H"
set "REQ_HASH_FILE=venv\.requirements.sha256"
set "INSTALLED_REQ_HASH="
if exist "%REQ_HASH_FILE%" set /p INSTALLED_REQ_HASH=<"%REQ_HASH_FILE%"

if not "%REQ_HASH%"=="%INSTALLED_REQ_HASH%" (
    call :log Installing Python dependencies. This may take a few minutes...
    python -m pip install --upgrade pip >>"%START_LOG%" 2>&1
    if errorlevel 1 (
        call :fail Failed to upgrade pip. See %START_LOG% for details.
        exit /b 1
    )

    python -m pip install -r requirements.txt >>"%START_LOG%" 2>&1
    if errorlevel 1 (
        call :fail Failed to install requirements. See %START_LOG% for details.
        exit /b 1
    )

    >"%REQ_HASH_FILE%" echo %REQ_HASH%
)

rem Merge conflict note: keep hashing %STREAMLIT_ENTRYPOINT%, not app.py.
rem app.py is only a compatibility wrapper; virtual_warehouse_app.py is the real Streamlit entrypoint.
for /f "usebackq delims=" %%H in (`python -c "from pathlib import Path; import hashlib; p=Path('%STREAMLIT_ENTRYPOINT%'); print(hashlib.sha256(p.read_bytes()).hexdigest()[:12])"`) do set "APP_HASH=%%H"
set "GIT_COMMIT=unknown"
for /f "usebackq delims=" %%H in (`git rev-parse --short HEAD 2^>nul`) do set "GIT_COMMIT=%%H"
call :log Streamlit entrypoint: %STREAMLIT_ENTRYPOINT%
call :log Entrypoint file hash: %APP_HASH%
call :log Git commit: %GIT_COMMIT%

call :free_port 8501

call :log Starting Streamlit on http://localhost:8501/
rem Merge conflict note: keep running %STREAMLIT_ENTRYPOINT%, not app.py.
python -m streamlit run "%STREAMLIT_ENTRYPOINT%" --server.address localhost --server.port 8501 --browser.serverAddress localhost --server.fileWatcherType poll
if errorlevel 1 (
    call :fail Streamlit stopped with an error. See %START_LOG% for setup details.
    exit /b 1
)

exit /b 0

:update_from_git
if not exist ".git" (
    call :log Git repository was not found. Skipping auto-update.
    exit /b 0
)

git --version >>"%START_LOG%" 2>&1
if errorlevel 1 (
    call :log Git was not found. Skipping auto-update.
    exit /b 0
)

set "GIT_DIRTY="
for /f "delims=" %%S in ('git status --porcelain 2^>nul') do set "GIT_DIRTY=1"
if defined GIT_DIRTY (
    call :log Local git changes were found. Skipping auto-update to avoid overwriting your work.
    call :log Run git status and resolve/commit/stash local changes, then start again.
    exit /b 0
)

set "GIT_BRANCH=unknown"
for /f "usebackq delims=" %%B in (`git branch --show-current 2^>nul`) do set "GIT_BRANCH=%%B"
call :log Checking git updates for branch: %GIT_BRANCH%

git fetch --prune >>"%START_LOG%" 2>&1
if errorlevel 1 (
    call :log Git fetch failed. Continuing with the local files. See %START_LOG% for details.
    exit /b 0
)

git pull --ff-only >>"%START_LOG%" 2>&1
if errorlevel 1 (
    call :fail Git pull --ff-only failed. Local files may be stale or branch has diverged. Run git status / git pull manually.
    exit /b 1
)

call :log Git auto-update completed.
exit /b 0

:free_port
set "PORT=%~1"
set "FOUND_PID="
for /f "tokens=5" %%P in ('netstat -ano -p tcp ^| findstr /R /C:":%PORT% .*LISTENING"') do (
    if not "%%P"=="0" set "FOUND_PID=%%P"
)
if defined FOUND_PID (
    call :log Port %PORT% is already used by PID %FOUND_PID%. Stopping old Streamlit process before restart...
    taskkill /PID %FOUND_PID% /F >>"%START_LOG%" 2>&1
    if errorlevel 1 (
        call :fail Failed to stop process on port %PORT%. Close the old Streamlit window or stop PID %FOUND_PID% manually.
        exit /b 1
    )
    timeout /t 2 /nobreak >nul
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
