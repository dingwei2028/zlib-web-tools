@echo off
chcp 65001 >nul
setlocal

echo ==================================================
echo   zlib login + search test
echo ==================================================
echo.
echo Step 1: text-mode login (no TUI form).
echo   - Domain: press Enter to use https://z-lib.gd
echo   - Email / password: typed below, password is hidden
echo.

python "%~dp0zlib_login.py"
if errorlevel 1 goto :loginfail

echo.
echo Step 2: searching queries from query.txt
python "%~dp0zlib_web.py" --search-file "%~dp0query.txt" --count 5
echo.
echo Done. Press any key to close.
pause
exit /b 0

:loginfail
echo [INFO] Login failed or cancelled. Search skipped.
pause
exit /b 1
