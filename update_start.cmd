@echo off
setlocal EnableExtensions

cd /d "%~dp0"

if not exist "%CD%\data\last_import" (
    mkdir "%CD%\data\last_import" >nul 2>&1
)

set "UPDATE_LOG=%CD%\data\last_import\update_start.log"

echo ============================================================
echo Updating TudaSuda from %CD%
echo Log file: %UPDATE_LOG%
echo ============================================================
>"%UPDATE_LOG%" echo ============================================================
>>"%UPDATE_LOG%" echo Updating TudaSuda from %CD%
>>"%UPDATE_LOG%" echo Log file: %UPDATE_LOG%
>>"%UPDATE_LOG%" echo ============================================================

if not exist ".git" (
    call :log Git repository was not found. Starting local version.
    call "%~dp0start.cmd"
    exit /b %errorlevel%
)

where git >nul 2>&1
if errorlevel 1 (
    call :log Git was not found in PATH. Starting local version.
    call "%~dp0start.cmd"
    exit /b %errorlevel%
)

set "GIT_DIRTY="

for /f "usebackq delims=" %%S in (`git status --porcelain 2^>^>"%UPDATE_LOG%"`) do (
    set "GIT_DIRTY=1"
)

if defined GIT_DIRTY (
    call :log Local changes detected. Skipping update to avoid overwriting your work.
    git status --short >>"%UPDATE_LOG%" 2>&1
    call "%~dp0start.cmd"
    exit /b %errorlevel%
)

call :log Fetching latest code metadata...
git fetch --prune >>"%UPDATE_LOG%" 2>&1
if errorlevel 1 (
    call :log git fetch failed. Starting local version; see %UPDATE_LOG% for details.
    call "%~dp0start.cmd"
    exit /b %errorlevel%
)

call :log Pulling latest code with fast-forward only...
git pull --ff-only >>"%UPDATE_LOG%" 2>&1
if errorlevel 1 (
    call :log git pull --ff-only failed. Starting local version; see %UPDATE_LOG% for details.
    call "%~dp0start.cmd"
    exit /b %errorlevel%
)

call :log Update completed. Starting application...
call "%~dp0start.cmd"
exit /b %errorlevel%

:log
echo %*
echo %*>>"%UPDATE_LOG%"
exit /b 0

:fail
echo ERROR: %*
echo ERROR: %*>>"%UPDATE_LOG%"
echo.
echo Update failed. Open this log file and send its contents if you need help:
echo %UPDATE_LOG%
echo.
pause
exit /b 1
