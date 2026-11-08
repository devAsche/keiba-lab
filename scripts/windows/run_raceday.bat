@echo off
rem 開催日のオッズ記録（モデルがあれば仮想購入も）。開催のない日はすぐ終わる。
chcp 65001 >nul
cd /d "%~dp0\..\.."
set PYTHONIOENCODING=utf-8
if not exist "data\logs" mkdir "data\logs"
".venv\Scripts\python.exe" -m keibalab raceday %* >> "data\logs\task_raceday.log" 2>&1
