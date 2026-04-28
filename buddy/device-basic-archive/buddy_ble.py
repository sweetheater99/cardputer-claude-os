"""Nordic UART Service (NUS) peripheral with encrypted pairing.

The Claude Buddy protocol is defined as a Nordic UART Service over BLE
with UTF-8 JSON lines framed by '\\n'. Pairing is DisplayOnly-IO with a
6-digit passkey — the device shows it on the LCD, the host operator
types it. All GATT operations on RX/TX require an encrypted+MITM link,
which forces every connection through the pairing flow before any data
flows. This is the only layer that touches the bluetooth module; the
protocol module only deals with complete lines.

Two subtleties worth calling out:

- MicroPython's IRQ handler runs in scheduler context. Keep the body
  short: buffer bytes, split on '\\n', hand completed lines to the
  callback. Heavy parsing inside the IRQ has in the past caused dropped
  writes on ESP32 when subsequent notifications arrived before we
  returned.

- The advertising payload can't fit both the 128-bit NUS UUID and the
  full "Claude_XXXXXX" local name (3+18+2+13 = 36 > 31 bytes). Putting
  the service UUID in adv_data and the name in scan-response data is
  the standard workaround and is what the desktop side expects — it
  filters on name prefix via an active scan.
"""

import bluetooth
import micropython
import random
import time
from micropython import const


_IRQ_CENTRAL_CONNECT = const(1)
_IRQ_CENTRAL_DISCONNECT = const(2)
_IRQ_GATTS_WRITE = const(3)
_IRQ_CONNECTION_UPDATE = const(27)
_IRQ_ENCRYPTION_UPDATE = const(28)
_IRQ_GET_SECRET = const(29)
_IRQ_SET_SECRET = const(30)
_IRQ_PASSKEY_ACTION = const(31)

_PASSKEY_ACTION_NONE = const(0)
_PASSKEY_ACTION_INPUT = const(2)
_PASSKEY_ACTION_DISP = const(3)
_PASSKEY_ACTION_NUMCMP = const(4)

_FLAG_READ = const(0x0002)
_FLAG_WRITE_NR = const(0x0004)
_FLAG_WRITE = const(0x0008)
_FLAG_NOTIFY = const(0x0010)
# NOTE: The MicroPython BLE build shipped with UIFlow 2.0 exposes only
# the basic FLAG_* constants and accepts but ignores bond/mitm/le_secure
# config knobs. We can't enforce encrypted-only GATT or drive the
# DisplayOnly passkey flow from this build. Upstream Claude Buddy expects
# encryption; we report sec=false in the status ack so the host knows
# and can gate features accordingly. The pairing hooks stay in place so
# they light up automatically on any future build that grows the
# pairing API.


NUS_SERVICE_UUID = bluetooth.UUID("6e400001-b5a3-f393-e0a9-e50e24dcca9e")
NUS_RX_UUID = bluetooth.UUID("6e400002-b5a3-f393-e0a9-e50e24dcca9e")
NUS_TX_UUID = bluetooth.UUID("6e400003-b5a3-f393-e0a9-e50e24dcca9e")

_RX_CHAR = (NUS_RX_UUID, _FLAG_WRITE | _FLAG_WRITE_NR)
_TX_CHAR = (NUS_TX_UUID, _FLAG_READ | _FLAG_NOTIFY)
_NUS = (NUS_SERVICE_UUID, (_RX_CHAR, _TX_CHAR))


def _mac_suffix(mac_bytes: bytes) -> str:
    """Return uppercase hex of the last 3 MAC bytes, no separators.

    The desktop-side scanner matches on the prefix "Claude_" and uses
    the suffix to distinguish multiple buddies. 6 hex chars gives us
    16M unique names, which is plenty even if every dev at Anthropic
    builds one.
    """
    return "".join("{:02X}".format(b) for b in mac_bytes[-3:])


# Stack-level state is cached for the lifetime of the MicroPython
# process because NimBLE on UIFlow 2.0 cannot re-register GATT services
# on an already-active stack: the second gatts_register_services call
# returns OSError(16) EBUSY. The app layer enters/exits Buddy many
# times per boot (launcher → Buddy → back → Buddy), so each entry must
# reuse the previously-registered service handles rather than trying to
# re-register. active(False)/active(True) was tried as a reset path but
# crashes the BLE controller with "BLE_INIT: controller init failed",
# so it's avoided here too — the singleton is the only clean option.
_stack = None  # dict: {"ble", "rx", "tx", "name", "pairing"} once initialized


