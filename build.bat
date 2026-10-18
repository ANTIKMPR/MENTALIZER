@echo off
setlocal
cd /d "%~dp0"
echo Installing build tools...
python -m pip install -U pyinstaller pillow numpy telethon "python-socks[asyncio]" pystray
if errorlevel 1 goto fail
if not exist bio_checkin.py goto nofiles
if not exist mentalizer_logo.png goto nofiles
if not exist mentalizer.ico goto nofiles
echo Building...
if exist fonts goto withfonts
python -m PyInstaller --noconfirm --clean --onedir --console --name mentalizer --icon mentalizer.ico --add-data "mentalizer_logo.png;." --collect-submodules telethon --hidden-import python_socks --hidden-import PIL.ImageTk --hidden-import pystray._win32 bio_checkin.py
goto built
:withfonts
python -m PyInstaller --noconfirm --clean --onedir --console --name mentalizer --icon mentalizer.ico --add-data "mentalizer_logo.png;." --add-data "fonts;fonts" --collect-submodules telethon --hidden-import python_socks --hidden-import PIL.ImageTk --hidden-import pystray._win32 bio_checkin.py
:built
if errorlevel 1 goto fail
echo.
echo Done: dist\mentalizer\mentalizer.exe
echo First run creates config.json, links.json, the session file and mentalizer.log next to the exe.
echo First Telegram login: dist\mentalizer\mentalizer.exe --login
goto end
:nofiles
echo Put bio_checkin.py, mentalizer_logo.png and mentalizer.ico next to build.bat and run again.
goto end
:fail
echo.
echo Build failed - see the messages above.
:end
pause
