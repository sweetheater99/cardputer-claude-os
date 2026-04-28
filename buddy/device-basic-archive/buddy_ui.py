"""Render the buddy's state to the 320x240 LCD.

Strategy: maintain a `Snapshot` dict that holds the most recent host
heartbeat plus local state (battery %, passkey, connection state, etc).
The UI layer reads snapshots and only repaints the regions whose data
changed. Fully redrawing every tick makes the whole display flicker on
the ILI9342; dirty-rect tracking is worth the complexity.

Coordinate system:
- Header strip:  y=0..22   (title + BT status, orange hairline at y=22)
- Identity band: y=24..44  (name / owner)
- Main panel:    y=48..168 (queue, tokens, prompt, or idle burst)
- Footer bar:    y=172..214 (stats + level)
- Button hints:  y=216..239 (orange hairline + A / B / C under the buttons)

Palette comes from `ui_theme` so the Buddy screens match the launcher
and the other apps — orange accents on near-black chrome, cream text.
"""

try:
    import M5

    _LCD = M5.Lcd
except ImportError:
    _LCD = None

import ui_theme
from ui_theme import (
    BLACK, WHITE, GRAY_DIM, GRAY_MID, ORANGE, CREAM, DARK, ORANGE_DIM,
    YELLOW, RED, GREEN, CYAN,
)


# Burst placement on the idle (advertising / disconnected) main panel.
# Sits to the right of the three lines of instructional text — those
# wrap at ~x=190, leaving the right quarter of the panel free.
_IDLE_BURST_CX = 262
_IDLE_BURST_CY = 110
_IDLE_BURST_R = 26


