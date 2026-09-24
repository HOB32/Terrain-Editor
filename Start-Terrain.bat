@echo off
rem Starts the H1Z1 Terrain Tool and opens it in your browser (http://127.0.0.1:8766/terrain/).
rem The game is found through Steam, so no paths need editing. The only requirement
rem is Python 3.10+, which this installs through winget if it is missing.
rem Extra options go straight through, e.g.  Start-Terrain.bat --port 8770
cd /d "%~dp0"

set "PY="
where py >nul 2>nul && set "PY=py -3"
if not defined PY (
    python -c "import sys; sys.exit(sys.version_info < (3, 10))" >nul 2>nul && set "PY=python"
)
if not defined PY (
    echo Python 3 is not installed. Installing it now with winget...
    winget install -e --id Python.Python.3.13 --scope user --accept-source-agreements --accept-package-agreements
    if errorlevel 1 (
        echo.
        echo Could not install Python automatically. Install it from https://www.python.org/downloads/
        echo ^(tick "Add python.exe to PATH"^), then run this again.
        pause
        exit /b 1
    )
    set PY="%LOCALAPPDATA%\Programs\Python\Python313\python.exe"
)

%PY% terrain_server.py %*
pause
