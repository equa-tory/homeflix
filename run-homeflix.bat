@echo off
setlocal enabledelayedexpansion
REM ============================================================================
REM  HomeFlix Windows launcher
REM
REM  Double-click this file to set up (once) and run HomeFlix on Windows.
REM  It creates a local virtualenv, installs Django, makes sure ffmpeg/ffprobe
REM  are available (downloading a copy if needed), then runs migrate + scan +
REM  the dev server. gunicorn (used in the Linux/systemd deployment) doesn't
REM  work on Windows, so this always uses "manage.py runserver".
REM ============================================================================

REM ---- 1. EDIT THESE -----------------------------------------------------
REM Folder with your videos (required).
set "HOMEFLIX_LIBRARY=Y:\YouTube"

REM Optional: UNC path shown under the player so you can jump to the file
REM from another PC on your LAN, e.g. \\MY-PC\Videos. Leave blank to use
REM config/settings.py's built-in placeholder.
set "HOMEFLIX_REMOTE_ROOT="

REM Port to serve on. 80 needs the terminal running "as Administrator";
REM 8002 (or anything >1024) does not.
set "PORT=80"
REM -------------------------------------------------------------------------

cd /d "%~dp0"
echo.
echo == HomeFlix (Windows) ==
echo Library: %HOMEFLIX_LIBRARY%
echo Port:    %PORT%
echo.

if not exist "%HOMEFLIX_LIBRARY%" (
    echo [WARN] HOMEFLIX_LIBRARY folder does not exist: %HOMEFLIX_LIBRARY%
    echo        Edit the HOMEFLIX_LIBRARY line at the top of this .bat, then re-run.
    echo.
)

REM ---- 2. Find a Python interpreter ---------------------------------------
set "PY_CMD="
where py >nul 2>nul && set "PY_CMD=py -3"
if not defined PY_CMD (
    where python >nul 2>nul && set "PY_CMD=python"
)
if not defined PY_CMD (
    echo [ERROR] No Python found on PATH. Install Python 3.10+ from python.org
    echo         ^(check "Add python.exe to PATH" during install^) and re-run.
    pause
    exit /b 1
)

REM ---- 3. Create / reuse the virtualenv -----------------------------------
if not exist ".venv\Scripts\python.exe" (
    echo Creating virtual environment in .venv ...
    %PY_CMD% -m venv .venv
    if not exist ".venv\Scripts\python.exe" (
        echo [ERROR] Failed to create .venv
        pause
        exit /b 1
    )
)
set "VENV_PY=%~dp0.venv\Scripts\python.exe"

REM ---- 4. Install Python dependencies -------------------------------------
echo Installing/upgrading Python dependencies ...
"%VENV_PY%" -m pip install --upgrade pip --quiet
"%VENV_PY%" -m pip install -r requirements.txt --quiet
if errorlevel 1 (
    echo [ERROR] pip install failed - see the output above.
    pause
    exit /b 1
)

REM ---- 5. Make sure ffmpeg/ffprobe are available --------------------------
REM  ffprobe/probing/thumbnails/HLS transcode/MKV conversion all shell out to
REM  "ffmpeg"/"ffprobe" on PATH. Installed OUTSIDE the homeflix folder (under
REM  Documents) so re-cloning/updating the repo never touches it, and so it's
REM  reused across projects.
set "FFMPEG_HOME=%USERPROFILE%\Documents\ffmpeg"
set "FFMPEG_ZIP_URL=https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip"
set "FFMPEG_BIN="

set "HAVE_FFMPEG=0"
where ffmpeg >nul 2>nul && where ffprobe >nul 2>nul && set "HAVE_FFMPEG=1"
if "%HAVE_FFMPEG%"=="1" (
    echo ffmpeg/ffprobe already on PATH - skipping download.
    goto :ffmpeg_ready
)

REM Already downloaded previously? (look for ffmpeg.exe under any bin\ subfolder)
if exist "%FFMPEG_HOME%" (
    for /f "delims=" %%D in ('dir /b /s "%FFMPEG_HOME%\ffmpeg.exe" 2^>nul') do (
        set "FFMPEG_BIN=%%~dpD"
    )
)

if defined FFMPEG_BIN (
    echo Found existing ffmpeg in "!FFMPEG_BIN!" - reusing it.
    goto :ffmpeg_addpath
)

echo ffmpeg not found - downloading a static build to "%FFMPEG_HOME%" ...
echo ^(one-time, ~80MB; needs internet access^)
if not exist "%FFMPEG_HOME%" mkdir "%FFMPEG_HOME%"
set "FFMPEG_ZIP=%TEMP%\ffmpeg-homeflix.zip"

powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$ProgressPreference='SilentlyContinue'; try { Invoke-WebRequest -Uri '%FFMPEG_ZIP_URL%' -OutFile '%FFMPEG_ZIP%' } catch { exit 1 }"
if errorlevel 1 (
    echo [ERROR] Could not download ffmpeg. Check your internet connection, or
    echo         install ffmpeg manually and make sure it's on PATH, then re-run.
    pause
    exit /b 1
)

powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "Expand-Archive -Path '%FFMPEG_ZIP%' -DestinationPath '%FFMPEG_HOME%' -Force"
del "%FFMPEG_ZIP%" >nul 2>nul

for /f "delims=" %%D in ('dir /b /s "%FFMPEG_HOME%\ffmpeg.exe" 2^>nul') do (
    set "FFMPEG_BIN=%%~dpD"
)
if not defined FFMPEG_BIN (
    echo [ERROR] Downloaded ffmpeg but couldn't locate ffmpeg.exe under %FFMPEG_HOME%
    pause
    exit /b 1
)

:ffmpeg_addpath
REM Strip trailing backslash before adding to PATH.
if "!FFMPEG_BIN:~-1!"=="\" set "FFMPEG_BIN=!FFMPEG_BIN:~0,-1!"
set "PATH=!FFMPEG_BIN!;%PATH%"
echo Using ffmpeg from "!FFMPEG_BIN!"

:ffmpeg_ready

REM ---- 6. Migrate + scan + run --------------------------------------------
echo.
echo Running migrations ...
"%VENV_PY%" manage.py migrate
:: if errorlevel 1 (
::    echo [ERROR] migrate failed - see the output above.
::    pause
::    exit /b 1
::)

echo Scanning library (first run may take a while) ...
:: "%VENV_PY%" manage.py scan

echo.
echo == Starting HomeFlix on http://0.0.0.0:%PORT%/  (Ctrl+C to stop) ==
echo    From another device on your LAN, browse to http://^<this-PC's-IP^>:%PORT%/
echo.
"%VENV_PY%" manage.py runserver 0.0.0.0:%PORT%

pause
