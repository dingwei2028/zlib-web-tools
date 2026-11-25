@echo off
chcp 65001 >nul
title zlib Cookie Tools
setlocal
set "PY=python"
where python >nul 2>&1
if errorlevel 1 set "PY=py"

:MENU
cls
echo ==================================================
echo   zlib session / cookie tools
echo ==================================================
echo.
echo   1  Edit cookie.txt  (fill id and key by hand)
echo   2  Check cookie status
echo   3  Backup cookie
echo   4  Restore cookie from backup
echo   5  Import browser cookies file (optional)
echo   6  Check / pin Z-Library domain
echo   7  Multi-account rotation download
echo   8  Exit
echo.
set /p "CHOICE=Select 1-8: "
if "%CHOICE%"=="1" goto EDITCOOKIE
if "%CHOICE%"=="2" goto STATUS
if "%CHOICE%"=="3" goto BACKUP
if "%CHOICE%"=="4" goto RESTORE
if "%CHOICE%"=="5" goto IMPORT
if "%CHOICE%"=="6" goto DOMAINS
if "%CHOICE%"=="7" goto ROTATE
if "%CHOICE%"=="8" goto END
goto MENU

:ROTATE
call "%~dp0rotate_download.bat"
goto MENU

:EDITCOOKIE
echo.
if not exist "%~dp0cookie.txt" (
  echo cookie.txt missing - creating it now.
  copy /Y "%~dp0cookie.example.txt" "%~dp0cookie.txt" >nul 2>&1
)
start notepad "%~dp0cookie.txt"
echo Edit in Notepad: paste values after the = signs, save, close.
echo Domain needs no change - it auto-follows.
echo.
pause
goto MENU

:STATUS
echo.
"%PY%" "%~dp0zlib_cookie.py" status
echo.
pause
goto MENU

:BACKUP
echo.
"%PY%" "%~dp0zlib_cookie.py" backup
echo.
pause
goto MENU

:RESTORE
echo.
"%PY%" "%~dp0zlib_cookie.py" restore
echo.
pause
goto MENU

:IMPORT
echo.
echo Put your Netscape cookies.txt in this folder first.
echo File name can be anything. Default: cookies.txt
echo.
set /p "CFILE=Cookie file path, blank for cookies.txt: "
if "%CFILE%"=="" set "CFILE=cookies.txt"
set /p "CDOM=Domain, blank for https://z-lib.gd: "
if "%CDOM%"=="" set "CDOM=https://z-lib.gd"
echo.
"%PY%" "%~dp0zlib_cookie.py" import "%CFILE%" "%CDOM%"
echo.
pause
goto MENU

:DOMAINS
echo.
echo Current: auto-follow latest working domain.
echo.
set /p "DPIN=Pin a domain, blank to just check: "
if "%DPIN%"=="" goto DOMAINS_CHECK
"%PY%" "%~dp0zlib_cookie.py" domains pin "%DPIN%"
echo.
pause
goto MENU

:DOMAINS_CHECK
"%PY%" "%~dp0zlib_cookie.py" domains
echo.
echo To undo a pin later, run: zlib_cookie.py domains auto
pause
goto MENU

:END
endlocal
