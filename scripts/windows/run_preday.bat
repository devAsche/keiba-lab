@echo off
rem 前日夜に、翌日の全レースのオッズを1回だけ記録する（翌日が開催でなければすぐ終わる）
chcp 65001 >nul
cd /d "%~dp0\..\.."
set PYTHONIOENCODING=utf-8
if not exist "data\logs" mkdir "data\logs"
".venv\Scripts\python.exe" -m keibalab odds-once --date tomorrow %* >> "data\logs\task_preday.log" 2>&1
