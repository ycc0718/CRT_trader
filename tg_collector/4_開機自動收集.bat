@echo off
chcp 65001 >nul
cd /d "%~dp0"

venv\Scripts\python.exe collector.py --autostart on
echo 之後登入 Windows 就會自動開始收集（視窗縮在工作列）。現在要開始的話，雙擊「3_開始收集.bat」。
pause
