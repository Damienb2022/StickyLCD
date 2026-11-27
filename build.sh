#!/usr/bin/env bash
# Builds StickyLCD on macOS (dist/StickyLCD.app) or Linux (dist/StickyLCD).
# Needs Python 3.10+ with Tk. A virtual environment is recommended:
#   python3 -m venv .venv && . .venv/bin/activate && ./build.sh
set -euo pipefail
cd "$(dirname "$0")"

python3 -m pip install --quiet -r requirements.txt pyinstaller
python3 -c "from app import make_icon; make_icon(512).save('icon.png')"

case "$(uname -s)" in
  Darwin)
    python3 -m PyInstaller --noconfirm --clean --windowed \
      --name StickyLCD --icon icon.png \
      --osx-bundle-identifier io.github.stickylcd \
      --collect-all libusb_package --collect-data certifi --collect-data tzdata \
      --hidden-import pystray._darwin --hidden-import truststore \
      app.py
    echo
    echo "Built: $(pwd)/dist/StickyLCD.app"
    ;;
  Linux)
    python3 -m PyInstaller --noconfirm --clean --onefile --windowed \
      --name StickyLCD \
      --collect-all libusb_package --collect-data certifi --collect-data tzdata \
      --hidden-import pystray._appindicator --hidden-import pystray._xorg --hidden-import truststore \
      app.py
    echo
    echo "Built: $(pwd)/dist/StickyLCD"
    echo "Remember the udev rule so you can use the screen without root: see packaging/linux/README.md"
    ;;
  *)
    echo "Unsupported system: $(uname -s) (on Windows use build.ps1)" >&2
    exit 1
    ;;
esac
