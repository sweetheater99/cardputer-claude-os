"""Confirm the device is online after WiFi config.

Waits for wlan.isconnected(), then does DNS + TCP + (optionally) ICMP
to google.com so we can distinguish "associated but no DHCP" from
"DHCP but no internet" from "fully working".
"""

from __future__ import annotations

import argparse
import os
import sys
import time

import mpy_repl


def _is_native_usb(port: str) -> bool:
    """True when the port belongs to an Espressif native USB peripheral.

    Checks by VID (0x303A) via pyserial so it works on macOS, Linux, and
    Windows without relying on port-name patterns that differ per OS.

    On native USB, the serial.Serial object can't survive hard_reset:
    the CDC device briefly disconnects and the OS may assign a new path.
    Skip the reset and rely on the device being already at a stable
    UIFlow state (configure_wifi leaves it there).
    """
    try:
        from serial.tools import list_ports
        for p in list_ports.comports():
            if p.device == port and p.vid == 0x303A:
                return True
    except Exception:
        pass
    name = os.path.basename(port)
    return "usbmodem" in name or name.startswith("ttyACM")


VERIFY_SCRIPT = """
import network, socket, time
w = network.WLAN(network.STA_IF)
_ = w.active(True)
t0 = time.ticks_ms()
while not w.isconnected() and time.ticks_diff(time.ticks_ms(), t0) < 20000:
    time.sleep_ms(200)
if not w.isconnected():
    print('WLAN-FAIL')
else:
    cfg = w.ifconfig()
    mac = ':'.join('{:02X}'.format(b) for b in w.config('mac'))
    print('WLAN-OK', cfg[0], mac)
    try:
        ai = socket.getaddrinfo('google.com', 443)
        print('DNS-OK', ai[0][-1][0])
    except Exception as e:
        print('DNS-FAIL', e)
    try:
        _s = socket.socket()
        _s.settimeout(5)
        _s.connect(('google.com', 443))
        _s.close()
        print('TCP-OK')
    except Exception as e:
        print('TCP-FAIL', e)
    # ICMP to 8.8.8.8 — Google public DNS is one of the most
    # reliably pingable public IPs. Still skippable: many ISPs and
    # APs rate-limit or block ICMP, so failure here doesn't mean
    # the device is broken.
    try:
        r = socket.socket(socket.AF_INET, socket.SOCK_RAW, 1)
        r.settimeout(3)
        pkt = b'\\x08\\x00\\xf7\\xff\\x00\\x01\\x00\\x01'
        _n = r.sendto(pkt, ('8.8.8.8', 0))
        data, _ = r.recvfrom(64)
        r.close()
        print('PING-OK', len(data))
    except Exception as e:
        print('PING-SKIP', e)
print('VERIFY-DONE')
"""


def main() -> int:
    ap = argparse.ArgumentParser(description="Verify an M5Stack reached the net.")
    ap.add_argument("--port", required=True)
    ap.add_argument(
        "--no-reset",
        action="store_true",
        help="Don't hard-reset before verifying.",
    )
    args = ap.parse_args()

    s = mpy_repl.open_port(args.port)
    try:
        native = _is_native_usb(args.port)
        do_reset = not args.no_reset and not native
        if native and not args.no_reset:
            sys.stderr.write(
                "Native USB port — skipping hard-reset "
                "(pass --no-reset explicitly to suppress this message).\n"
            )
        if do_reset:
            mpy_repl.hard_reset(s)
            mpy_repl.wait_for_boot(s, timeout=25.0)
        mpy_repl.interrupt_to_repl(s)
        out = mpy_repl.exec_and_capture(s, VERIFY_SCRIPT, settle=3.0)
        # DNS + TCP + ICMP probes happen sequentially on-device and
        # each can take a few seconds; give the whole thing 30s.
        out = mpy_repl.collect_until(s, out, "VERIFY-DONE", timeout=30.0)
    finally:
        s.close()

    clean = mpy_repl.strip_paste_echo(out)
    print(clean)

    if "WLAN-OK" not in clean:
        sys.stderr.write("\nWLAN did not connect. Check SSID/password.\n")
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
