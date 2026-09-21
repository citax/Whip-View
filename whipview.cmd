@echo off
REM Whip-View launcher (Windows). Double-click, or: whipview.cmd [project-root] [--port N]
REM Project root: 1st argument > first line of whipview.local > current folder.
setlocal
set "HERE=%~dp0"
set "PROJ="
set "A1=%~1"
if defined A1 if not "%A1:~0,2%"=="--" (
  set "PROJ=%A1%"
  shift
)
if "%PROJ%"=="" if exist "%HERE%whipview.local" set /p PROJ=<"%HERE%whipview.local"
if "%PROJ%"=="" set "PROJ=%CD%"
REM Try each candidate for real: skips the Windows Store "python" alias stub.
set "PY="
py -3 -c "import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)" >nul 2>&1 && set "PY=py -3"
if not defined PY python -c "import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)" >nul 2>&1 && set "PY=python"
if not defined PY (
  echo Python 3.9+ not found. Install it from https://www.python.org/downloads/
  pause
  exit /b 1
)
%PY% "%HERE%whipview.py" "%PROJ%" %1 %2 %3 %4
if errorlevel 1 pause
