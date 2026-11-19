@echo off
chcp 65001 >nul
cd /d "%~dp0"

title 2. 登入 Telegram、選頻道、設定輸出資料夾
if not exist venv\Scripts\python.exe (
  echo 先雙擊「1_安裝.bat」
  pause
  exit /b 1
)
venv\Scripts\python.exe collector.py --login
echo.
echo 下一步：雙擊「3_開始收集.bat」，或「4_開機自動收集.bat」讓它開機就跑。
pause
