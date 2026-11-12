@echo off
cd /d %~dp0..
set PULSE_DSN=postgresql://postgres@127.0.0.1:55432/pulse
set PULSE_ENV=dev
set PULSE_API_HOST=127.0.0.1
set PULSE_DEV_PRINCIPAL=00000000-0000-0000-0000-000000000001
.venv\Scripts\python.exe -m uvicorn pulse.api.app:app --host 127.0.0.1 --port 8200
