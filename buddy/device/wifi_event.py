"""Optional WiFi auto-connect on boot.

Set ``SSID`` / ``PASSWORD`` below to your own network if you want
the launcher to come up online (the Push-to-Claude voice/chat app
needs WiFi). Leave them empty to skip the auto-connect — the
launcher will display ``WiFi: offline`` and continue normally.

To disable the auto-connect entirely, remove the
``wifi_event.connect_with_splash(...)`` call from ``main.py``.

The module deliberately does NOT touch NVS. UIFlow's startup reads
WiFi creds from NVS keys (``ssid0``, ``pswd0``, ``net_mode``,
etc.); we set ``boot_option=2`` to bypass UIFlow's launcher, so
those keys may or may not be honored depending on UIFlow's exact
boot path. Doing the connect in pure Python from our own ``main.py``
is deterministic regardless of that.
"""

# --- WIFI CREDENTIALS ---------------------------------------------------
# Primary network (home). Leave empty to skip the auto-connect.
SSID = ""
PASSWORD = ""
# Backup network (e.g. iPhone hotspot) — tried if primary fails to associate
# within the per-network timeout. Leave both empty to disable fallback.
BACKUP_SSID = ""
BACKUP_PASSWORD = ""
# -----------------------------------------------------------------------

# How long to wait for an IP before giving up. The venue network is
# 2.4 GHz; on a fresh boot the WLAN chip needs a few seconds to scan
# and associate. 8 s is generous without being annoying if the
# network isn't actually present (e.g. running this code at home).
CONNECT_TIMEOUT_MS = 8000


def connect(timeout_ms=CONNECT_TIMEOUT_MS):
    """Try to connect to WiFi. Returns a status dict.

    Tries the primary (SSID/PASSWORD) network first, then falls back to
    the backup (BACKUP_SSID/BACKUP_PASSWORD) if configured. Each network
    gets up to `timeout_ms` to associate before moving on.

    On success:
      {"ok": True, "ssid": <str>, "ip": <str>, "rssi": <int|None>,
       "elapsed_ms": <int>}

    On failure:
      {"ok": False, "ssid": <str>, "err": <str>, "elapsed_ms": <int>}

    Idempotent: if the STA is already connected (e.g. retried after
    a soft reboot that didn't drop the link), returns success
    immediately without re-connecting.
    """
    import network
    import time

    networks = []
    if SSID:
        networks.append((SSID, PASSWORD))
    if BACKUP_SSID:
        networks.append((BACKUP_SSID, BACKUP_PASSWORD))
    if not networks:
        return {
            "ok": False,
            "ssid": "",
            "err": "no SSID configured (edit buddy/device/wifi_event.py)",
            "elapsed_ms": 0,
        }

    sta = network.WLAN(network.STA_IF)
    if not sta.active():
        sta.active(True)

    if sta.isconnected():
        info = sta.ifconfig()
        return {
            "ok": True,
            "ssid": sta.config("ssid") if hasattr(sta, "config") else SSID,
            "ip": info[0],
            "rssi": _safe_rssi(sta),
            "elapsed_ms": 0,
        }

    overall_t0 = time.ticks_ms()
    last_err = "no networks reachable"
    for ssid_try, pw_try in networks:
        t0 = time.ticks_ms()
        try:
            sta.disconnect()
        except Exception:
            pass
        try:
            sta.connect(ssid_try, pw_try)
        except Exception as e:
            last_err = "connect call failed: {}".format(e)
            continue
        while not sta.isconnected():
            if time.ticks_diff(time.ticks_ms(), t0) > timeout_ms:
                last_err = "no IP within {}ms on {}".format(timeout_ms, ssid_try)
                break
            time.sleep_ms(200)
        if sta.isconnected():
            info = sta.ifconfig()
            # NTP sync — without this the ESP32 clock stays at boot
            # default (~Jan 1 2000), TLS cert validation fails with
            # mbedtls -0x202. Best-effort: ignore failures.
            try:
                import ntptime
                ntptime.host = "pool.ntp.org"
                ntptime.settime()
            except Exception:
                pass
            return {
                "ok": True,
                "ssid": ssid_try,
                "ip": info[0],
                "rssi": _safe_rssi(sta),
                "elapsed_ms": time.ticks_diff(time.ticks_ms(), overall_t0),
            }
    return {
        "ok": False,
        "ssid": ",".join(n[0] for n in networks),
        "err": last_err,
        "elapsed_ms": time.ticks_diff(time.ticks_ms(), overall_t0),
    }


def is_connected():
    """Lightweight query for code that wants to render a status pip
    without re-attempting the connect. Returns True iff the STA
    currently reports an active link."""
    try:
        import network
        return network.WLAN(network.STA_IF).isconnected()
    except Exception:
        return False


# --- Watchdog ---------------------------------------------------------
# State for ensure_connected(). The watchdog is invoked from app screen
# loops every ~second; cheap when connected, exp-backs-off when not.

_watch_next_try_ms = 0
_watch_attempt = 0


def rssi():
    """Current RSSI as int, or None if no link / no API. Cheap enough
    to call every screen tick — backs the live status bar."""
    try:
        import network
        sta = network.WLAN(network.STA_IF)
        if not sta.isconnected():
            return None
        return _safe_rssi(sta)
    except Exception:
        return None


def ensure_connected():
    """Idempotent reconnect watchdog. Called from app loops at any
    cadence — internally throttles retries with exponential backoff
    so a long outage doesn't burn CPU. Returns:
      ``True``  → currently connected (fast path)
      ``False`` → not connected, retry scheduled for later
      ``"reconnected"`` → just reconnected this call (caller should
                          drop cached TLS sockets via
                          ``http_pool.drop_all()``)

    Backoff schedule: 0.1 / 0.3 / 1 / 3 / 5 s (capped). Resets
    after a successful associate.
    """
    global _watch_next_try_ms, _watch_attempt
    try:
        import network
        import time
        sta = network.WLAN(network.STA_IF)
        if sta.isconnected():
            _watch_attempt = 0
            return True
        now = time.ticks_ms()
        if time.ticks_diff(now, _watch_next_try_ms) < 0:
            return False
        delays = (100, 300, 1000, 3000, 5000)
        delay = delays[min(_watch_attempt, len(delays) - 1)]
        _watch_next_try_ms = time.ticks_add(now, delay)
        _watch_attempt += 1
        if not sta.active():
            sta.active(True)
        if not SSID:
            return False
        try:
            sta.connect(SSID, PASSWORD)
        except Exception:
            return False
        # Cap blocking at 3 s so screen stays responsive.
        t0 = time.ticks_ms()
        while not sta.isconnected():
            if time.ticks_diff(time.ticks_ms(), t0) > 3000:
                return False
            time.sleep_ms(100)
        _watch_attempt = 0
        return "reconnected"
    except Exception:
        return False


def _safe_rssi(sta):
    """``sta.status('rssi')`` is supported on most builds but not
    universally. Wrap so a missing implementation doesn't crash the
    caller."""
    try:
        return sta.status("rssi")
    except Exception:
        return None
