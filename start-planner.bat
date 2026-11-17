@echo off
rem ---------------------------------------------------------------------------
rem  Heilbronn LoRaWAN Planner - double-click to start
rem  1. installs / updates the libraries (uv sync)
rem  2. starts the web app (uv run hnplan serve)
rem  3. opens http://localhost:8000 in the browser as soon as it is ready
rem  The planner runs as long as this window is open; closing the window stops it.
rem ---------------------------------------------------------------------------
setlocal
title Heilbronn LoRaWAN Planner
cd /d "%~dp0"
rem optional: set PLANNER_PORT=8001 to use another port, PLANNER_NO_BROWSER=1 to skip opening the browser
set "PORT=8000"
if defined PLANNER_PORT set "PORT=%PLANNER_PORT%"
set "URL=http://localhost:%PORT%"
set "PYTHONIOENCODING=utf-8"
set "PYTHONWARNINGS=ignore"

echo.
echo   Heilbronn LoRaWAN Planner
echo   -------------------------
echo.

rem --- uv installed? ----------------------------------------------------------
where uv >nul 2>&1
if errorlevel 1 (
  echo   uv is not installed. Open PowerShell and run:
  echo.
  echo     powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
  echo.
  echo   Then close PowerShell and double-click this file again.
  echo.
  pause
  exit /b 1
)

rem --- this window owns the server: stop a planner left over from an earlier window --
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\free-port.ps1" -Port %PORT%
if errorlevel 1 (
  echo   Close that program, or start on another port:  set PLANNER_PORT=8010  and run this file again.
  echo.
  pause
  exit /b 1
)

rem --- install / update libraries ---------------------------------------------
echo   [1/2] Checking libraries (uv sync) ...
uv sync
if errorlevel 1 (
  echo.
  echo   uv sync failed - see the message above.
  echo   Tip: if it says a file is "used by another process", close any other
  echo   planner window first and try again.
  echo.
  pause
  exit /b 1
)

rem --- open the browser as soon as the server answers (in the background) -----
if not defined PLANNER_NO_BROWSER start "" /min powershell -NoProfile -WindowStyle Hidden -Command ^
  "for ($i = 0; $i -lt 120; $i++) { try { Invoke-WebRequest 'http://127.0.0.1:%PORT%/api/health' -UseBasicParsing -TimeoutSec 2 | Out-Null; Start-Process '%URL%'; break } catch { Start-Sleep -Seconds 1 } }"

rem --- start the planner ------------------------------------------------------
echo.
echo   [2/2] Starting the planner at %URL%
echo         The browser opens automatically. Keep this window open while you work;
echo         close it (or press Ctrl+C) to stop the planner.
echo.
uv run hnplan serve --host 127.0.0.1 --port %PORT%

echo.
echo   The planner has stopped.
pause
