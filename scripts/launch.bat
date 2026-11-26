@echo off
cd /d "%~dp0.."
where py >nul 2>nul
if errorlevel 1 (
    python scripts\launch.py %*
) else (
    py -3 scripts\launch.py %*
)
if errorlevel 1 pause