class BuddyUI:
    def __init__(self):
        self._last = {}
        self._passkey = None
        self._connection_state = "advertising"
        self._prompt = None  # most recent pending prompt, if any
        if _LCD is not None:
            _LCD.fillScreen(BLACK)
            _LCD.setRotation(1)
            try:
                _LCD.setBrightness(80)
            except Exception:
                pass
        self._redraw_chrome()

    # ---- public setters; each sets internal state and repaints its own region

    def set_connection(self, state: str):
        """state in: advertising | connected | encrypted | disconnected"""
        if state == self._connection_state:
            return
        self._connection_state = state
        self._draw_header()
        if state in ("advertising", "disconnected"):
            self._prompt = None
            self._draw_main()

    def show_passkey(self, pk: int):
        """Called when the stack asks us to display a pairing passkey.

        Takes over the whole main panel with a big 6-digit number; the
        host operator reads it and types it into Claude.app. Stays
        shown until encryption succeeds (or the link drops).
        """
        self._passkey = pk
        self._draw_passkey_overlay()

    def clear_passkey(self):
        if self._passkey is None:
            return
        self._passkey = None
        self._draw_main()

    def update_heartbeat(self, hb: dict):
        """Host heartbeat: totals, running/waiting, tokens, prompt."""
        self._last = hb
        self._prompt = hb.get("prompt")
        self._draw_main()

    def update_identity(self, name: str, owner: str):
        self._draw_identity(name, owner)

    def update_footer(self, stats: dict, battery: dict):
        self._draw_footer(stats, battery)

    def flash_decision(self, decision: str):
        """Brief visual ack after a permission decision was sent."""
        color = GREEN if decision == "once" else RED
        self.flash_toast(decision.upper() + " sent", color)

    def flash_toast(self, text: str, color: int = CYAN):
        """Overwrite the button-hint strip with a short status line.

        Any button press should trigger one of these so the operator
        has proof the device registered the tap — even when the
        underlying action is a no-op (e.g. BtnA while no prompt is
        pending). The hints get restored by the next UI redraw cycle;
        we don't bother restoring them synchronously.
        """
        if _LCD is None:
            return
        _LCD.fillRect(0, 217, 320, 23, color)
        _LCD.setTextColor(WHITE, color)
        _LCD.setCursor(8, 225)
        _LCD.print(text[:38])

    def restore_button_hints(self):
        """Re-paint the A/B/C hint strip in the shared theme."""
        if _LCD is None:
            return
        ui_theme.footer(_LCD, "A: once", "B: deny", "C: back")

    def is_idle(self) -> bool:
        """True when the main panel is just the 'waiting for Claude' text.

        This is what buddy_app polls each tick to decide whether to
        advance the idle burst animation.
        """
        return (
            self._connection_state in ("advertising", "disconnected")
            and self._passkey is None
            and self._prompt is None
        )

    def tick_idle_burst(self, frame, last_tick):
        """Advance the idle burst by one tick if we're on the idle screen.

        Callers pass their (frame, last_tick) state through their own
        event loop; we hand back the updated tuple. No-ops outside the
        idle state so the method is safe to call unconditionally.
        """
        if _LCD is None or not self.is_idle():
            return frame, last_tick
        return ui_theme.tick_burst(
            _IDLE_BURST_CX, _IDLE_BURST_CY, frame, last_tick,
            frame_ms=150, max_r=_IDLE_BURST_R,
        )

    # ---- drawing primitives below

    def _draw_header(self):
        if _LCD is None:
            return
        # Dark chrome strip + orange hairline, matching ui_theme.header
        # but with our extra right-side connection icon.
        _LCD.fillRect(0, 0, 320, 22, DARK)
        _LCD.fillRect(0, 22, 320, 1, ORANGE)
        _LCD.setTextColor(ORANGE, DARK)
        _LCD.setCursor(8, 7)
        _LCD.print("Claude Buddy")
        icon, color = self._connection_icon()
        _LCD.setTextColor(color, DARK)
        _LCD.setCursor(260, 7)
        _LCD.print(icon)

    def _connection_icon(self):
        s = self._connection_state
        if s == "encrypted":
            return ("LINKED", GREEN)
        if s == "connected":
            return ("PAIR...", YELLOW)
        if s == "disconnected":
            return ("OFFLINE", RED)
        return ("ADV", CYAN)

    def _draw_identity(self, name: str, owner: str):
        if _LCD is None:
            return
        _LCD.fillRect(0, 24, 320, 20, BLACK)
        _LCD.setTextColor(ORANGE, BLACK)
        _LCD.setCursor(6, 26)
        _LCD.print(name)
        if owner:
            _LCD.setTextColor(GRAY_MID, BLACK)
            _LCD.setCursor(6 + 8 * len(name) + 20, 26)
            _LCD.print("<- " + owner)

    def _draw_main(self):
        if _LCD is None:
            return
        _LCD.fillRect(0, 48, 320, 120, BLACK)
        if self._passkey is not None:
            self._draw_passkey_overlay()
            return
        if self._connection_state in ("advertising", "disconnected"):
            _LCD.setTextColor(CREAM, BLACK)
            _LCD.setCursor(6, 56)
            _LCD.print("Waiting for Claude...")
            _LCD.setTextColor(GRAY_MID, BLACK)
            _LCD.setCursor(6, 80)
            _LCD.print("Open Claude > Settings > Buddy")
            _LCD.setCursor(6, 100)
            _LCD.print("and pick this device.")
            # Seed frame 0 of the burst so there's something to see
            # before buddy_app's main loop starts advancing it.
            ui_theme.draw_burst(
                _IDLE_BURST_CX, _IDLE_BURST_CY, 0,
                max_r=_IDLE_BURST_R,
            )
            return

        hb = self._last
        _LCD.setTextColor(WHITE, BLACK)
        running = hb.get("running", 0)
        waiting = hb.get("waiting", 0)
        total = hb.get("total", 0)
        _LCD.setCursor(6, 52)
        _LCD.print("Queue: {} running  {} waiting  ({} total)".format(
            running, waiting, total
        ))

        tokens_today = hb.get("tokens_today", 0)
        _LCD.setCursor(6, 72)
        _LCD.setTextColor(CYAN, BLACK)
        _LCD.print("Today: {:,} tokens".format(tokens_today).replace(",", "'"))

        msg = hb.get("msg", "")
        if msg:
            _LCD.setTextColor(GRAY_MID, BLACK)
            _LCD.setCursor(6, 92)
            _LCD.print(msg[:40])

        if self._prompt:
            self._draw_prompt_box(self._prompt)

    def _draw_prompt_box(self, prompt: dict):
        if _LCD is None:
            return
        _LCD.drawRect(4, 112, 312, 52, ORANGE)
        _LCD.setTextColor(ORANGE, BLACK)
        _LCD.setCursor(10, 116)
        tool = prompt.get("tool", "?")
        _LCD.print("PERMISSION: {}".format(tool))
        hint = prompt.get("hint", "")
        _LCD.setTextColor(CREAM, BLACK)
        # Two 40-char lines is the max we can fit comfortably in 6x8 font
        _LCD.setCursor(10, 132)
        _LCD.print(hint[:40])
        if len(hint) > 40:
            _LCD.setCursor(10, 146)
            _LCD.print(hint[40:80])

    def _draw_passkey_overlay(self):
        if _LCD is None or self._passkey is None:
            return
        _LCD.fillRect(0, 48, 320, 120, BLACK)
        _LCD.setTextColor(ORANGE, BLACK)
        _LCD.setCursor(60, 56)
        _LCD.print("Pairing passkey:")
        _LCD.setTextColor(CREAM, BLACK)
        # Big centered number. ILI9342 has a text size multiplier;
        # 4x gives us ~32 px-tall digits which fill the panel nicely.
        try:
            _LCD.setTextSize(4)
        except Exception:
            pass
        pk_str = "{:06d}".format(self._passkey)
        _LCD.setCursor(60, 82)
        _LCD.print(pk_str)
        try:
            _LCD.setTextSize(1)
        except Exception:
            pass
        _LCD.setTextColor(GRAY_MID, BLACK)
        _LCD.setCursor(30, 142)
        _LCD.print("type it into Claude to accept")

    def _draw_footer(self, stats: dict, battery: dict):
        if _LCD is None:
            return
        _LCD.fillRect(0, 172, 320, 42, BLACK)
        _LCD.setTextColor(GRAY_MID, BLACK)
        _LCD.setCursor(6, 176)
        _LCD.print("Lv.{}  appr:{}  deny:{}  vel:{}/min".format(
            stats.get("lvl", 0),
            stats.get("appr", 0),
            stats.get("deny", 0),
            stats.get("vel", 0.0),
        ))
        # Battery bar — just the percentage, drawn as a filled inset.
        pct = max(0, min(100, battery.get("pct", 0)))
        bar_x, bar_y, bar_w, bar_h = 6, 194, 120, 10
        _LCD.drawRect(bar_x, bar_y, bar_w, bar_h, GRAY_MID)
        fill_w = int((bar_w - 2) * pct / 100)
        color = GREEN if pct > 40 else (YELLOW if pct > 15 else RED)
        _LCD.fillRect(bar_x + 1, bar_y + 1, fill_w, bar_h - 2, color)
        _LCD.setCursor(bar_x + bar_w + 8, bar_y + 1)
        _LCD.setTextColor(CREAM, BLACK)
        _LCD.print("{}%  {}".format(pct, "USB" if battery.get("usb") else "BAT"))

    def _redraw_chrome(self):
        if _LCD is None:
            return
        _LCD.fillScreen(BLACK)
        self._draw_header()
        self._draw_identity("Buddy", "")
        self._draw_main()
        self.restore_button_hints()
