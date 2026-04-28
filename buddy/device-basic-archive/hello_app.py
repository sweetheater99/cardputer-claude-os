"""Minimal 'Hello, World!' app.

Bundled as a proof-of-life for the launcher and the simplest possible
template for future tiny apps. Any button returns to the menu. The
greeting is idle, so we run the shared starburst as a decoration
while we wait — same animation as the launcher, just smaller.
"""

import time

import M5

import ui_theme


def run():
    lcd = M5.Lcd
    lcd.fillScreen(ui_theme.BLACK)

    ui_theme.header(lcd, "Hello")

    try:
        lcd.setTextSize(4)
    except Exception:
        pass
    lcd.setTextColor(ui_theme.CREAM, ui_theme.BLACK)
    lcd.setCursor(28, 70)
    lcd.print("Hello,")
    lcd.setTextColor(ui_theme.ORANGE, ui_theme.BLACK)
    lcd.setCursor(28, 130)
    lcd.print("World!")
    try:
        lcd.setTextSize(1)
    except Exception:
        pass

    ui_theme.footer(lcd, "", "any button to go back", "")

    # Small burst in the top-right corner, clear of the text.
    cx, cy = 272, 64
    ui_theme.draw_burst(cx, cy, 0, max_r=22)
    frame = 1
    last_tick = time.ticks_ms()
    while True:
        M5.update()
        if (M5.BtnA.wasPressed() or M5.BtnB.wasPressed()
                or M5.BtnC.wasPressed()):
            return
        frame, last_tick = ui_theme.tick_burst(cx, cy, frame, last_tick, max_r=22)
        time.sleep_ms(15)
