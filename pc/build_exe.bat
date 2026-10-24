@echo off
rem Builds dist\PocketMicReceiver\PocketMicReceiver.exe: a PyInstaller one-folder app without a
rem console window. Copy the whole dist\PocketMicReceiver folder wherever you like.
setlocal
cd /d "%~dp0"

python tools\make_icon.py "%~dp0build\PocketMic.ico" || goto :failed

python -m PyInstaller --noconfirm --clean --onedir --windowed ^
    --name PocketMicReceiver ^
    --icon "%~dp0build\PocketMic.ico" ^
    --hidden-import pystray._win32 ^
    --distpath "%~dp0dist" ^
    --workpath "%~dp0build\pyinstaller" ^
    --specpath "%~dp0build" ^
    "%~dp0PocketMic.pyw" || goto :failed

echo.
echo Built %~dp0dist\PocketMicReceiver\PocketMicReceiver.exe
exit /b 0

:failed
echo.
echo Build FAILED.
exit /b 1
