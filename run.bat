@echo off
rem V-trade launcher. No arguments = open the dashboard in your browser.
rem Otherwise passes arguments through, e.g.:  run.bat backtest  |  run.bat paper  |  run.bat status
cd /d %~dp0
if not exist .venv\Scripts\python.exe (
  echo Creating virtual environment...
  python -m venv .venv || exit /b 1
  .venv\Scripts\python -m pip install -r requirements.txt || exit /b 1
)
if "%~1"=="" (
  .venv\Scripts\python -m vtrade dashboard
) else (
  .venv\Scripts\python -m vtrade %*
)
