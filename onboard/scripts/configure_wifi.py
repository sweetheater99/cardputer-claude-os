"""Configure WiFi on a UIFlow 2.0 device by writing NVS strings over REPL.

Three things here are load-bearing and non-obvious:

1. UIFlow's startup reads config with nvs.get_str(). ESP-IDF's NVS
   stores strings and blobs under different type tags; a blob entry
   is invisible to get_str and raises ESP_ERR_NVS_NOT_FOUND, which
   boot-loops the device. So we always set_str and erase_key first
   in case a prior attempt wrote a blob.

2. UIFlow's startup has no defaults. If any of net_mode/ssid0/pswd0/
   protocol/ip_addr/netmask/gateway/dns/server/boot_option is absent,
   it crashes. We set all of them every time — empty strings are fine
   for the ones we don't care about.

3. SSID is case-sensitive and humans mistype case. We scan live WiFi
   first, find the closest-case match to what the user said, and use
   that. "Interwebs" given by the user becomes "interwebs" if that's
   what's actually broadcast.
"""

from __future__ import annotations

import argparse
import re
import sys
import time

import mpy_repl


NVS_KEYS = [
    ("net_mode", "WIFI"),
    ("ssid0", None),        # filled from arg
    ("pswd0", None),        # filled from arg
    ("protocol", "DHCP"),
    ("ip_addr", ""),
    ("netmask", ""),
    ("gateway", ""),
    ("dns", ""),
    ("server", "uiflow2.m5stack.com"),
]


# The scan has to happen when the STA radio isn't already busy trying
# to associate with the previously-configured SSID — otherwise w.scan()
# returns an empty list. We toggle active(False)/active(True) and call
# disconnect() first to make sure the radio is idle before scanning.
SCAN_SCRIPT = """
import network, time
w = network.WLAN(network.STA_IF)
w.active(False)
time.sleep(1)
w.active(True)
try:
    w.disconnect()
except Exception:
    pass
time.sleep(2)
nets = w.scan()
for n in nets:
    try:
        print('SCAN', n[0].decode('utf-8','replace'), n[3])
    except Exception:
        print('SCAN-RAW', n)
print('SCAN-DONE')
"""


def scan_for_ssid(s, wanted: str, timeout: float = 25.0) -> str:
    """Return the actual-broadcast SSID that best matches `wanted`.

    Prefers case-insensitive exact match. If the scan returns empty,
    falls back to `wanted` lowercased — in practice WiFi APs almost
    always broadcast lowercase SSIDs even when users type them with
    capitals, and the lowercase guess is right more often than the
    user's original typing.
    """
    out = mpy_repl.exec_and_capture(s, SCAN_SCRIPT, settle=3.0)
    out = mpy_repl.collect_until(s, out, "SCAN-DONE", timeout=timeout)

    found = []
    for line in out.splitlines():
        m = re.match(r"SCAN\s+(.+?)\s+(-?\d+)\s*$", line)
        if m:
            found.append((m.group(1), int(m.group(2))))

    if not found:
        # Radio may still be stuck associating. Try one more scan cycle.
        sys.stderr.write("First scan returned empty; retrying once...\n")
        out = mpy_repl.exec_and_capture(s, SCAN_SCRIPT, settle=3.0)
        out = mpy_repl.collect_until(s, out, "SCAN-DONE", timeout=timeout)
        for line in out.splitlines():
            m = re.match(r"SCAN\s+(.+?)\s+(-?\d+)\s*$", line)
            if m:
                found.append((m.group(1), int(m.group(2))))

    if not found:
        guess = wanted.lower() if wanted != wanted.lower() else wanted
        sys.stderr.write(
            f"WiFi scan still returned nothing. Guessing SSID {guess!r} "
            f"(lowercased — AP names usually are).\n"
        )
        return guess

    exact = [n for n, _ in found if n == wanted]
    if exact:
        return exact[0]
    ci = [n for n, _ in found if n.lower() == wanted.lower()]
    if ci:
        sys.stderr.write(
            f"SSID case correction: {wanted!r} -> {ci[0]!r} (matched broadcast)\n"
        )
        return ci[0]
    sys.stderr.write(
        f"SSID {wanted!r} not found in scan. Visible: "
        f"{', '.join(n for n, _ in found)}\n"
        f"Using {wanted!r} verbatim — check for typos if it fails.\n"
    )
    return wanted


def build_write_script(ssid: str, password: str) -> str:
    keys = list(NVS_KEYS)
    for i, (k, v) in enumerate(keys):
        if k == "ssid0":
            keys[i] = (k, ssid)
        elif k == "pswd0":
            keys[i] = (k, password)

    lines = ["import esp32", 'nvs = esp32.NVS("uiflow")']
    for k, v in keys:
        # erase_key protects against a prior set_blob having claimed
        # the key under a different type tag.
        lines.append(f"try: nvs.erase_key({k!r})")
        lines.append("except Exception: pass")
        lines.append(f"nvs.set_str({k!r}, {v!r})")
    lines.append('try: nvs.erase_key("boot_option")')
    lines.append("except Exception: pass")
    lines.append("nvs.set_u8('boot_option', 1)")
    lines.append("nvs.commit()")
    lines.append('print("NVS-OK")')
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description="Set WiFi on a UIFlow device.")
    ap.add_argument("--port", required=True)
    ap.add_argument("--ssid", required=True)
    ap.add_argument("--password", required=True)
    ap.add_argument(
        "--no-scan",
        action="store_true",
        help="Skip WiFi scan / case correction (use SSID verbatim).",
    )
    args = ap.parse_args()

    s = mpy_repl.open_port(args.port)
    try:
        mpy_repl.hard_reset(s)
        mpy_repl.wait_for_boot(s, timeout=20.0)
        mpy_repl.interrupt_to_repl(s)

        ssid = args.ssid if args.no_scan else scan_for_ssid(s, args.ssid)
        script = build_write_script(ssid, args.password)
        out = mpy_repl.exec_and_capture(s, script, settle=1.0)
        if "NVS-OK" not in out:
            sys.stderr.write("NVS write didn't report OK. REPL echo:\n")
            sys.stderr.write(out + "\n")
            return 2

        sys.stderr.write("NVS write OK. Rebooting...\n")
        # Use REPL reset — works on native USB (ESP32-S3/C3) where DTR/RTS
        # hard_reset has no effect because there is no UART bridge chip.
        mpy_repl.repl_reset(s)
    finally:
        s.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
