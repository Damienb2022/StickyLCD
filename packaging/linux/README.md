# Linux setup

## Screen permission (udev rule)

By default only root can talk to USB devices directly. Install the rule once, then unplug the screen and plug it back in:

```sh
sudo cp packaging/linux/99-stickylcd.rules /etc/udev/rules.d/
sudo udevadm control --reload-rules
sudo udevadm trigger
```

The rule uses `TAG+="uaccess"`, which gives access to whoever is logged in at the computer. It doesn't make the device writable by every account.

## Running from source

You need Python 3.10 or newer with Tk, plus a few system packages:

```sh
# Debian / Ubuntu
sudo apt install python3-venv python3-tk libusb-1.0-0 gir1.2-ayatanaappindicator3-0.1 fonts-dejavu-core
# Fedora
sudo dnf install python3-tkinter libusb1 libayatana-appindicator-gtk3 dejavu-sans-fonts

python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
python app.py
```

## Tray icon

The tray icon needs a desktop with a system tray: KDE, Cinnamon, XFCE and MATE all have one. GNOME needs the "AppIndicator and KStatusNotifierItem Support" extension. If there's no tray, closing the window minimises StickyLCD instead, and the screen keeps updating.

## Start at login

**Settings → Start StickyLCD when I log in** writes `~/.config/autostart/stickylcd.desktop`.
