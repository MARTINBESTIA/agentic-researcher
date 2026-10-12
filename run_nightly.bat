@echo off
REM Nocny beh Problem Radar v0.2: SearXNG, modely (Ollama alebo llama-swap), doctor --wait, run-all ako uloha Spravcu akcii (radar job).
REM Pri llama-swap ho na konci vypne aj s procesmi llama-server. Podrobnosti: scripts\run_weekly.ps1
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\run_weekly.ps1 -Minutes 480
