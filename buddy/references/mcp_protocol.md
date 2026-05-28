# Cardputer MCP BLE protocol — reference

The wire format the `cardputer_mcp.py` device app implements, and the
`mcp/server.py` host-side bridge speaks. Kept separate from the
Claude Buddy protocol (see `protocol.md`) because the two apps are
distinct surfaces with different threat models, different evolution
rates, and different MCP-client expectations.

## Status

Implemented through iter 5. `notify`, `ask`, and `confirm` work
end-to-end over BLE (device `cardputer_mcp.py` ↔ host `mcp/server.py`).
The host now speaks MCP over **two transports** to the same `Bridge`
and the same device:

- **stdio** — the original local path (`claude mcp add cardputer …`).
- **streamable-http** — `CARDPUTER_HTTP=1`, a long-lived daemon on
  `127.0.0.1:9000` that local Claude Code reaches over loopback and
  cloud agents reach through an MCP tunnel (`tunnel/`). See
  `/tunnel/README.md` and `/docs/superpowers/specs/`.

This file pins the BLE wire contract both transports share.

## Transport

Nordic-UART-shaped GATT service, line-delimited UTF-8 JSON with `\n`
terminators. Same wire shape as the Buddy protocol — chosen on
purpose so MicroPython knowledge transfers — but a fresh UUID block
so the two apps don't collide on a future build that hosts both.

| Role               | Characteristic UUID                    | Flags               |
| ------------------ | -------------------------------------- | ------------------- |
| Service            | `a5cd0001-c0de-4abe-9c1a-4d5e6f7a8b90` | —                   |
| RX (host → device) | `a5cd0002-c0de-4abe-9c1a-4d5e6f7a8b90` | `WRITE`, `WRITE_NR` |
| TX (device → host) | `a5cd0003-c0de-4abe-9c1a-4d5e6f7a8b90` | `READ`, `NOTIFY`    |

Advertising name: `CardputerMCP_<last 6 hex digits of BT MAC>`.

The `a5cd` UUID prefix is arbitrary but distinctive — it makes the
service easy to spot in a BLE scan. If you change it, update the
host-side constants in `mcp/server.py` (iter 2) and grep this repo
for `a5cd` to catch every place it's referenced.

## Authentication

The **BLE link** is unauthenticated: UIFlow 2.0's MicroPython build
strips the pairing API. We rely on a per-device confirmation gesture
(described under the `confirm` tool) for any destructive operation, and
we never accept file pushes over MCP — there's simply no `file` /
`chunk` command in this protocol. See `protocol.md` § Authentication for
the broader discussion that applies here too.

The **HTTP transport** is a different story: an MCP tunnel carries
encrypted traffic to the daemon but does not authenticate to it, so the
daemon requires a **bearer token** on every request (`mcp/auth.py`).
Each token maps to a short agent label (`claude-code`, `managed-agent`,
…) that becomes the `agent` field below — so the device banner shows
_which_ agent is asking, sourced from the authenticated token rather
than caller-supplied text. An empty token map denies everything (fail
closed). Configure tokens via `CARDPUTER_TOKENS=token=label,…`.

## Inbound (host → device)

All commands carry an `id` string the host generated; the device
echoes that `id` on every ack so the host can match replies to
in-flight requests.

| cmd       | shape                                                                                           | behavior                                                                                                                       |
| --------- | ----------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------ | ---------------------- | ------------------------------------------------------------ |
| `notify`  | `{"cmd":"notify","id":"...","title":"...","body":"...","urgency":"info"                         | "warn"                                                                                                                         | "crit","agent":"..."}` | Display the notification, beep per urgency, ack immediately. |
| `ask`     | `{"cmd":"ask","id":"...","question":"...","choices":["a","b",...],"timeout_s":N,"agent":"..."}` | Show question + choices, wait for keypress or timeout. Send a `pending` ack on receipt and a resolution ack later (see below). |
| `confirm` | `{"cmd":"confirm","id":"...","title":"...","danger":true,"timeout_s":N,"agent":"..."}`          | Show a danger banner, require hold-Y-for-3 s. Send `pending` ack on receipt + resolution ack later.                            |
| `show`    | `{"cmd":"show","id":"...","text":"...","channel":"agent-tag"}`                                  | Update one line of the ambient status area. Ack immediately. _(iter 4)_                                                        |
| `cancel`  | `{"cmd":"cancel","id":"...","target_id":"..."}`                                                 | Cancel a pending `ask` / `confirm`. Ack with cancellation state.                                                               |
| `ping`    | `{"cmd":"ping","id":"..."}`                                                                     | Round-trip liveness check.                                                                                                     |

The `agent` field carries a short tag (`"claude-code"`, `"managed-agent"`,
`"local"`, etc.). Over the HTTP transport it's set from the caller's
bearer token (`mcp/auth.py`), not from anything in the tool arguments,
so it can't be forged. The device:

