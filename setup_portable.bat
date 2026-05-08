@echo off
setlocal
set ROOT=%~dp0
set PYDIR=%ROOT%python
set PYVER=3.13.3
set PYZIP=%ROOT%python_embed.zip
set PYURL=https://www.python.org/ftp/python/%PYVER%/python-%PYVER%-embed-amd64.zip
set PIPURL=https://bootstrap.pypa.io/get-pip.py

echo === VideoTranscriber Portable Setup ===

:: 1. Download + extract embeddable Python
if exist "%PYDIR%\python.exe" (
    echo Python already exists, skipping download.
) else (
    echo Downloading Python %PYVER% embed...
    powershell -Command "Invoke-WebRequest '%PYURL%' -OutFile '%PYZIP%'"
    powershell -Command "Expand-Archive '%PYZIP%' -DestinationPath '%PYDIR%' -Force"
    del "%PYZIP%"
    echo Python extracted.
)

:: 2. Enable site-packages (uncomment import site in ._pth)
powershell -Command "(Get-Content (Get-ChildItem '%PYDIR%\*._pth')[0].FullName -Raw) -replace '#import site','import site' | Set-Content (Get-ChildItem '%PYDIR%\*._pth')[0].FullName -NoNewline"

:: 3. Install pip
if exist "%PYDIR%\Scripts\pip.exe" (
    echo pip already installed.
) else (
    echo Installing pip...
    powershell -Command "Invoke-WebRequest '%PIPURL%' -OutFile '%PYDIR%\get-pip.py'"
    "%PYDIR%\python.exe" "%PYDIR%\get-pip.py" --no-warn-script-location
    del "%PYDIR%\get-pip.py"
    echo pip installed.
)

:: 4. Install dependencies
echo Installing dependencies (may take a few minutes)...
"%PYDIR%\Scripts\pip.exe" install ^
    "PySide6>=6.7" ^
    "faster-whisper>=1.1.0" ^
    "imageio-ffmpeg>=0.5.1" ^
    "noisereduce>=3.0" ^
    "soundfile>=0.12" ^
    "scipy>=1.11" ^
    --no-warn-script-location ^
    --isolated

echo.
echo === Setup complete ===
echo Launch: double-click run.bat
pause
