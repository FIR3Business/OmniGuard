@echo off
cd /d "%~dp0"

where py >nul 2>nul
if %errorlevel%==0 (
    py start.py
    goto end
)

where python >nul 2>nul
if %errorlevel%==0 (
    python start.py
    goto end
)

echo Python was not found.
echo Install Python 3.10 or newer, then run this file again.
pause

:end
