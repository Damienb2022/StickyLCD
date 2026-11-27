"""Today's events for the calendar tile, from an iCal/ICS link (Google, Outlook.com / Microsoft 365,
iCloud, Nextcloud, ...) and/or classic Outlook on Windows.

The ICS link usually contains a private token, so: HTTPS only (no downgrade on redirect), the link
is never shown in error messages (only its host), and it's stored protected (platform_support)."""

from __future__ import annotations

import datetime as dt
import socket
import ssl
import sys
import threading
import urllib.error
import urllib.parse
import urllib.request

from platform_support import protect, unprotect  # noqa: F401 - re-exported for app.py

POLL_SECONDS = 300
USER_AGENT = "StickyLCD"
MAX_ICS_BYTES = 20_000_000   # a calendar feed bigger than this is refused
MAX_EVENTS = 100             # per day - more than fits on the tile anyway
MAX_FEED_EVENTS = 50_000     # entries in the whole feed
MAX_TEXT = 200               # characters kept from a title / location
IS_WIN = sys.platform == "win32"


class CalendarError(Exception):
    pass


class _NetError(Exception):
    """The request never got an HTTP response."""


# ---------------------------------------------------------------------------
# The link
# ---------------------------------------------------------------------------
def normalise_url(url: str) -> str:
    """Accept https:// and webcal:// (which means https). Anything else - http://, file://, ... - is
    refused: http would send the private token in the clear, and other schemes could read local files."""
    url = (url or "").strip()
    if not url:
        raise CalendarError("no calendar link set")
    parts = urllib.parse.urlsplit(url)
    scheme = parts.scheme.lower()
    if scheme in ("webcal", "webcals"):
        parts = parts._replace(scheme="https")
    elif scheme != "https":
        raise CalendarError("the calendar link must start with https:// or webcal://")
    if not parts.hostname:
        raise CalendarError("the calendar link has no server name")
    return urllib.parse.urlunsplit(parts)


def host_of(url: str) -> str:
    try:
        return urllib.parse.urlsplit(url).hostname or "the calendar server"
    except ValueError:
        return "the calendar server"


def _explain(err: BaseException, host: str) -> str:
    """Turn a low-level network error into something a person can act on (never includes the link)."""
    reason = getattr(err, "reason", err)
    if isinstance(reason, ssl.SSLCertVerificationError):
        return f"this computer doesn't trust {host}'s security certificate ({reason.verify_message})"
    if isinstance(reason, ssl.SSLError):
        return f"secure connection to {host} failed ({reason.reason or 'TLS error'})"
    if isinstance(reason, socket.gaierror):
        return f"can't find {host} - check the link, or the network may block it"
    if isinstance(reason, (TimeoutError, socket.timeout)) or "timed out" in str(reason):
        return f"timed out connecting to {host} - a firewall or proxy may be blocking it"
    if isinstance(reason, ConnectionRefusedError):
        return f"{host} refused the connection"
    if isinstance(reason, ConnectionResetError):
        return f"connection to {host} was cut off - a firewall or proxy may be blocking it"
    return f"can't reach {host} ({type(reason).__name__})"


# ---------------------------------------------------------------------------
# Transport 1 (Windows): WinINet - Windows' own HTTP stack, as used by Edge/Office. Uses the PC's
# proxy settings (PAC scripts, auto-detect, proxy sign-in) and Windows' certificate checks.
# WinINet refuses https -> http redirects on its own when no UI is allowed.
# ---------------------------------------------------------------------------
_WININET_ERRORS = {
    12002: "timed out connecting to {host} - a firewall or proxy may be blocking it",
    12007: "can't find {host} - check the link, or the network may block it",
    12029: "couldn't connect to {host} - a firewall may be blocking it",
    12030: "connection to {host} was cut off - a firewall or web filter may be blocking it",
    12031: "connection to {host} was cut off - a firewall or web filter may be blocking it",
    12157: "secure connection to {host} failed - the network may be inspecting HTTPS",
    12175: "secure connection to {host} failed - the network may be inspecting HTTPS",
    12045: "this computer doesn't trust {host}'s security certificate",
    12038: "{host}'s security certificate doesn't match the address",
    12037: "{host}'s security certificate has expired, or the clock is wrong",
    12039: "{host} tried to redirect to an insecure (http) address - refused",
    12040: "{host} tried to redirect to an insecure (http) address - refused",
    12180: "the proxy setup script couldn't be run",
    12166: "the proxy setup script has an error",
}


