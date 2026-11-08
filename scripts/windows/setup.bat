@echo off
rem 初回だけ実行: 仮想環境を作り、ライブラリを入れ、テストを流す（Museが SSH から実行する想定。pause はしない）
chcp 65001 >nul
cd /d "%~dp0\..\.."
if not exist ".venv" (
  py -3 -m venv .venv || python -m venv .venv
)
".venv\Scripts\python.exe" -m pip install --upgrade pip
".venv\Scripts\python.exe" -m pip install -r requirements.txt
".venv\Scripts\python.exe" -m pytest -q tests
".venv\Scripts\python.exe" -m keibalab status
