@echo off
title LeadScout
cd /d "%~dp0"

rem ---- Find Python ----
set "PY="
where py >nul 2>nul && set "PY=py -3"
if not defined PY where python >nul 2>nul && set "PY=python"
if not defined PY (
  echo Python was not found. Install Python 3.10+ from https://www.python.org/downloads/
  echo and tick "Add python.exe to PATH" during setup, then run this again.
  pause
  exit /b 1
)

rem ---- First run: virtual environment ----
if not exist ".venv\Scripts\python.exe" (
  echo Creating a private Python environment in .venv ...
  %PY% -m venv .venv
  if errorlevel 1 ( echo Could not create the environment. & pause & exit /b 1 )
)

echo Installing / checking dependencies (the first run takes a minute) ...
".venv\Scripts\python.exe" -m pip install --disable-pip-version-check -q -r requirements.txt
if errorlevel 1 ( echo Installing dependencies failed. Check your internet connection. & pause & exit /b 1 )

rem ---- Settings file ----
if not exist ".env" (
  copy ".env.example" ".env" >nul
  echo.
  echo Created .env. To turn on AI, open .env in Notepad and set GROQ_API_KEY=your key
  echo Without a key LeadScout still works using offline rules.
  echo.
)

echo.
echo  LeadScout is starting...
echo    Homepage : http://localhost:8000
echo    App      : http://localhost:8000/app
echo  Press Ctrl+C in this window to stop it.
echo.
start "" cmd /c "timeout /t 4 >nul & start http://localhost:8000"
".venv\Scripts\python.exe" -m uvicorn main:app --reload
pause
