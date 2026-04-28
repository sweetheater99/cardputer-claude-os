"""Anthropic-flavored palette and a shared starburst animation.

All apps on this device draw from the same palette so the launcher,
Hello, Snake, and Buddy screens feel like one product. The palette
maps to Anthropic's brand:

- ORANGE  ~#CC785C  (primary accent)
- CREAM   ~#F0EEE6  (warm off-white; text + cores)
- DARK    ~#1F1F1F  (header/footer chrome fill)

Colors are 24-bit 0xRRGGBB. That's what UIFlow 2.0's M5.Lcd accepts
on this build — not the RGB565 packed format you'd expect from other
ILI9342 drivers. We discovered this by drawing swatches with both
encodings and eyeballing which one actually rendered orange; the
16-bit hex produces cyan-blues because the display treats the
top 8 bits as the R channel of an otherwise 0-padded 24-bit value.

The starburst animation frames come from `burst_frames.py`, which is
generated at build time from an Anthropic splat WebP. Each frame is a
packed sequence of (y, x, length) byte triples — one horizontal run of
opaque pixels per triple — which we render as a loop of fillRect calls.
That encoding dodges the need for an on-device PNG decoder and keeps
the frame data small enough to ship inline in a `.py` module.
"""

import time

import M5

import burst_frames


ORANGE = 0xCC785C      # Anthropic orange
CREAM = 0xF0EEE6       # warm off-white
DARK = 0x1F1F1F        # near-black chrome
ORANGE_DIM = 0x6A3E2E  # muted orange for dividers
BLACK = 0x000000
WHITE = 0xFFFFFF
GRAY_DIM = 0x333333
GRAY_MID = 0x777777
GREEN = 0x00FF00
CYAN = 0x00FFFF
YELLOW = 0xFFFF00
RED = 0xFF0000


def header(lcd, title):
    """Paint the top 22-px chrome strip with an orange hairline underneath."""
    lcd.fillRect(0, 0, 320, 22, DARK)
    lcd.fillRect(0, 22, 320, 1, ORANGE)
    try:
        lcd.setTextSize(1)
    except Exception:
        pass
    lcd.setTextColor(ORANGE, DARK)
    lcd.setCursor(8, 7)
    lcd.print(title)


def footer(lcd, a="", b="", c=""):
    """Paint the bottom chrome strip with small cream A/B/C labels."""
    lcd.fillRect(0, 216, 320, 1, ORANGE)
    lcd.fillRect(0, 217, 320, 23, DARK)
    try:
        lcd.setTextSize(1)
    except Exception:
        pass
    lcd.setTextColor(CREAM, DARK)
    if a:
        lcd.setCursor(12, 225)
        lcd.print(a)
    if b:
        # Center-ish: a 10-char label lands roughly on the B button.
        lcd.setCursor(132, 225)
        lcd.print(b)
    if c:
        # Right-align the C label against the physical button.
        x = 320 - 12 - 6 * len(c)
        lcd.setCursor(x, 225)
        lcd.print(c)


def clear_body(lcd, bg=BLACK):
    """Fill the area between header and footer without touching either."""
    lcd.fillRect(0, 23, 320, 193, bg)


def draw_burst(cx, cy, frame, max_r=None, color=None, core=None, bg=BLACK):
    """Blit one frame of the generated burst animation at (cx, cy).

    The frame source is `burst_frames.FRAMES` — each entry is a flat
    bytes object of (y, x, length) triples describing horizontal runs
    of opaque pixels. We clear the bounding box once (so last frame's
    spokes don't ghost) and then iterate the triples, issuing one
    fillRect per run. ~200 runs per frame in the current bundle,
    which benches out to a comfortable ~60-80 ms per frame on the
    ILI9342 — well inside our 120 ms frame budget.

    `max_r`, `color`, and `core` are accepted for backwards
    compatibility with the old procedural burst but are ignored here
    (size and color are baked into the generated frames).
    """
    lcd = M5.Lcd
    w = burst_frames.WIDTH
    h = burst_frames.HEIGHT
    x0 = cx - w // 2
    y0 = cy - h // 2
    if bg is not None:
        lcd.fillRect(x0, y0, w, h, bg)

    data = burst_frames.FRAMES[frame % len(burst_frames.FRAMES)]
    c = burst_frames.COLOR
    i = 0
    n = len(data)
    while i < n:
        ry = data[i]
        rx = data[i + 1]
        rl = data[i + 2]
        lcd.fillRect(x0 + rx, y0 + ry, rl, 1, c)
        i += 3


def tick_burst(cx, cy, frame, last_tick, frame_ms=None, max_r=None):
    """Non-blocking burst driver — callers own their event loop.

    Advances one animation step if frame_ms has elapsed since
    last_tick; otherwise returns the inputs unchanged. Returns the
    updated (frame, last_tick) for the caller to store.

    Passing frame_ms=None picks up the per-bundle default from
    burst_frames.FRAME_MS. max_r is accepted for back-compat.
    """
    if frame_ms is None:
        frame_ms = burst_frames.FRAME_MS
    now = time.ticks_ms()
    if time.ticks_diff(now, last_tick) >= frame_ms:
        draw_burst(cx, cy, frame)
        return frame + 1, now
    return frame, last_tick
