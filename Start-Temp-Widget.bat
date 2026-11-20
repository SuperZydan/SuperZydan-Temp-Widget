@echo off
rem Starts the temp widget with no console window.
rem Looks for the Python launcher (pyw) first, then pythonw on PATH.
rem Right-click the widget for Settings, layout, size, lock position, quit.
setlocal
set "HERE=%~dp0"

where pyw >nul 2>nul
if %errorlevel%==0 (
    start "" pyw -3 "%HERE%temp_widget.py" %*
    exit /b 0
)

where pythonw >nul 2>nul
if %errorlevel%==0 (
    start "" pythonw "%HERE%temp_widget.py" %*
    exit /b 0
)

echo Could not find Python. Install Python 3 from https://www.python.org/downloads/
echo and tick "Add python.exe to PATH" (or keep the "py launcher" option) in the installer.
pause
exit /b 1