- **renders it on the `ask` / `confirm` banner** (`from:<agent>`) so the
  user sees who is asking before answering or holding Y — _implemented_
- could attribute notifications in a history view — _future_
- could apply a per-agent rate limit (~1 notify per 60 s; `crit` and
  blocking tools bypass) — _future; restraint is self-imposed today_

## Outbound (device → host)

| shape                                                                                | when                                                                                                                                                |
| ------------------------------------------------------------------------------------ | --------------------------------------------------------------------------------------------------------------------------------------------------- |
| `{"event":"hello","version":"0.2.0","name":"...","caps":["notify","ask","confirm"]}` | Once, ~1.5 s after the BLE central subscribes to TX (delay is to let the central settle its CCCD write). `caps` lists tools this firmware supports. |
| `{"event":"heartbeat","bat":{...},"rssi":...}`                                       | Every ~10 s while connected, mirroring Buddy's cadence.                                                                                             |
| `{"ack":"<cmd>","id":"...","ok":true}`                                               | Generic success ack — for `notify`, `show`, `ping`, `cancel`.                                                                                       |
| `{"ack":"<cmd>","id":"...","ok":false,"err":"..."}`                                  | Generic failure ack. `err` is a short stable string.                                                                                                |
| `{"ack":"ask","id":"...","pending":true}`                                            | Immediate ack for `ask` / `confirm` indicating the device received the request and is awaiting user input.                                          |
| `{"ack":"ask","id":"...","ok":true,"choice":"..."}`                                  | Resolution: user picked a choice.                                                                                                                   |
| `{"ack":"ask","id":"...","ok":false,"timed_out":true}`                               | Resolution: `timeout_s` elapsed.                                                                                                                    |
| `{"ack":"ask","id":"...","ok":false,"dnd":true}`                                     | Resolution: device was in do-not-disturb mode.                                                                                                      |
| `{"ack":"ask","id":"...","ok":false,"cancelled":true}`                               | Resolution: host sent `cancel` for this id, or user pressed ESC.                                                                                    |
| `{"ack":"confirm","id":"...","ok":true,"confirmed":true,"hold_ms":N}`                | Resolution: user held Y for ≥ 3 s. `hold_ms` is the measured hold duration.                                                                         |
| `{"ack":"confirm","id":"...","ok":false,"cancelled":true}`                           | Resolution: user pressed N or ESC.                                                                                                                  |
| `{"ack":"confirm","id":"...","ok":false,"timed_out":true}`                           | Resolution: `timeout_s` elapsed before the user could complete the hold.                                                                            |

### hello event body

```
{
  "event": "hello",
  "version": "0.1.0",      # firmware version of cardputer_mcp.py
  "name":    "Pip",        # device-set display name (default: "Cardputer")
  "caps":    ["notify", "ask"],   # tool surface this build implements
  "model":   "cardputer-adv",     # or "cardputer" for non-Adv
  "mtu":     20            # current ATT MTU; host should chunk smaller writes accordingly
}
```

### heartbeat body

Mirrors the Buddy heartbeat status payload so device-side state code
can be shared between apps:

```
{
  "event":  "heartbeat",
  "bat":    {"pct": 0|25|50|75|100, "usb": bool},
  "rssi":   -57,           # signed dBm; useful for debugging fade-outs
  "uptime": 142,           # seconds since this app booted
  "dnd":    false          # do-not-disturb state
}
```

## Timing

- 10 s heartbeat interval (matches Buddy).
- 30 s silence → host treats device as gone; subsequent tool calls
  return `unavailable` until reconnection.
- `ask` / `confirm` timeouts are host-supplied (`timeout_s`). The
  device enforces them with a small grace window (~200 ms) so a
  user pressing the key right at the deadline doesn't lose their
  answer to a race.
- BLE IRQ handlers should return within ~5 ms (same constraint as
  Buddy — the stack will drop bytes under back-pressure).

## Chunking

Writes from the host are line-delimited JSON. With the default ATT
MTU of 20 bytes (UIFlow 2.0 negotiates higher with macOS but we
can't rely on it being settled before the first write), the host
chunks each line into ≤ 20 byte fragments and the device reassembles
on RX. The same applies to TX notifications going the other way.

Use `gatts_set_buffer(rx_h, 512, True)` on the device side, same as
Buddy — without `append=True` a fast burst of fragments overflows
the 20-byte default and bytes drop silently. (`ble_on_micropython.md`
documents this trap in more detail.)

## Versioning

The `hello` event carries a `version` string. The host should treat
firmware versions lexicographically: tools listed in `caps` are
safe to call; tools not listed return `unavailable` without ever
hitting the radio. This lets the host advertise the full MVP tool
surface (5 tools at full build-out) while a partially-implemented
device gracefully no-ops the parts it doesn't yet support.

When breaking the wire format, bump the major version and add a
back-compat shim on the host side that recognizes older `hello`
events and downgrades its sent commands. We don't expect to break
the format often — the JSON-line shape is forgiving enough that
most extensions can be additive.
