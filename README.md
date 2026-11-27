# StickyLCD

Sticky notes on a Thermalright USB LCD: the 1920×462 "bar" screen with USB ID `0416:5408`, which shows up as `USBDISPLAY`. StickyLCD replaces the vendor's TRCC software for this screen. It runs on **Windows, macOS and Linux**.

- Notes with colours, bullets, ✓ ticks, strikethrough, text colours and per-selection text size
- Pages change automatically, with slide or fade transitions
- Clock and date, plus an optional **today's calendar** tile from any iCal/ICS link, or from classic Outlook on Windows
- Background colour, gradient or picture. Clock text switches between light and dark to stay readable
- Runs from the system tray (Windows/Linux) or the Dock (macOS)

> Not affiliated with or endorsed by Thermalright. The USB protocol was reverse-engineered for interoperability. No vendor code or files are included.

## Download

Ready-made builds are on the [Releases](../../releases) page:

| System | File | Notes |
|---|---|---|
| Windows 10/11 | `StickyLCD.exe` | Windows' built-in WinUSB driver handles the screen. Nothing else to install. |
| macOS 12+ | `StickyLCD-macos.zip` | The build is unsigned: the first time, right-click the app → **Open**. |
| Linux (x86-64) | `StickyLCD-linux` | Install the udev rule first. See [packaging/linux](packaging/linux/README.md). |

Only one program can drive the screen at a time, so quit TRCC before starting StickyLCD.

## Using it

- **+ New note** (Ctrl+N, or Cmd+N on a Mac), then type. The screen updates as you type, and notes save automatically.
- The first line of a note is its bold title. Turn this off in Settings if you prefer.
- **Formatting toolbar:** • Bullet, ✓ Done, Strike, text colours and A−/A+ size. Each applies to the selected text, or to the current line.
- Untick **Show on screen** to keep a note without displaying it.
- **File → Settings:** notes per screen, page timing and transitions, text sizes, clock, rotate 180°, background, calendar and start at login.

### Today's calendar

Paste your calendar's **secret iCal address** into **Settings → Today's calendar → Calendar link**:

- **Google Calendar:** Settings → your calendar → *Integrate calendar* → **Secret address in iCal format**
- **Outlook.com / Microsoft 365:** Settings → Calendar → *Shared calendars* → **Publish a calendar** → ICS link
- **iCloud:** Calendar app → share the calendar → **Public Calendar** → copy the link (`webcal://` is fine)

Treat this link like a password: anyone who has it can read the calendar. StickyLCD only accepts `https://` or `webcal://` links and never displays a saved link again. On Windows it stores the link encrypted for your account. On macOS and Linux it's in a settings file only you can read.

On Windows you can also tick **Use classic Outlook on this PC**. StickyLCD then reads your own calendar, or a shared mailbox by name, straight from Outlook, with no link needed. This is also useful where a firewall stops programs other than the browser from reaching the internet.

### Where things are stored

| System | Folder |
|---|---|
| Windows | `%APPDATA%\StickyLCD` |
| macOS | `~/Library/Application Support/StickyLCD` |
| Linux | `~/.config/StickyLCD` |

## Running from source

You need Python 3.10 or newer with Tk.

```sh
pip install -r requirements.txt
python app.py            # add --tray to start hidden
```

To build:

```sh
powershell -ExecutionPolicy Bypass -File build.ps1   # Windows -> dist\StickyLCD.exe
./build.sh                                          # macOS -> dist/StickyLCD.app, Linux -> dist/StickyLCD
```

| File | Purpose |
|---|---|
| `app.py` | Window, editor, settings, tray and the screen thread |
| `renderer.py` | Draws the screen image: cards, clock, calendar, backgrounds, transitions |
| `lcd_device.py` | The screen's USB protocol |
| `calendar_client.py` | iCal/ICS link and Outlook calendar sources |
| `platform_support.py` | Per-OS details: folders, fonts, start at login, single instance, secret storage |

### The USB protocol

The protocol is documented at the top of `lcd_device.py`. In short: a 2048-byte handshake, then each frame is a JPEG cut into 496-byte pieces, each with a 16-byte header, sent as 512-byte chunks.

## Security

See [SECURITY.md](SECURITY.md) for what StickyLCD does and doesn't touch, and how to report a problem.

## Licence

[MIT](LICENSE)
