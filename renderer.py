"""Draws sticky-note cards onto an image sized for the LCD."""

from __future__ import annotations

import datetime as dt
import functools
import os
from dataclasses import dataclass

from PIL import Image, ImageChops, ImageDraw, ImageFont, ImageOps, ImageStat

import platform_support


@dataclass(frozen=True)
class Note:
    text: str
    color: str = "yellow"
    spans: tuple = ()  # ((start, end, style), ...) character ranges in text; style in STYLE_TAGS


# Segoe UI on Windows, Arial on macOS, DejaVu Sans on Linux. The symbol font draws ✓
# (Segoe UI itself has no tick glyph).
FONT_REGULAR, FONT_BOLD, FONT_SYMBOL = platform_support.font_files()

TICK = "\u2713"    # "done" marker at the start of a line
BULLET = "\u2022"  # dot point marker at the start of a line
# text colour name -> (on light cards, on the dark charcoal card)
TEXT_COLORS = {
    "red": ("#C62828", "#FF8A80"),
    "orange": ("#E65100", "#FFB74D"),
    "green": ("#2E7D32", "#81C784"),
    "blue": ("#1565C0", "#82B1FF"),
    "grey": ("#8A8A8A", "#9A9A9A"),
}
SIZE_LEVELS = {"size-2": 0.6, "size-1": 0.8, "size+1": 1.25, "size+2": 1.5, "size+3": 2.0}
SIZE_STEPS = ["size-2", "size-1", None, "size+1", "size+2", "size+3"]  # None = normal
STYLE_TAGS = ("strike",) + tuple(TEXT_COLORS) + tuple(SIZE_LEVELS)

# (card background, top stripe, text colour) - matches Windows Sticky Notes themes
PALETTE = {
    "yellow": ("#FFF7B1", "#FFE66E", "#1F1F1F"),
    "green": ("#E4F9E0", "#A1EF9B", "#1F1F1F"),
    "pink": ("#FFE4F1", "#FFAFDF", "#1F1F1F"),
    "purple": ("#F2E6FF", "#D7AFFF", "#1F1F1F"),
    "blue": ("#E2F1FF", "#9DD7FF", "#1F1F1F"),
    "gray": ("#F3F2F1", "#E0E0E0", "#1F1F1F"),
    "charcoal": ("#3B3A39", "#1F1F1F", "#F3F2F1"),
}
BACKGROUND = "#111418"

_bg_cache: dict = {}


GRADIENTS = ("none", "horizontal", "vertical", "diagonal", "radial")


def _color_layer(size, color: str, color2: str, mode: str) -> Image.Image:
    """A plain colour, or a smooth gradient from colour to colour2 (left→right, top→bottom,
    corner→corner, or centre→edges)."""
    w, h = size
    if mode not in GRADIENTS[1:]:
        return Image.new("RGB", (w, h), color)
    across = Image.linear_gradient("L").transpose(Image.Transpose.TRANSPOSE)  # 0 at left, 255 at right
    down = Image.linear_gradient("L")                                          # 0 at top, 255 at bottom
    if mode == "horizontal":
        mask = across.resize((w, h), Image.BILINEAR)
    elif mode == "vertical":
        mask = down.resize((w, h), Image.BILINEAR)
    elif mode == "diagonal":  # (x/w + y/h) / 2
        mask = ImageChops.add(across.resize((w, h), Image.BILINEAR), down.resize((w, h), Image.BILINEAR), scale=2.0)
    else:  # radial: colour in the middle, colour2 at the edges
        mask = Image.radial_gradient("L").resize((w, h), Image.BILINEAR)
    return Image.composite(Image.new("RGB", (w, h), color2), Image.new("RGB", (w, h), color), mask)


