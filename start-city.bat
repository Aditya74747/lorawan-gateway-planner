@echo off
rem ---------------------------------------------------------------------------
rem  LoRaWAN Planner for ANOTHER city - double-click to start (default: Stuttgart)
rem  Usage:  start-city.bat            -> cities\stuttgart on http://localhost:8001
rem          start-city.bat ulm 8002   -> cities\ulm      on http://localhost:8002
rem  First start: builds the city from cities\<name>\raw (a few minutes), later starts at once.
rem  The planner runs as long as this window is open; closing the window stops it.
rem  Needs the Heilbronn model once: run start-planner.bat / "uv run hnplan all" before.
rem ---------------------------------------------------------------------------
setlocal
cd /d "%~dp0"
set "CITY=%~1"
if "%CITY%"=="" set "CITY=stuttgart"
set "PORT=%~2"
if "%PORT%"=="" set "PORT=8001"
set "URL=http://localhost:%PORT%"
set "PYTHONIOENCODING=utf-8"
set "PYTHONWARNINGS=ignore"
title LoRaWAN Planner - %CITY%

echo.
echo   LoRaWAN Planner - %CITY%
echo   ------------------------
echo.
where uv >nul 2>&1
if errorlevel 1 (
  echo   uv is not installed - see SETUP.md, step 1.
  pause
  exit /b 1
)
if not exist "cities\%CITY%\raw" (
  echo   Folder cities\%CITY%\raw not found. Create it and put the city's data there - see cities\README.md
  pause
  exit /b 1
)
rem --- this window owns the server: stop a planner left over from an earlier window --
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\free-port.ps1" -Port %PORT%
if errorlevel 1 (
  echo   Close that program, or start on another port:  start-city.bat %CITY% 8010
  echo.
  pause
  exit /b 1
)
if not exist "cities\%CITY%\web_data\insights.json" (
  echo   [1/2] First start: analysing cities\%CITY%\raw ^(3D city, addresses, rooftop sites, reception^) ...
  uv run hnplan city %CITY% all
  if errorlevel 1 (
    echo.
    echo   The analysis failed - see the message above.
    pause
    exit /b 1
  )
) else (
  echo   [1/2] Using the analysed data in cities\%CITY%\web_data  ^(rebuild: uv run hnplan city %CITY% all --force^)
)
if not defined PLANNER_NO_BROWSER start "" /min powershell -NoProfile -WindowStyle Hidden -Command ^
  "for ($i = 0; $i -lt 120; $i++) { try { Invoke-WebRequest 'http://127.0.0.1:%PORT%/api/health' -UseBasicParsing -TimeoutSec 2 | Out-Null; Start-Process '%URL%'; break } catch { Start-Sleep -Seconds 1 } }"
echo.
echo   [2/2] Starting the planner at %URL%  - close this window to stop it.
echo.
uv run hnplan city %CITY% serve --port %PORT%
echo.
echo   The planner has stopped.
pause
