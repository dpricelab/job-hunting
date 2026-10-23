@echo off
rem Double-click to check every careers page and open your job board.
rem Close this window when you have finished.

setlocal
title Job hunt
set "HOMEDIR=%~dp0"
set "VPY="
if exist "%HOMEDIR%.program" set /p VPY=<"%HOMEDIR%.program"
if not defined VPY goto :missing
if not exist "%VPY%" goto :missing

echo Checking careers pages - this takes a minute or two...
echo.
"%VPY%" -m jobhunt --config "%HOMEDIR%config.yaml" --data-dir "%HOMEDIR%data" fetch
echo.
echo Opening your job board in the browser.
echo Leave this window open while you use it; close it when you are done.
echo.
"%VPY%" -m jobhunt --config "%HOMEDIR%config.yaml" --data-dir "%HOMEDIR%data" serve
pause
exit /b 0

:missing
echo The program is missing. Double-click Install in the download again.
pause
exit /b 1
