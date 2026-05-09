@echo off
echo [1/4] Building Python backend...
cd ..
python -m PyInstaller api.spec --noconfirm

echo [2/4] Copying backend to resources...
if not exist "gui\resources\api" mkdir "gui\resources\api"
copy "dist\api.exe" "gui\resources\api\api.exe" /Y

echo [3/4] Installing frontend dependencies...
cd gui
call npm install

echo [4/4] Building Electron application...
call npm run build

echo Done! Packaged app can be found in gui/dist
pause
