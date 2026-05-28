"""Claude Pager — fire off cloud agents from the Cardputer.

Three screens, switched by the arrow keys:

  COMPOSE   ←→   INBOX   →   DETAIL
                              (Enter on a row)

Compose
  Type a task on the QWERTY, Enter to launch. Routes to the Worker's
  /pager/spawn, which provisions an Agent + Environment (cached after
  first call) and creates a Managed Agents session.

Inbox
  Live list of recent sessions with one-line status. Refreshed every
  ~4 seconds. Up/Down to scroll, Enter to drill into Detail, D to
  delete the highlighted row, N to jump back to Compose.

Detail
  Live ticker for one session. Long-polls /pager/poll so deltas show
  up within a couple of seconds of the agent acting. Keys:
    R  reply (sends a follow-up message)
    I  interrupt (sends user.interrupt)
    Y/N approve/deny pending tool confirmation (when present)
    O  refresh files (one-shot)
    ESC back to Inbox

Same secret model as Push to Claude — WORKER_BASE + DEVICE_SECRET
loaded from the gitignored config.py.
"""

import gc
import json
import time

import M5
import machine
from hardware import MatrixKeyboard

# Persistent-connection HTTPS client. Replaces ``requests`` per-call
# socket churn — the same TLS handshake (~1-2 s on this build) is
# reused for every pager poll and detail long-poll. See
# buddy/device/http_pool.py for the rationale. Lives at /flash/.
try:
    import http_pool as _pool  # type: ignore
except Exception as e:
    print("pager: http_pool unavailable, falling back to requests:", e)
    _pool = None

# WiFi watchdog. Called from idle loops; ensures we don't get stuck
# on a dropped link without the user knowing why pager polls fail.
try:
    import wifi_event as _wifi  # type: ignore
except Exception:
    _wifi = None


# ---- DEPLOYMENT CONFIG ----------------------------------------------

try:
    from . import config as _cfg  # type: ignore
except Exception:
    try:
        import config as _cfg  # type: ignore
    except Exception:
        _cfg = None

_WORKER_BASE = (getattr(_cfg, "WORKER_BASE", "") if _cfg else "").rstrip("/")
DEVICE_SECRET = getattr(_cfg, "DEVICE_SECRET", "") if _cfg else ""
DEVICE_PIN = getattr(_cfg, "DEVICE_PIN", "") if _cfg else ""

# Hotkey presets — type the key and hit Enter, buffer expands to full prompt
# before sending. Keeps the Cardputer's tiny keyboard fast for common ops.
PRESETS = {
    "w": "whoop today: recovery, strain, sleep — read from pi dashboard",
    "f": "forex bot P&L today + open positions + last trade",
    "p": "pi health: disk, journal size, services up, recent crashes",
    "m": "milemarkt new buyer messages from supabase mm_messages",
    "r": "mostly_chill_tbh recent comments + upvote totals from reddit",
    "n": "networth dashboard total + delta today from pi :8501",
    "a": "award radar inbox top 5 unread from supabase award table",
    "?": "list all available skills and their short triggers",
}

# Patterns that look like real-money spend — trigger an on-device Y/N
# confirmation before sending. Cardputer typing is fast enough that
# typos like 'swiggy 1000' instead of '100' are a real risk.
_BRAND_RE = ("swiggy", "amazon", "starbucks", "flipkart", "myntra",
             "bigbasket", "nykaa", "blinkit", "zomato", "tatacliq",
             "lifestyle", "ajio", "shoppers", "cleartrip", "yatra",
             "easemytrip", "ola", "uber", "pvr", "inox", "decathlon",
             "ikea", "croma", "tanishq", "kfc", "dominos", "pizzahut",
             "gyftr", "buy")

URL_SPAWN = _WORKER_BASE + "/pager/spawn"
URL_SESSIONS = _WORKER_BASE + "/pager/sessions"
URL_POLL = _WORKER_BASE + "/pager/poll"
URL_REPLY = _WORKER_BASE + "/pager/reply"
URL_INTERRUPT = _WORKER_BASE + "/pager/interrupt"
URL_DELETE = _WORKER_BASE + "/pager/delete"
URL_CONFIRM = _WORKER_BASE + "/pager/confirm"


# ---- THEME (matches the rest of the bundle) -------------------------

_BLACK = 0x000000
_ORANGE = 0xCC785C
_CREAM = 0xF0EEE6
_DARK = 0x1F1F1F
_GRAY_MID = 0x777777
_GRAY_DARK = 0x444444
_GREEN = 0x6EA85F
_RED = 0xCF5B56
_YELLOW = 0xD6B85C
_BLUE = 0x6E9BCF

_LCD = M5.Lcd
_W = 240
_H = 135


# ---- HTTP helpers ---------------------------------------------------

def _hdrs(extra=None):
    h = {
        "x-device-secret": DEVICE_SECRET,
        "content-type": "application/json",
    }
    if extra:
        h.update(extra)
    return h


# Latency tracking for the live status pip. Last successful request's
# round-trip in ms; surfaced in the header as "online · 80ms" etc.
_last_rtt_ms = 0
_last_req_ok = True


