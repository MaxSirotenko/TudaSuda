@echo off
setlocal

cd /d "%~dp0"

if not exist "venv\Scripts\python.exe" (
    echo Creating virtual environment...
    py -3 -m venv venv
    if errorlevel 1 python -m venv venv
)

call "venv\Scripts\activate"
python -m pip install --upgrade pip
python -m pip install -r requirements.txt

streamlit run row_constructor.py --server.port 8502
