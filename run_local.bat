@echo off
REM Run XODUS on this laptop (SQLite, data kept in App_Data). Double-click or run from the repo folder.
REM Optional: set DATABASE_URL first to use PostgreSQL instead (or use: docker compose up --build).
cd /d "%~dp0"
if not exist .venv\Scripts\python.exe (
  echo Creating virtual environment...
  py -3 -m venv .venv || python -m venv .venv || goto :nopython
)
call .venv\Scripts\activate.bat
python -m pip install -q -r requirements.txt || goto :fail
if "%XODUS_BOOTSTRAP_ADMIN_EMAIL%"=="" set XODUS_BOOTSTRAP_ADMIN_EMAIL=jeeva@simplicitywealth.com
if "%XODUS_SEED_DEMO%"=="" set XODUS_SEED_DEMO=1
echo.
echo XODUS is starting at http://127.0.0.1:8000
echo First time: open /register and register %XODUS_BOOTSTRAP_ADMIN_EMAIL% (it becomes Platform Admin).
echo Stop with Ctrl+C.
echo.
python -m uvicorn ui.app:app --host 127.0.0.1 --port 8000
goto :eof
:nopython
echo Python 3 was not found. Install it from python.org, then run this again.
pause
goto :eof
:fail
echo Installing requirements failed. See the messages above.
pause
