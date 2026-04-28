"""Claude Buddy BLE client, packaged as a launcher app.

Entry point: `run()`. Returns when the user taps BtnC to go back to
the launcher. BtnA / BtnB answer the currently-displayed permission
prompt; when no prompt is pending they flash a confirmation toast so
the operator can tell the press registered.

BLE is torn down on exit via `BuddyBLE.deinit()` — advertising stops
and any active link drops. Re-entering the app creates a fresh BLE
peripheral, so the bond/advertise state comes from zero each time.
"""

import time

import M5
from machine import I2C, Pin

import buddy_ble
import buddy_chars
import buddy_protocol
import buddy_state
import buddy_ui


# IP5306 power IC on the classic M5Stack Basic. Register 0x78 exposes
# a 4-LED battery gauge (high nibble = bitmap of lit LEDs). No
# voltage/current readout, so pct is quantized to 0/25/50/75/100.
_IP5306_ADDR = 0x75
_IP5306_REG_LED = 0x78


def _make_battery_reader():
    i2c = I2C(0, sda=Pin(21), scl=Pin(22), freq=100000)

    def _read():
        try:
            buf = i2c.readfrom_mem(_IP5306_ADDR, _IP5306_REG_LED, 1)
            high = buf[0] & 0xF0
            bits = 0
            v = high
            while v & 0x80:
                bits += 1
                v = (v << 1) & 0xFF
            return {"pct": bits * 25, "mV": 0, "mA": 0, "usb": True}
        except OSError:
            return {"pct": 0, "mV": 0, "mA": 0, "usb": True}

    return _read


def run():
    ui = buddy_ui.BuddyUI()
    state = buddy_state.BuddyState()
    ui.update_identity(state.name, state.owner)

    buddy_chars.sweep_partials()
    chars = buddy_chars.CharReceiver()

    battery = _make_battery_reader()

    # Protocol needs a reference to the BLE object (for disconnects/bond
    # clearing), but BLE needs the on_line callback, which needs the
    # protocol. We break the cycle by stashing the protocol in a dict
    # that the BLE callback reads at event time — by then it's set.
    proto_holder = {"p": None}

    def on_line(raw):
        p = proto_holder["p"]
        if p is not None:
            p.on_line(raw)

    def on_passkey(pk):
        ui.show_passkey(pk)

    def on_state_change(s):
        # On builds without a pairing API the controller never fires
        # _IRQ_ENCRYPTION_UPDATE, so "connected" is as ready as we ever
        # get. Remap it to "encrypted" before handing to the UI so the
        # header reaches LINKED and the main panel flips from the idle
        # burst to the heartbeat layout; without this the indicator
        # gets stuck at PAIR... even though data is flowing.
        effective = s
        if s == "connected" and not ble.pairing_supported:
            effective = "encrypted"
        print("buddy_app: state", s, "->", effective)
        ui.set_connection(effective)
        if effective == "encrypted":
            ui.clear_passkey()
            p = proto_holder["p"]
            if p is not None:
                p.send_hello()

    ble = buddy_ble.BuddyBLE(
        on_line=on_line,
        on_passkey=on_passkey,
        on_state=on_state_change,
    )

    proto = buddy_protocol.BuddyProtocol(
        state=state,
        ui=ui,
        chars=chars,
        ble=ble,
        battery_reader=battery,
    )
    proto_holder["p"] = proto

    ui.update_footer(state.stats(), battery())
    print("Claude Buddy up as", ble.advertised_name)

    last_footer_ms = time.ticks_ms()
    last_toast_ms = 0
    footer_interval = 3000
    toast_dwell_ms = 1500

    # Idle burst state. Advances only while the UI reports is_idle()
    # (advertising/disconnected, no passkey, no prompt). We own it here
    # rather than in BuddyUI because the UI is event-driven — it has no
    # tick loop of its own, and this app's loop is the natural place to
    # drive animation.
    burst_frame = 0
    burst_last_tick = time.ticks_ms()

    try:
        while True:
            M5.update()

            if M5.BtnA.wasPressed():
                if not proto.send_permission("once"):
                    ui.flash_toast("A: no prompt pending", buddy_ui.GRAY_DIM)
                    ui.update_footer(state.stats(), battery())
                last_toast_ms = time.ticks_ms()
            if M5.BtnB.wasPressed():
                if not proto.send_permission("deny"):
                    ui.flash_toast("B: no prompt pending", buddy_ui.GRAY_DIM)
                last_toast_ms = time.ticks_ms()
            if M5.BtnC.wasPressed():
                # Exit back to the launcher menu. The finally clause
                # shuts BLE down cleanly on the way out.
                return

            now = time.ticks_ms()
            if time.ticks_diff(now, last_footer_ms) >= footer_interval:
                state.tick_nap()
                ui.update_footer(state.stats(), battery())
                last_footer_ms = now
            if last_toast_ms and time.ticks_diff(now, last_toast_ms) >= toast_dwell_ms:
                ui.restore_button_hints()
                last_toast_ms = 0

            burst_frame, burst_last_tick = ui.tick_idle_burst(
                burst_frame, burst_last_tick
            )

            time.sleep_ms(40)
    finally:
        # Order: BLE first, then LCD wipe. ble.deinit() clears the IRQ
        # handler so a late async disconnect event can't repaint Buddy
        # chrome over the launcher. The fillScreen is defense in depth
        # — the launcher also does one in _draw_menu_static, but doing
        # it here gives the screen a known black state during the tiny
        # gap between "run() returns" and "launcher starts drawing",
        # which has been enough on this display to leak Buddy pixels
        # through when the launcher's first draws hadn't landed yet.
        try:
            ble.deinit()
        except Exception as e:
            print("buddy_app: deinit warning:", e)
        try:
            M5.Lcd.fillScreen(buddy_ui.BLACK)
        except Exception as e:
            print("buddy_app: screen-clear warning:", e)
