# Security

## Reporting a problem

Please report security issues privately through GitHub's **Report a vulnerability** button on the repository's Security tab, not as a public issue.

## What StickyLCD does

- **Network:** StickyLCD makes no network requests unless you add a calendar link. Then it fetches only that link, every 5 minutes. There is no telemetry, no update check and no other server.
- **Listening:** StickyLCD opens no network ports. The single-instance check uses OS-local mechanisms only: a named event on Windows, and a Unix socket in the private data folder on macOS/Linux. The only message it accepts is "show the window".
- **Files:** it reads and writes only its own data folder (see README). Background pictures are copied into that folder and re-encoded. Old copies are deleted only from that folder.
- **USB:** it talks only to the device `0416:5408`.

## How the code is hardened

| Area | Protection |
|---|---|
| Calendar link | Only `https://` and `webcal://` are accepted; `http://`, `file://` and other schemes are refused. Redirects to non-HTTPS addresses are refused. Certificates are always verified. |
| Link secrecy | The link is never shown again after saving. Error messages name only the host, never the full link. Stored with DPAPI on Windows; in a `0600` file inside a `0700` folder on macOS/Linux. |
| Calendar data | Downloads are capped at 20 MB, and feeds with more than 50,000 entries are refused. Events repeating every second or minute are ignored, because they could otherwise tie up the CPU for hours. Titles and locations are trimmed and drawn as plain text. |
| Settings and notes files | Validated on load: wrong types, out-of-range numbers, bad colours and unknown formatting are dropped or reset, so a damaged file can't crash the app. A background picture path outside the app's own folder is ignored. |
| Pictures | Size limit of 15 MB. Pillow's decompression-bomb protection is on. Pictures are re-encoded before use. |
| Start at login | Uses the per-user location only: the HKCU Run key, a LaunchAgent, or `~/.config/autostart`. Never system-wide, and never needs admin rights. |
| Linux USB access | The udev rule uses `TAG+="uaccess"` (the logged-in user only) rather than making the device world-writable. |
| Builds | CI runs with read-only repository permissions. Only the tag-release job can create a draft release. Release files come with SHA-256 checksums. |

## Known limits

- **Unsigned builds:** the macOS and Windows builds aren't code-signed, so the OS will warn on first run. Check the release's `SHA256SUMS.txt`, or build from source.
- **Secret storage on macOS/Linux:** the calendar link is protected by file permissions, not the system keychain. Another program running as your user could read it, as it could any of your files.
