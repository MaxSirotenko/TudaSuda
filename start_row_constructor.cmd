@echo off
setlocal

cd /d "%~dp0"

if not exist "venv\Scripts\python.exe" (
    echo Creating virtual environment...
    py -3 -m venv venv
    if errorlevel 1 python -m venv venv
    if errorlevel 1 (
        echo Failed to create virtual environment.
        pause
        exit /b 1
    )
)

call "venv\Scripts\activate.bat"
if errorlevel 1 (
    echo Failed to activate virtual environment.
    pause
    exit /b 1
)

for /f "usebackq delims=" %%H in (`python -c "from pathlib import Path; import hashlib; p=Path('requirements.txt'); print(hashlib.sha256(p.read_bytes()).hexdigest())"`) do set "REQ_HASH=%%H"
set "REQ_HASH_FILE=venv\.requirements.sha256"
set "INSTALLED_REQ_HASH="
if exist "%REQ_HASH_FILE%" set /p INSTALLED_REQ_HASH=<"%REQ_HASH_FILE%"

if not "%REQ_HASH%"=="%INSTALLED_REQ_HASH%" (
    echo Installing Python dependencies. This may take a few minutes...
    python -m pip install --upgrade pip
    if errorlevel 1 (
        echo Failed to upgrade pip.
        pause
        exit /b 1
    )

    python -m pip install -r requirements.txt
    if errorlevel 1 (
        echo Failed to install requirements.
        pause
        exit /b 1
    )

    >"%REQ_HASH_FILE%" echo %REQ_HASH%
    echo ok>"venv\.deps_installed"
)

echo Starting row constructor on http://localhost:8502/
python -m streamlit run row_constructor.py --server.port 8502
if errorlevel 1 (
    echo Streamlit stopped with an error.
    pause
    exit /b 1
)
