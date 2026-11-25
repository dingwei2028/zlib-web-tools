@echo off
chcp 65001 >nul
setlocal
set "ZLIB_EXE=C:\GitHub\zlib\zlib.exe"
set "SESSION=%USERPROFILE%\.config\zlib\session.json"

if not exist "%ZLIB_EXE%" goto :noexe
if not exist "%SESSION%" goto :needlogin

echo Searching queries from query.txt
python "%~dp0zlib_web.py" --search-file "%~dp0query.txt" --count 5
echo.
echo Press any key to close.
pause
exit /b 0

:needlogin
echo Not logged in. Opening login flow first.
call "%~dp0login_and_test.bat"
exit /b 0

:noexe
echo [ERROR] Not found: %ZLIB_EXE%
pause
exit /b 1
