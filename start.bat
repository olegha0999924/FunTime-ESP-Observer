@echo off
chcp 65001 >nul
cd /d "%~dp0"
py -m pip install --upgrade pip
py -m pip install -r requirements.txt
if errorlevel 1 (
  echo.
  echo Ошибка установки библиотек. Проверьте Python и интернет.
  pause
  exit /b 1
)
py app.py
echo.
echo Программа завершилась. Если выше есть ошибка, пришлите ее скриншот.
pause
