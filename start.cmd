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

python -c "import streamlit" >nul 2>nul
if errorlevel 1 del "venv\.deps_installed" >nul 2>nul

if not exist "venv\.deps_installed" (
    echo Installing Python dependencies. This may take a few minutes on first launch...
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

    echo ok>"venv\.deps_installed"
)

echo Starting Streamlit on http://localhost:8501/
echo If localhost does not open, try http://127.0.0.1:8501/
python -m streamlit run app.py --server.port 8501
if errorlevel 1 (
    echo Streamlit stopped with an error.
    pause
    exit /b 1
)