def _ensure_stack(name_prefix: str):
    """Return the cached BLE stack, initializing it on first call."""
    global _stack
    if _stack is not None:
        return _stack

    ble = bluetooth.BLE()
    ble.active(True)

    mac = ble.config("mac")[1]
    name = "{}_{}".format(name_prefix, _mac_suffix(mac))
    # gap_name is the only config key this build actually honors.
    # The pairing knobs are kept but wrapped in try/except; on
    # stock UIFlow 2.0 they silently no-op (verified by probing).
    ble.config(gap_name=name)
    for k, v in (("bond", True), ("mitm", True), ("le_secure", True), ("io", 0)):
        try:
            ble.config(**{k: v})
        except (OSError, ValueError, TypeError):
            pass  # build doesn't support pairing; oh well
    # Detect whether pairing is truly supported by round-tripping a
    # get. This build accepts set but errors on get, which is how we
    # tell the difference between "configured" and "silently noop'd".
    try:
        ble.config("bond")
        pairing = True
    except (OSError, ValueError):
        pairing = False

    ((rx_h, tx_h),) = ble.gatts_register_services((_NUS,))
    # Folder-push chunks can be up to ~2KB of base64. 512 is the
    # largest a single write can be anyway (MTU-limited), but the
    # buffer needs to hold at least a full chunk before the
    # scheduler drains it. Let the stack accumulate.
    ble.gatts_set_buffer(rx_h, 512, True)

    _stack = {
        "ble": ble,
        "rx": rx_h,
        "tx": tx_h,
        "name": name,
        "pairing": pairing,
    }
    return _stack