def _http_json(method, url, body=None, timeout=15):
    """Request → (status, parsed_json_or_text). Routes through the
    persistent-connection pool if available; falls back to one-shot
    ``requests`` calls otherwise. Always wraps transport errors as
    ``(0, "transport: ...")`` for the same contract the rest of the
    file already handles."""
    global _last_rtt_ms, _last_req_ok
    t0 = time.ticks_ms()
    if _pool is not None:
        # Pool handles JSON encode + content-type; we just pass dict.
        status, value = _pool.request_json(
            method, url, body=body, headers=_hdrs(),
            timeout_ms=timeout * 1000,
        )
        _last_rtt_ms = time.ticks_diff(time.ticks_ms(), t0)
        _last_req_ok = (status != 0)
        return status, value

    # Fallback: legacy urequests path.
    import requests
    try:
        if method == "GET":
            r = requests.get(url, headers=_hdrs(), timeout=timeout)
        else:
            data = json.dumps(body or {}).encode("utf-8")
            r = requests.post(url, data=data, headers=_hdrs(), timeout=timeout)
    except Exception as e:
        _last_req_ok = False
        return 0, "transport: {}".format(e)

    status = r.status_code
    text = ""
    try:
        text = r.text
    except Exception:
        text = ""
    try:
        r.close()
    except Exception:
        pass

    _last_rtt_ms = time.ticks_diff(time.ticks_ms(), t0)
    _last_req_ok = True
    if not text:
        return status, {}
    try:
        return status, json.loads(text)
    except Exception:
        return status, text


# --- WiFi watchdog ---------------------------------------------------
# Cheap to call every idle tick; ensure_connected throttles internally.
# When the link recovers, drop pool sockets so we don't try to reuse a
# TCP underlay that died with the WiFi association.

def _tick_wifi():
    if _wifi is None:
        return
    state = _wifi.ensure_connected()
    if state == "reconnected" and _pool is not None:
        try:
            _pool.drop_all()
        except Exception:
            pass


def _live_pip():
    """Build the header status pip (text, color) from current WiFi +
    last-request state. Surfaces RSSI bars + RTT so the user can tell
    at a glance whether the network or the agent is the slow part."""
    if _wifi is not None and not _wifi.is_connected():
        return ("OFFLINE", _RED)
    if not _last_req_ok:
        return ("ERR", _RED)
    rssi = _wifi.rssi() if _wifi is not None else None
    bars = "..."
    if rssi is not None:
        # -50 strong / -65 ok / -78 weak / worse → very weak
        if rssi >= -55:
            bars = "###"
        elif rssi >= -68:
            bars = "##-"
        elif rssi >= -78:
            bars = "#--"
        else:
            bars = "..."
    if _last_rtt_ms:
        return ("{} {}ms".format(bars, _last_rtt_ms), _GREEN)
    return (bars, _GREEN)


# ---- Drawing helpers ------------------------------------------------

def _set_font():
    try:
        _LCD.setFont(_LCD.FONTS.DejaVu9)
    except Exception as e:
        print("pager: setFont fallback:", e)


def _wrap_lines(text, max_w_px, char_size=1):
    _LCD.setTextSize(char_size)
    out = []
    for raw_line in (text or "").split("\n"):
        words = raw_line.split(" ")
        cur = ""
        for w in words:
            cand = w if not cur else cur + " " + w
            if _LCD.textWidth(cand) <= max_w_px:
                cur = cand
            else:
                if cur:
                    out.append(cur)
                cur = w
                # Force-break overlong tokens.
                while _LCD.textWidth(cur) > max_w_px and len(cur) > 1:
                    cut = len(cur) - 1
                    while cut > 1 and _LCD.textWidth(cur[:cut]) > max_w_px:
                        cut -= 1
                    out.append(cur[:cut])
                    cur = cur[cut:]
        out.append(cur)
    return out


# ---- Notifications --------------------------------------------------
# Background poller that runs from every screen's idle loop. Fires a
# beep + on-screen banner when an agent transitions running->idle (DONE),
# pings for tool confirmation (NEEDS YOU), or terminates (ERROR). Per-
# session last-seen state is persisted to /flash/.pager_notif.json so
# the same DONE doesn't fire after a reboot.

_NOTIF_POLL_MS = 15000
_NOTIF_STATE_FILE = "/flash/.pager_notif.json"
_notif_status = {}
_notif_pending = {}
_notif_last_poll = 0
_notif_loaded = False


def _notif_load():
    global _notif_status, _notif_pending, _notif_loaded
    if _notif_loaded:
        return
    try:
        with open(_NOTIF_STATE_FILE) as f:
            d = json.load(f)
        _notif_status = d.get("status") or {}
        _notif_pending = d.get("pending") or {}
    except Exception:
        _notif_status, _notif_pending = {}, {}
    _notif_loaded = True


def _notif_save():
    try:
        with open(_NOTIF_STATE_FILE, "w") as f:
            json.dump({"status": _notif_status, "pending": _notif_pending}, f)
    except Exception:
        pass


def _beep(seq):
    """Play a sequence of (freq_hz, duration_ms) tones via the M5
    Speaker. Wrapped in try/except so notifications still flash on
    a build where Speaker isn't wired up."""
    try:
        sp = M5.Speaker
        for f, d in seq:
            sp.tone(f, d)
            time.sleep_ms(d + 20)
    except Exception:
        pass


