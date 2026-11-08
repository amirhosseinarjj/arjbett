@echo off
chcp 65001 >nul
cd /d "%~dp0backend"
if not exist .venv (
  py -m venv .venv
)
call .venv\Scripts\activate
python -m pip install --upgrade pip
pip install -r requirements.txt
start "" http://127.0.0.1:8000
uvicorn main:app --host 127.0.0.1 --port 8000
pause
