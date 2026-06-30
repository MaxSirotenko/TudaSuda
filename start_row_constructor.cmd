@echo off
cd /d "%~dp0"
call venv\Scripts\activate
streamlit run row_constructor.py --server.port 8502
pause