def _get_wininet(url: str, timeout: float, host: str):
    import ctypes
    from ctypes import wintypes
    wi = ctypes.WinDLL("wininet", use_last_error=True)
    HINTERNET = ctypes.c_void_p
    wi.InternetOpenW.restype = HINTERNET
    wi.InternetOpenW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD]
    wi.InternetOpenUrlW.restype = HINTERNET
    wi.InternetOpenUrlW.argtypes = [HINTERNET, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
                                    wintypes.DWORD, ctypes.c_size_t]
    wi.InternetSetOptionW.argtypes = [HINTERNET, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD]
    wi.HttpQueryInfoW.argtypes = [HINTERNET, wintypes.DWORD, ctypes.c_void_p,
                                  ctypes.POINTER(wintypes.DWORD), ctypes.POINTER(wintypes.DWORD)]
    wi.InternetReadFile.argtypes = [HINTERNET, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
    wi.InternetCloseHandle.argtypes = [HINTERNET]

    session = wi.InternetOpenW(USER_AGENT, 0, None, None, 0)  # 0 = INTERNET_OPEN_TYPE_PRECONFIG
    if not session:
        raise _NetError(f"Windows networking unavailable (error {ctypes.get_last_error()})")
    request = None
    try:
        ms = wintypes.DWORD(int(timeout * 1000))
        for opt in (2, 5, 6):  # connect / send / receive timeouts
            wi.InternetSetOptionW(session, opt, ctypes.byref(ms), ctypes.sizeof(ms))
        headers = "Accept: text/calendar, */*\r\n"
        flags = 0x80000000 | 0x04000000 | 0x00080000 | 0x00000200 | 0x00000100 | 0x00800000
        # RELOAD | NO_CACHE_WRITE | NO_COOKIES | NO_UI | PRAGMA_NOCACHE | SECURE
        request = wi.InternetOpenUrlW(session, url, headers, len(headers), flags, 0)
        if not request:
            code = ctypes.get_last_error()
            raise _NetError(_WININET_ERRORS.get(code, "can't reach {host} (Windows error %d)" % code)
                            .format(host=host))
        status, size = wintypes.DWORD(), wintypes.DWORD(4)
        wi.HttpQueryInfoW(request, 19 | 0x20000000, ctypes.byref(status), ctypes.byref(size), None)

        chunks, total, buf, got = [], 0, ctypes.create_string_buffer(65536), wintypes.DWORD()
        while True:
            if not wi.InternetReadFile(request, buf, len(buf), ctypes.byref(got)):
                code = ctypes.get_last_error()
                raise _NetError(_WININET_ERRORS.get(code, "reply from {host} was cut off (Windows error %d)" % code)
                                .format(host=host))
            if got.value == 0:
                break
            total += got.value
            if total > MAX_ICS_BYTES:
                raise CalendarError(f"the calendar from {host} is too big (over {MAX_ICS_BYTES // 1_000_000} MB)")
            chunks.append(buf.raw[:got.value])
        return status.value, b"".join(chunks)
    finally:
        if request:
            wi.InternetCloseHandle(request)
        wi.InternetCloseHandle(session)


# ---------------------------------------------------------------------------
# Transport 2: Python's own HTTPS
# ---------------------------------------------------------------------------
class _HttpsOnlyRedirects(urllib.request.HTTPRedirectHandler):
    """Follow redirects only to other https:// addresses (never leak the link over http)."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if urllib.parse.urlsplit(newurl).scheme.lower() != "https":
            raise CalendarError(f"{host_of(req.full_url)} tried to redirect to an insecure address - refused")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _ssl_contexts():
    """Certificate checks to try, in order: the OS's own (truststore - also trusts company
    certificates installed by IT), Mozilla's list (certifi), then Python's default."""
    contexts = []
    try:
        import truststore
        contexts.append(truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT))
    except Exception:  # noqa: BLE001 - optional
        pass
    try:
        import certifi
        contexts.append(ssl.create_default_context(cafile=certifi.where()))
    except Exception:  # noqa: BLE001 - optional
        pass
    contexts.append(ssl.create_default_context())
    return contexts


def _read_capped(resp, host: str) -> bytes:
    data = resp.read(MAX_ICS_BYTES + 1)
    if len(data) > MAX_ICS_BYTES:
        raise CalendarError(f"the calendar from {host} is too big (over {MAX_ICS_BYTES // 1_000_000} MB)")
    return data


def _get_urllib(url: str, timeout: float, host: str):
    req = urllib.request.Request(url, headers={"Accept": "text/calendar, */*", "User-Agent": USER_AGENT})
    last_err = None
    for ctx in _ssl_contexts():
        opener = urllib.request.build_opener(urllib.request.HTTPSHandler(context=ctx), _HttpsOnlyRedirects())
        try:
            with opener.open(req, timeout=timeout) as resp:
                return resp.status, _read_capped(resp, host)
        except urllib.error.HTTPError as e:
            return e.code, b""
        except (urllib.error.URLError, OSError) as e:
            last_err = e
            if isinstance(getattr(e, "reason", e), ssl.SSLCertVerificationError):
                continue  # try the next way of checking certificates
            raise _NetError(_explain(e, host)) from None  # 'from None': keep the link out of tracebacks
    raise _NetError(_explain(last_err, host)) from None


# ---------------------------------------------------------------------------
# ICS
# ---------------------------------------------------------------------------
def download_ics(url: str, timeout: float = 20) -> bytes:
    url = normalise_url(url)
    host = host_of(url)
    errors = []
    for transport in ((_get_wininet, _get_urllib) if IS_WIN else (_get_urllib,)):
        try:
            status, body = transport(url, timeout, host)
            break
        except _NetError as e:
            errors.append(str(e))
        except CalendarError:
            raise
        except Exception as e:  # noqa: BLE001 - e.g. WinINet unavailable; try the next way
            errors.append(f"{type(e).__name__}")
    else:
        raise CalendarError(errors[0])
    if status in (401, 403, 404, 410):
        raise CalendarError(f"{host} refused the calendar link (HTTP {status}) - it may have been reset; "
                            "copy a fresh link")
    if status != 200:
        raise CalendarError(f"{host} answered HTTP {status}")
    return body


def _clip(v) -> str:
    s = " ".join(str(v or "").split())  # also flattens line breaks
    return s[:MAX_TEXT]


def parse_ics(data: bytes, day: dt.date | None = None) -> list[dict]:
    """Events overlapping `day` (default today, local time) from an ICS file."""
    import icalendar
    import recurring_ical_events

    if not data.lstrip().upper().startswith(b"BEGIN:VCALENDAR"):
        raise CalendarError("that link didn't return a calendar (expected an .ics file)")
    try:
        cal = icalendar.Calendar.from_ical(data)
    except ValueError as e:
        raise CalendarError("the calendar file couldn't be read") from e
    # Guard against feeds that would take forever to expand: an event repeating every second or
    # minute since years ago means hundreds of millions of steps. Real calendars don't use these.
    kept = []
    for comp in cal.subcomponents:
        if comp.name == "VEVENT":
            rules = comp.get("RRULE")
            rules = rules if isinstance(rules, list) else [rules] if rules is not None else []
            if any(str(f).upper() in ("SECONDLY", "MINUTELY") for r in rules for f in (r.get("FREQ") or [])):
                continue
        kept.append(comp)
    if sum(c.name == "VEVENT" for c in kept) > MAX_FEED_EVENTS:
        raise CalendarError(f"that calendar has over {MAX_FEED_EVENTS:,} entries - too many to read")
    cal.subcomponents[:] = kept

    day = day or dt.date.today()
    start = dt.datetime.combine(day, dt.time()).astimezone()
    end = start + dt.timedelta(days=1)
    events = []
    try:
        found = recurring_ical_events.of(cal, skip_bad_series=True).between(start, end)
    except Exception as e:  # noqa: BLE001 - malformed recurrence rules etc.
        raise CalendarError("the calendar file has entries that couldn't be read") from e
    for ev in found:
        if str(ev.get("STATUS", "")).upper() == "CANCELLED":
            continue
        s, e = ev.get("DTSTART"), ev.get("DTEND")
        s = s.dt if s is not None else None
        e = e.dt if e is not None else None
        if s is None:
            continue
        all_day = not isinstance(s, dt.datetime)
        if all_day:
            s_dt = dt.datetime.combine(s, dt.time()).astimezone()
            e_dt = dt.datetime.combine(e if e is not None else s + dt.timedelta(days=1), dt.time()).astimezone()
        else:
            s_dt = s.astimezone()  # a "floating" time (no zone) is taken as local time
            e_dt = (e.astimezone() if isinstance(e, dt.datetime) else s_dt) if e is not None else s_dt
        if not (s_dt < end and e_dt > start):
            continue
        tentative = (str(ev.get("STATUS", "")).upper() == "TENTATIVE"
                     or str(ev.get("X-MICROSOFT-CDO-BUSYSTATUS", "")).upper() == "TENTATIVE")
        events.append({
            "title": _clip(ev.get("SUMMARY")) or "Untitled event",
            "all_day": all_day,
            "start": s_dt,
            "end": e_dt,
            "location": _clip(ev.get("LOCATION")),
            "tentative": tentative,
        })
    events.sort(key=lambda x: (not x["all_day"], x["start"]))
    return events[:MAX_EVENTS]


def fetch_ics(url: str) -> list[dict]:
    return parse_ics(download_ics(url))


# ---------------------------------------------------------------------------
# Classic Outlook on this PC (Windows, COM). Needs no network access of its own, so it also
# works where a firewall stops StickyLCD from reaching a calendar server.
# ---------------------------------------------------------------------------
def outlook_supported() -> bool:
    return IS_WIN


def _short_date(day: dt.date) -> str:
    """The date in the user's own short-date format - the format Outlook's Restrict() parses."""
    import ctypes
    from ctypes import wintypes

    class SYSTEMTIME(ctypes.Structure):
        _fields_ = [(n, wintypes.WORD) for n in
                    ("wYear", "wMonth", "wDayOfWeek", "wDay", "wHour", "wMinute", "wSecond", "wMilliseconds")]

    st = SYSTEMTIME(day.year, day.month, 0, day.day, 0, 0, 0, 0)
    buf = ctypes.create_unicode_buffer(80)
    ctypes.windll.kernel32.GetDateFormatW(0x0400, 1, ctypes.byref(st), None, buf, 80)  # USER_DEFAULT, SHORTDATE
    return buf.value


def _com_time(v) -> dt.datetime:
    # Outlook hands back local wall-clock time (pywin32 may label it UTC; ignore that)
    return dt.datetime(v.year, v.month, v.day, v.hour, v.minute, v.second).astimezone()


def fetch_outlook(mailbox: str) -> list[dict]:
    """Today's events from the default calendar of the Outlook mailbox named `mailbox`
    (a shared mailbox's display name), or your own calendar if it's blank."""
    if not IS_WIN:
        raise CalendarError("Outlook is only supported on Windows")
    try:
        import pythoncom
        import win32com.client.dynamic
    except ImportError as e:
        raise CalendarError("Outlook support isn't installed (pywin32)") from e
    name = (mailbox or "").strip()
    pythoncom.CoInitialize()
    try:
        try:
            ns = win32com.client.dynamic.Dispatch("Outlook.Application").GetNamespace("MAPI")
        except Exception as e:  # noqa: BLE001
            raise CalendarError("classic Outlook isn't available on this PC") from e
        if name:
            store = next((s for s in ns.Stores if s.DisplayName.lower() == name.lower()), None)
            if store is None:
                raise CalendarError(f"Outlook has no mailbox called “{_clip(name)}”")
            folder = store.GetDefaultFolder(9)  # olFolderCalendar
        else:
            folder = ns.GetDefaultFolder(9)

        today = dt.date.today()
        day_start = dt.datetime.combine(today, dt.time()).astimezone()
        day_end = day_start + dt.timedelta(days=1)
        items = folder.Items
        items.IncludeRecurrences = True
        items.Sort("[Start]")
        found = items.Restrict(f"[Start] < '{_short_date(today + dt.timedelta(days=1))}' "
                               f"AND [End] > '{_short_date(today)}'")
        events = []
        for it in found:
            if len(events) >= MAX_EVENTS:
                break
            if it.MeetingStatus in (5, 7):  # cancelled meetings
                continue
            start, end = _com_time(it.Start), _com_time(it.End)
            if not (start < day_end and end > day_start):  # double-check in case of date parsing quirks
                continue
            events.append({
                "title": _clip(it.Subject) or "Untitled event",
                "all_day": bool(it.AllDayEvent),
                "start": start,
                "end": end,
                "location": _clip(it.Location),
                "tentative": it.BusyStatus == 1,  # olTentative
            })
        events.sort(key=lambda e: (not e["all_day"], e["start"]))
        return events
    except CalendarError:
        raise
    except Exception as e:  # noqa: BLE001 - COM errors
        raise CalendarError(f"couldn't read Outlook ({type(e).__name__})") from e
    finally:
        pythoncom.CoUninitialize()


# ---------------------------------------------------------------------------
def fetch_any(cfg: dict, ics_url: str) -> tuple[list[dict], str]:
    """The ICS link first; if there's none or it fails, Outlook (Windows, when enabled).
    Returns (events, source) with source "ics" or "outlook"."""
    ics_err = None
    if ics_url:
        try:
            return fetch_ics(ics_url), "ics"
        except CalendarError as e:
            ics_err = e
    if IS_WIN and cfg.get("use_outlook"):
        try:
            return fetch_outlook(cfg.get("outlook_calendar", "")), "outlook"
        except CalendarError as e:
            if ics_err:  # the link is the main source - report its problem first
                raise CalendarError(f"{ics_err}; Outlook: {e}") from None
            raise
    if ics_err:
        raise ics_err
    raise CalendarError("no calendar set up - paste a calendar link" + (" or turn on Outlook" if IS_WIN else ""))


class CalendarWorker(threading.Thread):
    """Polls the calendar and hands results to on_update(state_dict)."""

    def __init__(self, get_settings, on_update):
        super().__init__(daemon=True)
        self.get_settings = get_settings
        self.on_update = on_update
        self.wake = threading.Event()
        self.stop_event = threading.Event()
        self.state = {"enabled": False, "events": [], "error": None, "updated": None}

    def refresh_now(self):
        self.wake.set()

    def run(self):
        while not self.stop_event.is_set():
            cfg = self.get_settings()
            state = dict(self.state)
            state["enabled"] = bool(cfg.get("show_calendar"))
            if not state["enabled"]:
                state.update(events=[], error=None, updated=None)
            else:
                try:
                    events, source = fetch_any(cfg, unprotect(cfg.get("ics_url", "")))
                    state.update(events=events, source=source, error=None, updated=dt.datetime.now())
                except CalendarError as e:
                    # keep showing what we had (if it's from today) and flag the problem
                    if state["updated"] and state["updated"].date() != dt.date.today():
                        state["events"] = []
                    state["error"] = str(e)
                except Exception as e:  # noqa: BLE001 - never let the worker die
                    state["error"] = f"calendar error ({type(e).__name__})"
            if state != self.state:
                self.state = state
                self.on_update(state)
            self.wake.wait(POLL_SECONDS)
            self.wake.clear()
