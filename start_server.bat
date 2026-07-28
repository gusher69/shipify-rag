@echo off
cd /d C:\Users\ThanarojThanarojthan\shipify-rag
python -m uvicorn admin.routes:app --host 0.0.0.0 --port 8001 --log-level debug 2>&1
pause
