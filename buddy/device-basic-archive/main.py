"""Launcher menu for the M5Stack.

Boot into a three-button menu. Pick A / B / C to run the matching app;
when the app's `run()` returns, we come back here and redraw the menu.

Apps live as sibling modules and are lazy-imported the first time
they're picked — keeps the launcher screen fast and leaves headroom
for whichever app we end up running (Buddy's BLE stack is the biggest
heap consumer; Hello is trivial; Snake is small).

Adding a new app: write `my_app.py` with a `run()` function that
returns cleanly on user exit, then wire it into `_LAUNCH` below.
"""

import time

import M5

import ui_theme


# (key, label, module). `module` is lazy-imported on first launch.
_LAUNCH = (
    ("A", "Hello, World!", "hello_app"),
    ("B", "Claude Buddy",  "buddy_app"),
    ("C", "Snake",         "snake_app"),
)


def _draw_menu_static():
    """Paint the pieces that don't animate. The burst owns its own area."""
    lcd = M5.Lcd
    lcd.fillScreen(ui_theme.BLACK)

    ui_theme.header(lcd, "M5 Launcher")

    # Product title on the left; the burst lives on the right.
    try:
        lcd.setTextSize(2)
    except Exception:
        pass
    lcd.setTextColor(ui_theme.ORANGE, ui_theme.BLACK)
    lcd.setCursor(14, 38)
    lcd.print("CLAUDE")
    lcd.setTextColor(ui_theme.CREAM, ui_theme.BLACK)
    lcd.setCursor(14, 66)
    lcd.print("WHIM5Y")
    try:
        lcd.setTextSize(1)
    except Exception:
        pass

    # Divider between title block and menu.
    lcd.fillRect(14, 104, 292, 1, ui_theme.ORANGE_DIM)

    # Menu items with orange key chips.
    y = 118
    for key, label, _mod in _LAUNCH:
        lcd.fillRect(18, y - 3, 22, 20, ui_theme.ORANGE)
        lcd.setTextColor(ui_theme.DARK, ui_theme.ORANGE)
        lcd.setCursor(25, y + 3)
        lcd.print(key)
        lcd.setTextColor(ui_theme.CREAM, ui_theme.BLACK)
        lcd.setCursor(52, y + 3)
        lcd.print(label)
        y += 28

    ui_theme.footer(lcd, "", "press A / B / C", "")


def _wait_for_choice():
    """Animate the idle burst while waiting for a button press."""
    # Burst position: right of the title, above the divider.
    cx, cy = 250, 60
    # Draw frame 0 immediately so the screen isn't blank while we wait
    # for the first tick_ms budget to elapse.
    ui_theme.draw_burst(cx, cy, 0)
    frame = 1
    last_tick = time.ticks_ms()
    while True:
        M5.update()
        if M5.BtnA.wasPressed():
            return _LAUNCH[0]
        if M5.BtnB.wasPressed():
            return _LAUNCH[1]
        if M5.BtnC.wasPressed():
            return _LAUNCH[2]
        frame, last_tick = ui_theme.tick_burst(cx, cy, frame, last_tick)
        time.sleep_ms(15)


def _show_crash(mod_name, e):
    """Paint a short crash notice and wait for any button to return to menu."""
    lcd = M5.Lcd
    lcd.fillScreen(ui_theme.BLACK)
    ui_theme.header(lcd, "Crash")
    lcd.setTextColor(ui_theme.RED, ui_theme.BLACK)
    lcd.setCursor(14, 60)
    lcd.print("{} crashed:".format(mod_name))
    lcd.setTextColor(ui_theme.CREAM, ui_theme.BLACK)
    lcd.setCursor(14, 84)
    lcd.print(str(e)[:44])
    ui_theme.footer(lcd, "", "any button to return", "")
    while True:
        M5.update()
        if (M5.BtnA.wasPressed() or M5.BtnB.wasPressed()
                or M5.BtnC.wasPressed()):
            return
        time.sleep_ms(40)


def main():
    M5.begin()
    while True:
        _draw_menu_static()
        _key, _label, mod_name = _wait_for_choice()
        # Lazy import so we only pay the heap cost of an app the
        # first time it's picked. Subsequent calls reuse the cached
        # module.
        mod = __import__(mod_name)
        try:
            mod.run()
        except Exception as e:
            import sys
            sys.print_exception(e)
            _show_crash(mod_name, e)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        import sys
        sys.print_exception(e)
        raise