def background(size, cfg: dict) -> Image.Image:
    """The screen background: an optional picture (cropped to fill), with the background colour
    (or gradient) laid over it at bg_opacity %. With no picture it's just the colour/gradient.
    Cached - don't modify it."""
    color = cfg.get("bg_color") or BACKGROUND
    color2 = cfg.get("bg_color2") or "#2A3442"
    mode = cfg.get("bg_gradient") or "none"
    opacity = max(0, min(100, int(cfg.get("bg_opacity", 30))))
    path = cfg.get("bg_image") or ""
    try:
        stamp = os.path.getmtime(path) if path else 0
    except OSError:
        path, stamp = "", 0
    key = (tuple(size), color, color2, mode, opacity, path, stamp)
    if key not in _bg_cache:
        base = _color_layer(tuple(size), color, color2, mode)
        if path:
            try:
                with Image.open(path) as im:
                    pic = ImageOps.fit(im.convert("RGB"), tuple(size), Image.LANCZOS)
                base = Image.blend(pic, base, opacity / 100)
            except (OSError, ValueError, Image.DecompressionBombError):
                pass  # unreadable picture: plain colour
        _bg_cache.clear()
        _bg_cache[key] = base
    return _bg_cache[key]


def tone(img: Image.Image, box) -> str:
    """'light', 'mid' or 'dark': how bright this part of the image is, to pick readable text."""
    m = ImageStat.Stat(img.crop(tuple(int(v) for v in box)).convert("L")).mean[0]
    return "light" if m > 140 else "mid" if m > 45 else "dark"


def is_light(img: Image.Image, box) -> bool:
    """Is this part of the image light enough to need dark text on it?"""
    return tone(img, box) == "light"


# clock text (time, day, date) for each background tone
_CLOCK_COLORS = {
    "light": ("#111418", "#2F3842", "#4A5561"),
    "mid": ("#FFFFFF", "#F0F3F6", "#DDE3EA"),  # busy/mid backgrounds: the greys get lost, so go bright
    "dark": ("#FFFFFF", "#B8C0CC", "#7D8794"),
}


def _panel(img, box, fill_rgba, radius=18):
    """Rounded panel that lets the background show through faintly."""
    x0, y0, x1, y1 = (int(v) for v in box)
    layer = Image.new("RGBA", (x1 - x0, y1 - y0), (0, 0, 0, 0))
    ImageDraw.Draw(layer).rounded_rectangle((0, 0, x1 - x0 - 1, y1 - y0 - 1), radius=radius, fill=fill_rgba)
    img.paste(layer, (x0, y0), layer)

_font_cache: dict = {}


def font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    key = (size, bold)
    if key not in _font_cache:
        path = FONT_BOLD if bold else FONT_REGULAR
        try:
            _font_cache[key] = ImageFont.truetype(path, size)
        except (OSError, TypeError, AttributeError):  # no font file found (path None)
            _font_cache[key] = ImageFont.load_default(size)
    return _font_cache[key]


def symbol_font(size: int) -> ImageFont.FreeTypeFont:
    key = (size, "sym")
    if key not in _font_cache:
        try:
            _font_cache[key] = ImageFont.truetype(FONT_SYMBOL, size)
        except (OSError, TypeError, AttributeError):
            _font_cache[key] = font(size)
    return _font_cache[key]


def _tlen(draw, s: str, fnt) -> float:
    """Text width, measuring ticks in the symbol font they're drawn with."""
    if TICK not in s:
        return draw.textlength(s, font=fnt)
    return (draw.textlength(s.replace(TICK, ""), font=fnt)
            + s.count(TICK) * draw.textlength(TICK, font=symbol_font(fnt.size)))


def _hang_prefix(para: str) -> str:
    """The list marker(s) a paragraph starts with ("• ", "✓ ", "• ✓ "), for a hanging indent."""
    p = ""
    for mark in (BULLET + " ", TICK + " "):
        if para[len(p):].startswith(mark):
            p += mark
    return p


