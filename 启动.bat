@echo off
setlocal enabledelayedexpansion
chcp 65001 >nul
title Video Extractor
cd /d "%~dp0"

set "PYCMD="

rem 1) py launcher
py -3 -c "import sys" >nul 2>&1
if not errorlevel 1 (
    set "PYCMD=py -3"
    goto :found
)

rem 2) python already in PATH
python -c "import sys" >nul 2>&1
if not errorlevel 1 (
    set "PYCMD=python"
    goto :found
)

rem 3) common install locations (quoted, may contain spaces)
for %%D in (
    "%LOCALAPPDATA%\Programs\Python\Python313\python.exe"
    "%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
    "%LOCALAPPDATA%\Programs\Python\Python311\python.exe"
    "%LOCALAPPDATA%\Programs\Python\Python310\python.exe"
    "%USERPROFILE%\.workbuddy\binaries\python\versions\3.13.12\python.exe"
) do (
    if exist %%D (
        set PYCMD="%%~D"
        goto :found
    )
)

echo.
echo  [ERROR] No usable Python found.
echo.
echo  Install Python 3.9+ from https://www.python.org/downloads/
echo  and tick "Add python.exe to PATH" during setup.
echo.
pause
exit /b 1

:found
echo.
echo  Python : !PYCMD!
echo  Folder : %CD%
echo  ----------------------------------------------------
%PYCMD% server.py

echo.
echo  ----------------------------------------------------
echo  Server stopped. Press any key to close.
pause >nul
