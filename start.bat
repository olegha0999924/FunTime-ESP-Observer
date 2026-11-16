@echo off
setlocal
cd /d "%~dp0"
echo Checking Python...
set "PY="
where py >nul 2>nul
if not errorlevel 1 set "PY=py -3"
if not defined PY (
  where python >nul 2>nul
  if not errorlevel 1 set "PY=python"
)
if not defined PY (
  echo ERROR: Python was not found.
  echo Install Python 3.11 or newer from https://www.python.org/downloads/windows/
  echo During installation, enable Add Python to PATH.
  pause
  exit /b 1
)
%PY% --version
if errorlevel 1 goto python_error
echo Checking pip...
%PY% -m pip --version
if errorlevel 1 goto pip_error
echo Installing required packages...
%PY% -m pip install -r requirements.txt
if errorlevel 1 goto install_error
echo Starting application...
%PY% app.py
if errorlevel 1 goto app_error
echo Application closed.
pause
exit /b 0
:python_error
echo ERROR: Python could not start. Reinstall Python 3.11 or newer.
goto failed
:pip_error
echo ERROR: pip is missing or could not start. Run: %PY% -m ensurepip --upgrade
goto failed
:install_error
echo ERROR: Package installation failed. Check the messages above and your internet connection.
goto failed
:app_error
echo ERROR: The application stopped with an error. Check the traceback above.
goto failed
:failed
echo.
echo Copy or screenshot the error messages above this line and send them here.
pause
exit /b 1