def _flash_banner(text, color):
    """Centered banner band. The caller's next screen redraw clears it;
    we don't try to save/restore — repaint is always cheaper than the
    pixel-level diff."""
    band_y = 50
    band_h = 28
    _LCD.fillRect(0, band_y, _W, band_h, color)
    _LCD.setTextSize(1)
    _LCD.setTextColor(_BLACK, color)
    _LCD.drawString(text, (_W - _LCD.textWidth(text)) // 2, band_y + 10)
    time.sleep_ms(1200)


def _notif_tick():
    """Poll /pager/sessions if the interval elapsed; fire notifications
    for state transitions. Returns True if any banner was drawn so the
    calling screen knows to repaint."""
    global _notif_last_poll
    _notif_load()
    now = time.ticks_ms()
    if time.ticks_diff(now, _notif_last_poll) < _NOTIF_POLL_MS:
        return False
    _notif_last_poll = now
    sessions, err = _fetch_sessions()
    if err or not sessions:
        return False
    fired = False
    for s in sessions:
        sid = s.get("session_id")
        if not sid:
            continue
        sm = s.get("summary") or {}
        status = sm.get("status") or "idle"
        pending = bool(sm.get("pendingConfirm"))
        title = _safe_text(s.get("title") or sid[-8:], max_chars=24)
        prev_status = _notif_status.get(sid)
        prev_pending = _notif_pending.get(sid, False)
        if prev_status == "running" and status == "idle":
            _beep([(880, 80), (1320, 120)])
            _flash_banner("DONE: " + title, _GREEN)
            fired = True
        elif prev_status and prev_status != "terminated" and status == "terminated":
            _beep([(440, 120), (220, 200)])
            _flash_banner("ERROR: " + title, _RED)
            fired = True
        if pending and not prev_pending:
            _beep([(1175, 60), (1175, 60), (1175, 80)])
            _flash_banner("NEEDS YOU: " + title, _YELLOW)
            fired = True
        _notif_status[sid] = status
        _notif_pending[sid] = pending
    if fired:
        _notif_save()
    return fired


# ---- Chrome ---------------------------------------------------------
# Original full-repaint pattern (matches push_to_claude.py + launcher).
# An earlier cached-chrome refactor saved redraws but caused a hard
# reset on device — heap was too tight after parsing the source. With
# .mpy bytecode that constraint is gone; could revisit later.
def _draw_chrome(title, hint, status_pip=None):
    _LCD.fillScreen(_BLACK)
    _LCD.fillRect(0, 0, _W, 20, _DARK)
    _LCD.fillRect(0, 20, _W, 1, _ORANGE)
    _LCD.setTextSize(1)
    _LCD.setTextColor(_ORANGE, _DARK)
    _LCD.drawString(title, 6, 5)
    if status_pip:
        text, color = status_pip
        _LCD.setTextColor(color, _DARK)
        _LCD.drawString(text, _W - _LCD.textWidth(text) - 6, 5)
    _LCD.fillRect(0, _H - 18, _W, 18, _DARK)
    _LCD.setTextColor(_GRAY_MID, _DARK)
    _LCD.drawString(hint, (_W - _LCD.textWidth(hint)) // 2, _H - 14)


def _reset_chrome():
    pass  # no-op; left in for call-site compatibility


def _safe_text(s, max_chars=None):
    """Strip newlines + non-ASCII for safe display. DejaVu9 has no
    glyphs for em-dashes, smart quotes, etc., and literal newlines
    render as tofu / shove text off-screen. Tight loop, no dict
    literal — keeps the source small and avoids heap pressure
    during the module import."""
    if not s:
        return ""
    out = []
    for ch in s:
        o = ord(ch)
        if o == 9 or o == 10 or o == 13:
            out.append(" ")
        elif 0x20 <= o <= 0x7E:
            out.append(ch)
        else:
            out.append("?")
    s = " ".join("".join(out).split())
    if max_chars is not None:
        s = s[:max_chars]
    return s


def _flash(text, color=_GREEN, ms=600):
    _LCD.fillRect(0, _H - 36, _W, 18, _BLACK)
    _LCD.setTextSize(1)
    _LCD.setTextColor(color, _BLACK)
    _LCD.drawString(text, (_W - _LCD.textWidth(text)) // 2, _H - 32)
    time.sleep_ms(ms)


# ---- Key helpers ----------------------------------------------------

def _to_char(k):
    if k is None:
        return None
    if isinstance(k, int):
        if 0x20 <= k <= 0x7E:
            return chr(k)
        return None
    if isinstance(k, str) and k:
        return k
    return None


def _is_enter(k):
    if isinstance(k, int) and k in (0x0A, 0x0D):
        return True
    return isinstance(k, str) and k in ("\r", "\n")


def _is_esc(k):
    return isinstance(k, int) and k == 0x1B


def _is_backspace(k):
    if isinstance(k, int) and k in (0x08, 0x7F):
        return True
    return isinstance(k, str) and k in ("\b", "\x7f")


def _arrow(k):
    """Return 'up' / 'down' / 'left' / 'right' / None for the
    Cardputer-Adv arrow cluster (semicolon=up, comma=left, period=down,
    slash=right). Same mapping the launcher uses.
    """
    ch = _to_char(k)
    if not ch:
        return None
    ch = ch.lower()
    if ch == ";":
        return "up"
    if ch == ".":
        return "down"
    if ch == ",":
        return "left"
    if ch == "/":
        return "right"
    return None


# ---- Status formatting ----------------------------------------------

_STATUS_COLORS = {
    "running": _GREEN,
    "idle": _ORANGE,
    "rescheduling": _YELLOW,
    "terminated": _RED,
}


def _status_color(s):
    return _STATUS_COLORS.get(s or "idle", _GRAY_MID)


def _summarize_subline(summary):
    """One short line about what the agent is currently doing.
    Falls back through tool -> text -> blank. Always passes through
    _safe_text so non-ASCII glyphs (em-dash, smart quotes) and
    embedded newlines from agent tool inputs don't render as tofu
    or shove text off-screen."""
    if not summary:
        return ""
    tool = summary.get("lastTool")
    if tool:
        s = tool.get("summary") or ""
        n = tool.get("name") or "tool"
        raw = "{}: {}".format(n, s) if s else n
        return _safe_text(raw, max_chars=48)
    text = summary.get("lastText") or ""
    return _safe_text(text, max_chars=48)


# =====================================================================
# COMPOSE SCREEN
# =====================================================================

def _draw_compose(buf, cursor_on, status_msg=None, status_color=_GREEN):
    pip = ("NO URL", _RED) if not _WORKER_BASE else _live_pip()
    _draw_chrome(
        "Pager Compose",
        "Enter send  -> inbox  Q exit",
        status_pip=pip,
    )
    _LCD.setTextSize(1)
    _LCD.setTextColor(_GRAY_MID, _BLACK)
    _LCD.drawString("Type a task for Claude:", 6, 26)
    _LCD.fillRect(6, 42, _W - 12, 56, _BLACK)
    lines = _wrap_lines(buf or " ", _W - 12, 1) or [""]
    if len(lines) > 5:
        lines = lines[-5:]
    y = 42
    _LCD.setTextColor(_CREAM, _BLACK)
    for line in lines:
        _LCD.drawString(line, 6, y)
        y += 11
    last = lines[-1] if lines else ""
    cur_x = 6 + _LCD.textWidth(last)
    cur_y = y - 11
    if cursor_on:
        _LCD.fillRect(cur_x, cur_y + 1, 6, 9, _ORANGE)
    if status_msg:
        _LCD.fillRect(0, _H - 36, _W, 14, _BLACK)
        _LCD.setTextColor(status_color, _BLACK)
        _LCD.drawString(status_msg, (_W - _LCD.textWidth(status_msg)) // 2, _H - 32)


def _run_compose(kb):
    """Returns next screen name ('inbox', 'compose', 'exit')."""
    buf = ""
    cursor_on = True
    last_blink = time.ticks_ms()
    status_msg = None
    status_color = _GREEN
    _draw_compose(buf, cursor_on)

    while True:
        kb.tick()
        k = kb.get_key()
        now = time.ticks_ms()
        if time.ticks_diff(now, last_blink) >= 500:
            cursor_on = not cursor_on
            _draw_compose(buf, cursor_on, status_msg, status_color)
            last_blink = now
        # WiFi watchdog — cheap when connected, throttled retries
        # when not. Run on every loop tick so dropped links are
        # caught within a few seconds rather than on next user
        # request.
        _tick_wifi()
        # Notification poller — repaints if a banner fired.
        if _notif_tick():
            _draw_compose(buf, cursor_on, status_msg, status_color)
        if k is None:
            time.sleep_ms(40)
            continue
        if _is_esc(k):
            return "exit"
        ch = _to_char(k)
        if ch and ch.lower() == "q" and not buf:
            return "exit"
        if _arrow(k) == "right":
            return "inbox"
        if _is_enter(k):
            prompt = buf.strip()
            if not prompt:
                continue
            # Expand single-char preset to full prompt (e.g. "w" -> "whoop ...")
            prompt = _expand_preset(prompt)
            # Spend guard: require Y/N confirmation on real-money patterns
            if _looks_like_spend(prompt):
                if not _confirm_spend(kb, prompt):
                    status_msg = "cancelled"
                    status_color = _GRAY_MID
                    _draw_compose(buf, True, status_msg, status_color)
                    continue
            status_msg = "launching..."
            status_color = _ORANGE
            _draw_compose(buf, True, status_msg, status_color)
            status, body = _http_json("POST", URL_SPAWN, {"prompt": prompt}, timeout=20)
            if status == 200 and isinstance(body, dict) and body.get("ok"):
                _flash("launched", _GREEN, 600)
                return "inbox"
            msg = "spawn failed"
            if isinstance(body, dict):
                msg = body.get("message") or body.get("error") or msg
            elif isinstance(body, str):
                msg = body[:32]
            status_msg = _safe_text(msg, max_chars=36)
            status_color = _RED
            _draw_compose(buf, True, status_msg, status_color)
            continue
        if _is_backspace(k):
            if buf:
                buf = buf[:-1]
                status_msg = None
                _draw_compose(buf, True, None)
            continue
        if ch and len(buf) < 280:
            buf += ch
            status_msg = None
            _draw_compose(buf, True, None)


# =====================================================================
# INBOX SCREEN
# =====================================================================

_INBOX_REFRESH_MS = 4000


def _fetch_sessions():
    status, body = _http_json("GET", URL_SESSIONS, timeout=12)
    if status != 200 or not isinstance(body, dict):
        return None, "list failed"
    return body.get("sessions") or [], None


def _draw_inbox(sessions, cursor, scroll_top, err=None):
    pip = ("ERR", _RED) if err else _live_pip()
    _draw_chrome(
        "Pager · Inbox",
        "<- compose  Enter open  D del",
        status_pip=pip,
    )
    _LCD.setTextSize(1)

    if err:
        _LCD.setTextColor(_RED, _BLACK)
        _LCD.drawString(err, 6, 42)
        return

    if not sessions:
        _LCD.setTextColor(_GRAY_MID, _BLACK)
        _LCD.drawString("No sessions yet.", 6, 42)
        _LCD.drawString("<- to compose a task.", 6, 56)
        return

    # 4 rows visible at 24 px each, leaving room for header + footer.
    rows = 4
    row_h = 26
    visible = sessions[scroll_top:scroll_top + rows]
    y = 24
    for i, s in enumerate(visible):
        abs_i = scroll_top + i
        is_sel = (abs_i == cursor)
        # Row background
        if is_sel:
            _LCD.fillRect(0, y, _W, row_h, 0x1A1A18)
            _LCD.fillRect(0, y, 3, row_h, _ORANGE)

        title = (s.get("title") or s.get("session_id", "")[:12])
        title = title[:32]

        summary = s.get("summary") or {}
        st = summary.get("status") or "idle"
        st_color = _status_color(st)

        # Top line: title + status pip
        _LCD.setTextColor(_CREAM if is_sel else _CREAM, _BLACK if not is_sel else 0x1A1A18)
        _LCD.drawString(title, 6, y + 3)
        _LCD.setTextColor(st_color, _BLACK if not is_sel else 0x1A1A18)
        st_label = st[:10]
        _LCD.drawString(st_label, _W - _LCD.textWidth(st_label) - 6, y + 3)

        # Bottom line: subline (last tool / last text)
        sub = _summarize_subline(summary)
        _LCD.setTextColor(_GRAY_MID, _BLACK if not is_sel else 0x1A1A18)
        _LCD.drawString(sub, 6, y + 14)

        y += row_h

    # Scroll indicators on the right edge
    if scroll_top > 0:
        _LCD.setTextColor(_ORANGE, _BLACK)
        _LCD.drawString("^", _W - 12, 24)
    if scroll_top + rows < len(sessions):
        _LCD.setTextColor(_ORANGE, _BLACK)
        _LCD.drawString("v", _W - 12, _H - 32)


def _run_inbox(kb):
    sessions = []
    cursor = 0
    scroll_top = 0
    err = None
    visible_rows = 4
    last_refresh = 0

    def _refresh():
        nonlocal sessions, err, cursor, scroll_top
        new_sessions, e = _fetch_sessions()
        if e:
            err = e
            return
        err = None
        sessions = new_sessions or []
        if cursor >= len(sessions):
            cursor = max(0, len(sessions) - 1)
        if scroll_top > cursor:
            scroll_top = cursor
        if scroll_top + visible_rows <= cursor:
            scroll_top = max(0, cursor - visible_rows + 1)

    _refresh()
    last_refresh = time.ticks_ms()
    _draw_inbox(sessions, cursor, scroll_top, err)

    while True:
        kb.tick()
        k = kb.get_key()

        if k is not None:
            if _is_esc(k):
                return "exit"
            arrow = _arrow(k)
            ch = _to_char(k)

            if arrow == "left":
                return "compose"
            if arrow == "up":
                if sessions:
                    cursor = (cursor - 1) % len(sessions)
                    if cursor < scroll_top:
                        scroll_top = cursor
                    elif cursor >= scroll_top + visible_rows:
                        scroll_top = max(0, len(sessions) - visible_rows)
                    _draw_inbox(sessions, cursor, scroll_top, err)
            elif arrow == "down":
                if sessions:
                    cursor = (cursor + 1) % len(sessions)
                    if cursor >= scroll_top + visible_rows:
                        scroll_top = cursor - visible_rows + 1
                    elif cursor < scroll_top:
                        scroll_top = 0
                    _draw_inbox(sessions, cursor, scroll_top, err)
            elif _is_enter(k):
                if sessions:
                    sid = sessions[cursor].get("session_id")
                    if sid:
                        return ("detail", sid)
            elif ch and ch.lower() == "n":
                return "compose"
            elif ch and ch.lower() == "d":
                if sessions:
                    sid = sessions[cursor].get("session_id")
                    title = sessions[cursor].get("title") or sid
                    _confirm_delete(kb, sid, title[:24])
                    _refresh()
                    last_refresh = time.ticks_ms()
                    _draw_inbox(sessions, cursor, scroll_top, err)
            elif ch and ch.lower() == "q":
                return "exit"

        # Periodic refresh.
        now = time.ticks_ms()
        if time.ticks_diff(now, last_refresh) >= _INBOX_REFRESH_MS:
            _refresh()
            last_refresh = now
            _draw_inbox(sessions, cursor, scroll_top, err)
            gc.collect()
        # WiFi watchdog runs every iteration; throttles internally.
        _tick_wifi()
        # Notification poller. Inbox already shows session statuses, so
        # the banner is mostly redundant here, but it gives audio +
        # consistency across screens.
        if _notif_tick():
            _draw_inbox(sessions, cursor, scroll_top, err)

        time.sleep_ms(50)


def _confirm_delete(kb, sid, title):
    _LCD.fillRect(0, 22, _W, _H - 22 - 18, _BLACK)
    _LCD.setTextSize(1)
    _LCD.setTextColor(_RED, _BLACK)
    _LCD.drawString("Delete session?", 6, 30)
    _LCD.setTextColor(_CREAM, _BLACK)
    _LCD.drawString(title, 6, 50)
    _LCD.setTextColor(_GRAY_MID, _BLACK)
    _LCD.drawString("Y/N", 6, 80)

    while True:
        kb.tick()
        k = kb.get_key()
        ch = _to_char(k)
        if ch and ch.lower() == "y":
            _flash("deleting...", _ORANGE, 200)
            status, body = _http_json("POST", URL_DELETE, {"session_id": sid}, timeout=15)
            if status == 200:
                _flash("deleted", _GREEN, 500)
            else:
                _flash("delete failed", _RED, 700)
            return
        if (ch and ch.lower() == "n") or _is_esc(k):
            return
        time.sleep_ms(40)


# =====================================================================
# DETAIL SCREEN
# =====================================================================

_DETAIL_POLL_BUDGET_MS = "10000"
_MAX_VISIBLE_EVENTS = 5


def _format_event_for_detail(ev):
    """Compact per-event line for the Cardputer detail view."""
    t = ev.get("type") or ""
    p = ev.get("payload") or {}
    if t == "agent.message":
        text_parts = []
        for b in (p.get("content") or []):
            if b.get("type") == "text":
                text_parts.append(b.get("text") or "")
        text = " ".join(text_parts).strip()
        if not text:
            return None, None
        # Last line wins — typically the punchline.
        last = text.split("\n")[-1].strip()
        return ("•", last[:120])
    if t == "agent.tool_use":
        name = p.get("name") or "tool"
        inp = p.get("input") or {}
        if name == "bash":
            cmd = (inp.get("command") or "").split("\n")[0]
            return ("$", cmd[:80])
        path = inp.get("path") or ""
        return ("⚙", "{} {}".format(name, path)[:80])
    if t == "agent.thinking":
        return ("…", "thinking")
    if t == "session.status_idle":
        return ("✓", "idle · {}".format(p.get("stop_reason") or ""))
    if t == "session.status_running":
        return ("▶", "running")
    if t == "session.status_terminated":
        return ("✗", "terminated")
    if t == "session.error":
        return ("!", (p.get("error") or {}).get("message") or "error")
    return None, None


def _draw_detail(meta, summary, recent_events, err=None, busy=False, scroll_off=0):
    title = (meta or {}).get("title") or "—"
    title = title[:24]
    st = (summary or {}).get("status") or "idle"
    pip_text = "BUSY" if busy else st.upper()[:7]
    pip_color = _ORANGE if busy else _status_color(st)
    _draw_chrome(
        "» " + title,
        "R reply  I stop  D del  Esc",
        status_pip=(pip_text, pip_color),
    )
    _LCD.setTextSize(1)

    if err:
        _LCD.setTextColor(_RED, _BLACK)
        for i, line in enumerate(_wrap_lines(err, _W - 12, 1)[:4]):
            _LCD.drawString(line, 6, 28 + i * 12)
        return

    # Summary line: lastTool or lastText
    sub = _summarize_subline(summary)
    _LCD.setTextColor(_GRAY_MID, _BLACK)
    if sub:
        _LCD.drawString(sub[:36], 6, 24)

    # Pending tool confirmation banner — overrides the event log.
    pc = (summary or {}).get("pendingConfirm")
    if pc:
        _LCD.fillRect(0, 38, _W, 50, 0x2A1F1A)
        _LCD.setTextColor(_YELLOW, 0x2A1F1A)
        _LCD.drawString("CONFIRM TOOL CALL", 6, 42)
        _LCD.setTextColor(_CREAM, 0x2A1F1A)
        _LCD.drawString((pc.get("name") or "tool")[:30], 6, 56)
        _LCD.setTextColor(_GRAY_MID, 0x2A1F1A)
        _LCD.drawString("Y approve   N deny", 6, 72)
        return

    # Event log lines (newest at bottom).
    y = 38
    visible = recent_events[-_MAX_VISIBLE_EVENTS - scroll_off:][:_MAX_VISIBLE_EVENTS] if scroll_off else recent_events[-_MAX_VISIBLE_EVENTS:]
    for sigil, text in visible:
        if sigil == "$":
            _LCD.setTextColor(_GREEN, _BLACK)
        elif sigil == "•":
            _LCD.setTextColor(_CREAM, _BLACK)
        elif sigil == "…":
            _LCD.setTextColor(_GRAY_MID, _BLACK)
        elif sigil == "✓":
            _LCD.setTextColor(_ORANGE, _BLACK)
        elif sigil == "!":
            _LCD.setTextColor(_RED, _BLACK)
        else:
            _LCD.setTextColor(_BLUE, _BLACK)
        line = "{} {}".format(sigil, text)
        # Hard truncate to fit; 1px font ~ 35-40 chars across 240 px.
        _LCD.drawString(line[:40], 6, y)
        y += 11
        if y > _H - 20:
            break


def _detail_poll_once(sid, since):
    # `compact=1` asks the Worker to strip large fields (full message
    # text, tool outputs, thinking traces) before returning. The
    # Cardputer's MicroPython requests library ECONNABORTs on multi-KB
    # SSL responses, so this isn't optional — sessions with chunky
    # agent.message bodies (e.g. research roundups) would otherwise
    # be impossible to view from the device.
    url = "{}?session={}&since={}&wait=1&budget_ms={}&compact=1".format(
        URL_POLL, sid, since, _DETAIL_POLL_BUDGET_MS,
    )
    return _http_json("GET", url, timeout=15)


def _run_detail(kb, sid):
    meta = None
    summary = None
    recent = []           # list of (sigil, text), newest last
    since = 0
    err = None

    def _ingest(events):
        nonlocal recent
        for ev in events or []:
            sigil, text = _format_event_for_detail(ev)
            if sigil:
                recent.append((sigil, text))
        # Cap length so we don't grow unbounded over hours.
        if len(recent) > 60:
            recent = recent[-60:]

    # Initial sync — read snapshot without long-poll, then begin live loop.
    # `compact=1` keeps the response under MicroPython's SSL-buffer limit.
    status, body = _http_json(
        "GET", "{}?session={}&since=0&wait=0&compact=1".format(URL_POLL, sid), timeout=12,
    )
    if status == 200 and isinstance(body, dict):
        meta = body.get("meta") or meta
        summary = body.get("summary")
        _ingest(body.get("events"))
        since = (summary or {}).get("seq") or 0
    elif status == 0 or status >= 500:
        err = body if isinstance(body, str) else (body or {}).get("message", "fetch failed")

    _draw_detail(meta, summary, recent, err)

    while True:
        # Process keys for up to one poll cycle so the UI stays
        # responsive even though _http_json blocks for ~10s.
        kb.tick()
        k = kb.get_key()
        if k is not None:
            ch = _to_char(k)
            if _is_esc(k):
                return "inbox"
            if ch and ch.lower() == "r":
                _detail_reply(kb, sid)
                # Force a one-shot refresh on return to surface the
                # new user message in the log promptly.
                since_local = since
                status, body = _http_json(
                    "GET", "{}?session={}&since={}&wait=0&compact=1".format(URL_POLL, sid, since_local), timeout=10,
                )
                if status == 200 and isinstance(body, dict):
                    summary = body.get("summary") or summary
                    _ingest(body.get("events"))
                    since = (summary or {}).get("seq") or since
                _draw_detail(meta, summary, recent, None)
                continue
            if ch and ch.lower() == "i":
                _detail_interrupt(sid)
                _draw_detail(meta, summary, recent, None)
                continue
            if ch and ch.lower() == "d":
                title = (meta or {}).get("title") or sid
                _confirm_delete(kb, sid, title[:24])
                return "inbox"
            if ch and ch.lower() == "y" and (summary or {}).get("pendingConfirm"):
                _confirm_pending(sid, summary["pendingConfirm"], True)
                continue
            if ch and ch.lower() == "n" and (summary or {}).get("pendingConfirm"):
                _confirm_pending(sid, summary["pendingConfirm"], False)
                continue
            if ch and ch.lower() == "o":
                # Force a fresh poll without waiting.
                pass

        # Long-poll for new events.
        status, body = _detail_poll_once(sid, since)
        if status == 200 and isinstance(body, dict):
            err = None
            meta = body.get("meta") or meta
            summary = body.get("summary") or summary
            new_events = body.get("events") or []
            if new_events:
                _ingest(new_events)
                since = (summary or {}).get("seq") or since
        elif status == 0 or status >= 500:
            err = body if isinstance(body, str) else (body or {}).get("message") or "poll failed"
        elif status in (401, 403):
            err = "auth rejected"

        _draw_detail(meta, summary, recent, err)
        # WiFi watchdog between long-poll cycles.
        _tick_wifi()
        # Cross-session notification poll (this session's status is
        # already implied by what we just rendered).
        if _notif_tick():
            _draw_detail(meta, summary, recent, err)
        gc.collect()


def _detail_reply(kb, sid):
    """Modal text input that sends a follow-up message."""
    buf = ""
    cursor_on = True
    last_blink = time.ticks_ms()
    err = None

    def _draw():
        _LCD.fillRect(0, 21, _W, _H - 21 - 18, _BLACK)
        _LCD.fillRect(0, _H - 18, _W, 18, _DARK)
        _LCD.setTextSize(1)
        _LCD.setTextColor(_GRAY_MID, _DARK)
        h = "Enter send  Esc cancel"
        _LCD.drawString(h, (_W - _LCD.textWidth(h)) // 2, _H - 14)
        _LCD.setTextColor(_ORANGE, _BLACK)
        _LCD.drawString("Reply to agent:", 6, 28)
        lines = _wrap_lines(buf or " ", _W - 12, 1) or [""]
        if len(lines) > 5:
            lines = lines[-5:]
        y = 44
        _LCD.setTextColor(_CREAM, _BLACK)
        for line in lines:
            _LCD.drawString(line, 6, y)
            y += 11
        last = lines[-1] if lines else ""
        cur_x = 6 + _LCD.textWidth(last)
        if cursor_on:
            _LCD.fillRect(cur_x, y - 10, 6, 9, _ORANGE)
        if err:
            _LCD.setTextColor(_RED, _BLACK)
            _LCD.drawString(err[:36], 6, _H - 30)

    _draw()

    while True:
        kb.tick()
        k = kb.get_key()
        now = time.ticks_ms()
        if time.ticks_diff(now, last_blink) >= 350:
            cursor_on = not cursor_on
            _draw()
            last_blink = now
        if k is None:
            time.sleep_ms(40)
            continue
        if _is_esc(k):
            return
        if _is_enter(k):
            text = buf.strip()
            if not text:
                continue
            status, body = _http_json("POST", URL_REPLY,
                                     {"session_id": sid, "prompt": text}, timeout=15)
            if status == 200:
                _flash("sent", _GREEN, 400)
                return
            err = "send failed"
            if isinstance(body, dict):
                err = body.get("message") or body.get("error") or err
            _draw()
            continue
        if _is_backspace(k):
            if buf:
                buf = buf[:-1]
                _draw()
            continue
        ch = _to_char(k)
        if ch and len(buf) < 240:
            buf += ch
            _draw()


def _detail_interrupt(sid):
    _flash("interrupting...", _ORANGE, 200)
    status, body = _http_json("POST", URL_INTERRUPT, {"session_id": sid}, timeout=10)
    if status == 200:
        _flash("interrupt sent", _RED, 500)
    else:
        _flash("interrupt failed", _RED, 600)


def _confirm_pending(sid, pc, approve):
    body = {
        "session_id": sid,
        "tool_use_id": pc.get("toolUseId"),
        "approve": bool(approve),
    }
    status, _ = _http_json("POST", URL_CONFIRM, body, timeout=10)
    _flash(("approved" if approve else "denied") if status == 200 else "confirm failed",
           _GREEN if (status == 200 and approve) else (_YELLOW if status == 200 else _RED),
           400)


# =====================================================================
# ENTRY
# =====================================================================

def _config_check():
    if not _WORKER_BASE or not DEVICE_SECRET:
        _LCD.fillScreen(_BLACK)
        _LCD.setTextSize(1)
        _LCD.setTextColor(_RED, _BLACK)
        _LCD.drawString("Pager: missing config", 6, 30)
        _LCD.setTextColor(_CREAM, _BLACK)
        for i, line in enumerate([
            "Copy config.example.py to",
            "config.py and fill in",
            "WORKER_BASE + DEVICE_SECRET.",
            "",
            "Press any key to exit.",
        ]):
            _LCD.drawString(line, 6, 50 + i * 11)
        kb = MatrixKeyboard()
        while True:
            kb.tick()
            if kb.get_key() is not None:
                return False
            time.sleep_ms(80)
    return True


def _expand_preset(buf):
    """If buf is a single-char preset trigger, return the full prompt.
    Otherwise return buf unchanged."""
    s = buf.strip()
    if len(s) == 1 and s in PRESETS:
        return PRESETS[s]
    return buf


def _looks_like_spend(prompt):
    """Heuristic: prompt looks like a real-money spend → confirm first.
    Matches '<brand> <amount>' where brand is in the known list and
    amount is a 2-5 digit number."""
    p = prompt.strip().lower()
    parts = p.split()
    if len(parts) < 2:
        return False
    has_brand = any(b in parts[0] or parts[0] in b for b in _BRAND_RE)
    if not has_brand:
        # Also catch "buy X 250" two-word lead
        if len(parts) >= 3 and parts[0] == "buy":
            has_brand = True
    if not has_brand:
        return False
    # last token should be digits-only between 10 and 99999
    last = parts[-1].replace(",", "").replace("₹", "").strip()
    if not last.isdigit():
        return False
    n = int(last)
    return 10 <= n <= 99999


def _confirm_spend(kb, prompt):
    """Show a Y/N confirm screen for a spend prompt. Return True on Y."""
    _LCD.fillScreen(_BLACK)
    _LCD.setTextSize(1)
    _LCD.setTextColor(_RED, _BLACK)
    _LCD.drawString("CONFIRM SPEND", 6, 6)
    _LCD.setTextColor(_CREAM, _BLACK)
    # Wrap the prompt across up to 4 lines
    for i, line in enumerate(_wrap_lines(prompt, _W - 12, 1)[:4]):
        _LCD.drawString(line, 6, 24 + i * 12)
    _LCD.setTextColor(_GREEN, _BLACK)
    _LCD.drawString("Y = send", 6, _H - 30)
    _LCD.setTextColor(_RED, _BLACK)
    _LCD.drawString("N / ESC = cancel", 6, _H - 16)
    while True:
        kb.tick()
        k = kb.get_key()
        if k is None:
            time.sleep_ms(40)
            continue
        ch = _to_char(k)
        if ch and ch.lower() == "y":
            return True
        if ch and ch.lower() == "n":
            return False
        if _is_esc(k):
            return False


def _draw_pin(entry, attempts, locked=False):
    _LCD.fillScreen(_BLACK)
    _LCD.setTextSize(1)
    _LCD.setTextColor(_GREEN, _BLACK)
    _LCD.drawString("Cardputer PIN", 6, 18)
    _LCD.setTextSize(2)
    _LCD.setTextColor(_CREAM, _BLACK)
    n = len(DEVICE_PIN)
    dots = "* " * len(entry) + "_ " * (n - len(entry))
    _LCD.drawString(dots.strip(), 6, 50)
    _LCD.setTextSize(1)
    if attempts > 0 and not locked:
        _LCD.setTextColor(_RED, _BLACK)
        _LCD.drawString("wrong. {}/3".format(attempts), 6, 92)
    if locked:
        _LCD.setTextColor(_RED, _BLACK)
        _LCD.drawString("LOCKED — power cycle", 6, 92)


def _pin_gate(kb):
    """Return True iff user enters the correct PIN. Three wrong attempts
    locks the device until power-cycled."""
    if not DEVICE_PIN:
        return True
    attempts = 0
    while attempts < 3:
        entry = ""
        _draw_pin(entry, attempts)
        while len(entry) < len(DEVICE_PIN):
            kb.tick()
            k = kb.get_key()
            if k is None:
                time.sleep_ms(40)
                continue
            ch = _to_char(k)
            if ch and ch.isdigit():
                entry += ch
                _draw_pin(entry, attempts)
            elif _is_backspace(k) and entry:
                entry = entry[:-1]
                _draw_pin(entry, attempts)
            elif _is_esc(k):
                return False
        if entry == DEVICE_PIN:
            return True
        attempts += 1
        if attempts < 3:
            time.sleep_ms(800)
    _draw_pin("", 3, locked=True)
    while True:
        time.sleep_ms(500)


def main():
    _set_font()
    if not _config_check():
        return
    kb = MatrixKeyboard()
    if not _pin_gate(kb):
        return
    # The launcher already debounced its Enter; small extra settle.
    time.sleep_ms(150)
    gc.collect()  # reclaim parse-time allocations before screen loop

    # NOTE: don't eagerly warm the TLS pool here. The mbedTLS handshake
    # against Cloudflare on this build takes 25-40 s, which would block
    # the UI with no feedback. Instead, let the first organic request
    # (compose-launch or inbox-refresh) pay the handshake while showing
    # its own progress message ("launching..." / "LIVE"); every
    # subsequent request reuses the warm connection at ~8 s vs the
    # ~25 s urequests baseline. Net win for any session with >1 call.

    screen = "compose"
    payload = None

    while True:
        try:
            if screen == "compose":
                screen = _run_compose(kb)
                payload = None
            elif screen == "inbox":
                result = _run_inbox(kb)
                if isinstance(result, tuple):
                    screen, payload = result
                else:
                    screen = result
                    payload = None
            elif screen == "detail":
                screen = _run_detail(kb, payload)
                payload = None
            elif screen == "exit":
                break
            else:
                screen = "compose"
        except Exception as e:
            # Catch-all so a transient error doesn't dump us back to
            # the launcher on every loop iteration. Show, wait, retry.
            print("pager: error in screen", screen, ":", e)
            _LCD.fillScreen(_BLACK)
            _LCD.setTextSize(1)
            _LCD.setTextColor(_RED, _BLACK)
            _LCD.drawString("Pager error:", 6, 28)
            _LCD.setTextColor(_CREAM, _BLACK)
            for i, line in enumerate(_wrap_lines(str(e), _W - 12, 1)[:5]):
                _LCD.drawString(line, 6, 46 + i * 12)
            _LCD.setTextColor(_GRAY_MID, _BLACK)
            _LCD.drawString("any key to retry", 6, _H - 14)
            kb2 = MatrixKeyboard()
            while True:
                kb2.tick()
                if kb2.get_key() is not None:
                    break
                time.sleep_ms(80)
            screen = "compose"

    machine.reset()


main()
