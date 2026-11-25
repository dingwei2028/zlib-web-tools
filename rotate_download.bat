@echo off
chcp 65001 >nul
title zlib Multi-Account Rotation
setlocal
set "PY=python"
where python >nul 2>&1
if errorlevel 1 set "PY=py"

:MENU
cls
echo ==================================================
echo   zlib multi-account rotation download
echo ==================================================
echo.
echo   1  Check all accounts quota
echo   2  Add an account  (guided, verifies cookie)
echo   3  Edit accounts.txt directly
echo   4  Download by book ID  (auto-switch on quota)
echo   5  Open download link in browser  (if 403 IP block)
echo   6  Open download folder
echo   7  Exit
echo.
echo Book ID comes from search: id:hash
echo Example: 6200944:b19fda
echo.
set /p "CHOICE=Select 1-7: "
if "%CHOICE%"=="1" goto QUOTA
if "%CHOICE%"=="2" goto ADD
if "%CHOICE%"=="3" goto EDIT
if "%CHOICE%"=="4" goto DOWNLOAD
if "%CHOICE%"=="5" goto BROWSER
if "%CHOICE%"=="6" goto FOLDER
if "%CHOICE%"=="7" goto END
goto MENU

:QUOTA
echo.
"%PY%" "%~dp0zlib_rotate.py" list
echo.
pause
goto MENU

:EDIT
start notepad "%~dp0accounts.txt"
echo Edit accounts.txt, one account per line: email ^| userid ^| userkey
echo Save and close, then come back.
echo.
pause
goto MENU

:ADD
echo.
echo Paste the email and two cookie values when prompted.
"%PY%" "%~dp0zlib_rotate.py" add
echo.
pause
goto MENU

:DOWNLOAD
echo.
echo Paste one or more book IDs (id:hash), separated by space:
set /p "IDS=IDs: "
if "%IDS%"=="" goto MENU
"%PY%" "%~dp0zlib_rotate.py" download %IDS%
echo.
pause
goto MENU

:BROWSER
echo.
echo Paste one or more book IDs (id:hash), separated by space:
set /p "IDS=IDs: "
if "%IDS%"=="" goto MENU
"%PY%" "%~dp0zlib_rotate.py" browser %IDS%
echo.
pause
goto MENU

:FOLDER
for /f "usebackq delims=" %%D in (`python "%~dp0zlib_rotate.py" folder`) do set "DLDIR=%%D"
if defined DLDIR explorer "%DLDIR%"
goto MENU

:END
endlocal
