@echo off
rem Double-click to install, or to update after downloading a newer version.
rem
rem Your own files (settings, what you have applied to) live in
rem Documents\Job hunt and are never overwritten. The program itself goes in
rem %LOCALAPPDATA%\jobhunt, out of the way of OneDrive syncing.
rem
rem Written with goto rather than ( ) blocks on purpose: a folder name with a
rem bracket in it, such as "OneDrive - Company (UK)", breaks blocks.

setlocal
title Job hunt - installer
set "HERE=%~dp0"
set "RC=0"

echo Job hunt - installer
echo ====================
echo.

if not exist "%HERE%program\pyproject.toml" goto :nozip

set "DOCS="
for /f "usebackq delims=" %%D in (`powershell -NoProfile -Command "[Environment]::GetFolderPath('MyDocuments')"`) do set "DOCS=%%D"
if not defined DOCS set "DOCS=%USERPROFILE%\Documents"
set "HOMEDIR=%DOCS%\Job hunt"
set "APPDIR=%LOCALAPPDATA%\jobhunt"
set "VPY=%APPDIR%\venv\Scripts\python.exe"

rem Files downloaded from the internet are marked as such. Clearing the mark
rem now means the other launchers open without a security prompt.
powershell -NoProfile -Command "Get-ChildItem -LiteralPath $env:HERE -Recurse | Unblock-File" >nul 2>nul

rem Find Python 3.10 or newer: the py launcher first, as python.org installs
rem it, then plain python. The Microsoft Store placeholder fails this check.
set "PY="
py -3 -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>nul
if not errorlevel 1 set "PY=py -3"
if defined PY goto :havepy
python -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>nul
if not errorlevel 1 set "PY=python"
if defined PY goto :havepy
goto :nopython

:havepy
for /f "delims=" %%V in ('%PY% --version 2^>^&1') do echo Using %%V
echo Setting up the program (this takes a minute)...

if not exist "%APPDIR%" mkdir "%APPDIR%"
if not exist "%HOMEDIR%\data" mkdir "%HOMEDIR%\data"
if not exist "%VPY%" %PY% -m venv "%APPDIR%\venv"
if not exist "%VPY%" goto :venvfail

"%VPY%" -m pip install --quiet --disable-pip-version-check --upgrade --force-reinstall --no-deps "%HERE%program"
if errorlevel 1 goto :pipfail
"%VPY%" -m pip install --quiet --disable-pip-version-check "%HERE%program"
if errorlevel 1 goto :pipfail

if exist "%HOMEDIR%\config.yaml" goto :keepconfig
copy /y "%HERE%program\config.yaml" "%HOMEDIR%\config.yaml" >nul
echo Created your settings file.
goto :configdone
:keepconfig
echo Keeping your existing settings.
:configdone

copy /y "%HERE%program\installer\home\Open job board.bat" "%HOMEDIR%\Open job board.bat" >nul
copy /y "%HERE%program\installer\home\Edit my settings.bat" "%HOMEDIR%\Edit my settings.bat" >nul
copy /y "%HERE%START HERE.html" "%HOMEDIR%\START HERE.html" >nul 2>nul

rem Remember where the program is, so the launchers find it.
> "%HOMEDIR%\.program" echo %VPY%

echo.
echo Done. Your Job hunt folder is in Documents and is opening now.
echo From now on, double-click "Open job board" in that folder.
echo You can delete the download.
if not defined JOBHUNT_NO_PAUSE explorer "%HOMEDIR%"
goto :finish

:nozip
echo Can't find the "program" folder next to this file.
echo Right-click the download, choose "Extract All...", then double-click
echo Install inside the extracted folder.
set "RC=1"
goto :finish

:nopython
echo Python 3.10 or newer is needed and was not found.
echo.
echo 1. Install it from python.org (opening the page now).
echo    Click "Download Python", run the installer, and tick
echo    "Add python.exe to PATH" on its first screen.
echo 2. Then double-click this Install file again.
if not defined JOBHUNT_NO_PAUSE start "" "https://www.python.org/downloads/"
set "RC=1"
goto :finish

:venvfail
echo Could not set up Python in %APPDIR%.
set "RC=1"
goto :finish

:pipfail
echo.
echo The install did not finish. Check you are online and try again.
set "RC=1"
goto :finish

:finish
echo.
if not defined JOBHUNT_NO_PAUSE pause
exit /b %RC%