def _wrap_pos(text: str, measure, max_w: int, base: int = 0) -> list[tuple[str, int, int]]:
    """Word-wrap text. measure(s, offset) gives the width of s, which starts at offset (+ base) -
    so per-character sizes can be taken into account. Returns (line, offset, indent_px) triples;
    each line is an exact slice of text, so per-character styles can follow it.
    Wrapped lines of a bullet / ticked paragraph are indented to line up under its text."""
    out = []
    pos = base
    for para in text.split("\n"):
        prefix = _hang_prefix(para)
        hang = int(measure(prefix, pos)) if prefix else 0
        if hang > max_w // 2:
            hang = 0
        first = True

        def emit(s, start):
            nonlocal first
            out.append((s, start, 0 if first else hang))
            first = False

        def limit():
            return max_w if first else max_w - hang

        line, line_start, wpos = "", pos, pos
        for word in para.split(" "):
            w_len = len(word)
            trial = word if not line else line + " " + word
            if measure(trial, line_start if line else wpos) <= limit():
                if not line:
                    line_start = wpos
                line = trial
            else:
                if line:
                    emit(line, line_start)
                start = wpos
                # hard-break words longer than the line
                while measure(word, start) > limit() and len(word) > 1:
                    cut = len(word)
                    while cut > 1 and measure(word[:cut], start) > limit():
                        cut -= 1
                    emit(word[:cut], start)
                    word, start = word[cut:], start + cut
                line, line_start = word, start
            wpos += w_len + 1
        emit(line, line_start)
        pos += len(para) + 1
    return out


def _wrap(draw: ImageDraw.ImageDraw, text: str, fnt, max_w: int) -> list[str]:
    return [line for line, _, _ in _wrap_pos(text, lambda s, _off: _tlen(draw, s, fnt), max_w)]


def _char_styles(note: Note):
    """Per-character (colour name or None, struck, size scale) for note.text."""
    n = len(note.text)
    colors, struck, scales = [None] * n, [False] * n, [1.0] * n
    for start, end, style in note.spans:
        for i in range(max(0, start), min(n, end)):
            if style == "strike":
                struck[i] = True
            elif style in TEXT_COLORS:
                colors[i] = style
            elif style in SIZE_LEVELS:
                scales[i] = SIZE_LEVELS[style]
    return colors, struck, scales


def _run_font(base: int, scale: float, bold: bool, tick: bool):
    size = max(8, round(base * scale))
    return symbol_font(size) if tick else font(size, bold)


def _runs(line: str, off: int, text: str, styles):
    """Split a wrapped line into runs of one style: (segment, colour, struck, scale, is_tick)."""
    colors, struck, scales = styles

    def style_at(i):
        j, tick = off + i, line[i] == TICK
        if 0 <= j < len(text) and text[j] == line[i]:  # the " …" added by truncation has no style
            return colors[j], struck[j], scales[j], tick
        return None, False, 1.0, tick

    runs, i = [], 0
    while i < len(line):
        st = style_at(i)
        k = i + 1
        while k < len(line) and style_at(k) == st:
            k += 1
        runs.append((line[i:k], *st))
        i = k
    return runs


def _line_box(line, off, text, styles, base, bold):
    """(height, ascent) of a wrapped line: set by the biggest text on it."""
    runs = _runs(line, off, text, styles) or [(" ", None, False, 1.0, False)]
    biggest = max(r[3] for r in runs)
    ascent = max(_run_font(base, r[3], bold, r[4]).getmetrics()[0] for r in runs)
    return int(base * biggest * 1.25), ascent


