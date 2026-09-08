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
  rem Use only the explicitly configured company mirror. --isolated prevents
  rem pip from silently reading a user config or extra index on the Internet.
  if not defined SNAPSHOT_PIP_INDEX_URL if defined PIP_INDEX_URL set "SNAPSHOT_PIP_INDEX_URL=!PIP_INDEX_URL!"
  if not defined SNAPSHOT_PIP_TRUSTED_HOST if defined PIP_TRUSTED_HOST set "SNAPSHOT_PIP_TRUSTED_HOST=!PIP_TRUSTED_HOST!"
  if not defined SNAPSHOT_PIP_INDEX_URL (
    for /f "delims=" %%I in ('.venv\Scripts\python.exe -m pip config get global.index-url 2^>nul') do if not defined SNAPSHOT_PIP_INDEX_URL set "SNAPSHOT_PIP_INDEX_URL=%%I"
  )
  if not defined SNAPSHOT_PIP_TRUSTED_HOST (
    for /f "delims=" %%I in ('.venv\Scripts\python.exe -m pip config get global.trusted-host 2^>nul') do if not defined SNAPSHOT_PIP_TRUSTED_HOST set "SNAPSHOT_PIP_TRUSTED_HOST=%%I"
  )
  if not defined SNAPSHOT_PIP_INDEX_URL goto mirror_missing
  if /I "!SNAPSHOT_PIP_INDEX_URL:~0,7!"=="http://" if not defined SNAPSHOT_PIP_TRUSTED_HOST goto mirror_trust_missing
  if defined SNAPSHOT_PIP_TRUSTED_HOST (
    .venv\Scripts\python.exe -m pip --isolated install --index-url "!SNAPSHOT_PIP_INDEX_URL!" --trusted-host "!SNAPSHOT_PIP_TRUSTED_HOST!" -r requirements.txt
  ) else (
    .venv\Scripts\python.exe -m pip --isolated install --index-url "!SNAPSHOT_PIP_INDEX_URL!" -r requirements.txt
  )
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
:mirror_missing
echo Set SNAPSHOT_PIP_INDEX_URL to the internal Python package mirror before first run.
echo Example in PowerShell: $env:SNAPSHOT_PIP_INDEX_URL="https://packages.example.local/simple"
echo An existing pip.ini global.index-url is also accepted. For an HTTP mirror or private CA, set SNAPSHOT_PIP_TRUSTED_HOST to its hostname.
pause
exit /b 1
:mirror_trust_missing
echo The HTTP package mirror needs SNAPSHOT_PIP_TRUSTED_HOST set to its hostname.
echo Example in PowerShell: $env:SNAPSHOT_PIP_TRUSTED_HOST="packages.example.local"
pause
exit /b 1
:install_fail
echo Dependency installation failed. The detailed pip error is shown above.
echo Confirm that the internal mirror contains all requirements and that its URL and trusted-host settings are correct.
pause
exit /b 1
:run_fail
echo Streamlit could not start. The detailed error is shown above.
pause
exit /b 1
