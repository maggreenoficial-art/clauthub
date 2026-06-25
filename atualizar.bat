@echo off
cd /d "%~dp0.."
python scripts\update_metrics.py
pause
