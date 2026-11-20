@echo off
REM Builds a standalone RemoteCam.exe (in dist\)
python -m pip install -r requirements.txt pyinstaller
python -m PyInstaller --noconfirm --onefile --windowed --name RemoteCam remotecam_sender.py
pause
