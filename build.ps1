# Builds dist\StickyLCD.exe on Windows (single file, no Python needed on the target PC).
$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot

python -m pip install --quiet -r requirements.txt pyinstaller
python -c "from app import make_icon; make_icon(256).save('icon.ico', sizes=[(16,16),(24,24),(32,32),(48,48),(64,64),(128,128),(256,256)])"

python -m PyInstaller --noconfirm --clean --onefile --windowed `
    --name StickyLCD --icon icon.ico `
    --collect-all libusb_package --collect-data certifi --collect-data tzdata `
    --hidden-import pystray._win32 --hidden-import truststore `
    --hidden-import pythoncom --hidden-import win32com.client.dynamic --hidden-import win32timezone `
    app.py
if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed (is StickyLCD.exe still running? Quit it from the tray first)." }

Write-Host "`nBuilt: $PSScriptRoot\dist\StickyLCD.exe"
