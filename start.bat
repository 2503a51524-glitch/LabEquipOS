@echo off
title LabEquip - Resource Control
cd /d "%~dp0"
python -m pip install -q -r requirements.txt
echo Starting LabEquip at http://127.0.0.1:5000  (close this window to stop the server)
python app.py
pause