@functools.lru_cache(maxsize=128)
def _card_layout(note: Note, box_w: int, box_h: int, max_size: int, min_size: int, use_title: bool):
    """Largest base font size at which the note fits. Cached: notes rarely change, but the screen
    is redrawn up to 30 times a second while calendar text scrolls.
    Returns (base size, styles, [(line, offset, indent, bold, height, ascent), ...], title/body gap)."""
    text = note.text
    lines = text.split("\n")
    if use_title and len(lines) > 1:
        first = lines[0]
        title, t_off = first.strip(), len(first) - len(first.lstrip())
        rest = text[len(first) + 1:]
        body, b_off = rest.strip(), len(first) + 1 + len(rest) - len(rest.lstrip())
    else:
        title, t_off, body, b_off = "", 0, text, 0
    styles = _char_styles(note)

    def measure(size, bold):
        return lambda s, off: sum(_run_font(size, sc, bold, tk).getlength(seg)
                                  for seg, _c, _st, sc, tk in _runs(s, off, text, styles))

    def build(size):
        out = []
        for part, off, bold in ((title, t_off, True), (body, b_off, False)):
            if part:
                for ln, o, ind in _wrap_pos(part, measure(size, bold), box_w, off):
                    out.append((ln, o, ind, bold, *_line_box(ln, o, text, styles, size, bold)))
        gap = int(size * 0.35) if title and body else 0
        return out, gap

    for size in range(max_size, min_size - 1, -2):
        out, gap = build(size)
        if sum(l[4] for l in out) + gap <= box_h:
            return size, styles, out, gap
    # doesn't fit even at the smallest size: keep what fits and end with "…"
    out, gap = build(min_size)
    kept, used = [], gap
    for l in out:
        if kept and used + l[4] > box_h:
            break
        kept.append(l)
        used += l[4]
    if len(kept) < len(out) and kept:
        ln, o, ind, bold, h, asc = kept[-1]
        kept[-1] = (ln.rstrip() + " …", o, ind, bold, h, asc)
    return min_size, styles, kept, gap


