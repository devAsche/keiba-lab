@echo off
rem 毎晩の処理: 過去結果の追加収集（最大6時間、続きから再開）と仮想購入の精算
chcp 65001 >nul
cd /d "%~dp0\..\.."
set PYTHONIOENCODING=utf-8
if not exist "data\logs" mkdir "data\logs"
".venv\Scripts\python.exe" -m keibalab nightly --max-minutes 360 %* >> "data\logs\task_nightly.log" 2>&1
