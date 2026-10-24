@echo off
rem Starts the PocketMic receiver from source. Extra arguments are passed through, e.g.
rem   run_pocketmic.bat --nogui
rem   run_pocketmic.bat --list-devices
cd /d "%~dp0"
python -m pocketmic %*
