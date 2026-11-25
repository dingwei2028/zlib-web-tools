@echo off
chcp 65001 >nul
cd /d "%~dp0"
python "zlib_web.py"
pause
