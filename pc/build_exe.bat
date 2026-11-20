@echo off
REM Builds a standalone RemoteCam.exe (in dist\)
REM Optional: a portable Python in pc\Python312_x64 (with python_env.bat) is used if present.
cd /d "%~dp0"
if exist "%~dp0Python312_x64\python_env.bat" call "%~dp0Python312_x64\python_env.bat"
python -m pip install -r requirements.txt pyinstaller
python -m PyInstaller --noconfirm --onefile --windowed --name RemoteCam remotecam_sender.py
pause
