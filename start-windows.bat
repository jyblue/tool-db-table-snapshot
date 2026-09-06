@echo off
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  py -3 -m venv .venv
  if errorlevel 1 goto fail
  .venv\Scripts\python.exe -m pip install -e .
  if errorlevel 1 goto fail
)
.venv\Scripts\python.exe -m streamlit run app.py --server.address 127.0.0.1
if errorlevel 1 goto fail
exit /b 0
:fail
echo Installation or startup failed. Python 3.11+ is required.
pause
exit /b 1
