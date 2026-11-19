@echo off
chcp 65001 >nul
cd /d "%~dp0"

title Yuki 訊號收集中（關掉這個視窗就停止）
venv\Scripts\python.exe collector.py --loop
pause