def _draw_styled(draw, x, baseline, line: str, off: int, text: str, base: int, bold: bool, fg,
                 dark: bool, styles):
    """Draw one wrapped line, switching size / colour / strikethrough / tick font as the note's styles do."""
    for seg, color, strike, scale, is_tick in _runs(line, off, text, styles):
        f = _run_font(base, scale, bold, is_tick)
        fill = TEXT_COLORS[color][1 if dark else 0] if color else fg
        draw.text((x, baseline), seg, font=f, fill=fill, anchor="ls")
        w = f.getlength(seg)
        if strike and seg.strip():
            strike_y = baseline + f.getbbox("x", anchor="ls")[1] // 2  # middle of the x-height
            draw.line((x, strike_y, x + w, strike_y), fill=fill, width=max(2, f.size // 14))
        x += w


def _draw_card(img, draw, note: Note, box, cfg):
    x0, y0, x1, y1 = box
    bg, stripe, fg = PALETTE.get(note.color, PALETTE["yellow"])
    radius = 18
    draw.rounded_rectangle(box, radius=radius, fill=bg)
    # 16px coloured strip across the top with rounded corners
    draw.rounded_rectangle((x0, y0, x1, y0 + 2 * radius), radius=radius, fill=stripe)
    draw.rectangle((x0, y0 + 16, x1, y0 + 2 * radius), fill=bg)

    pad = 26
    tx0, ty0 = x0 + pad, y0 + 16 + pad - 6
    box_w, box_h = (x1 - pad) - tx0, (y1 - pad + 6) - ty0

    max_size = int(cfg.get("max_font_size", 64))
    min_size = min(int(cfg.get("min_font_size", 20)), max_size)  # sliders can cross; largest wins
    size, styles, lines, gap = _card_layout(note, box_w, box_h, max_size, min_size,
                                            bool(cfg.get("first_line_as_title", True)))
    dark = note.color == "charcoal"
    y, prev_bold = ty0, None
    for ln, off, ind, bold, height, ascent in lines:
        if prev_bold and not bold:
            y += gap  # space between the title and the body
        _draw_styled(draw, tx0 + ind, y + ascent, ln, off, note.text, size, bold, fg, dark, styles)
        y += height
        prev_bold = bold


def _draw_clock(draw, box, now: dt.datetime, cfg, bg_tone: str = "dark"):
    x0, y0, x1, y1 = box
    c_time, c_day, c_date = _CLOCK_COLORS.get(bg_tone, _CLOCK_COLORS["dark"])
    if cfg.get("clock_24h", True):
        time_s = now.strftime("%H:%M")
    else:
        time_s = now.strftime("%I:%M %p").lstrip("0")
    day_s = now.strftime("%A")
    date_s = now.strftime("%d %b %Y")
    cx = (x0 + x1) // 2
    f_time = font(96 if cfg.get("clock_24h", True) else 64, True)
    draw.text((cx, y0 + (y1 - y0) // 2 - 40), time_s, font=f_time, fill=c_time, anchor="mm")
    draw.text((cx, y0 + (y1 - y0) // 2 + 55), day_s, font=font(40), fill=c_day, anchor="mm")
    draw.text((cx, y0 + (y1 - y0) // 2 + 105), date_s, font=font(34), fill=c_date, anchor="mm")


SCROLL_GAP = 80     # px between the end of the text and its repeat
SCROLL_FPS = 30     # upper limit; the USB link may manage fewer
_EDGE = 24          # px of soft fade at the clipped edges


def _draw_marquee(img, xy, text, fnt, fill, max_w, tick: float, cfg) -> bool:
    """Draw text at xy; if it's wider than max_w, scroll it like a ticker (position from tick, in
    seconds). Returns True if it is scrolling (so the caller knows to keep redrawing)."""
    d = ImageDraw.Draw(img)
    tw = d.textlength(text, font=fnt)
    if tw <= max_w:
        d.text(xy, text, font=fnt, fill=fill)
        return False
    speed = max(5.0, float(cfg.get("scroll_speed", 30)))   # px per second
    pause = max(0.0, float(cfg.get("scroll_pause", 3.0)))  # rest at the start of each loop
    loop = tw + SCROLL_GAP
    phase = tick % (pause + loop / speed)
    off = 0.0 if phase < pause else round((phase - pause) * speed)
    asc, desc = fnt.getmetrics()
    layer = Image.new("RGBA", (int(max_w), asc + desc + 4), (0, 0, 0, 0))
    ld = ImageDraw.Draw(layer)
    ld.text((-off, 0), text, font=fnt, fill=fill)
    ld.text((-off + loop, 0), text, font=fnt, fill=fill)
    # soft edges: always on the right, on the left only while text is passing under it
    mask = Image.new("L", layer.size, 255)
    md = ImageDraw.Draw(mask)
    w = layer.width
    for i in range(_EDGE):
        a = int(255 * i / _EDGE)
        md.line((w - 1 - i, 0, w - 1 - i, layer.height), fill=a)
        if off > 0:
            md.line((i, 0, i, layer.height), fill=a)
    alpha = ImageChops.multiply(layer.getchannel("A"), mask)
    layer.putalpha(alpha)
    img.paste(layer, (int(xy[0]), int(xy[1])), layer)
    return True


def _fmt_time(t: dt.datetime, h24: bool) -> str:
    if h24:
        return t.strftime("%H:%M")
    return t.strftime("%I:%M").lstrip("0") + t.strftime("%p").lower()


def _draw_calendar(img, draw, box, cal: dict, now: dt.datetime, cfg, tick: float = 0.0) -> bool:
    """Returns True if any text on the tile is scrolling."""
    x0, y0, x1, y1 = box
    scrolling = False
    _panel(img, box, (27, 32, 39, 230))  # #1B2027, slightly see-through so custom backgrounds show
    pad = 22
    cx0, cx1 = x0 + pad, x1 - pad
    h24 = cfg.get("clock_24h", True)

    draw.text((cx0, y0 + 16), "TODAY", font=font(26, True), fill="#8B949E")
    error = cal.get("error")
    events = cal.get("events") or []
    if error:
        draw.text((cx1, y0 + 22), "offline" if events else "", font=font(20), fill="#D29922", anchor="ra")

    y = y0 + 58
    bottom = y1 - 14
    local_now = now.astimezone()

    all_day = [e for e in events if e["all_day"]]
    timed = [e for e in events if not e["all_day"] and e["end"] and e["end"] > local_now]

    if not events and error:
        msg = "Calendar " + error
        for ln in _wrap(draw, msg, font(24), cx1 - cx0)[:4]:
            draw.text((cx0, y), ln, font=font(24), fill="#6E7681")
            y += 32
        return False

    if all_day:
        text = "All day · " + " · ".join(e["title"] for e in all_day)
        for ln in _wrap(draw, text, font(22, True), cx1 - cx0)[:2]:
            draw.text((cx0, y), ln, font=font(22, True), fill="#79C0FF")
            y += 30
        y += 8

    if not timed:
        msg = "Nothing else today" if any(not e["all_day"] for e in events) else "No meetings today"
        draw.text(((x0 + x1) // 2, (y + bottom) // 2), msg, font=font(30), fill="#6E7681", anchor="mm")
        return False

    time_w = 104 if h24 else 132
    row_h_loc, row_h = 74, 50
    shown = 0
    for i, ev in enumerate(timed):
        rh = row_h_loc if ev["location"] else row_h
        remaining = len(timed) - i - 1
        needs_more_line = remaining > 0
        if y + rh + (30 if needs_more_line else 0) > bottom and shown > 0:
            break
        current = ev["start"] <= local_now < ev["end"]
        if current:
            draw.rounded_rectangle((x0 + 8, y - 6, x1 - 8, y + rh - 8), radius=10, fill="#1F3A2A")
            draw.rectangle((x0 + 8, y - 6, x0 + 14, y + rh - 8), fill="#3FB950")
        title_col = "#9DA7B3" if ev["tentative"] else "#F0F3F6"
        draw.text((cx0, y), _fmt_time(ev["start"], h24), font=font(28, True),
                  fill="#3FB950" if current else "#C9D1D9")
        title = ("NOW · " if current else "") + ev["title"] + (" ?" if ev["tentative"] else "")
        text_w = cx1 - cx0 - time_w
        scrolling |= _draw_marquee(img, (cx0 + time_w, y), title, font(28), title_col, text_w, tick, cfg)
        if ev["location"]:
            scrolling |= _draw_marquee(img, (cx0 + time_w, y + 36), ev["location"], font(21), "#8B949E",
                                       text_w, tick, cfg)
        y += rh
        shown += 1

    if shown < len(timed):
        draw.text((cx0, min(y, bottom - 26)), f"+{len(timed) - shown} more", font=font(22), fill="#8B949E")
    return scrolling


MARGIN, GAP, CLOCK_W, CAL_W, DOTS_H = 18, 18, 330, 460, 22


def notes_area(size, cfg: dict, calendar: dict | None = None) -> list[int]:
    """The [x0, y0, x1, y1] box the note cards (and page dots) are drawn in."""
    w, h = size
    area = [MARGIN, MARGIN, w - MARGIN, h - MARGIN]
    if cfg.get("show_clock", True):
        area[2] -= CLOCK_W + GAP
    if calendar and calendar.get("enabled"):
        area[2] -= CAL_W + GAP
    return area


def _content_mask(img_crop: Image.Image, bg_crop: Image.Image) -> Image.Image:
    """Where img differs from the plain background (i.e. the note cards)."""
    return ImageChops.difference(img_crop, bg_crop).convert("L").point(lambda v: 255 if v else 0)


def transition_frame(a: Image.Image, b: Image.Image, t: float, mode: str, box,
                     bg: Image.Image | None = None) -> Image.Image:
    """One frame of a page change from image a to image b, t = 0..1 (already eased).
    'slide' moves only the cards inside box (clock/calendar stay put) - and, given the background
    bg, only the cards themselves, so a background picture stays still; 'fade' cross-fades."""
    if mode == "fade":
        return Image.blend(a, b, t)
    box = tuple(box)
    x0, y0, x1, y1 = box
    bw = x1 - x0
    off = round(t * (bw + GAP))
    a_crop, b_crop = a.crop(box), b.crop(box)
    frame = b.copy()
    if bg is not None and bg.size == a.size:
        bg_crop = bg.crop(box)
        layer = bg_crop.copy()
        layer.paste(a_crop, (-off, 0), _content_mask(a_crop, bg_crop))
        layer.paste(b_crop, (bw + GAP - off, 0), _content_mask(b_crop, bg_crop))
    else:
        strip = Image.new("RGB", (2 * bw + GAP, y1 - y0), BACKGROUND)
        strip.paste(a_crop, (0, 0))
        strip.paste(b_crop, (bw + GAP, 0))
        layer = strip.crop((off, 0, off + bw, y1 - y0))
    frame.paste(layer, (x0, y0))
    return frame


def render(notes: list[Note], size, cfg: dict, page: int = 0, now: dt.datetime | None = None,
           calendar: dict | None = None, tick: float = 0.0):
    """Returns (image, page_count, scrolling). tick (seconds) drives scrolling text; when
    scrolling is True the caller should redraw about SCROLL_FPS times a second."""
    w, h = size
    now = now or dt.datetime.now()
    img = background((w, h), cfg).copy()
    draw = ImageDraw.Draw(img)

    gap = GAP
    area = notes_area(size, cfg, calendar)
    if cfg.get("show_clock", True):
        clock_box = (w - MARGIN - CLOCK_W, MARGIN, w - MARGIN, h - MARGIN)
        _draw_clock(draw, clock_box, now, cfg, tone(img, clock_box))

    show_cal = bool(calendar and calendar.get("enabled"))
    scrolling = False
    if show_cal:
        scrolling = _draw_calendar(img, draw, (area[2] + GAP, area[1], area[2] + GAP + CAL_W, area[3]),
                                   calendar, now, cfg, tick)

    per_page = max(1, int(cfg.get("notes_per_page", 4)))
    if show_cal:
        per_page = min(per_page, 3)  # cards get too narrow beside the calendar tile
    pages = max(1, -(-len(notes) // per_page))
    page %= pages

    if not notes:
        draw.text(
            ((area[0] + area[2]) // 2, h // 2),
            "No notes yet - add one in StickyLCD",
            font=font(48),
            fill="#4A5561" if is_light(img, area) else "#5C6570",
            anchor="mm",
        )
        return img, 1, scrolling

    shown = notes[page * per_page : (page + 1) * per_page]
    dots_h = DOTS_H if pages > 1 else 0
    avail_w = area[2] - area[0]
    n = len(shown)
    card_w = (avail_w - gap * (n - 1)) // n
    for i, note in enumerate(shown):
        x = area[0] + i * (card_w + gap)
        _draw_card(img, draw, note, (x, area[1], x + card_w, area[3] - dots_h), cfg)

    if pages > 1:
        cx = (area[0] + area[2]) // 2
        cy = area[3] - 6
        dots_tone = tone(img, (cx - 13 * pages, cy - 8, cx + 13 * pages, cy + 8))
        on, off = {"light": ("#111418", "#9AA3AD"), "mid": ("#FFFFFF", "#1B2027")}.get(
            dots_tone, ("#FFFFFF", "#4A525C"))
        for p in range(pages):
            px = cx + (p - (pages - 1) / 2) * 26
            fill = on if p == page else off
            draw.ellipse((px - 6, cy - 6, px + 6, cy + 6), fill=fill)

    return img, pages, scrolling
