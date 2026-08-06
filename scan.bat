@echo off
cd /d "%~dp0"
uv run poescan scan --tabs "#1,#2"
pause