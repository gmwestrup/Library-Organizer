@echo off
echo Installing the Python packages Library Organizer needs...
py -m pip install --upgrade -r "%~dp0requirements.txt" || pip install --upgrade -r "%~dp0requirements.txt"
echo.
echo Optional: to let it LISTEN to audiobook intros (title / author / narrator
echo are usually read out in the first minute), also run:
echo     py -m pip install faster-whisper
echo.
echo Optional: install ffmpeg (https://ffmpeg.org) for the most accurate audio probing.
echo.
echo Done. Now double-click LibraryOrganizer.pyw
pause
