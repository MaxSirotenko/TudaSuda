@echo off
setlocal EnableExtensions

if defined START_CMD_BOOTSTRAPPED goto :bootstrapped

set "START_CMD_PROJECT_ROOT=%~dp0"
set "START_CMD_BOOTSTRAPPED=1"
set "START_CMD_TEMP=%TEMP%\tudasuda_start_%RANDOM%_%RANDOM%.cmd"
copy /Y "%~f0" "%START_CMD_TEMP%" >nul 2>&1
if errorlevel 1 goto :bootstrap_copy_failed

call "%START_CMD_TEMP%"
set "START_CMD_EXIT_CODE=%ERRORLEVEL%"
del "%START_CMD_TEMP%" >nul 2>&1
exit /b %START_CMD_EXIT_CODE%

:bootstrap_copy_failed
echo ERROR: Failed to create a temporary launcher copy in "%TEMP%".
echo The application was not started.
exit /b 1

:bootstrapped

if not defined START_CMD_PROJECT_ROOT (
    echo ERROR: START_CMD_PROJECT_ROOT is not defined for the bootstrapped launcher.
    echo The application was not started.
    exit /b 1
)

cd /d "%START_CMD_PROJECT_ROOT%"
if errorlevel 1 (
    echo ERROR: Failed to enter project directory "%START_CMD_PROJECT_ROOT%".
    echo The application was not started.
    exit /b 1
)

if not exist "%CD%\data\last_import" mkdir "%CD%\data\last_import" >nul 2>&1
set "START_LOG=%CD%\data\last_import\start.log"

call :log Starting TudaSuda recognizer from %CD%
call :log Project path: %CD%
call :log Log file: %START_LOG%

call :safe_git_update
if errorlevel 1 exit /b 1

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

rem Keep hashing %STREAMLIT_ENTRYPOINT%; virtual_warehouse_app.py is the real Streamlit entrypoint.
for /f "usebackq delims=" %%H in (`python -c "from pathlib import Path; import hashlib; p=Path('%STREAMLIT_ENTRYPOINT%'); print(hashlib.sha256(p.read_bytes()).hexdigest()[:12])"`) do set "APP_HASH=%%H"
set "GIT_COMMIT=unknown"
for /f "usebackq delims=" %%H in (`git rev-parse --short HEAD 2^>nul`) do set "GIT_COMMIT=%%H"
call :log Streamlit entrypoint: %STREAMLIT_ENTRYPOINT%
call :log Entrypoint file hash: %APP_HASH%
call :log Git commit: %GIT_COMMIT%

call :free_port 8501

call :log Starting Streamlit on http://localhost:8501/
python -m streamlit run "%STREAMLIT_ENTRYPOINT%" --server.address localhost --server.port 8501 --browser.serverAddress localhost --server.fileWatcherType poll
if errorlevel 1 (
    call :fail Streamlit stopped with an error. See %START_LOG% for setup details.
    exit /b 1
)

exit /b 0


:safe_git_update
if not exist ".git" (
    call :fail Git repository was not found in %CD%. The application was not started.
    exit /b 1
)
where git >nul 2>&1
if errorlevel 1 (
    call :fail Git was not found in PATH. The application was not started.
    exit /b 1
)
set "INITIAL_BRANCH=unknown"
set "INITIAL_COMMIT=unknown"
set "INITIAL_SHORT_COMMIT=unknown"
for /f "usebackq delims=" %%B in (`git branch --show-current 2^>^>"%START_LOG%"`) do set "INITIAL_BRANCH=%%B"
for /f "usebackq delims=" %%H in (`git rev-parse HEAD 2^>^>"%START_LOG%"`) do set "INITIAL_COMMIT=%%H"
for /f "usebackq delims=" %%H in (`git rev-parse --short HEAD 2^>^>"%START_LOG%"`) do set "INITIAL_SHORT_COMMIT=%%H"
call :log Initial branch: %INITIAL_BRANCH%
call :log Initial commit: %INITIAL_COMMIT%
call :log Initial short commit: %INITIAL_SHORT_COMMIT%

set "GIT_DIRTY="
for /f "usebackq delims=" %%S in (`git status --porcelain --untracked-files^=all 2^>^>"%START_LOG%"`) do set "GIT_DIRTY=1"
if defined GIT_DIRTY (
    call :log Dirty tree check: local changes detected.
    call :log Auto-update stopped because local changes were detected.
    call :log No files were deleted or modified.
    call :log The application was not started.
    echo git status --short:
    git status --short
    echo git status --short:>>"%START_LOG%"
    git status --short >>"%START_LOG%" 2>&1
    exit /b 1
)
call :log Dirty tree check: clean working tree.

git remote get-url origin >>"%START_LOG%" 2>&1
if errorlevel 1 (
    call :fail Git remote origin was not found. The application was not started.
    exit /b 1
)

call :log Fetching origin main...
git fetch --prune origin main >>"%START_LOG%" 2>&1
if errorlevel 1 (
    call :fail git fetch --prune origin main failed. The application was not started.
    exit /b 1
)
call :log Fetch result: success.

git rev-parse --verify refs/remotes/origin/main >>"%START_LOG%" 2>&1
if errorlevel 1 (
    call :fail refs/remotes/origin/main was not found after fetch. The application was not started.
    exit /b 1
)

git show-ref --verify --quiet refs/heads/main
if errorlevel 1 (
    call :log Local main branch was not found. Creating main from origin/main...
    git switch --create main --track origin/main >>"%START_LOG%" 2>&1
) else (
    call :log Switching to local main branch...
    git switch main >>"%START_LOG%" 2>&1
)
if errorlevel 1 (
    call :fail git switch main failed. The application was not started.
    exit /b 1
)
call :log Switch result: main is active.

git branch --set-upstream-to=origin/main main >>"%START_LOG%" 2>&1
if errorlevel 1 (
    call :fail Failed to set upstream to origin/main. The application was not started.
    exit /b 1
)

call :log Pulling origin main with fast-forward only...
git pull --ff-only origin main >>"%START_LOG%" 2>&1
if errorlevel 1 (
    call :fail git pull --ff-only origin main failed. The application was not started.
    exit /b 1
)
call :log Pull result: success.

set "LOCAL_HEAD="
set "ORIGIN_MAIN="
for /f "usebackq delims=" %%H in (`git rev-parse HEAD 2^>^>"%START_LOG%"`) do set "LOCAL_HEAD=%%H"
for /f "usebackq delims=" %%H in (`git rev-parse refs/remotes/origin/main 2^>^>"%START_LOG%"`) do set "ORIGIN_MAIN=%%H"
call :log Local HEAD after update: %LOCAL_HEAD%
call :log origin/main after update: %ORIGIN_MAIN%
if not "%LOCAL_HEAD%"=="%ORIGIN_MAIN%" (
    call :fail Local HEAD does not match origin/main after update. HEAD=%LOCAL_HEAD% origin/main=%ORIGIN_MAIN%. The application was not started.
    exit /b 1
)
call :log Branch after update: main
call :log SHA before update: %INITIAL_COMMIT%
call :log SHA after update: %LOCAL_HEAD%
call :log SHA origin/main: %ORIGIN_MAIN%
call :log Local main is synchronized with origin/main.
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
