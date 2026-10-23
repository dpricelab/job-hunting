@echo off
rem Double-click to change which roles, places and companies are searched.
rem Opens the settings in Notepad, then checks them once Notepad is closed.

setlocal
title Job hunt - settings
set "HOMEDIR=%~dp0"
set "VPY="
if exist "%HOMEDIR%.program" set /p VPY=<"%HOMEDIR%.program"

echo Your settings are open in Notepad.
echo Make your changes, save (Ctrl+S) and close Notepad to have them checked.
notepad "%HOMEDIR%config.yaml"
echo.

if not defined VPY goto :done
if not exist "%VPY%" goto :done
"%VPY%" -m jobhunt --config "%HOMEDIR%config.yaml" --data-dir "%HOMEDIR%data" rescore
if errorlevel 1 goto :bad
echo.
echo Settings look good. They apply next time you open the job board.
goto :done

:bad
echo.
echo Something in the settings could not be read (see above).
echo Double-click Edit my settings again to fix it.

:done
echo.
pause
