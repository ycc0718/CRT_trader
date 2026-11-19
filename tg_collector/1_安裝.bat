@echo off
chcp 65001 >nul
cd /d "%~dp0"

title 1. 安裝 Telegram 收集器
if not exist venv\Scripts\python.exe (
  where py >nul 2>nul && py -3 -m venv venv
)
if not exist venv\Scripts\python.exe (
  where python >nul 2>nul && python -m venv venv
)
if not exist venv\Scripts\python.exe (
  echo 找不到 Python。到 https://www.python.org/downloads/ 下載安裝，安裝畫面一定要勾「Add python.exe to PATH」，裝好再雙擊這個檔。
  pause
  exit /b 1
)
echo 安裝 Telethon 中（要網路，約 1 分鐘）...
venv\Scripts\python.exe -m pip install --disable-pip-version-check -q -r requirements.txt
if errorlevel 1 (
  echo 安裝失敗，看上面的錯誤訊息。
  pause
  exit /b 1
)
echo.
echo 安裝好了。下一步：雙擊「2_登入Telegram.bat」
pause