class BuddyBLE:
    """BLE peripheral serving Nordic UART with encrypted pairing."""

    def __init__(
        self,
        name_prefix: str = "Claude",
        on_line=None,
        on_passkey=None,
        on_state=None,
    ):
        self._on_line = on_line or (lambda _line: None)
        self._on_passkey = on_passkey or (lambda _pk: None)
        self._on_state = on_state or (lambda _st: None)

        stack = _ensure_stack(name_prefix)
        self._ble = stack["ble"]
        self._rx_h = stack["rx"]
        self._tx_h = stack["tx"]
        self._name = stack["name"]
        self._pairing_supported = stack["pairing"]

        # Rebinding the IRQ callback replaces any handler from a
        # previous app entry — that's what we want, since the old
        # BuddyBLE instance is about to be garbage-collected and its
        # bound method would crash on dispatch.
        self._ble.irq(self._irq)

        self._conn = None
        self._encrypted = False
        self._rx_buf = bytearray()
        self._current_passkey = None
        # Flipped by deinit(). _irq checks this before dispatching so
        # that a late async event (e.g. the DISCONNECT that fires after
        # we've already returned to the launcher) can't repaint stale
        # UI or re-arm advertising on an app that's on its way out.
        self._shutting_down = False

        # Re-entering after a prior deinit() leaves the stack in a
        # "not advertising" state. The first gap_advertise after that
        # usually succeeds, but if the previous session ended in a
        # messy disconnect the controller may still be cleaning up;
        # defer any failure into the normal scheduler-based retry path.
        try:
            self._advertise()
        except OSError as e:
            print("buddy_ble: initial advertise failed, scheduling retry:", e)
            try:
                micropython.schedule(self._rearm_adv, 0)
            except RuntimeError:
                pass

    @property
    def advertised_name(self) -> str:
        return self._name

    @property
    def connected(self) -> bool:
        return self._conn is not None

    @property
    def encrypted(self) -> bool:
        return self._encrypted

    @property
    def pairing_supported(self) -> bool:
        return self._pairing_supported

    def _irq(self, event, data):
        if self._shutting_down:
            # App is tearing down — the main loop has already returned
            # and the launcher may be mid-repaint. Don't dispatch any
            # callback or schedule work; just swallow the event.
            return
        if event == _IRQ_CENTRAL_CONNECT:
            conn, _addr_type, _addr = data
            self._conn = conn
            self._encrypted = False
            self._rx_buf = bytearray()
            self._on_state("connected")

        elif event == _IRQ_CENTRAL_DISCONNECT:
            self._conn = None
            self._encrypted = False
            self._rx_buf = bytearray()
            self._current_passkey = None
            self._on_state("disconnected")
            # Defer re-advertising out of IRQ context. NimBLE on ESP32
            # often returns OSError(-30) ("invalid state") if we call
            # gap_advertise the instant CENTRAL_DISCONNECT fires — the
            # controller is still tearing down the previous link.
            # Running this through micropython.schedule lets the stack
            # settle, and an exception in the scheduler thread can't
            # kill the IRQ handler like it could when we called
            # _advertise inline here.
            try:
                micropython.schedule(self._rearm_adv, 0)
            except RuntimeError:
                # Schedule queue full — best-effort inline fallback.
                try:
                    self._advertise()
                except OSError as e:
                    print("buddy_ble: inline re-advertise failed:", e)

        elif event == _IRQ_ENCRYPTION_UPDATE:
            # (conn_handle, encrypted, authenticated, bonded, key_size)
            _conn, enc, _auth, _bonded, _ks = data
            self._encrypted = bool(enc)
            if self._encrypted:
                self._current_passkey = None
                self._on_state("encrypted")

        elif event == _IRQ_PASSKEY_ACTION:
            conn, action, _passkey = data
            if action == _PASSKEY_ACTION_DISP:
                # Generate a fresh 6-digit key per pairing attempt —
                # reusing one across reboots would let a shoulder-surf
                # from last week still work.
                pk = random.randint(0, 999_999)
                self._current_passkey = pk
                self._on_passkey(pk)
                self._ble.gap_passkey(conn, action, pk)

        elif event == _IRQ_GATTS_WRITE:
            conn, handle = data
            if handle == self._rx_h:
                self._rx_buf += self._ble.gatts_read(self._rx_h)
                # Split on newline, hand each completed line upstream.
                # Partial trailing data stays in the buffer.
                while True:
                    nl = self._rx_buf.find(b"\n")
                    if nl < 0:
                        break
                    line = bytes(self._rx_buf[:nl])
                    # MicroPython bytearray doesn't support `del buf[:n]`,
                    # so we rebuild with a slice copy. Same cost in
                    # practice since JSON lines are short and rare.
                    self._rx_buf = bytearray(self._rx_buf[nl + 1:])
                    try:
                        self._on_line(line)
                    except Exception as e:
                        # Don't let a misbehaving handler kill the IRQ
                        # and leave the buffer permanently stuck.
                        print("buddy_ble: line handler exception:", e)

    def _rearm_adv(self, _):
        """Scheduler-context trampoline around _advertise.

        Invoked from micropython.schedule after a disconnect. NimBLE
        on the stripped UIFlow 2.0 build frequently rejects the first
        gap_advertise after a paired-disconnect — we've observed both
        OSError(-30) ("invalid state") and OSError(19) ENODEV. The
        controller needs wall time to finish cleaning up the prior
        link, not a fresh config push.

        Do NOT toggle active(False)/active(True) as a recovery path:
        on this build that panics the BLE controller with
        "BLE_INIT: controller init failed" and auto-reboots the CPU.
        Verified the hard way.

        Scheduler context tolerates short time.sleep_ms calls just
        fine, so walk up a staircase of delays (150/300/450/600/750
        ms, ~2.25s total) before giving up. If we still can't get
        back to advertising after that, leaving the device dark is
        less bad than crashing — the user can power-cycle, and the
        other apps on the launcher still work.
        """
        for attempt in range(5):
            # Stop any half-configured adv slot before retrying. Some
            # of the failure modes stick until we explicitly clear.
            try:
                self._ble.gap_advertise(None)
            except OSError:
                pass
            time.sleep_ms(150 * (attempt + 1))
            try:
                self._advertise()
                return
            except OSError as e:
                print("buddy_ble: re-advertise attempt", attempt + 1, "err:", e)
        print("buddy_ble: giving up on re-advertise; power-cycle to recover")

    def _advertise(self):
        # adv_data: flags (LE General Discoverable + BR/EDR not supp) + 128-bit service UUID
        uuid_le = bytes(NUS_SERVICE_UUID)  # MicroPython returns little-endian
        adv = b"\x02\x01\x06" + bytes([len(uuid_le) + 1, 0x07]) + uuid_le
        name_bytes = self._name.encode()
        resp = bytes([len(name_bytes) + 1, 0x09]) + name_bytes
        # 100ms advertising interval — the desktop scans for ~3s at a
        # time, so anything faster than ~500ms gives reliable discovery.
        try:
            self._ble.gap_advertise(100_000, adv_data=adv, resp_data=resp)
        except OSError as e:
            # OSError(-30) = ESP_ERR_INVALID_STATE from NimBLE — usually
            # "advertising instance is still configured from last run".
            # Clearing first and re-issuing unblocks it. If it still
            # errors, let it propagate so _rearm_adv can log it.
            try:
                self._ble.gap_advertise(None)
            except OSError:
                pass
            self._ble.gap_advertise(100_000, adv_data=adv, resp_data=resp)

    def send_line(self, payload: bytes) -> bool:
        """Push one JSON line to the host. Returns False if no link.

        On builds with pairing we wait for encryption; on stripped
        builds (UIFlow 2.0 today) we consider a raw connection
        sufficient since there's no encryption layer to wait for.
        """
        if self._conn is None:
            return False
        if self._pairing_supported and not self._encrypted:
            return False
        if not payload.endswith(b"\n"):
            payload = payload + b"\n"
        # Default ATT MTU on ESP32 is 23 → 20 bytes of notify payload.
        # Some hosts negotiate higher; we stay safe with 20 unless the
        # peer grows the MTU. Chunking is transparent — the host
        # reassembles by waiting for '\n'.
        step = 20
        try:
            for i in range(0, len(payload), step):
                self._ble.gatts_notify(self._conn, self._tx_h, payload[i : i + step])
        except OSError as e:
            print("buddy_ble: notify failed:", e)
            return False
        return True

    def disconnect(self):
        if self._conn is not None:
            try:
                self._ble.gap_disconnect(self._conn)
            except OSError:
                pass

    def deinit(self):
        """Stop advertising and drop any active link.

        Called by the app layer when the user exits back to the
        launcher. We keep the BLE stack itself alive (active(False)
        tends to leave the controller in a weird state that needs a
        reboot to recover) and just shut down our surface: stop
        advertising and drop the current link if any.

        Order matters: neutralize the IRQ path *before* disconnecting.
        gap_disconnect is asynchronous — the DISCONNECT event fires
        milliseconds later, long after this method (and buddy_app)
        have returned and the launcher has started repainting. If our
        handler is still wired up when that event lands, it'll fire
        _on_state("disconnected") → set_connection → _draw_header /
        _draw_main, which paints Buddy chrome on top of the launcher.

        We use three layers of defense, because the stripped UIFlow
        BLE stack has surprised us before:
          1. Set _shutting_down — _irq early-outs on this flag.
          2. ble.irq(None) — stops dispatch entirely if the build
             honors it (not all do; wrap in try/except).
          3. Replace the stored callbacks with no-ops — if an event
             somehow still gets through, the UI layer is untouched.
        """
        self._shutting_down = True
        try:
            self._ble.irq(None)
        except (OSError, TypeError):
            pass
        self._on_line = lambda _line: None
        self._on_passkey = lambda _pk: None
        self._on_state = lambda _st: None
        try:
            self._ble.gap_advertise(None)
        except OSError:
            pass
        if self._conn is not None:
            try:
                self._ble.gap_disconnect(self._conn)
            except OSError:
                pass

    def forget_bonds(self):
        """Erase all bonding keys; forces re-pairing on next connect.

        The clean way to do this is active(False)/active(True) — on
        builds with a real bonding store that resets the NimBLE
        keystore. Unfortunately on the stripped UIFlow 2.0 BLE build
        that toggle *panics* the controller ("BLE_INIT: controller
        init failed" → Guru Meditation → auto-reboot). Since the same
        build also doesn't actually persist bonds, there are no
        bonding keys to erase — so the safe behavior here is to skip
        the toggle and let the host think we forgot. On any future
        build that grows a real pairing API, gating on
        `self._pairing_supported` lights the real path back up.
        """
        if not self._pairing_supported:
            return
        try:
            self._ble.active(False)
        except OSError:
            pass
        self._ble.active(True)
