@echo off
setlocal EnableExtensions EnableDelayedExpansion
cd /d "%~dp0"

rem The Python launcher may select an older 3.x installation. Pick a 3.10+
rem interpreter explicitly, then fall back to python on systems without py.exe.
set "PYTHON=py -3"
%PYTHON% -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>&1
if errorlevel 1 set "PYTHON="
for %%V in (3.14 3.13 3.12 3.11 3.10) do if not defined PYTHON (
  set "PYTHON=py -%%V"
  !PYTHON! -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>&1
  if errorlevel 1 set "PYTHON="
)
if not defined PYTHON (
  set "PYTHON=python"
  !PYTHON! -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>&1
  if errorlevel 1 set "PYTHON="
)
if not defined PYTHON goto no_python
for /f "delims=" %%V in ('!PYTHON! --version 2^>^&1') do echo Using %%V

if not exist ".venv\Scripts\python.exe" (
  !PYTHON! -m venv .venv
  if errorlevel 1 goto venv_fail
) else (
  .venv\Scripts\python.exe -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>&1
  if errorlevel 1 goto venv_version
)

rem Install runtime wheels directly. This avoids the editable-build step that
rem can fail while resolving a setuptools build dependency on restricted networks.
.venv\Scripts\python.exe -c "import streamlit, pymysql, keyring, psutil" >nul 2>&1
if errorlevel 1 (
  .venv\Scripts\python.exe -m pip install -r requirements.txt
  if errorlevel 1 goto install_fail
)
.venv\Scripts\python.exe -m streamlit run app.py --server.address 127.0.0.1
if errorlevel 1 goto run_fail
exit /b 0
:no_python
echo Python 3.10 or newer was not found. Install Python from https://www.python.org/downloads/windows/ and rerun this file.
pause
exit /b 1
:venv_fail
echo Could not create the virtual environment with !PYTHON!. Check that venv/ensurepip is installed.
pause
exit /b 1
:venv_version
echo The existing .venv uses Python older than 3.10. Delete the .venv folder and rerun this file.
pause
exit /b 1
:install_fail
echo Dependency installation failed. The detailed pip error is shown above; check the network or package index and rerun.
pause
exit /b 1
:run_fail
echo Streamlit could not start. The detailed error is shown above.
pause
exit /b 1
