@echo off
chcp 65001 >nul
title Telegram 登入（收集 Yuki 訊號）
cd /d "%~dp0"
echo 第一次設定：登入你的 Telegram（唯讀，不會發訊息），選要收集的頻道，然後抓全部歷史、整理成對照檔。
echo 還沒有 api_id / api_hash 的話：瀏覽器開 https://my.telegram.org ，登入後點 API development tools，建一個 App（名稱隨便）。
echo.
"%~dp0venv\Scripts\python.exe" tg_collect.py --login
echo.
echo 好了之後，CRT 儀表板會每 5 分鐘自動抓新訊息（上方的 Yuki 狀態）。這個視窗可以關掉。
pause
