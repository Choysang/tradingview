@echo off
rem Daily forward run on this PC (scheduled task PulseDailyForward): see daily_forward.py.
cd /d "%~dp0..\.."
set PYTHONIOENCODING=utf-8
"backend\.venv\Scripts\python.exe" scripts\lab\daily_forward.py >> .work\lab\live\daily_forward.out 2>&1
