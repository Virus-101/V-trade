@echo off
rem V-trade launcher. No arguments = paper trading. Otherwise passes arguments through:
rem   run.bat fetch  |  run.bat train  |  run.bat backtest  |  run.bat status
cd /d %~dp0
if not exist .venv\Scripts\python.exe (
  echo Creating virtual environment...
  python -m venv .venv || exit /b 1
  .venv\Scripts\python -m pip install -r requirements.txt || exit /b 1
)
if "%~1"=="" (
  .venv\Scripts\python -m vtrade paper
) else (
  .venv\Scripts\python -m vtrade %*
)
