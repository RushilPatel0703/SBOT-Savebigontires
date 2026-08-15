@echo off
title Save Big On Tires — Inventory Dashboard
cd /d "%~dp0"

:: Activate virtual environment if present
if exist "venv\Scripts\activate.bat"  call venv\Scripts\activate.bat
if exist ".venv\Scripts\activate.bat" call .venv\Scripts\activate.bat

cd pipelines
echo.
echo  ============================================
echo   Save Big On Tires — Inventory Dashboard
echo  ============================================
echo.
echo  Opening dashboard in your browser...
echo  URL: http://localhost:8501
echo.
echo  IMPORTANT: Keep this window open while
echo  pipelines are running.
echo.
python -m streamlit run dashboard.py --server.headless false
pause
