@echo off
rem Spustí GUI Problem Radar 0.2 (127.0.0.1:8501) – funguje z hocijakého priečinka aj dvojklikom.
cd /d "%~dp0"
".venv\Scripts\python.exe" -m radar gui
