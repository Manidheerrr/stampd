@echo off
title Stampd - invoice decisions
cd /d "%~dp0"
echo.
echo   Starting Stampd...
set "PY="
for %%P in ("%LOCALAPPDATA%\Programs\Python\Python313\python.exe" "%LOCALAPPDATA%\Programs\Python\Python312\python.exe" "%LOCALAPPDATA%\Programs\Python\Python311\python.exe" "%ProgramFiles%\Python312\python.exe" "%ProgramFiles%\Python313\python.exe") do if exist %%P if not defined PY set "PY=%%~P"
if not defined PY ( py -3 --version >/dev/null 2>&1 && set "PY=py -3" )
if not defined PY (
  echo   Python not found. Installing Python 3.12 for your user - about 1-2 minutes...
  winget install -e --id Python.Python.3.12 --scope user --silent --accept-package-agreements --accept-source-agreements
  if exist "%LOCALAPPDATA%\Programs\Python\Python312\python.exe" set "PY=%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
)
if not defined PY (
  echo.
  echo   Could not install Python automatically.
  echo   Install it from https://www.python.org/downloads/ then run start.bat again.
  pause
  exit /b
)
echo   Using Python: %PY%
echo   Installing packages - first run only, about 1 minute...
"%PY%" -m pip install -q --disable-pip-version-check -r requirements.txt 2>/dev/null || %PY% -m pip install -q --disable-pip-version-check -r requirements.txt
echo.
echo   ============================================================
echo     Stampd is running at  http://localhost:8000
echo     Keep this window open while you use it.
echo   ============================================================
start "" http://localhost:8000
"%PY%" -m uvicorn app.main:app --port 8000 2>/dev/null || %PY% -m uvicorn app.main:app --port 8000
pause
